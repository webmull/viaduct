"""Viaduct client (`viaduct`): control connection, data-connection pool, local pipes.

M1: connect once, no reconnect backoff (M5). Each data connection is one-shot:
it idles until the server assigns it a request, serves it, and is replaced.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Coroutine
from typing import Annotated, Any

import typer
from rich.console import Console

from viaduct import config, protocol
from viaduct.relay import CHUNK, splice
from viaduct.routing import plain_response

log = logging.getLogger("viaduct.client")

DEFAULT_POOL_SIZE = 20


class TunnelError(Exception):
    """The server refused the tunnel (bad token, subdomain taken, ...)."""


class TunnelClient:
    def __init__(
        self,
        *,
        server_host: str,
        server_port: int,
        token: str,
        subdomain: str,
        local_port: int,
        local_host: str = "127.0.0.1",
        pool_size: int = DEFAULT_POOL_SIZE,
    ) -> None:
        self._server_host = server_host
        self._server_port = server_port
        self._token = token
        self._subdomain = subdomain
        self._local_host = local_host
        self._local_port = local_port
        self._pool_size = pool_size
        self.hostname: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = asyncio.Event()
        self._stopping = False

    async def start(self) -> str:
        """Authenticate, fill the pool, and return the public hostname."""
        reader, writer = await asyncio.open_connection(self._server_host, self._server_port)
        await protocol.write_frame(
            writer, protocol.hello(self._token, self._subdomain, self._local_port)
        )
        resp = await protocol.read_frame(reader)
        if resp["type"] == "error":
            writer.close()
            raise TunnelError(protocol.require_str(resp, "reason"))
        if resp["type"] != "ok":
            writer.close()
            raise TunnelError(f"unexpected reply type {resp['type']!r}")
        self.hostname = protocol.require_str(resp, "hostname")
        self._reader, self._writer = reader, writer
        self._spawn(self._control_loop(reader, writer))
        self._spawn(self._heartbeat(writer))
        for _ in range(self._pool_size):
            self._spawn(self._run_data_conn())
        return self.hostname

    async def wait_closed(self) -> None:
        """Block until the control connection drops."""
        await self._closed.wait()

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(OSError):
                await self._writer.wait_closed()
        self._closed.set()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        if self._stopping:
            coro.close()
            return
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _control_loop(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                frame = await protocol.read_frame(reader)
                if frame["type"] == "ping":
                    await protocol.write_frame(writer, protocol.pong())
        except (protocol.ProtocolError, ConnectionError):
            pass
        finally:
            self._closed.set()

    async def _heartbeat(self, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            while True:
                await asyncio.sleep(protocol.HEARTBEAT_INTERVAL)
                await protocol.write_frame(writer, protocol.ping())

    async def _run_data_conn(self) -> None:
        """One pooled connection: idle at the server until assigned, then serve."""
        try:
            reader, writer = await asyncio.open_connection(self._server_host, self._server_port)
        except OSError:
            return
        try:
            await protocol.write_frame(writer, protocol.data_hello(self._token, self._subdomain))
            first = await reader.read(CHUNK)
            if not first:
                return  # server dropped the idle connection
            # This connection just went busy — open its replacement now so the
            # idle pool stays at target size (architecture step 5).
            self._spawn(self._run_data_conn())
            try:
                l_reader, l_writer = await asyncio.open_connection(
                    self._local_host, self._local_port
                )
            except OSError:
                log.warning("local app refused connection port=%s", self._local_port)
                writer.write(plain_response("502 Bad Gateway", "viaduct: local app is down\n"))
                await writer.drain()
                return
            try:
                l_writer.write(first)
                await l_writer.drain()
            except ConnectionError:
                l_writer.close()
                return
            await splice(reader, writer, l_reader, l_writer)
        except (protocol.ProtocolError, OSError):
            pass
        finally:
            writer.close()


console = Console()
app = typer.Typer(help="Viaduct client — expose a local service through a viaduct server.")


@app.callback()
def _cli() -> None:
    """Viaduct client."""


@app.command()
def http(
    port: Annotated[int, typer.Argument(help="Local port to expose")],
    subdomain: Annotated[str, typer.Option(help="Subdomain to claim on the server")],
    server: Annotated[
        str | None, typer.Option(help="Server tunnel address as host:port (default: config.toml)")
    ] = None,
    token: Annotated[
        str | None, typer.Option(envvar="VIADUCT_TOKEN", help="Auth token (default: config.toml)")
    ] = None,
    pool_size: Annotated[
        int, typer.Option(help="Idle data connections to maintain")
    ] = DEFAULT_POOL_SIZE,
) -> None:
    """Open a tunnel exposing local PORT. Blocks until the connection drops."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        console.print(f"[bold red]viaduct: {exc}[/]")
        raise typer.Exit(2) from exc
    server = server or cfg.get("server") or "127.0.0.1:4443"
    token = token or cfg.get("token")
    if not token:
        console.print(
            "[bold red]viaduct: no token configured[/] — pass --token, set VIADUCT_TOKEN, "
            f'or add token = "..." to {config.config_path()}'
        )
        raise typer.Exit(2)
    host, _, port_str = server.rpartition(":")
    if not host or not port_str.isdigit():
        raise typer.BadParameter("--server must be host:port")
    try:
        asyncio.run(_run_http(host, int(port_str), token, subdomain, port, pool_size))
    except KeyboardInterrupt:
        console.print("[yellow]viaduct: interrupted[/]")
    except TunnelError as exc:
        console.print(f"[bold red]viaduct: server refused tunnel: {exc}[/]")
        raise typer.Exit(1) from exc
    except OSError as exc:
        console.print(f"[bold red]viaduct: cannot reach server: {exc}[/]")
        raise typer.Exit(1) from exc


async def _run_http(
    server_host: str,
    server_port: int,
    token: str,
    subdomain: str,
    local_port: int,
    pool_size: int,
) -> None:
    client = TunnelClient(
        server_host=server_host,
        server_port=server_port,
        token=token,
        subdomain=subdomain,
        local_port=local_port,
        pool_size=pool_size,
    )
    hostname = await client.start()
    console.print(f"[bold green]tunnel up[/] {hostname} → 127.0.0.1:{local_port}")
    try:
        await client.wait_closed()
    finally:
        await client.stop()
    console.print("[yellow]viaduct: connection to server lost[/]")


def main() -> None:
    app()
