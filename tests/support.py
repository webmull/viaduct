"""Shared integration-test helpers: local echo app, server/client stacks.

The local app answers plain HTTP with an echo of the request path, and answers
WebSocket upgrades with a real 101 handshake followed by a raw byte echo.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ssl
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from viaduct.client import TunnelClient
from viaduct.server import TunnelServer
from viaduct.store import Store, hash_token

TOKEN = "test-token"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_KEY = "dGhlIHNhbXBsZSBub25jZQ=="  # RFC 6455 example key


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=15))


async def local_app(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, ConnectionError):
        writer.close()
        return
    if b"upgrade: websocket" in head.lower():
        key = next(
            line.split(b":", 1)[1].strip()
            for line in head.split(b"\r\n")
            if line.lower().startswith(b"sec-websocket-key")
        )
        accept = base64.b64encode(hashlib.sha1(key + WS_GUID.encode()).digest()).decode()
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
        )
        with contextlib.suppress(ConnectionError):
            await writer.drain()
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        writer.close()
    else:
        path = head.split(b" ")[1].decode()
        body = f"echo:{path}".encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body
        )
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()


@contextlib.asynccontextmanager
async def bare_server(
    *reserved: str, tls: ssl.SSLContext | None = None, **server_kwargs: Any
) -> AsyncIterator[TunnelServer]:
    """A running TunnelServer whose store has a reservation (token=TOKEN) per subdomain."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "viaduct.db")
        for subdomain in reserved:
            store.create_reservation(subdomain, hash_token(TOKEN))
        server = TunnelServer(
            store=store,
            bind="127.0.0.1",
            public_port=0,
            tunnel_port=0,
            base_domain="viaduct.test",
            tls=tls,
            **server_kwargs,
        )
        await server.start()
        try:
            yield server
        finally:
            await server.stop()
            store.close()


def make_client(
    server: TunnelServer,
    *,
    token: str = TOKEN,
    subdomain: str = "pmesh",
    local_port: int = 1,
    pool_size: int = 1,
    ssl_ctx: ssl.SSLContext | None = None,
) -> TunnelClient:
    return TunnelClient(
        server_host="127.0.0.1",
        server_port=server.tunnel_port,
        token=token,
        subdomain=subdomain,
        local_port=local_port,
        pool_size=pool_size,
        ssl_ctx=ssl_ctx,
    )


@contextlib.asynccontextmanager
async def tunnel_stack(
    subdomain: str = "pmesh",
    pool_size: int = 3,
    server_tls: ssl.SSLContext | None = None,
    client_ssl: ssl.SSLContext | None = None,
    **server_kwargs: Any,
) -> AsyncIterator[tuple[TunnelServer, TunnelClient]]:
    async with bare_server(subdomain, tls=server_tls, **server_kwargs) as server:
        local = await asyncio.start_server(local_app, "127.0.0.1", 0)
        local_port = local.sockets[0].getsockname()[1]
        client = make_client(
            server,
            subdomain=subdomain,
            local_port=local_port,
            pool_size=pool_size,
            ssl_ctx=client_ssl,
        )
        try:
            assert await client.start() == f"{subdomain}.viaduct.test"
            await wait_for_idle(server, subdomain)
            yield server, client
        finally:
            await client.stop()
            local.close()


async def wait_for_idle(server: TunnelServer, subdomain: str, n: int = 1) -> None:
    for _ in range(500):
        tunnel = server.tunnels.get(subdomain)
        if tunnel is not None and tunnel.pool.qsize() >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("data connection pool never filled")


async def http_get(port: int, host: str, path: str = "/") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    data = await reader.read(-1)
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return data
