"""Viaduct client (`viaduct`): control connection, data-connection pool, local pipes.

Each data connection is one-shot: it idles until the server assigns it a
request, serves it, and is replaced. The CLI reconnects on drop with
exponential backoff (1s, 2s, 4s ... capped at 30s) and drains gracefully on
SIGTERM/SIGINT: active transfers get time to finish, nothing new is accepted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import ssl
from collections.abc import Coroutine
from typing import Annotated, Any

import typer
from rich.console import Console

from viaduct import config, protocol
from viaduct.relay import CHUNK, splice
from viaduct.routing import plain_response

log = logging.getLogger("viaduct.client")

DEFAULT_POOL_SIZE = 40


class TunnelError(Exception):
    """The server refused the tunnel."""


class TunnelClient:
    def __init__(
        self,
        *,
        server_host: str,
        server_port: int,
        local_port: int,
        local_host: str = "127.0.0.1",
        pool_size: int = DEFAULT_POOL_SIZE,
        ssl_ctx: ssl.SSLContext | None = None,
    ) -> None:
        self._server_host = server_host
        self._server_port = server_port
        self._ssl_ctx = ssl_ctx
        self._local_host = local_host
        self._local_port = local_port
        self._pool_size = pool_size
        self.hostname: str | None = None
        #: assigned by the server, learned from the hostname it returns
        self.subdomain: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._busy: set[asyncio.Task[None]] = set()
        self._closed = asyncio.Event()
        self._stopping = False

    async def start(self) -> str:
        """Open the control connection, fill the pool, return the assigned hostname."""
        reader, writer = await asyncio.open_connection(
            self._server_host, self._server_port, ssl=self._ssl_ctx
        )
        await protocol.write_frame(writer, protocol.hello(self._local_port))
        resp = await protocol.read_frame(reader)
        if resp["type"] == "error":
            writer.close()
            raise TunnelError(protocol.require_str(resp, "reason"))
        if resp["type"] != "ok":
            writer.close()
            raise TunnelError(f"unexpected reply type {resp['type']!r}")
        self.hostname = protocol.require_str(resp, "hostname")
        # The server picked our subdomain; it is the leading label of the host.
        self.subdomain = self.hostname.split(".", 1)[0]
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

    async def drain(self, grace: float = 30.0) -> None:
        """Graceful shutdown: no new work, let in-flight transfers finish, then stop."""
        self._stopping = True
        if self._writer is not None:
            self._writer.close()  # server unregisters; no new assignments arrive
        for task in list(self._tasks - self._busy):
            task.cancel()
        if self._busy:
            log.info("draining active=%s grace=%.0fs", len(self._busy), grace)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(grace):
                    await asyncio.gather(*self._busy, return_exceptions=True)
        await self.stop()

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
                async with asyncio.timeout(protocol.DEAD_PEER_TIMEOUT):
                    frame = await protocol.read_frame(reader)
                if frame["type"] == "ping":
                    await protocol.write_frame(writer, protocol.pong())
        except TimeoutError:
            log.warning("server went silent — treating connection as dead")
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
        """One pool worker: keep an idle connection at the server until assigned.

        Connect failures back off exponentially so a restarting server isn't
        hammered; a dropped idle connection is replaced after a short pause.
        On assignment, a replacement worker is spawned immediately so the idle
        pool stays at target size (architecture step 5), then this worker
        serves its one request and exits.
        """
        backoff = 1.0
        while not self._stopping and not self._closed.is_set():
            try:
                reader, writer = await asyncio.open_connection(
                    self._server_host, self._server_port, ssl=self._ssl_ctx
                )
            except OSError:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 1.0
            try:
                await protocol.write_frame(writer, protocol.data_hello(self.subdomain or ""))
                first = await reader.read(CHUNK)
            except (protocol.ProtocolError, OSError):
                first = b""
            if not first:  # server dropped the idle connection
                writer.close()
                await asyncio.sleep(1.0)
                continue
            self._spawn(self._run_data_conn())
            task = asyncio.current_task()
            if task is not None:
                self._busy.add(task)
            try:
                await self._serve_assignment(reader, writer, first)
            finally:
                if task is not None:
                    self._busy.discard(task)
                writer.close()
            return

    async def _serve_assignment(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, first: bytes
    ) -> None:
        try:
            l_reader, l_writer = await asyncio.open_connection(self._local_host, self._local_port)
        except OSError:
            log.warning("local app refused connection port=%s", self._local_port)
            with contextlib.suppress(OSError):
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


console = Console()
app = typer.Typer(help="Viaduct client — expose a local service through a viaduct server.")

_ServerOpt = Annotated[
    str | None,
    typer.Option("--server", help="Server tunnel address as host:port (default: config.toml)"),
]
_TlsOpt = Annotated[
    bool | None,
    typer.Option("--tls/--no-tls", help="TLS to the server tunnel port (default: config.toml)"),
]
_TlsCaOpt = Annotated[
    str | None,
    typer.Option("--tls-ca", help="Extra CA bundle (PEM) to trust, e.g. a self-signed server cert"),
]


@app.callback()
def _cli() -> None:
    """Viaduct client."""


@app.command()
def http(
    port: Annotated[int, typer.Argument(help="Local port to expose")],
    server: _ServerOpt = None,
    pool_size: Annotated[
        int, typer.Option(help="Idle data connections to maintain")
    ] = DEFAULT_POOL_SIZE,
    tls: _TlsOpt = None,
    tls_ca: _TlsCaOpt = None,
) -> None:
    """Open a tunnel exposing local PORT. The server assigns a random public URL.

    Blocks until interrupted, reconnecting on drop (each reconnect gets a fresh
    URL).
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host, srv_port, ssl_ctx = _connection_settings(server, tls, tls_ca)
    try:
        asyncio.run(_run_http(host, srv_port, port, pool_size, ssl_ctx))
    except KeyboardInterrupt:
        console.print("[yellow]viaduct: interrupted[/]")
    except TunnelError as exc:
        console.print(f"[bold red]viaduct: server refused tunnel: {exc}[/]")
        raise typer.Exit(1) from exc
    except protocol.ProtocolError as exc:
        console.print(
            f"[bold red]viaduct: protocol error talking to server: {exc}[/] "
            "(is TLS configured the same on both ends?)"
        )
        raise typer.Exit(1) from exc
    except OSError as exc:
        console.print(f"[bold red]viaduct: cannot reach server: {exc}[/]")
        raise typer.Exit(1) from exc


