"""Viaduct server (`viaductd`): public listener, tunnel registry, idle pools.

Auth: each subdomain has a persistent reservation (SQLite) storing the sha256
of its token; hello/data_hello frames are checked against it. Runtime state
lives in a `dict[str, Tunnel]` and dies with the process — clients redial on
restart; a persisted binding to a dead socket would be worse than nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import secrets
import ssl
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

import typer

from viaduct import protocol, routing
from viaduct.relay import splice
from viaduct.store import (
    DEFAULT_DB_PATH,
    DomainTaken,
    Reservation,
    Store,
    SubdomainTaken,
    hash_token,
    valid_hostname,
    valid_subdomain,
)

log = logging.getLogger("viaduct.server")

Conn = tuple[asyncio.StreamReader, asyncio.StreamWriter]


class Tunnel:
    """One connected client: its subdomain and its pool of idle data connections."""

    def __init__(self, subdomain: str) -> None:
        self.subdomain = subdomain
        self.pool: asyncio.Queue[Conn] = asyncio.Queue()

    def pop_idle(self) -> Conn | None:
        """Pop a live idle connection, discarding any that died while queued."""
        while True:
            try:
                reader, writer = self.pool.get_nowait()
            except asyncio.QueueEmpty:
                return None
            if writer.is_closing() or reader.at_eof():
                writer.close()
                continue
            return reader, writer

    def close_pool(self) -> None:
        while True:
            try:
                _, writer = self.pool.get_nowait()
            except asyncio.QueueEmpty:
                return
            writer.close()


class TunnelServer:
    def __init__(
        self,
        *,
        store: Store,
        bind: str = "127.0.0.1",
        public_port: int = 8080,
        tunnel_port: int = 4443,
        base_domain: str = "localhost",
        tls: ssl.SSLContext | None = None,
    ) -> None:
        self.store = store
        self.bind = bind
        self.public_port = public_port
        self.tunnel_port = tunnel_port
        self.base_domain = base_domain
        self._tls = tls
        self.tunnels: dict[str, Tunnel] = {}
        self._public_server: asyncio.Server | None = None
        self._tunnel_server: asyncio.Server | None = None

    async def start(self) -> None:
        self._public_server = await asyncio.start_server(
            self._handle_public, self.bind, self.public_port, limit=routing.MAX_HEAD
        )
        self._tunnel_server = await asyncio.start_server(
            self._handle_tunnel, self.bind, self.tunnel_port, ssl=self._tls
        )
        self.public_port = self._public_server.sockets[0].getsockname()[1]
        self.tunnel_port = self._tunnel_server.sockets[0].getsockname()[1]
        log.info(
            "listening public=%s:%s tunnel=%s:%s base_domain=%s",
            self.bind,
            self.public_port,
            self.bind,
            self.tunnel_port,
            self.base_domain,
        )

    async def stop(self) -> None:
        for server in (self._public_server, self._tunnel_server):
            if server is not None:
                server.close()
        for tunnel in list(self.tunnels.values()):
            tunnel.close_pool()
        self.tunnels.clear()

    # -- tunnel listener: control + data connections from viaduct clients ------

    async def _handle_tunnel(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            frame = await protocol.read_frame(reader)
            if frame["type"] == "hello":
                await self._handle_control(frame, reader, writer)
            elif frame["type"] == "data_hello":
                self._handle_data(frame, reader, writer)
            elif frame["type"] in ("domain_add", "domain_list", "domain_remove"):
                await self._handle_domain_op(frame, writer)
            else:
                log.warning("unexpected first frame type=%r", frame["type"])
                writer.close()
        except protocol.ProtocolError:
            writer.close()

    async def _handle_control(
        self, frame: protocol.Frame, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        token = protocol.require_str(frame, "token")
        subdomain = protocol.require_str(frame, "subdomain")
        protocol.require_int(frame, "local_port")
        reservation = self.store.get(subdomain)
        if reservation is None:
            log.warning("no reservation subdomain=%s", subdomain)
            await self._reject(writer, "unknown_subdomain")
            return
        if not self._token_matches(token, reservation.token_hash):
            log.warning("bad token subdomain=%s", subdomain)
            await self._reject(writer, "bad_token")
            return
        if subdomain in self.tunnels:
            await self._reject(writer, "subdomain_taken")
            return

        tunnel = Tunnel(subdomain)
        self.tunnels[subdomain] = tunnel
        hostname = f"{subdomain}.{self.base_domain}"
        ping_task = asyncio.create_task(self._ping_loop(writer))
        try:
            await protocol.write_frame(writer, protocol.ok(hostname=hostname))
            log.info("tunnel registered subdomain=%s hostname=%s", subdomain, hostname)
            self.store.touch(subdomain)
            while True:
                msg = await protocol.read_frame(reader)
                if msg["type"] == "ping":
                    await protocol.write_frame(writer, protocol.pong())
        except (protocol.ProtocolError, ConnectionError):
            pass
        finally:
            ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ping_task
            if self.tunnels.get(subdomain) is tunnel:
                del self.tunnels[subdomain]
            tunnel.close_pool()
            writer.close()
            self.store.touch(subdomain)
            log.info("tunnel closed subdomain=%s", subdomain)

    def _handle_data(
        self, frame: protocol.Frame, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        token = protocol.require_str(frame, "token")
        subdomain = protocol.require_str(frame, "subdomain")
        tunnel = self.tunnels.get(subdomain)
        reservation = self.store.get(subdomain)
        if (
            tunnel is None
            or reservation is None
            or not self._token_matches(token, reservation.token_hash)
        ):
            writer.close()
            return
        # The handler returns here; the pool keeps the streams alive until a
        # public request claims them.
        tunnel.pool.put_nowait((reader, writer))
        log.debug("data conn pooled subdomain=%s idle=%s", subdomain, tunnel.pool.qsize())

    async def _handle_domain_op(self, frame: protocol.Frame, writer: asyncio.StreamWriter) -> None:
        """One-shot custom-domain management: reply with ok/error, then close."""
        token = protocol.require_str(frame, "token")
        if frame["type"] == "domain_add":
            subdomain = protocol.require_str(frame, "subdomain")
            reservation = self.store.get(subdomain)
            if reservation is None:
                await self._reject(writer, "unknown_subdomain")
                return
            if not self._token_matches(token, reservation.token_hash):
                await self._reject(writer, "bad_token")
                return
            hostname = protocol.require_str(frame, "hostname").lower().rstrip(".")
            if not valid_hostname(hostname):
                await self._reject(writer, "invalid_hostname")
                return
            if hostname == self.base_domain or hostname.endswith("." + self.base_domain):
                await self._reject(writer, "hostname_under_base_domain")
                return
            try:
                self.store.add_domain(hostname, subdomain)
            except DomainTaken:
                await self._reject(writer, "domain_taken")
                return
            log.info("domain added hostname=%s subdomain=%s", hostname, subdomain)
            await self._ack(
                writer, protocol.ok(hostname=hostname, target=f"{subdomain}.{self.base_domain}")
            )
            return

        # list/remove identify the reservation by token alone
        reservation = self._reservation_for_token(token)
        if reservation is None:
            await self._reject(writer, "bad_token")
            return
        if frame["type"] == "domain_list":
            domains = [
                {"hostname": d.hostname, "subdomain": d.subdomain, "created_at": d.created_at}
                for d in self.store.domains_for(reservation.subdomain)
            ]
            await self._ack(writer, protocol.ok(domains=domains))
            return
        hostname = protocol.require_str(frame, "hostname").lower().rstrip(".")
        domain = self.store.get_domain(hostname)
        if domain is None or domain.subdomain != reservation.subdomain:
            await self._reject(writer, "unknown_domain")
            return
        self.store.remove_domain(hostname)
        log.info("domain removed hostname=%s subdomain=%s", hostname, reservation.subdomain)
        await self._ack(writer, protocol.ok())

    def _reservation_for_token(self, token: str) -> Reservation | None:
        return next(
            (
                r
                for r in self.store.reservations.values()
                if self._token_matches(token, r.token_hash)
            ),
            None,
        )

    async def _ack(self, writer: asyncio.StreamWriter, frame: protocol.Frame) -> None:
        with contextlib.suppress(ConnectionError):
            await protocol.write_frame(writer, frame)
        writer.close()

    async def _ping_loop(self, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            while True:
                await asyncio.sleep(protocol.HEARTBEAT_INTERVAL)
                await protocol.write_frame(writer, protocol.ping())

    async def _reject(self, writer: asyncio.StreamWriter, reason: str) -> None:
        with contextlib.suppress(ConnectionError):
            await protocol.write_frame(writer, protocol.error(reason))
        writer.close()

    @staticmethod
    def _token_matches(token: str, token_hash: str) -> bool:
        return hmac.compare_digest(hash_token(token), token_hash)

    # -- public listener: plaintext HTTP from Caddy (or curl, pre-TLS) ---------

    async def _handle_public(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            async with asyncio.timeout(30):
                head = await routing.read_head(reader)
        except (routing.BadRequest, TimeoutError, ConnectionError):
            await self._respond(writer, "400 Bad Request", "viaduct: malformed request\n")
            return
        host = routing.extract_host(head)
        subdomain = (
            routing.resolve_subdomain(host, self.base_domain, self.store.domain_routes())
            if host
            else None
        )
        tunnel = self.tunnels.get(subdomain) if subdomain else None
        if tunnel is None:
            if subdomain is None and await self._maybe_tls_check(head, writer):
                return
            log.info("no tunnel host=%s", host)
            await self._respond(writer, "404 Not Found", "viaduct: no such tunnel\n")
            return
        conn = tunnel.pop_idle()
        if conn is None:
            log.warning("pool empty subdomain=%s", subdomain)
            await self._respond(
                writer, "503 Service Unavailable", "viaduct: no idle tunnel connections\n"
            )
            return
        t_reader, t_writer = conn
        try:
            t_writer.write(head)
            await t_writer.drain()
        except ConnectionError:
            t_writer.close()
            await self._respond(writer, "502 Bad Gateway", "viaduct: tunnel connection died\n")
            return
        await splice(reader, writer, t_reader, t_writer)

    async def _maybe_tls_check(self, head: bytes, writer: asyncio.StreamWriter) -> bool:
        """Answer Caddy's on-demand-TLS ask endpoint: 200 only for known custom domains.

        Only reachable for hosts that resolve to no tunnel (Caddy asks with
        Host: localhost), so tunneled apps keep their own /_viaduct/* paths.
        Without this gate the service would be an open certificate mill.
        """
        target = routing.extract_path(head)
        if target is None or urlsplit(target).path != "/_viaduct/tls-check":
            return False
        query = parse_qs(urlsplit(target).query)
        domain = (query.get("domain") or [""])[0].lower().rstrip(".")
        if domain and self.store.get_domain(domain) is not None:
            await self._respond(writer, "200 OK", "ok\n")
        else:
            log.info("tls-check refused domain=%s", domain)
            await self._respond(writer, "404 Not Found", "unknown domain\n")
        return True

    async def _respond(self, writer: asyncio.StreamWriter, status: str, body: str) -> None:
        with contextlib.suppress(ConnectionError):
            writer.write(routing.plain_response(status, body))
            await writer.drain()
        writer.close()


app = typer.Typer(help="Viaduct server daemon.")


@app.callback(invoke_without_command=True)
def _cli(
    ctx: typer.Context,
    bind: Annotated[str, typer.Option(help="Address to bind both listeners to")] = "127.0.0.1",
    public_port: Annotated[int, typer.Option(help="Port for public HTTP traffic")] = 8080,
    tunnel_port: Annotated[int, typer.Option(help="Port for client tunnel connections")] = 4443,
    base_domain: Annotated[str, typer.Option(help="Domain that subdomains hang off")] = "localhost",
    db: Annotated[Path, typer.Option(help="SQLite database path")] = DEFAULT_DB_PATH,
    tls_cert: Annotated[
        Path | None, typer.Option(help="PEM certificate enabling TLS on the tunnel listener")
    ] = None,
    tls_key: Annotated[Path | None, typer.Option(help="PEM private key for --tls-cert")] = None,
) -> None:
    """Run the tunnel server."""
    if ctx.invoked_subcommand is not None:
        return
    if (tls_cert is None) != (tls_key is None):
        raise typer.BadParameter("--tls-cert and --tls-key must be provided together")
    tls = None
    if tls_cert is not None and tls_key is not None:
        tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        tls.load_cert_chain(tls_cert, tls_key)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(bind, public_port, tunnel_port, base_domain, db, tls))


async def _serve(
    bind: str,
    public_port: int,
    tunnel_port: int,
    base_domain: str,
    db: Path,
    tls: ssl.SSLContext | None,
) -> None:
    store = Store(db)
    server = TunnelServer(
        store=store,
        bind=bind,
        public_port=public_port,
        tunnel_port=tunnel_port,
        base_domain=base_domain,
        tls=tls,
    )
    await server.start()
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
        store.close()


token_app = typer.Typer(help="Manage subdomain reservations and their auth tokens.")
app.add_typer(token_app, name="token")


@token_app.command("create")
def token_create(
    subdomain: Annotated[str, typer.Option(help="Subdomain to reserve")],
    db: Annotated[Path, typer.Option(help="SQLite database path")] = DEFAULT_DB_PATH,
) -> None:
    """Reserve a subdomain and print its auth token — shown once; only the sha256 is stored."""
    if not valid_subdomain(subdomain):
        raise typer.BadParameter("subdomain must be a lowercase DNS label (a-z, 0-9, hyphens)")
    token = secrets.token_urlsafe(32)
    store = Store(db)
    try:
        store.create_reservation(subdomain, hash_token(token))
    except SubdomainTaken:
        typer.echo(f"viaductd: subdomain {subdomain!r} is already reserved", err=True)
        raise typer.Exit(1) from None
    finally:
        store.close()
    typer.echo(token)
    typer.echo(
        f"viaductd: reserved {subdomain!r} — the token above is shown once; "
        "put it in the client's ~/.config/viaduct/config.toml",
        err=True,
    )


def main() -> None:
    app()
