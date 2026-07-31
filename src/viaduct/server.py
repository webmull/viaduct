"""Viaduct server (`viaductd`): public listener, tunnel registry, idle pools.

Auth: `viaductd token create` mints per-user tokens, stored only as sha256
hashes. A client presents its token; the server assigns a random subdomain for
the life of that connection and frees it on disconnect. Subdomains are never
persisted — runtime state lives in a `dict[str, Tunnel]` and dies with the
process, so clients simply redial (and get a fresh name) on restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import secrets
import signal
import ssl
from pathlib import Path
from typing import Annotated

import typer

from viaduct import names, protocol, routing
from viaduct.relay import splice
from viaduct.store import DEFAULT_DB_PATH, Store, hash_token

log = logging.getLogger("viaduct.server")

Conn = tuple[asyncio.StreamReader, asyncio.StreamWriter]


class Tunnel:
    """One connected client: its subdomain, owning token, and idle pool."""

    def __init__(self, subdomain: str, token_hash: str) -> None:
        self.subdomain = subdomain
        self.token_hash = token_hash
        self.pool: asyncio.Queue[Conn] = asyncio.Queue()
        #: pooled + in-flight data connections, bounded by --max-conns-per-tunnel
        self.data_conns = 0

    async def acquire(self, wait: float) -> Conn | None:
        """Wait up to *wait* seconds for a live idle connection.

        Waiting (rather than failing fast) turns a request burst into slightly
        slower establishment while the client replenishes the pool, instead of
        a wall of 503s.
        """
        try:
            async with asyncio.timeout(wait):
                while True:
                    reader, writer = await self.pool.get()
                    if writer.is_closing() or reader.at_eof():
                        writer.close()
                        self.data_conns -= 1
                        continue
                    return reader, writer
        except TimeoutError:
            return None

    def close_pool(self) -> None:
        while True:
            try:
                _, writer = self.pool.get_nowait()
            except asyncio.QueueEmpty:
                return
            writer.close()
            self.data_conns -= 1


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
        max_conns_per_tunnel: int = 128,
        idle_timeout: float | None = 300.0,
        pool_wait: float = 10.0,
    ) -> None:
        self.store = store
        self.bind = bind
        self.public_port = public_port
        self.tunnel_port = tunnel_port
        self.base_domain = base_domain
        self._tls = tls
        self.max_conns_per_tunnel = max_conns_per_tunnel
        self.idle_timeout = idle_timeout
        self.pool_wait = pool_wait
        self.tunnels: dict[str, Tunnel] = {}
        self._active_splices = 0
        self._no_active_splices = asyncio.Event()
        self._no_active_splices.set()
        self._public_server: asyncio.Server | None = None
        self._tunnel_server: asyncio.Server | None = None

    async def start(self) -> None:
        self._public_server = await asyncio.start_server(
            self._handle_public, self.bind, self.public_port, limit=routing.MAX_HEAD, backlog=512
        )
        self._tunnel_server = await asyncio.start_server(
            self._handle_tunnel, self.bind, self.tunnel_port, ssl=self._tls, backlog=512
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

    async def drain(self, grace: float = 30.0) -> None:
        """Graceful shutdown: stop accepting, let active splices finish, then stop."""
        for server in (self._public_server, self._tunnel_server):
            if server is not None:
                server.close()
        log.info("draining active=%s grace=%.0fs", self._active_splices, grace)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(grace):
                await self._no_active_splices.wait()
        await self.stop()

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
            else:
                log.warning("unexpected first frame type=%r", frame["type"])
                writer.close()
        except protocol.ProtocolError:
            writer.close()

    async def _handle_control(
        self, frame: protocol.Frame, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        token = protocol.require_str(frame, "token")
        protocol.require_int(frame, "local_port")
        tok = self.store.get_by_token(token)
        if tok is None:
            log.warning("bad token")
            await self._reject(writer, "bad_token")
            return

        subdomain = names.unique_name(self.tunnels)
        tunnel = Tunnel(subdomain, tok.token_hash)
        self.tunnels[subdomain] = tunnel
        hostname = f"{subdomain}.{self.base_domain}"
        ping_task = asyncio.create_task(self._ping_loop(writer))
        try:
            await protocol.write_frame(writer, protocol.ok(hostname=hostname))
            log.info("tunnel registered subdomain=%s hostname=%s", subdomain, hostname)
            self.store.touch(tok.token_hash)
            while True:
                async with asyncio.timeout(protocol.DEAD_PEER_TIMEOUT):
                    msg = await protocol.read_frame(reader)
                if msg["type"] == "ping":
                    await protocol.write_frame(writer, protocol.pong())
        except TimeoutError:
            log.info("dead peer subdomain=%s", subdomain)
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
            self.store.touch(tok.token_hash)
            log.info("tunnel closed subdomain=%s", subdomain)

    def _handle_data(
        self, frame: protocol.Frame, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        token = protocol.require_str(frame, "token")
        subdomain = protocol.require_str(frame, "subdomain")
        tunnel = self.tunnels.get(subdomain)
        if tunnel is None or not self._token_matches(token, tunnel.token_hash):
            writer.close()
            return
        if tunnel.data_conns >= self.max_conns_per_tunnel:
            log.warning(
                "connection cap reached subdomain=%s cap=%s", subdomain, self.max_conns_per_tunnel
            )
            writer.close()
            return
        # The handler returns here; the pool keeps the streams alive until a
        # public request claims them.
        tunnel.data_conns += 1
        tunnel.pool.put_nowait((reader, writer))
        log.debug("data conn pooled subdomain=%s idle=%s", subdomain, tunnel.pool.qsize())

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
        subdomain = routing.subdomain_for_host(host, self.base_domain) if host else None
        tunnel = self.tunnels.get(subdomain) if subdomain else None
        if tunnel is None:
            log.info("no tunnel host=%s", host)
            await self._respond(writer, "404 Not Found", "viaduct: no such tunnel\n")
            return
        conn = await tunnel.acquire(wait=self.pool_wait)
        if conn is None:
            log.warning("pool starved subdomain=%s waited=%.0fs", subdomain, self.pool_wait)
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
            tunnel.data_conns -= 1
            await self._respond(writer, "502 Bad Gateway", "viaduct: tunnel connection died\n")
            return
        self._active_splices += 1
        self._no_active_splices.clear()
        try:
            await splice(reader, writer, t_reader, t_writer, idle_timeout=self.idle_timeout)
        finally:
            self._active_splices -= 1
            if self._active_splices == 0:
                self._no_active_splices.set()
            tunnel.data_conns -= 1

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
    max_conns_per_tunnel: Annotated[
        int, typer.Option(help="Max pooled + active data connections per tunnel")
    ] = 128,
    idle_timeout: Annotated[
        float, typer.Option(help="Close proxied connections idle this many seconds (0 = never)")
    ] = 300.0,
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
        asyncio.run(
            _serve(
                bind,
                public_port,
                tunnel_port,
                base_domain,
                db,
                tls,
                max_conns_per_tunnel,
                idle_timeout if idle_timeout > 0 else None,
            )
        )


async def _serve(
    bind: str,
    public_port: int,
    tunnel_port: int,
    base_domain: str,
    db: Path,
    tls: ssl.SSLContext | None,
    max_conns_per_tunnel: int,
    idle_timeout: float | None,
) -> None:
    store = Store(db)
    server = TunnelServer(
        store=store,
        bind=bind,
        public_port=public_port,
        tunnel_port=tunnel_port,
        base_domain=base_domain,
        tls=tls,
        max_conns_per_tunnel=max_conns_per_tunnel,
        idle_timeout=idle_timeout,
    )
    await server.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
        log.info("shutdown requested — draining")
        await server.drain(grace=30.0)
    finally:
        await server.stop()
        store.close()


token_app = typer.Typer(help="Manage auth tokens.")
app.add_typer(token_app, name="token")


@token_app.command("create")
def token_create(
    label: Annotated[
        str | None, typer.Option(help="Optional note to remember who this token is for")
    ] = None,
    db: Annotated[Path, typer.Option(help="SQLite database path")] = DEFAULT_DB_PATH,
) -> None:
    """Mint an auth token and print it — shown once; only its sha256 is stored.

    Each tunnel gets a randomly generated subdomain at connect time, so a token
    is not tied to any name; one token can open many tunnels.
    """
    token = secrets.token_urlsafe(32)
    store = Store(db)
    try:
        store.create_token(hash_token(token), label)
    finally:
        store.close()
    typer.echo(token)
    note = f" for {label!r}" if label else ""
    typer.echo(
        f"viaductd: created token{note} — shown once; "
        "put it in the client's ~/.config/viaduct/config.toml",
        err=True,
    )


def main() -> None:
    app()
