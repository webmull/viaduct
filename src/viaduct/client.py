"""Viaduct client (`viaduct`): control connection, data-connection pool, local pipes.

Each data connection is one-shot: it idles until the server assigns it a
request, serves it, and is replaced. The CLI reconnects on drop with
exponential backoff (1s, 2s, 4s ... capped at 30s) and drains gracefully on
SIGTERM/SIGINT: active transfers get time to finish, nothing new is accepted.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import ipaddress
import logging
import os
import random
import signal
import ssl
import sys
import time
from collections.abc import Callable, Coroutine, Iterator
from typing import Annotated, Any

import typer
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from viaduct import auth, config, protocol, registry, update
from viaduct.relay import CHUNK, splice
from viaduct.routing import error_response

log = logging.getLogger("viaduct.client")

DEFAULT_POOL_SIZE = 40

#: Built-in server used when neither --server nor config.toml names one, so
#: ``viaduct http 4000`` works out of the box against the hosted instance.
DEFAULT_SERVER = "viaduct.sh:4443"

#: Hosts assumed to be a local dev server with no certificate, so TLS defaults
#: off for them (it stays on for every other host).
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Region nodes: code -> (display name, server host:port). --region is a friendly
#: alias over --server, so `--region nyc` == `--server nyc.viaduct.sh:4443`.
REGIONS: dict[str, tuple[str, str]] = {
    # London is the default region, so its endpoint is the apex viaduct.sh (whose
    # cert is served on the tunnel port); tunnels there are also reachable at the
    # lon.viaduct.sh alias. Other regions have their own endpoint + cert.
    "lon": ("London", "viaduct.sh:4443"),
    "nyc": ("New York", "nyc.viaduct.sh:4443"),
    "sg": ("Singapore", "sg.viaduct.sh:4443"),
    "syd": ("Sydney", "syd.viaduct.sh:4443"),
    "blr": ("Bangalore", "blr.viaduct.sh:4443"),
}
# Keep REGIONS and the shared REGION_CODES in lockstep: a new region added to one
# but not the other would let its code be claimed as a custom --name (or break
# --region). Fail loudly at import rather than drift silently.
if tuple(REGIONS) != config.REGION_CODES:
    raise RuntimeError("client.REGIONS is out of sync with config.REGION_CODES")
_REGION_HINT = ", ".join(f"{code} ({name})" for code, (name, _host) in REGIONS.items())

#: A surge (temporary) pool connection retires if it sits idle this long unused,
#: so the pool drains back to the baseline after a burst subsides.
SURGE_IDLE_TIMEOUT = 20.0

#: Backstop to TCP keepalive: a baseline (permanent) idle connection is recycled
#: after roughly this long unused. Replacing it *while the path is still live*
#: means the graceful close reaches the server, so a connection silently dropped
#: by a NAT/firewall never lingers in either pool to black-hole a request. The
#: jitter spreads recycles so the whole pool never reconnects in lockstep.
POOL_IDLE_RECYCLE = 45.0
POOL_IDLE_JITTER = 15.0


class TunnelError(Exception):
    """The server refused the tunnel."""

    #: for client_too_old: the minimum client version the server reported.
    min_client: str | None = None


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
        inspect: bool = False,
        pin_seed: str | None = None,
        host_header: str | None = None,
        auth: dict | None = None,
        name: str | None = None,
    ) -> None:
        self._server_host = server_host
        self._server_port = server_port
        self._ssl_ctx = ssl_ctx
        self._local_host = local_host
        self._local_port = local_port
        self._pool_size = pool_size
        self._inspect = inspect
        #: opaque --pin seed; when set the server derives a stable subdomain
        self._pin_seed = pin_seed
        #: --name: a specific subdomain to request; the server validates and pins it
        self._name = name
        #: when set, each request's Host header is rewritten to this before the
        #: local app sees it (fixes dev servers that reject "unknown" hosts)
        self._host_header = host_header.encode("latin-1") if host_header else None
        #: opaque auth payload (credential hashes + allowlist + messages),
        #: enforced by the server at the edge; None when no auth was requested.
        self._auth = auth
        #: per-tunnel capability token from the server's ok frame; sent on each
        #: data connection so the server binds it to this tunnel
        self._token: str | None = None
        #: adaptive pool: the baseline is pool_size idle connections; under a
        #: burst it grows toward _max_pool with short-lived surge connections
        #: that drain away on their own once demand falls.
        self._max_pool = max(pool_size, pool_size * 3)
        self._idle = 0  # connections currently waiting idle at the server
        self._live = 0  # total data-connection workers alive (idle + serving)
        self.hostname: str | None = None
        #: assigned by the server, learned from the hostname it returns
        self.subdomain: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._busy: set[asyncio.Task[None]] = set()
        self._closed = asyncio.Event()
        self._stopping = False
        #: our pings sent since the last pong; reset on any pong (acknowledged
        #: heartbeat). Grows when the server stops answering.
        self._unanswered_pings = 0

    async def start(self) -> str:
        """Open the control connection, fill the pool, return the assigned hostname."""
        reader, writer = await asyncio.open_connection(
            self._server_host, self._server_port, ssl=self._ssl_ctx
        )
        protocol.enable_keepalive(writer)
        try:
            await protocol.write_frame(
                writer,
                protocol.hello(
                    self._local_port,
                    self._pin_seed,
                    self._auth,
                    client=update.installed_version(),
                    name=self._name,
                ),
            )
            resp = await protocol.read_frame(reader)
            if resp["type"] == "error":
                reason = protocol.require_str(resp, "reason")
                exc = TunnelError(reason)
                if reason == "client_too_old" and isinstance(resp.get("min_client"), str):
                    exc.min_client = resp["min_client"]
                raise exc
            if resp["type"] != "ok":
                raise TunnelError(f"unexpected reply type {resp['type']!r}")
            if self._auth is not None and not resp.get("auth_enforced"):
                # The server took the tunnel but did not confirm it applied our
                # auth: it is too old. Refuse rather than serve unprotected.
                raise TunnelError("auth_unsupported")
            self.hostname = protocol.require_str(resp, "hostname")
            token = resp.get("token")  # absent only against a pre-token server
            self._token = token if isinstance(token, str) else None
        except BaseException:
            writer.close()  # never leak the socket if the handshake fails
            raise
        # The server picked our subdomain; it is the leading label of the host.
        self.subdomain = self.hostname.split(".", 1)[0]
        self._reader, self._writer = reader, writer
        self._spawn(self._control_loop(reader, writer))
        self._spawn(self._heartbeat(writer))
        for _ in range(self._pool_size):
            self._spawn_data_conn(permanent=True)
        return self.hostname

    async def wait_closed(self) -> None:
        """Block until the control connection drops."""
        await self._closed.wait()

    def set_inspect(self, on: bool) -> None:
        """Toggle request logging on the live tunnel; read per request, so it
        takes effect immediately for subsequent requests with no reconnect."""
        self._inspect = on

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
                elif frame["type"] == "pong":
                    self._unanswered_pings = 0  # the server is answering us
        except TimeoutError:
            log.warning("server went silent; treating connection as dead")
        except (protocol.ProtocolError, ConnectionError):
            pass
        finally:
            self._closed.set()

    async def _heartbeat(self, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            while True:
                await asyncio.sleep(protocol.HEARTBEAT_INTERVAL)
                if self._unanswered_pings >= protocol.HEARTBEAT_MAX_MISSED:
                    log.warning("server not answering heartbeats; treating connection as dead")
                    self._closed.set()
                    writer.close()
                    return
                self._unanswered_pings += 1
                await protocol.write_frame(writer, protocol.ping())

    def _spawn_data_conn(self, permanent: bool) -> None:
        """Launch one pooled data-connection worker, tracking the live count."""
        if self._stopping:
            return
        self._live += 1

        async def worker() -> None:
            try:
                await self._run_data_conn(permanent)
            finally:
                self._live -= 1

        self._spawn(worker())

    async def _run_data_conn(self, permanent: bool) -> None:
        """One pooled worker: keep an idle connection at the server until assigned.

        There are ``pool_size`` *permanent* workers; each replaces itself on
        assignment, holding the baseline idle pool steady. When the idle pool
        runs low under a burst, *temporary* surge workers are spawned (up to
        ``_max_pool`` total); they serve one request and exit, so the pool
        shrinks back to the baseline on its own once demand falls. Connect
        failures back off exponentially; a temporary worker gives up instead.

        A permanent worker also recycles its idle connection after
        ~``POOL_IDLE_RECYCLE`` seconds (a backstop to TCP keepalive): the
        still-live connection is closed and re-established, so one silently
        dropped by a NAT/firewall is replaced before a request black-holes on it.
        """
        backoff = 1.0
        while not self._stopping and not self._closed.is_set():
            try:
                reader, writer = await asyncio.open_connection(
                    self._server_host, self._server_port, ssl=self._ssl_ctx
                )
                protocol.enable_keepalive(writer)
            except OSError:
                if not permanent:
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 1.0
            try:
                await protocol.write_frame(
                    writer, protocol.data_hello(self.subdomain or "", self._token)
                )
                self._idle += 1
                try:
                    # A timeout here (surge retire, or baseline recycle) raises
                    # TimeoutError, an OSError, so it lands in the handler below
                    # as "no data" and the worker reconnects (permanent) or exits
                    # (surge). A real assignment returns bytes and breaks out.
                    idle_cap = (
                        POOL_IDLE_RECYCLE + random.uniform(0, POOL_IDLE_JITTER)
                        if permanent
                        else SURGE_IDLE_TIMEOUT
                    )
                    async with asyncio.timeout(idle_cap):
                        first = await reader.read(CHUNK)
                finally:
                    self._idle -= 1
            except (protocol.ProtocolError, OSError):
                first = b""
            if not first:  # server dropped the idle connection
                writer.close()
                if not permanent:
                    return
                await asyncio.sleep(1.0)
                continue
            # Assigned. Keep the baseline full, and add surge capacity if the
            # idle pool is running low (a burst is draining it).
            if permanent:
                self._spawn_data_conn(permanent=True)
            if self._idle <= self._pool_size // 4 and self._live < self._max_pool:
                for _ in range(min(self._pool_size, self._max_pool - self._live)):
                    self._spawn_data_conn(permanent=False)
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
            if self._inspect:
                method, path = _req_line(first)
                _log_request(method, path, "502", 0.0)
            with contextlib.suppress(OSError):
                writer.write(
                    error_response(
                        "502 Bad Gateway",
                        "Nothing is running here",
                        "The tunnel is connected, but nothing is listening on the local port "
                        "it forwards to. Start your app and refresh.",
                    )
                )
                await writer.drain()
            return
        on_b_first: Callable[[bytes], None] | None = None
        if self._inspect:
            method, path = _req_line(first)
            t0 = asyncio.get_running_loop().time()

            def on_b_first(head: bytes) -> None:
                elapsed_ms = (asyncio.get_running_loop().time() - t0) * 1000
                _log_request(method, path, _status_code(head), elapsed_ms)

        if self._host_header:
            # route the first head + everything after through the Host rewriter
            await splice(
                reader, writer, l_reader, l_writer, on_b_first=on_b_first,
                rewrite_host=self._host_header, rewrite_initial=first,
            )
            return
        try:
            l_writer.write(first)
            await l_writer.drain()
        except ConnectionError:
            l_writer.close()
            return
        await splice(reader, writer, l_reader, l_writer, on_b_first=on_b_first)


console = Console()
app = typer.Typer(help="Viaduct client: expose a local service through a viaduct server.")

_ServerOpt = Annotated[
    str | None,
    typer.Option("--server", help="Server tunnel address as host:port (default: viaduct.sh:4443)"),
]
_TlsOpt = Annotated[
    bool | None,
    typer.Option("--tls/--no-tls", help="TLS to the tunnel port (on except localhost)"),
]
_TlsCaOpt = Annotated[
    str | None,
    typer.Option("--tls-ca", help="Extra CA bundle (PEM) to trust, e.g. a self-signed server cert"),
]


def _version_cb(value: bool) -> None:
    if value:
        console.print(f"viaduct {update.installed_version()}")
        raise typer.Exit()


@app.callback()
def _cli(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_cb, is_eager=True, help="Show version and exit"
        ),
    ] = False,
) -> None:
    """Viaduct client."""


@app.command()
def upgrade() -> None:
    """Upgrade viaduct to the latest stable release (via pipx)."""
    current = update.installed_version()
    latest = update.fetch_stable_version()
    if latest is None:
        console.print(
            "[bold red]viaduct: could not reach PyPI[/] "
            "(check your connection, or upgrade by hand: "
            f"{update.UPGRADE_HINT})"
        )
        raise typer.Exit(1)
    if not update.is_newer(latest, current):
        console.print(f"[green]already up to date[/] (viaduct {current})")
        return
    console.print(f"upgrading viaduct {current} [dim]→[/] {latest}…")
    if update.run_upgrade():
        console.print("[bold green]done[/]. Restart any running tunnels to pick it up")
    else:
        console.print(
            "[bold red]viaduct: upgrade failed[/]. Is pipx installed? Then run: "
            f"{update.UPGRADE_HINT}"
        )
        raise typer.Exit(1)


@app.command()
def regions() -> None:
    """List the server regions you can pass to viaduct http --region."""
    console.print("[bold]Regions[/] (viaduct http PORT --region <code>):", highlight=False)
    for code, (name, host) in REGIONS.items():
        console.print(f"  [bold]{code:<4}[/] {name:<12} [dim]{host}[/]", highlight=False)
    console.print("  [dim]omit --region to use the default server[/]", highlight=False)


def _uptime(seconds: float) -> str:
    s = int(max(0.0, seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h}h"


@app.command("list")
def list_tunnels() -> None:
    """List the tunnels you have open on this machine."""
    tunnels = registry.active()
    if not tunnels:
        console.print("[dim]no active tunnels on this machine[/]", highlight=False)
        return
    table = Table(box=box.SIMPLE, pad_edge=False, highlight=False)
    table.add_column("NAME", style="bold")
    table.add_column("PORT", justify="right")
    table.add_column("URL", style="green")
    table.add_column("UP", justify="right")
    table.add_column("PID", justify="right", style="dim")
    now = time.time()
    for t in tunnels:
        table.add_row(
            str(t.get("subdomain", "?")),
            str(t.get("port", "?")),
            str(t.get("url", "?")),
            _uptime(now - float(t.get("started", now))),
            str(t.get("pid", "?")),
        )
    console.print(table)


@app.command()
def kill(
    targets: Annotated[
        list[str] | None,
        typer.Argument(help="Subdomain(s) or PID(s) to stop; omit when using --all"),
    ] = None,
    all_tunnels: Annotated[
        bool, typer.Option("--all", help="Stop every tunnel on this machine")
    ] = False,
) -> None:
    """Stop tunnels running on this machine (they drain, then exit)."""
    tunnels = registry.active()
    if not tunnels:
        console.print("[dim]no active tunnels on this machine[/]", highlight=False)
        return
    if all_tunnels:
        chosen = tunnels
    elif targets:
        wanted = set(targets)
        chosen = [
            t for t in tunnels
            if str(t.get("subdomain")) in wanted or str(t.get("pid")) in wanted
        ]
        known = {str(t.get("subdomain")) for t in tunnels} | {str(t.get("pid")) for t in tunnels}
        for miss in sorted(wanted - known):
            console.print(f"[yellow]viaduct: no tunnel matching {miss!r}[/]", highlight=False)
    else:
        console.print("[bold red]viaduct: name a tunnel to stop, or pass --all[/]", highlight=False)
        raise typer.Exit(2)
    stopped = 0
    for t in chosen:
        if registry.terminate(int(t["pid"])):
            console.print(
                f"[green]✓[/] stopping [bold]{t.get('subdomain')}[/] [dim](pid {t['pid']})[/]",
                highlight=False,
            )
            stopped += 1
    if stopped:
        console.print(f"[dim]{stopped} tunnel(s) draining[/]", highlight=False)


@app.command()
def http(
    port: Annotated[int, typer.Argument(help="Local port to expose")],
    server: _ServerOpt = None,
    region: Annotated[
        str | None,
        typer.Option("--region", help=f"Server region ({_REGION_HINT}); a shortcut for --server"),
    ] = None,
    pool_size: Annotated[
        int, typer.Option(help="Idle data connections to maintain")
    ] = DEFAULT_POOL_SIZE,
    inspect: Annotated[
        bool,
        typer.Option(
            "--inspect",
            help="Start with the traffic inspector on (log each request: method, "
            "path, status, time); toggle it live with 'i'",
        ),
    ] = False,
    pin: Annotated[
        bool,
        typer.Option("--pin", help="Keep the same public URL across reconnects (stable subdomain)"),
    ] = False,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            metavar="SUBDOMAIN",
            help="Request a specific subdomain, pinned if it is free (the server validates it)",
        ),
    ] = None,
    tls: _TlsOpt = None,
    tls_ca: _TlsCaOpt = None,
    host_header: Annotated[
        str | None,
        typer.Option(
            "--host-header",
            help="Rewrite the Host header sent to your app (e.g. localhost), for "
            "dev servers that reject unknown hosts",
        ),
    ] = None,
    basic_auth: Annotated[
        str | None,
        typer.Option(
            "--basic-auth",
            metavar="USER:PASS",
            help="Protect the public URL with HTTP Basic auth (user:password)",
        ),
    ] = None,
    allow_ip: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-ip",
            help="Only allow requests from these IPs/CIDRs (repeatable, or "
            "comma-separated); uses the X-Forwarded-For the Caddy front sets",
        ),
    ] = None,
    auth_message: Annotated[
        str | None,
        typer.Option(
            "--auth-message",
            help="Custom line on the 401 (password) page (e.g. how to request access)",
        ),
    ] = None,
    deny_message: Annotated[
        str | None,
        typer.Option("--deny-message", help="Custom line on the 403 (IP not allowed) page"),
    ] = None,
    bearer: Annotated[
        str | None,
        typer.Option(
            "--bearer",
            metavar="TOKEN",
            help="Require this bearer token (Authorization: Bearer ...) on the public URL",
        ),
    ] = None,
    auth_realm: Annotated[
        str | None,
        typer.Option("--auth-realm", help="Realm shown in the browser's Basic-auth prompt"),
    ] = None,
) -> None:
    """Open a tunnel exposing local PORT. The server assigns a random public URL.

    Blocks until interrupted, reconnecting on drop. Each reconnect gets a fresh
    URL, unless --pin is given, which keeps the same URL for this port.
    """
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    stalled_at = update.maybe_auto_upgrade()  # opt-in; re-execs on success
    if stalled_at:
        console.print(
            f"[yellow]viaduct: could not auto-upgrade to {stalled_at}; "
            f"continuing on {update.installed_version()}[/]"
        )
    host, srv_port, ssl_ctx = _connection_settings(server, tls, tls_ca, region)
    host_header = host_header or _cfg_str(config.load(), "host_header")
    cfg = config.load()
    basic_auth = basic_auth or os.environ.get("VIADUCT_BASIC_AUTH") or _cfg_str(cfg, "basic_auth")
    bearer = bearer or os.environ.get("VIADUCT_BEARER") or _cfg_str(cfg, "bearer")
    if basic_auth and ":" not in basic_auth:
        # a username with no ":" means "prompt me for the password" (keeps it out
        # of argv and shell history)
        basic_auth = f"{basic_auth}:{getpass.getpass(f'Password for {basic_auth}: ')}"
    # accept both --allow-ip A --allow-ip B and --allow-ip A,B (and any mix)
    allow_ips = _expand_allow_ips(allow_ip)
    for _ip in allow_ips:
        try:
            ipaddress.ip_network(_ip, strict=False)
        except ValueError:
            console.print(f"[bold red]viaduct: --allow-ip {_ip!r} is not a valid IP or CIDR[/]")
            raise typer.Exit(2) from None
    if auth_message and not basic_auth:
        console.print("[yellow]viaduct: --auth-message has no effect without --basic-auth[/]")
    if deny_message and not allow_ips:
        console.print("[yellow]viaduct: --deny-message has no effect without --allow-ip[/]")
    # One tunnel per local port on this machine: a new tunnel supersedes any
    # existing one already mapped to this port (last wins), so a port never ends
    # up served by more than one URL.
    for other in registry.active():
        if (
            other.get("port") == port
            and other.get("pid") != os.getpid()
            and registry.terminate(other["pid"])
        ):
            console.print(
                f"[dim]viaduct: superseding {other.get('subdomain', 'a tunnel')} "
                f"already on port {port}[/]",
                highlight=False,
            )
    auth_payload = auth.client_payload(
        basic_auth, bearer, allow_ips, auth_message, deny_message, auth_realm
    )
    if name and pin:
        console.print("[bold red]viaduct: use --name or --pin, not both[/]")
        raise typer.Exit(2)
    try:
        pin_seed = config.pin_seed(port) if pin else None
    except config.ConfigError as exc:
        console.print(f"[bold red]viaduct: {exc}[/]")
        raise typer.Exit(1) from exc
    try:
        asyncio.run(
            _run_http(
                host, srv_port, port, pool_size, ssl_ctx, inspect, pin_seed,
                host_header, auth_payload, name,
            )
        )
    except KeyboardInterrupt:
        console.print("[yellow]viaduct: interrupted[/]")
    except TunnelError as exc:
        if str(exc) == "client_too_old":
            need = f" (needs {exc.min_client} or newer)" if exc.min_client else ""
            console.print(
                f"[bold red]viaduct: your client is too old for this server{need}.[/] "
                "Run [bold]viaduct upgrade[/] and reconnect."
            )
            raise typer.Exit(1) from exc
        messages = {
            "ip_tunnel_limit": "too many tunnels already open from this address (server limit)",
            "pin_in_use": "that pinned URL is already in use by another live tunnel "
            "(is the same --pin tunnel already open elsewhere?)",
            "auth_unsupported": "the server did not confirm it applied your auth "
            "(--basic-auth/--bearer/--allow-ip); it is likely too old. Upgrade the "
            "server or drop the auth flags. Refusing to serve unprotected.",
            "name_invalid": "that --name is not allowed (use 3 to 63 characters of "
            "a-z, 0-9 and hyphens, no leading or trailing hyphen; some names are reserved)",
            "name_rejected": "that --name was rejected by the name filter; pick another",
            "name_taken": "that --name is already in use on this server; pick another "
            "(or try a different --region)",
        }
        msg = messages.get(str(exc), f"server refused tunnel: {exc}")
        console.print(f"[bold red]viaduct: {msg}[/]")
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
    finally:
        registry.deregister()


def _connection_settings(
    server: str | None, tls: bool | None, tls_ca: str | None, region: str | None = None
) -> tuple[str, int, ssl.SSLContext | None]:
    """Resolve server/TLS from flags, env, and config.toml (in that order)."""
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        console.print(f"[bold red]viaduct: {exc}[/]")
        raise typer.Exit(2) from exc
    if region is not None:
        if server is not None:
            raise typer.BadParameter("use either --region or --server, not both")
        entry = REGIONS.get(region.lower())
        if entry is None:
            raise typer.BadParameter(f"unknown region {region!r}; available: {_REGION_HINT}")
        server = entry[1]
    server = server or _cfg_str(cfg, "server") or DEFAULT_SERVER
    host, port = _split_hostport(server)
    # TLS: explicit flag wins, then config, else on for real hosts / off locally.
    if tls is not None:
        use_tls = tls
    elif "tls" in cfg:
        use_tls = cfg.get("tls") is True
    else:
        use_tls = host not in _LOCAL_HOSTS
    ca = tls_ca or _cfg_str(cfg, "tls_ca")
    ssl_ctx = None
    if use_tls:
        # start from the system trust store, then ADD any extra CA (a bundle
        # passed via cafile would otherwise *replace* the public CAs).
        ssl_ctx = ssl.create_default_context()
        if ca:
            try:
                ssl_ctx.load_verify_locations(cafile=ca)
            except (OSError, ssl.SSLError) as exc:
                raise typer.BadParameter(f"--tls-ca: cannot load {ca!r} ({exc})") from exc
    return host, port, ssl_ctx


def _split_hostport(server: str) -> tuple[str, int]:
    """Parse ``host:port``, supporting bracketed IPv6 (e.g. ``[::1]:4443``)."""
    s = server.strip()
    if s.startswith("["):  # [ipv6]:port
        addr, _, rest = s[1:].partition("]")
        host, port_str = addr, (rest[1:] if rest.startswith(":") else "")
    else:
        host, sep, port_str = s.rpartition(":")
        if not sep:  # no colon at all
            host, port_str = s, ""
    if not host or not port_str.isdigit():
        raise typer.BadParameter("--server must be host:port (use [ipv6]:port for IPv6)")
    port = int(port_str)
    if not 1 <= port <= 65535:
        raise typer.BadParameter("--server port must be between 1 and 65535")
    return host, port


def _cfg_str(cfg: dict[str, str | bool], key: str) -> str | None:
    value = cfg.get(key)
    return value if isinstance(value, str) else None


#: Refusal reasons the client should not retry; it reports them and exits.
FATAL_REASONS = (
    "ip_tunnel_limit",
    "pin_in_use",
    "auth_unsupported",
    "client_too_old",
    "name_invalid",
    "name_rejected",
    "name_taken",
)


def _expand_allow_ips(values: list[str] | None) -> list[str]:
    """Flatten --allow-ip so repeated flags and comma-separated values both work:
    --allow-ip A --allow-ip B, --allow-ip A,B, and any mix all give [A, B]."""
    return [part.strip() for entry in (values or []) for part in entry.split(",") if part.strip()]


def _auth_summary(payload: dict | None) -> str | None:
    """A short 'what gates this tunnel' label for the startup banner, or None if
    nothing actually gates it (a message-only payload does not count)."""
    if not payload:
        return None
    parts: list[str] = []
    if payload.get("basic"):
        parts.append("Basic auth")
    if payload.get("bearer"):
        parts.append("bearer token")
    allow = payload.get("allow_ips")
    if allow:
        parts.append(f"IP allowlist ({len(allow)} rule{'s' if len(allow) != 1 else ''})")
    return " + ".join(parts) or None


def _print_tunnel_up(
    hostname: str,
    local_port: int,
    quit_hint: bool = False,
    inspecting: bool = False,
    protected: str | None = None,
) -> None:
    console.print("[green]✓[/] tunnel live", highlight=False)
    console.print()
    console.print(f"  [bold green]https://{hostname}[/]", highlight=False)
    console.print(f"  [dim]→ http://localhost:{local_port}[/]", highlight=False)
    if protected:
        console.print(f"  [#f5b544]● protected[/][dim] · {protected}[/]", highlight=False)
    if quit_hint:
        console.print()
        if inspecting:
            console.print(
                "  [bold green]● traffic inspector ON[/][dim] · press i to turn off[/]",
                highlight=False,
            )
        else:
            console.print(
                "  [dim]○ traffic inspector off · press i to turn on[/]", highlight=False
            )
        console.print(
            "  [dim]press [/][bold]q[/][dim] to quit (this terminates the tunnel)[/]",
            highlight=False,
        )
    console.print()


def _req_line(first: bytes) -> tuple[str, str]:
    """Best-effort (method, path) from the start of an HTTP request."""
    parts = first.split(b"\n", 1)[0].rstrip(b"\r")[:2048].split(b" ")
    if len(parts) >= 2:
        return parts[0].decode("latin-1", "replace"), parts[1].decode("latin-1", "replace")
    return "?", "?"


def _status_code(head: bytes) -> str:
    """Best-effort status code from the start of an HTTP response."""
    parts = head.split(b"\n", 1)[0].rstrip(b"\r")[:2048].split(b" ")
    if len(parts) >= 2 and parts[1].isdigit():
        return parts[1].decode()
    return "?"


def _clean(s: str) -> str:
    """Drop control chars (incl. ESC) and cap length, for safe console logging.

    Request lines are attacker-controlled, so this prevents terminal-escape and
    Rich-markup injection into the operator's --inspect output.
    """
    return "".join(ch for ch in s if ch >= " " and ch != "\x7f")[:256]


def _log_request(method: str, path: str, status: str, elapsed_ms: float) -> None:
    color = {"2": "green", "3": "cyan", "4": "yellow", "5": "red"}.get(status[:1], "white")
    method = escape(_clean(method))
    path = escape(_clean(path))
    status = escape(_clean(status))
    console.print(
        f"[dim]{time.strftime('%H:%M:%S')}[/] [bold]{method:<6}[/] {path}  "
        f"[{color}]{status}[/] [dim]{elapsed_ms:.0f}ms[/]",
        highlight=False,
    )


async def _probe_local(host: str, port: int) -> bool:
    """Best-effort check that something is listening locally (non-fatal)."""
    try:
        async with asyncio.timeout(0.5):
            _, writer = await asyncio.open_connection(host, port)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


@contextlib.contextmanager
def _quit_on_q(
    loop: asyncio.AbstractEventLoop,
    stop: asyncio.Event,
    enabled: bool,
    on_toggle: Callable[[], None] | None = None,
) -> Iterator[bool]:
    """On an interactive terminal, make 'q' (or Ctrl+C/D) stop the tunnel, and
    'i' call *on_toggle* (used to flip --inspect on the live tunnel).

    Yields whether the key handler is active, and always restores the terminal
    mode on exit. On non-Unix or a non-tty it is a no-op (Ctrl+C still works via
    the signal handler).
    """
    if not (enabled and sys.stdin.isatty()):
        yield False
        return
    try:
        import termios
        import tty
    except ImportError:
        yield False
        return
    fd = sys.stdin.fileno()
    try:
        old_attrs = termios.tcgetattr(fd)
    except termios.error:
        yield False
        return

    def _on_key() -> None:
        with contextlib.suppress(OSError, ValueError):
            ch = sys.stdin.read(1)
            if ch in ("q", "Q", "\x03", "\x04"):
                stop.set()
            elif ch in ("i", "I") and on_toggle is not None:
                on_toggle()

    tty.setcbreak(fd)
    loop.add_reader(fd, _on_key)
    try:
        yield True
    finally:
        with contextlib.suppress(Exception):
            loop.remove_reader(fd)
        with contextlib.suppress(Exception):
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


async def _run_http(
    server_host: str,
    server_port: int,
    local_port: int,
    pool_size: int,
    ssl_ctx: ssl.SSLContext | None,
    inspect: bool = False,
    pin_seed: str | None = None,
    host_header: str | None = None,
    auth: dict | None = None,
    name: str | None = None,
) -> None:
    """Keep the tunnel up: reconnect on drop with 1s→30s exponential backoff."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    fancy = console.is_terminal
    if fancy:  # version banner + a prominent (once-a-day) update nudge
        current = update.installed_version()
        newer = await asyncio.to_thread(update.check_for_update)
        if newer:
            console.print(
                f"[dim]viaduct {current}[/]  [bold green]↑ {newer} available[/]"
                f"  [green]run 'viaduct upgrade'[/]",
                highlight=False,
            )
        else:
            console.print(f"[dim]viaduct {current}[/]", highlight=False)
    if not await _probe_local("127.0.0.1", local_port):
        console.print(
            f"[yellow]viaduct: nothing is listening on 127.0.0.1:{local_port} yet; "
            f"requests will return 502 until you start it[/]"
        )

    started = time.time()
    inspect_state: dict[str, Any] = {"on": inspect, "client": None}

    def toggle_inspect() -> None:
        inspect_state["on"] = not inspect_state["on"]
        if inspect_state["client"] is not None:
            inspect_state["client"].set_inspect(inspect_state["on"])
        if inspect_state["on"]:
            console.print(
                "  [bold green]● traffic inspector ON[/]  "
                "[dim]· logging each request · press i to turn off[/]",
                highlight=False,
            )
        else:
            console.print("  [dim]○ traffic inspector off[/]", highlight=False)

    with _quit_on_q(loop, stop, fancy, on_toggle=toggle_inspect) as q_active:
        delay = 1.0
        first = True
        while not stop.is_set():
            client = TunnelClient(
                server_host=server_host,
                server_port=server_port,
                local_port=local_port,
                pool_size=pool_size,
                ssl_ctx=ssl_ctx,
                inspect=inspect_state["on"],
                pin_seed=pin_seed,
                host_header=host_header,
                auth=auth,
                name=name,
            )
            inspect_state["client"] = client
            connecting = (
                console.status("[bold]establishing tunnel…", spinner="dots")
                if fancy and first
                else contextlib.nullcontext()
            )
            try:
                with connecting:
                    hostname = await client.start()
            except TunnelError as exc:
                first = False
                await client.stop()
                if str(exc) in FATAL_REASONS:
                    raise  # don't retry; let http() report it and exit
                console.print(f"[yellow]viaduct: {exc}; retrying in {delay:.0f}s[/]")
                if await _wait_or_stop(stop, delay):
                    return
                delay = min(delay * 2, 30.0)
                continue
            except (protocol.ProtocolError, OSError) as exc:
                first = False
                await client.stop()
                console.print(
                    f"[yellow]viaduct: cannot reach server ({exc}); retrying in {delay:.0f}s[/]"
                )
                if await _wait_or_stop(stop, delay):
                    return
                delay = min(delay * 2, 30.0)
                continue

            first = False
            delay = 1.0
            protected = _auth_summary(auth)
            if fancy:
                _print_tunnel_up(
                    hostname,
                    local_port,
                    q_active,
                    inspecting=inspect_state["on"],
                    protected=protected,
                )
            else:
                suffix = f" (protected: {protected})" if protected else ""
                console.print(
                    f"[bold green]tunnel up[/] {hostname} → 127.0.0.1:{local_port}{suffix}"
                )
            registry.register(
                port=local_port,
                subdomain=hostname.split(".", 1)[0],
                url=f"https://{hostname}",
                server=f"{server_host}:{server_port}",
                started=started,
            )
            closed = asyncio.ensure_future(client.wait_closed())
            stopped = asyncio.ensure_future(stop.wait())
            await asyncio.wait({closed, stopped}, return_when=asyncio.FIRST_COMPLETED)
            for pending in (closed, stopped):
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
            if stop.is_set():
                console.print("[yellow]viaduct: shutting down, draining[/]")
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