def _connection_settings(
    server: str | None, tls: bool | None, tls_ca: str | None
) -> tuple[str, int, ssl.SSLContext | None]:
    """Resolve server/TLS from flags, env, and config.toml (in that order)."""
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        console.print(f"[bold red]viaduct: {exc}[/]")
        raise typer.Exit(2) from exc
    server = server or _cfg_str(cfg, "server") or "127.0.0.1:4443"
    use_tls = tls if tls is not None else cfg.get("tls") is True
    ca = tls_ca or _cfg_str(cfg, "tls_ca")
    ssl_ctx = ssl.create_default_context(cafile=ca) if use_tls else None
    host, _, port_str = server.rpartition(":")
    if not host or not port_str.isdigit():
        raise typer.BadParameter("--server must be host:port")
    return host, int(port_str), ssl_ctx


def _cfg_str(cfg: dict[str, str | bool], key: str) -> str | None:
    value = cfg.get(key)
    return value if isinstance(value, str) else None


async def _run_http(
    server_host: str,
    server_port: int,
    local_port: int,
    pool_size: int,
    ssl_ctx: ssl.SSLContext | None,
) -> None:
    """Keep the tunnel up: reconnect on drop with 1s→30s exponential backoff."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    delay = 1.0
    while not stop.is_set():
        client = TunnelClient(
            server_host=server_host,
            server_port=server_port,
            local_port=local_port,
            pool_size=pool_size,
            ssl_ctx=ssl_ctx,
        )
        try:
            hostname = await client.start()
        except TunnelError as exc:
            await client.stop()
            console.print(f"[yellow]viaduct: {exc}; retrying in {delay:.0f}s[/]")
            if await _wait_or_stop(stop, delay):
                return
            delay = min(delay * 2, 30.0)
            continue
        except (protocol.ProtocolError, OSError) as exc:
            await client.stop()
            console.print(
                f"[yellow]viaduct: cannot reach server ({exc}); retrying in {delay:.0f}s[/]"
            )
            if await _wait_or_stop(stop, delay):
                return
            delay = min(delay * 2, 30.0)
            continue

        delay = 1.0
        console.print(f"[bold green]tunnel up[/] {hostname} → 127.0.0.1:{local_port}")
        closed = asyncio.ensure_future(client.wait_closed())
        stopped = asyncio.ensure_future(stop.wait())
        await asyncio.wait({closed, stopped}, return_when=asyncio.FIRST_COMPLETED)
        for pending in (closed, stopped):
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        if stop.is_set():
            console.print("[yellow]viaduct: shutting down — draining[/]")
            await client.drain()
            return
        await client.stop()
        console.print(f"[yellow]viaduct: connection lost; reconnecting in {delay:.0f}s[/]")
        if await _wait_or_stop(stop, delay):
            return
        delay = min(delay * 2, 30.0)


async def _wait_or_stop(stop: asyncio.Event, delay: float) -> bool:
    """Sleep for *delay* but wake early on shutdown; returns True if stopping."""
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(delay):
            await stop.wait()
    return stop.is_set()


def main() -> None:
    app()
