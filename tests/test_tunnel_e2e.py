"""Integration tests: a real TunnelServer and TunnelClient over localhost sockets.

The local app is a stdlib asyncio server that answers plain HTTP with an echo
of the request path, and answers WebSocket upgrades with a real 101 handshake
followed by a raw byte echo — proving the tunnel passes upgrades untouched.
Auth runs against a real SQLite store holding a hashed reservation.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from viaduct import protocol
from viaduct.client import TunnelClient, TunnelError
from viaduct.server import TunnelServer
from viaduct.store import Store, hash_token

TOKEN = "test-token"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_KEY = "dGhlIHNhbXBsZSBub25jZQ=="  # RFC 6455 example key


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=15))


async def _local_app(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
async def bare_server(*reserved: str) -> AsyncIterator[TunnelServer]:
    """A running TunnelServer whose store has a reservation (token=TOKEN) per subdomain."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "viaduct.db")
        for subdomain in reserved:
            store.create_reservation(subdomain, hash_token(TOKEN))
        server = TunnelServer(
            store=store, bind="127.0.0.1", public_port=0, tunnel_port=0, base_domain="viaduct.test"
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
) -> TunnelClient:
    return TunnelClient(
        server_host="127.0.0.1",
        server_port=server.tunnel_port,
        token=token,
        subdomain=subdomain,
        local_port=local_port,
        pool_size=pool_size,
    )


@contextlib.asynccontextmanager
async def tunnel_stack(
    subdomain: str = "pmesh", pool_size: int = 3
) -> AsyncIterator[tuple[TunnelServer, TunnelClient]]:
    async with bare_server(subdomain) as server:
        local = await asyncio.start_server(_local_app, "127.0.0.1", 0)
        local_port = local.sockets[0].getsockname()[1]
        client = make_client(
            server, subdomain=subdomain, local_port=local_port, pool_size=pool_size
        )
        try:
            assert await client.start() == f"{subdomain}.viaduct.test"
            await _wait_for_idle(server, subdomain)
            yield server, client
        finally:
            await client.stop()
            local.close()


async def _wait_for_idle(server: TunnelServer, subdomain: str, n: int = 1) -> None:
    for _ in range(500):
        tunnel = server.tunnels.get(subdomain)
        if tunnel is not None and tunnel.pool.qsize() >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("data connection pool never filled")


async def _http_get(port: int, host: str, path: str = "/") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    data = await reader.read(-1)
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return data


def test_http_round_trip() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, _client):
            resp = await _http_get(server.public_port, "pmesh.viaduct.test", "/hi")
            assert resp.startswith(b"HTTP/1.1 200"), resp[:80]
            assert b"echo:/hi" in resp

    run(scenario())


def test_websocket_upgrade_survives() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, _client):
            reader, writer = await asyncio.open_connection("127.0.0.1", server.public_port)
            writer.write(
                b"GET /ws HTTP/1.1\r\n"
                b"Host: pmesh.viaduct.test\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Key: " + WS_KEY.encode() + b"\r\n"
                b"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            assert b"101 Switching Protocols" in head
            expected = base64.b64encode(hashlib.sha1((WS_KEY + WS_GUID).encode()).digest())
            assert expected in head

            payload = b"payload-after-upgrade"
            writer.write(payload)
            await writer.drain()
            assert await reader.readexactly(len(payload)) == payload
            writer.close()

    run(scenario())


def test_unknown_host_gets_404() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, _client):
            resp = await _http_get(server.public_port, "ghost.viaduct.test")
            assert resp.startswith(b"HTTP/1.1 404"), resp[:80]
            resp = await _http_get(server.public_port, "example.com")
            assert resp.startswith(b"HTTP/1.1 404"), resp[:80]

    run(scenario())


def test_wrong_token_rejected() -> None:
    async def scenario() -> None:
        async with bare_server("pmesh") as server:
            client = make_client(server, token="wrong")
            try:
                with pytest.raises(TunnelError, match="bad_token"):
                    await client.start()
            finally:
                await client.stop()

    run(scenario())


def test_unreserved_subdomain_rejected() -> None:
    async def scenario() -> None:
        async with bare_server("pmesh") as server:
            client = make_client(server, subdomain="ghost")
            try:
                with pytest.raises(TunnelError, match="unknown_subdomain"):
                    await client.start()
            finally:
                await client.stop()

    run(scenario())


def test_duplicate_subdomain_rejected() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, _client):
            other = make_client(server)
            try:
                with pytest.raises(TunnelError, match="subdomain_taken"):
                    await other.start()
            finally:
                await other.stop()

    run(scenario())


def test_data_hello_with_bad_token_is_dropped() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, _client):
            reader, writer = await asyncio.open_connection("127.0.0.1", server.tunnel_port)
            await protocol.write_frame(writer, protocol.data_hello("wrong", "pmesh"))
            assert await reader.read() == b""  # server hangs up without pooling
            writer.close()

    run(scenario())


def test_pool_replenishes_across_requests() -> None:
    async def scenario() -> None:
        async with tunnel_stack(pool_size=2) as (server, _client):
            for i in range(6):
                resp = await _http_get(server.public_port, "pmesh.viaduct.test", f"/r{i}")
                assert resp.startswith(b"HTTP/1.1 200"), (i, resp[:80])

    run(scenario())


def test_tunnel_unregisters_when_client_stops() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, client):
            assert "pmesh" in server.tunnels
            await client.stop()
            for _ in range(500):
                if "pmesh" not in server.tunnels:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("tunnel never unregistered")

    run(scenario())


def test_last_seen_recorded_after_connect() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, _client):
            res = server.store.get("pmesh")
            assert res is not None and res.last_seen is not None

    run(scenario())
