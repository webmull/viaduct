"""Viaduct server (`viaductd`): public listener, tunnel registry, idle pools.

No auth: any client that reaches the tunnel port gets a tunnel and a random
subdomain for the life of that connection, freed on disconnect. Restrict who
can reach the tunnel port at the firewall if that matters. Nothing is
persisted — there is no database. Runtime state lives in a `dict[str, Tunnel]`
and dies with the process, so clients simply redial (fresh name) on restart.
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
from urllib.parse import parse_qs, urlsplit

import typer

from viaduct import dns, names, protocol, routing
from viaduct.relay import splice

log = logging.getLogger("viaduct.server")

Conn = tuple[asyncio.StreamReader, asyncio.StreamWriter]

#: how long a resolved custom-domain -> subdomain mapping is trusted (seconds)
DOMAIN_CACHE_TTL = 60.0
#: how many CNAME hops to follow when resolving a custom domain to a tunnel
CNAME_MAX_HOPS = 3
#: Caddy's on-demand-TLS ask endpoint, served on the public listener
TLS_CHECK_PATH = "/_viaduct/tls-check"


class Tunnel:
    """One connected client: its subdomain and idle pool of data connections."""

    def __init__(self, subdomain: str, token: str) -> None:
        self.subdomain = subdomain
        #: per-tunnel capability: a data connection must present this exact token
        #: (sent to the owning client over the TLS control channel) to attach.
        self.token = token
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
        bind: str = "127.0.0.1",
        public_bind: str = "127.0.0.1",
        public_port: int = 8080,
        tunnel_port: int = 4443,
        base_domain: str = "localhost",
        tls: ssl.SSLContext | None = None,
        max_conns_per_tunnel: int = 128,
        idle_timeout: float | None = 300.0,
        pool_wait: float = 10.0,
        max_tunnels_per_ip: int = 4,
    ) -> None:
        self.bind = bind
        #: the plaintext public listener only ever needs to be reached by Caddy
        #: on localhost, so it binds here (not --bind), independent of the tunnel
        #: listener; keeps un-TLS'd HTTP off any external interface.
        self.public_bind = public_bind
        self.public_port = public_port
        self.tunnel_port = tunnel_port
        self.base_domain = base_domain
        self._tls = tls
        self.max_conns_per_tunnel = max_conns_per_tunnel
        self.idle_timeout = idle_timeout
        self.pool_wait = pool_wait
        #: abuse safeguard for a no-auth server: cap concurrent tunnels per
        #: source IP. Data connections don't count — only tunnels (control
        #: connections). ``_ip_tunnels`` maps source IP -> live tunnel count.
        self.max_tunnels_per_ip = max_tunnels_per_ip
        self._ip_tunnels: dict[str, int] = {}
        self.tunnels: dict[str, Tunnel] = {}
        #: custom domain -> tunnel subdomain (or None), resolved via CNAME, cached
        self._domain_cache: dict[str, tuple[str | None, float]] = {}
        self._active_splices = 0
        self._no_active_splices = asyncio.Event()
        self._no_active_splices.set()
        self._public_server: asyncio.Server | None = None
        self._tunnel_server: asyncio.Server | None = None

    async def start(self) -> None:
        self._public_server = await asyncio.start_server(
            self._handle_public,
            self.public_bind,
            self.public_port,
            limit=routing.MAX_HEAD,
            backlog=512,
        )
        self._tunnel_server = await asyncio.start_server(
            self._handle_tunnel, self.bind, self.tunnel_port, ssl=self._tls, backlog=512
        )
        self.public_port = self._public_server.sockets[0].getsockname()[1]
        self.tunnel_port = self._tunnel_server.sockets[0].getsockname()[1]
        log.info(
            "listening public=%s:%s tunnel=%s:%s base_domain=%s",
            self.public_bind,
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
            async with asyncio.timeout(protocol.HANDSHAKE_TIMEOUT):
                frame = await protocol.read_frame(reader)
            if frame["type"] == "hello":
                await self._handle_control(frame, reader, writer)
            elif frame["type"] == "data_hello":
                self._handle_data(frame, reader, writer)
            else:
                log.warning("unexpected first frame type=%r", frame["type"])
                writer.close()
        except (protocol.ProtocolError, TimeoutError):
            writer.close()  # bad frame or a silent/slowloris connection

    async def _handle_control(
        self, frame: protocol.Frame, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        protocol.require_int(frame, "local_port")
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "unknown"
        if self._ip_tunnels.get(ip, 0) >= self.max_tunnels_per_ip:
            log.warning("ip tunnel limit reached ip=%s limit=%s", ip, self.max_tunnels_per_ip)
            await self._reject(writer, "ip_tunnel_limit")
            return

        pin = frame.get("pin")
        if isinstance(pin, str) and pin:
            # --pin: a stable, server-derived name (no state; same seed -> same
            # name). Reject rather than reassign if it is already live.
            subdomain = names.derived_name(pin)
            if subdomain in self.tunnels:
                log.info("pinned name in use subdomain=%s ip=%s", subdomain, ip)
                await self._reject(writer, "pin_in_use")
                return
        else:
            subdomain = names.unique_name(self.tunnels)
        token = secrets.token_hex(16)  # capability binding data conns to this client
        tunnel = Tunnel(subdomain, token)
        self.tunnels[subdomain] = tunnel
        self._ip_tunnels[ip] = self._ip_tunnels.get(ip, 0) + 1
        hostname = f"{subdomain}.{self.base_domain}"
        unanswered = [0]  # our pings not yet ponged; shared with the ping loop
        ping_task = asyncio.create_task(self._ping_loop(writer, unanswered))
        try:
            await protocol.write_frame(writer, protocol.ok(hostname=hostname, token=token))
            log.info("tunnel registered subdomain=%s hostname=%s", subdomain, hostname)
            while True:
                async with asyncio.timeout(protocol.DEAD_PEER_TIMEOUT):
                    msg = await protocol.read_frame(reader)
                if msg["type"] == "ping":
                    await protocol.write_frame(writer, protocol.pong())
                elif msg["type"] == "pong":
                    unanswered[0] = 0  # the client is answering us
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
            self._ip_tunnels[ip] = self._ip_tunnels.get(ip, 1) - 1
            if self._ip_tunnels[ip] <= 0:
                del self._ip_tunnels[ip]
            tunnel.close_pool()
            writer.close()
            log.info("tunnel closed subdomain=%s", subdomain)

    def _handle_data(
        self, frame: protocol.Frame, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        subdomain = protocol.require_str(frame, "subdomain")
        token = frame.get("token")
        tunnel = self.tunnels.get(subdomain)
        # Bind the data connection to its tunnel's owner: knowing the (public)
        # subdomain is not enough, the caller must present the capability token
        # the server handed the client over the TLS control channel. Without
        # this, anyone reaching the tunnel port could attach to another tunnel
        # and intercept or serve its traffic.
        if (
            tunnel is None
            or not isinstance(token, str)
            or not hmac.compare_digest(token, tunnel.token)
        ):
            if tunnel is not None:
                log.warning("data conn rejected (bad token) subdomain=%s", subdomain)
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

    async def _ping_loop(self, writer: asyncio.StreamWriter, unanswered: list[int]) -> None:
        with contextlib.suppress(ConnectionError):
            while True:
                await asyncio.sleep(protocol.HEARTBEAT_INTERVAL)
                if unanswered[0] >= protocol.HEARTBEAT_MAX_MISSED:
                    writer.close()  # client not answering pings; drop the dead peer
                    return
                unanswered[0] += 1
                await protocol.write_frame(writer, protocol.ping())

    async def _reject(self, writer: asyncio.StreamWriter, reason: str) -> None:
        with contextlib.suppress(ConnectionError):
            await protocol.write_frame(writer, protocol.error(reason))
        writer.close()

    # -- public listener: plaintext HTTP from Caddy (or curl, pre-TLS) ---------

    async def _handle_public(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            async with asyncio.timeout(30):
                head = await routing.read_head(reader)
        except (routing.BadRequest, TimeoutError, ConnectionError):
            await self._respond(
                writer, "400 Bad Request", "Bad request", "That request could not be understood."
            )
            return
        if routing.request_target(head).startswith(TLS_CHECK_PATH):
            await self._tls_check(head, writer)  # Caddy on-demand-TLS ask endpoint
            return
        host = routing.extract_host(head)
        subdomain = routing.subdomain_for_host(host, self.base_domain) if host else None
        if subdomain is None and host:
            subdomain = await self._custom_subdomain(host)  # bring-your-own-domain via CNAME
        tunnel = self.tunnels.get(subdomain) if subdomain else None
        if tunnel is None:
            log.info("no tunnel host=%r", host)  # %r escapes any control chars in the header
            await self._respond(
                writer,
                "404 Not Found",
                "Tunnel not found",
                "No tunnel is registered for this address. "
                "It may have closed, or the link is out of date.",
            )
            return
        conn = await tunnel.acquire(wait=self.pool_wait)
        if conn is None:
            log.warning("pool starved subdomain=%s waited=%.0fs", subdomain, self.pool_wait)
            await self._respond(
                writer,
                "503 Service Unavailable",
                "Tunnel busy",
                "This tunnel is out of free connections right now. It will retry on its own.",
                retry_after=10,
            )
            return
        t_reader, t_writer = conn
        # Count as active from acquisition (before the head write), so a drain in
        # this window waits for the request instead of cancelling it mid-flight.
        self._active_splices += 1
        self._no_active_splices.clear()
        try:
            try:
                t_writer.write(head)
                await t_writer.drain()
            except ConnectionError:
                t_writer.close()
                await self._respond(
                    writer,
                    "502 Bad Gateway",
                    "Tunnel connection dropped",
                    "The tunnel connection closed mid-request. Refresh to try again.",
                )
                return
            await splice(reader, writer, t_reader, t_writer, idle_timeout=self.idle_timeout)
        finally:
            self._active_splices -= 1
            if self._active_splices == 0:
                self._no_active_splices.set()
            tunnel.data_conns -= 1

    async def _custom_subdomain(self, host: str) -> str | None:
        """Resolve a non-wildcard Host to a tunnel by following its CNAME chain.

        A custom domain CNAME'd to ``name.BASE_DOMAIN`` resolves back to that
        ``name``; the mapping lives in the user's DNS, so nothing is stored here
        beyond a short-lived cache. An A record (no CNAME to follow) yields None.
        """
        loop = asyncio.get_running_loop()
        now = loop.time()
        cached = self._domain_cache.get(host)
        if cached is not None and cached[1] > now:
            return cached[0]
        if len(self._domain_cache) > 4096:  # bound memory against random-Host floods
            self._domain_cache = {k: v for k, v in self._domain_cache.items() if v[1] > now}
        subdomain = None
        name = host
        for _ in range(CNAME_MAX_HOPS):
            target = await dns.resolve_cname(name)
            if target is None:
                break
            candidate = routing.subdomain_for_host(target, self.base_domain)
            if candidate is not None:
                subdomain = candidate
                break
            name = target  # follow the chain toward BASE_DOMAIN
        self._domain_cache[host] = (subdomain, now + DOMAIN_CACHE_TTL)
        return subdomain

    async def _tls_check(self, head: bytes, writer: asyncio.StreamWriter) -> None:
        """Answer Caddy's on-demand-TLS ask: 200 only for a live tunnel's domain.

        Without this gate, on-demand issuance would be an open certificate mill.
        """
        query = urlsplit(routing.request_target(head)).query
        domain = (parse_qs(query).get("domain") or [""])[0].lower().rstrip(".")
        subdomain = routing.subdomain_for_host(domain, self.base_domain) if domain else None
        if subdomain is None and domain:
            subdomain = await self._custom_subdomain(domain)
        ok = bool(subdomain and subdomain in self.tunnels)
        status = "200 OK" if ok else "404 Not Found"
        reply = f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        with contextlib.suppress(ConnectionError):
            writer.write(reply.encode())
            await writer.drain()
        writer.close()

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        status: str,
        title: str,
        detail: str,
        retry_after: int | None = None,
    ) -> None:
        with contextlib.suppress(ConnectionError):
            writer.write(routing.error_response(status, title, detail, retry_after))
            await writer.drain()
        writer.close()


app = typer.Typer(help="Viaduct server daemon.")


@app.callback(invoke_without_command=True)
def _cli(
    ctx: typer.Context,
    bind: Annotated[str, typer.Option(help="Address to bind the tunnel listener to")] = "127.0.0.1",
    public_bind: Annotated[
        str, typer.Option(help="Address for the plaintext public listener (Caddy is local)")
    ] = "127.0.0.1",
    public_port: Annotated[int, typer.Option(help="Port for public HTTP traffic")] = 8080,
    tunnel_port: Annotated[int, typer.Option(help="Port for client tunnel connections")] = 4443,
    base_domain: Annotated[str, typer.Option(help="Domain that subdomains hang off")] = "localhost",
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
    max_tunnels_per_ip: Annotated[
        int, typer.Option(help="Max concurrent tunnels allowed from one source IP")
    ] = 4,
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
                public_bind,
                public_port,
                tunnel_port,
                base_domain,
                tls,
                max_conns_per_tunnel,
                idle_timeout if idle_timeout > 0 else None,
                max_tunnels_per_ip,
            )
        )


async def _serve(
    bind: str,
    public_bind: str,
    public_port: int,
    tunnel_port: int,
    base_domain: str,
    tls: ssl.SSLContext | None,
    max_conns_per_tunnel: int,
    idle_timeout: float | None,
    max_tunnels_per_ip: int,
) -> None:
    server = TunnelServer(
        bind=bind,
        public_bind=public_bind,
        public_port=public_port,
        tunnel_port=tunnel_port,
        base_domain=base_domain,
        tls=tls,
        max_conns_per_tunnel=max_conns_per_tunnel,
        idle_timeout=idle_timeout,
        max_tunnels_per_ip=max_tunnels_per_ip,
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


def main() -> None:
    app()
