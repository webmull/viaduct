"""Integration tests: a real TunnelServer and TunnelClient over localhost sockets.

The server assigns each tunnel a random subdomain; tests read it back from the
client. Auth runs against a real SQLite token store. Shared harness in support.py.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest

import support
from support import (
    BASE_DOMAIN,
    TOKEN,
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
from viaduct.store import hash_token


def test_http_round_trip() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, client):
            resp = await http_get(server.public_port, client.hostname, "/hi")
            assert resp.startswith(b"HTTP/1.1 200"), resp[:80]
            assert b"echo:/hi" in resp

    run(scenario())


def test_assigned_hostname_is_a_friendly_name() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (_server, client):
            assert client.subdomain and "-" in client.subdomain
            assert client.hostname == f"{client.subdomain}.{BASE_DOMAIN}"

    run(scenario())


def test_websocket_upgrade_survives() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, client):
            reader, writer = await asyncio.open_connection("127.0.0.1", server.public_port)
            writer.write(
                b"GET /ws HTTP/1.1\r\n"
                b"Host: " + client.hostname.encode() + b"\r\n"
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
        async with bare_server() as server:
            client = make_client(server, token="wrong")
            try:
                with pytest.raises(TunnelError, match="bad_token"):
                    await client.start()
            finally:
                await client.stop()

    run(scenario())


def test_two_tunnels_get_distinct_names() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, first):
            second = make_client(server, local_port=first._local_port, pool_size=1)
            try:
                await second.start()
                assert first.subdomain != second.subdomain
                assert first.subdomain in server.tunnels
                assert second.subdomain in server.tunnels
            finally:
                await second.stop()

    run(scenario())


def test_data_hello_with_bad_token_is_dropped() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, client):
            reader, writer = await asyncio.open_connection("127.0.0.1", server.tunnel_port)
            await protocol.write_frame(writer, protocol.data_hello("wrong", client.subdomain))
            assert await reader.read() == b""  # server hangs up without pooling
            writer.close()

    run(scenario())


def test_pool_replenishes_across_requests() -> None:
    async def scenario() -> None:
        async with tunnel_stack(pool_size=2) as (server, client):
            for i in range(6):
                resp = await http_get(server.public_port, client.hostname, f"/r{i}")
                assert resp.startswith(b"HTTP/1.1 200"), (i, resp[:80])

    run(scenario())


def test_tunnel_unregisters_when_client_stops() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, client):
            subdomain = client.subdomain
            assert subdomain in server.tunnels
            await client.stop()
            for _ in range(500):
                if subdomain not in server.tunnels:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("tunnel never unregistered")

    run(scenario())


def test_last_seen_recorded_after_connect() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, _client):
            tok = server.store.get_by_token(TOKEN)
            assert tok is not None and tok.last_seen is not None

    run(scenario())


def test_multiple_tokens_are_independent() -> None:
    async def scenario() -> None:
        async with bare_server() as server:
            server.store.create_token(hash_token("second-token"))
            local = await asyncio.start_server(support.local_app, "127.0.0.1", 0)
            local_port = local.sockets[0].getsockname()[1]
            a = make_client(server, local_port=local_port, pool_size=2)
            b = make_client(server, token="second-token", local_port=local_port, pool_size=2)
            try:
                await a.start()
                await b.start()
                await support.wait_for_idle(server, a.subdomain)
                await support.wait_for_idle(server, b.subdomain)
                assert b"echo:/a" in await http_get(server.public_port, a.hostname, "/a")
                assert b"echo:/b" in await http_get(server.public_port, b.hostname, "/b")
            finally:
                await a.stop()
                await b.stop()
                local.close()

    run(scenario())
