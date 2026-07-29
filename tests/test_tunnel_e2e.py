"""Integration tests: a real TunnelServer and TunnelClient over localhost sockets.

Auth runs against a real SQLite store holding a hashed reservation. Shared
harness (local echo app, stacks) lives in support.py.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest

import support
from support import (
    WS_GUID,
    WS_KEY,
    bare_server,
    http_get,
    make_client,
    run,
    tunnel_stack,
)
from viaduct import protocol
from viaduct.client import TunnelError


def test_http_round_trip() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, _client):
            resp = await http_get(server.public_port, "pmesh.viaduct.test", "/hi")
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
            resp = await http_get(server.public_port, "ghost.viaduct.test")
            assert resp.startswith(b"HTTP/1.1 404"), resp[:80]
            resp = await http_get(server.public_port, "example.com")
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
                resp = await http_get(server.public_port, "pmesh.viaduct.test", f"/r{i}")
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


def test_multiple_tunnels_route_independently() -> None:
    async def scenario() -> None:
        async with bare_server("alpha", "beta") as server:
            local = await asyncio.start_server(support.local_app, "127.0.0.1", 0)
            local_port = local.sockets[0].getsockname()[1]
            alpha = make_client(server, subdomain="alpha", local_port=local_port, pool_size=2)
            beta = make_client(server, subdomain="beta", local_port=local_port, pool_size=2)
            try:
                await alpha.start()
                await beta.start()
                await support.wait_for_idle(server, "alpha")
                await support.wait_for_idle(server, "beta")
                resp = await http_get(server.public_port, "alpha.viaduct.test", "/a")
                assert b"echo:/a" in resp
                resp = await http_get(server.public_port, "beta.viaduct.test", "/b")
                assert b"echo:/b" in resp
            finally:
                await alpha.stop()
                await beta.stop()
                local.close()

    run(scenario())
