"""Viaduct client (`viaduct`): control connection, data-connection pool, local pipes.

M1: connect once, no reconnect backoff (M5). Each data connection is one-shot:
it idles until the server assigns it a request, serves it, and is replaced.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
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
        ssl_ctx: ssl.SSLContext | None = None,
    ) -> None:
        self._server_host = server_host
        self._server_port = server_port
        self._ssl_ctx = ssl_ctx
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
        reader, writer = await asyncio.open_connection(
            self._server_host, self._server_port, ssl=self._ssl_ctx
        )
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
            reader, writer = await asyncio.open_connection(
                self._server_host, self._server_port, ssl=self._ssl_ctx
            )
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


async def domain_request(
    server_host: str,
    server_port: int,
    ssl_ctx: ssl.SSLContext | None,
    frame: protocol.Frame,
) -> protocol.Frame:
    """One-shot domain-management exchange: send a frame, return the ok reply."""
    reader, writer = await asyncio.open_connection(server_host, server_port, ssl=ssl_ctx)
    try:
        await protocol.write_frame(writer, frame)
        reply = await protocol.read_frame(reader)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    if reply["type"] == "error":
        raise TunnelError(protocol.require_str(reply, "reason"))
    if reply["type"] != "ok":
        raise TunnelError(f"unexpected reply type {reply['type']!r}")
    return reply


console = Console()
app = typer.Typer(help="Viaduct client — expose a local service through a viaduct server.")
domain_app = typer.Typer(help="Manage custom domains routed to your tunnel.")
app.add_typer(domain_app, name="domain")

_ServerOpt = Annotated[
    str | None,
    typer.Option("--server", help="Server tunnel address as host:port (default: config.toml)"),
]
_TokenOpt = Annotated[
    str | None,
    typer.Option("--token", envvar="VIADUCT_TOKEN", help="Auth token (default: config.toml)"),
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
    subdomain: Annotated[str, typer.Option(help="Subdomain to claim on the server")],
    server: _ServerOpt = None,
    token: _TokenOpt = None,
    pool_size: Annotated[
        int, typer.Option(help="Idle data connections to maintain")
    ] = DEFAULT_POOL_SIZE,
    tls: _TlsOpt = None,
    tls_ca: _TlsCaOpt = None,
) -> None:
    """Open a tunnel exposing local PORT. Blocks until the connection drops."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host, srv_port, tok, ssl_ctx = _connection_settings(server, token, tls, tls_ca)
    try:
        asyncio.run(_run_http(host, srv_port, tok, subdomain, port, pool_size, ssl_ctx))
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


@domain_app.command("add")
def domain_add(
    hostname: Annotated[str, typer.Argument(help="Custom domain, e.g. demo.example.com")],
    subdomain: Annotated[str, typer.Option(help="Reserved subdomain it should route to")],
    server: _ServerOpt = None,
    token: _TokenOpt = None,
    tls: _TlsOpt = None,
    tls_ca: _TlsCaOpt = None,
) -> None:
    """Register a custom domain and print the DNS record to create."""
    host, srv_port, tok, ssl_ctx = _connection_settings(server, token, tls, tls_ca)
    reply = _domain_op(
        domain_request(host, srv_port, ssl_ctx, protocol.domain_add(tok, subdomain, hostname))
    )
    registered = protocol.require_str(reply, "hostname")
    target = protocol.require_str(reply, "target")
    console.print(f"[bold green]domain registered[/] {registered} → {subdomain}")
    console.print(f"\nCreate this DNS record:\n\n    CNAME  {registered}  →  {target}\n")
    console.print(
        "[yellow]note:[/] apex domains cannot take a CNAME — "
        "use your DNS provider's ALIAS/ANAME record type instead."
    )


@domain_app.command("list")
def domain_list(
    server: _ServerOpt = None,
    token: _TokenOpt = None,
    tls: _TlsOpt = None,
    tls_ca: _TlsCaOpt = None,
) -> None:
    """List custom domains registered for your subdomain."""
    host, srv_port, tok, ssl_ctx = _connection_settings(server, token, tls, tls_ca)
    reply = _domain_op(domain_request(host, srv_port, ssl_ctx, protocol.domain_list(tok)))
    domains = reply.get("domains") or []
    if not domains:
        console.print("no custom domains registered")
        return
    for entry in domains:
        console.print(f"{entry.get('hostname')}  →  {entry.get('subdomain')}")


@domain_app.command("remove")
def domain_remove(
    hostname: Annotated[str, typer.Argument(help="Custom domain to remove")],
    server: _ServerOpt = None,
    token: _TokenOpt = None,
    tls: _TlsOpt = None,
    tls_ca: _TlsCaOpt = None,
) -> None:
    """Remove a custom domain."""
    host, srv_port, tok, ssl_ctx = _connection_settings(server, token, tls, tls_ca)
    _domain_op(domain_request(host, srv_port, ssl_ctx, protocol.domain_remove(tok, hostname)))
    console.print(f"[bold green]domain removed[/] {hostname}")


def _domain_op(coro: Coroutine[Any, Any, protocol.Frame]) -> protocol.Frame:
    try:
        return asyncio.run(coro)
    except TunnelError as exc:
        console.print(f"[bold red]viaduct: server refused request: {exc}[/]")
        raise typer.Exit(1) from exc
    except (protocol.ProtocolError, OSError) as exc:
        console.print(f"[bold red]viaduct: cannot talk to server: {exc}[/]")
        raise typer.Exit(1) from exc


def _connection_settings(
    server: str | None, token: str | None, tls: bool | None, tls_ca: str | None
) -> tuple[str, int, str, ssl.SSLContext | None]:
    """Resolve server/token/TLS from flags, env, and config.toml (in that order)."""
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        console.print(f"[bold red]viaduct: {exc}[/]")
        raise typer.Exit(2) from exc
    server = server or _cfg_str(cfg, "server") or "127.0.0.1:4443"
    token = token or _cfg_str(cfg, "token")
    if not token:
        console.print(
            "[bold red]viaduct: no token configured[/] — pass --token, set VIADUCT_TOKEN, "
            f'or add token = "..." to {config.config_path()}'
        )
        raise typer.Exit(2)
    use_tls = tls if tls is not None else cfg.get("tls") is True
    ca = tls_ca or _cfg_str(cfg, "tls_ca")
    ssl_ctx = ssl.create_default_context(cafile=ca) if use_tls else None
    host, _, port_str = server.rpartition(":")
    if not host or not port_str.isdigit():
        raise typer.BadParameter("--server must be host:port")
    return host, int(port_str), token, ssl_ctx


def _cfg_str(cfg: dict[str, str | bool], key: str) -> str | None:
    value = cfg.get(key)
    return value if isinstance(value, str) else None


async def _run_http(
    server_host: str,
    server_port: int,
    token: str,
    subdomain: str,
    local_port: int,
    pool_size: int,
    ssl_ctx: ssl.SSLContext | None,
) -> None:
    client = TunnelClient(
        server_host=server_host,
        server_port=server_port,
        token=token,
        subdomain=subdomain,
        local_port=local_port,
        pool_size=pool_size,
        ssl_ctx=ssl_ctx,
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
