"""Viaduct server (`viaductd`): public listener, tunnel registry, idle pools.

M1: single hardcoded token, no persistence, no TLS. Runtime state lives in a
`dict[str, Tunnel]` and dies with the process — clients redial on restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from typing import Annotated

import typer

from viaduct import protocol, routing
from viaduct.relay import splice

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
        bind: str = "127.0.0.1",
        public_port: int = 8080,
        tunnel_port: int = 4443,
        base_domain: str = "localhost",
        token: str = "dev-token",
    ) -> None:
        self.bind = bind
        self.public_port = public_port
        self.tunnel_port = tunnel_port
        self.base_domain = base_domain
        self.token = token
        self.tunnels: dict[str, Tunnel] = {}
        self._public_server: asyncio.Server | None = None
        self._tunnel_server: asyncio.Server | None = None

    async def start(self) -> None:
        self._public_server = await asyncio.start_server(
            self._handle_public, self.bind, self.public_port, limit=routing.MAX_HEAD
        )
        self._tunnel_server = await asyncio.start_server(
            self._handle_tunnel, self.bind, self.tunnel_port
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
        if not self._token_ok(token):
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
            await protocol.write_frame(writer, protocol.ok(hostname))
            log.info("tunnel registered subdomain=%s hostname=%s", subdomain, hostname)
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
            log.info("tunnel closed subdomain=%s", subdomain)

    def _handle_data(
        self, frame: protocol.Frame, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        token = protocol.require_str(frame, "token")
        subdomain = protocol.require_str(frame, "subdomain")
        tunnel = self.tunnels.get(subdomain)
        if tunnel is None or not self._token_ok(token):
            writer.close()
            return
        # The handler returns here; the pool keeps the streams alive until a
        # public request claims them.
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

    def _token_ok(self, token: str) -> bool:
        return hmac.compare_digest(token.encode(), self.token.encode())

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
    token: Annotated[
        str,
        typer.Option(envvar="VIADUCT_TOKEN", help="Shared auth token (M1 placeholder)"),
    ] = "dev-token",
) -> None:
    """Run the tunnel server. M2 replaces the shared token with hashed per-user tokens."""
    if ctx.invoked_subcommand is not None:
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(bind, public_port, tunnel_port, base_domain, token))


async def _serve(
    bind: str, public_port: int, tunnel_port: int, base_domain: str, token: str
) -> None:
    server = TunnelServer(
        bind=bind,
        public_port=public_port,
        tunnel_port=tunnel_port,
        base_domain=base_domain,
        token=token,
    )
    await server.start()
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


def main() -> None:
    app()
