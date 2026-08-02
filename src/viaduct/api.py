"""Embeddable API: open a viaduct tunnel from your own code.

    import viaduct

    async with viaduct.tunnel(8080) as t:      # async
        print(t.url)

    with viaduct.tunnel_sync(8080) as t:        # sync (scripts, notebooks, pytest)
        print(t.url)

The tunnel stays up for the lifetime of the ``with`` block and is torn down on
exit. Keyword options mirror the CLI: ``server``, ``region``, ``tls``, ``pin``,
``host_header``, ``pool_size``.
"""

from __future__ import annotations

import asyncio
import ssl
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from viaduct import config
from viaduct.client import (
    _LOCAL_HOSTS,
    DEFAULT_POOL_SIZE,
    DEFAULT_SERVER,
    REGIONS,
    TunnelClient,
    _split_hostport,
)


class Tunnel:
    """A live tunnel. ``url`` is the public HTTPS address."""

    def __init__(self, client: TunnelClient, hostname: str) -> None:
        self._client = client
        self.hostname = hostname
        self.subdomain = client.subdomain
        self.url = f"https://{hostname}"

    async def aclose(self) -> None:
        await self._client.stop()

    def __repr__(self) -> str:
        return f"<viaduct.Tunnel {self.url}>"


def _resolve(
    server: str | None, region: str | None, tls: bool | None
) -> tuple[str, int, ssl.SSLContext | None]:
    if region is not None:
        if server is not None:
            raise ValueError("pass either server or region, not both")
        entry = REGIONS.get(region.lower())
        if entry is None:
            raise ValueError(f"unknown region {region!r}; available: {', '.join(REGIONS)}")
        server = entry[1]
    host, port = _split_hostport(server or DEFAULT_SERVER)
    use_tls = (host not in _LOCAL_HOSTS) if tls is None else tls
    return host, port, (ssl.create_default_context() if use_tls else None)


@asynccontextmanager
async def tunnel(
    port: int,
    *,
    server: str | None = None,
    region: str | None = None,
    tls: bool | None = None,
    pin: bool = False,
    host_header: str | None = None,
    pool_size: int = DEFAULT_POOL_SIZE,
) -> AsyncIterator[Tunnel]:
    """Open a tunnel to local *port* and yield a live :class:`Tunnel`."""
    host, srv_port, ssl_ctx = _resolve(server, region, tls)
    pin_seed = config.pin_seed(port) if pin else None
    client = TunnelClient(
        server_host=host,
        server_port=srv_port,
        local_port=port,
        pool_size=pool_size,
        ssl_ctx=ssl_ctx,
        pin_seed=pin_seed,
        host_header=host_header,
    )
    hostname = await client.start()
    try:
        yield Tunnel(client, hostname)
    finally:
        await client.stop()


class _SyncTunnel:
    """Runs :func:`tunnel` on a background event loop so sync code can use it."""

    def __init__(self, port: int, **kwargs: object) -> None:
        self._port = port
        self._kwargs = kwargs
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self.url: str | None = None
        self.hostname: str | None = None
        self.subdomain: str | None = None

    def __enter__(self) -> _SyncTunnel:
        ready = threading.Event()
        error: list[BaseException] = []

        def _run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)

            async def _main() -> None:
                self._stop = asyncio.Event()
                async with tunnel(self._port, **self._kwargs) as t:  # type: ignore[arg-type]
                    self.url, self.hostname, self.subdomain = t.url, t.hostname, t.subdomain
                    ready.set()
                    await self._stop.wait()

            try:
                loop.run_until_complete(_main())
            except BaseException as exc:  # surface startup failures to __enter__
                error.append(exc)
                ready.set()
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run, name="viaduct-tunnel", daemon=True)
        self._thread.start()
        if not ready.wait(timeout=35):
            raise TimeoutError("viaduct: tunnel did not come up within 35s")
        if error:
            raise error[0]
        return self

    def __exit__(self, *exc: object) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=10)


def tunnel_sync(port: int, **kwargs: object) -> _SyncTunnel:
    """Synchronous :func:`tunnel` (a context manager) for scripts, notebooks,
    and test suites that are not async."""
    return _SyncTunnel(port, **kwargs)
