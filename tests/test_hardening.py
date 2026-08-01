"""M5 hardening: dead-peer detection, connection caps, pool-starved 503,
idle timeout, graceful drain."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from support import WS_KEY, bare_server, http_get, make_client, run, tunnel_stack
from viaduct import protocol
from viaduct.client import TunnelClient, TunnelError


def test_server_unregisters_silent_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "DEAD_PEER_TIMEOUT", 0.3)

    async def scenario() -> None:
        async with bare_server() as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.tunnel_port)
            await protocol.write_frame(writer, protocol.hello(3000))
            ok = await protocol.read_frame(reader)
            subdomain = ok["hostname"].split(".", 1)[0]
            assert subdomain in server.tunnels
            for _ in range(500):  # no pings from us → server must give up
                if subdomain not in server.tunnels:
                    break
                await asyncio.sleep(0.01)
            assert subdomain not in server.tunnels
            writer.close()

    run(scenario())


def test_client_detects_silent_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "DEAD_PEER_TIMEOUT", 0.3)

    async def scenario() -> None:
        async def silent_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await protocol.read_frame(reader)
            await protocol.write_frame(writer, protocol.ok(hostname="quiet-mole.viaduct.test"))
            with contextlib.suppress(Exception):
                await reader.read()  # hold open, never ping

        srv = await asyncio.start_server(silent_server, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        client = TunnelClient(
            server_host="127.0.0.1",
            server_port=port,
            local_port=1,
            pool_size=0,
        )
        try:
            await client.start()
            await asyncio.wait_for(client.wait_closed(), timeout=5)
        finally:
            await client.stop()
            srv.close()

    run(scenario())


def test_client_detects_unanswered_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    # A half-open link: the server keeps sending pings (so the client's read
    # never times out) but never answers the client's pings. The acknowledged
    # heartbeat must catch it well under the long read backstop.
    monkeypatch.setattr(protocol, "HEARTBEAT_INTERVAL", 0.1)
    monkeypatch.setattr(protocol, "DEAD_PEER_TIMEOUT", 100.0)

    async def scenario() -> None:
        async def half_open_server(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await protocol.read_frame(reader)  # hello
            await protocol.write_frame(writer, protocol.ok(hostname="quiet-mole.viaduct.test"))

            async def swallow() -> None:
                with contextlib.suppress(Exception):
                    while await reader.read(4096):
                        pass  # read (and drop) the client's pings, never pong

            sink = asyncio.ensure_future(swallow())
            try:
                with contextlib.suppress(Exception):
                    while True:  # keep the client's read side fresh
                        await protocol.write_frame(writer, protocol.ping())
                        await asyncio.sleep(0.1)
            finally:
                sink.cancel()

        srv = await asyncio.start_server(half_open_server, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        client = TunnelClient(server_host="127.0.0.1", server_port=port, local_port=1, pool_size=0)
        try:
            await client.start()
            await asyncio.wait_for(client.wait_closed(), timeout=3)  # not the 100s backstop
        finally:
            await client.stop()
            srv.close()

    run(scenario())


def test_per_tunnel_connection_cap() -> None:
    async def scenario() -> None:
        async with bare_server(max_conns_per_tunnel=2) as server:
            control = make_client(server, pool_size=0)
            await control.start()
            subdomain = control.subdomain
            conns = []
            try:
                for _ in range(2):
                    reader, writer = await asyncio.open_connection("127.0.0.1", server.tunnel_port)
                    await protocol.write_frame(
                        writer, protocol.data_hello(subdomain, control._token)
                    )
                    conns.append((reader, writer))
                for _ in range(500):
                    if server.tunnels[subdomain].pool.qsize() >= 2:
                        break
                    await asyncio.sleep(0.01)
                assert server.tunnels[subdomain].data_conns == 2

                reader, writer = await asyncio.open_connection("127.0.0.1", server.tunnel_port)
                await protocol.write_frame(writer, protocol.data_hello(subdomain, control._token))
                assert await reader.read() == b""  # over cap: dropped
                assert server.tunnels[subdomain].data_conns == 2
                writer.close()
            finally:
                for _, w in conns:
                    w.close()
                await control.stop()

    run(scenario())


def test_data_hello_wrong_token_is_rejected() -> None:
    async def scenario() -> None:
        async with bare_server() as server:
            control = make_client(server, pool_size=0)
            await control.start()
            subdomain = control.subdomain
            # correct (public) subdomain but a bogus token: the hijack guard drops it
            reader, writer = await asyncio.open_connection("127.0.0.1", server.tunnel_port)
            await protocol.write_frame(writer, protocol.data_hello(subdomain, "deadbeefdeadbeef"))
            assert await reader.read() == b""
            assert server.tunnels[subdomain].data_conns == 0
            writer.close()
            # and a missing token is likewise dropped
            reader, writer = await asyncio.open_connection("127.0.0.1", server.tunnel_port)
            await protocol.write_frame(writer, protocol.data_hello(subdomain))
            assert await reader.read() == b""
            assert server.tunnels[subdomain].data_conns == 0
            writer.close()
            # the real token (handed to the owning client) does attach
            reader, writer = await asyncio.open_connection("127.0.0.1", server.tunnel_port)
            await protocol.write_frame(writer, protocol.data_hello(subdomain, control._token))
            for _ in range(300):
                if server.tunnels[subdomain].data_conns == 1:
                    break
                await asyncio.sleep(0.01)
            assert server.tunnels[subdomain].data_conns == 1
            writer.close()
            await control.stop()

    run(scenario())


def test_503_when_pool_starved() -> None:
    async def scenario() -> None:
        async with bare_server(pool_wait=0.2) as server:
            control = make_client(server, pool_size=0)
            await control.start()
            try:
                resp = await http_get(server.public_port, control.hostname)
                assert resp.startswith(b"HTTP/1.1 503"), resp[:80]
            finally:
                await control.stop()

    run(scenario())


def test_idle_timeout_closes_quiet_connection() -> None:
    async def scenario() -> None:
        async with tunnel_stack(idle_timeout=0.3) as (server, client):
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
            assert b"101" in head
            writer.write(b"x")
            await writer.drain()
            assert await reader.readexactly(1) == b"x"
            # then silence — the shared watchdog must kill the splice
            assert await asyncio.wait_for(reader.read(), timeout=5) == b""
            writer.close()

    run(scenario())


def test_ip_tunnel_limit() -> None:
    async def scenario() -> None:
        async with bare_server(max_tunnels_per_ip=3) as server:
            clients = []
            try:
                for _ in range(3):  # all from 127.0.0.1
                    c = make_client(server, pool_size=0)
                    await c.start()
                    clients.append(c)
                # one more from the same IP must be refused
                extra = make_client(server, pool_size=0)
                try:
                    with pytest.raises(TunnelError, match="ip_tunnel_limit"):
                        await extra.start()
                finally:
                    await extra.stop()
                # freeing one slot lets a new tunnel in again
                await clients.pop().stop()
                for _ in range(300):
                    if len(server.tunnels) < 3:
                        break
                    await asyncio.sleep(0.01)
                again = make_client(server, pool_size=0)
                await again.start()
                clients.append(again)
            finally:
                for c in clients:
                    await c.stop()

    run(scenario())


def _ws_upgrade(host: str) -> bytes:
    return (
        b"GET /ws HTTP/1.1\r\nHost: " + host.encode() + b"\r\n"
        b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        b"Sec-WebSocket-Key: " + WS_KEY.encode() + b"\r\n"
        b"Sec-WebSocket-Version: 13\r\n\r\n"
    )


def test_adaptive_pool_grows_then_shrinks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("viaduct.client.SURGE_IDLE_TIMEOUT", 0.3)

    async def scenario() -> None:
        async with tunnel_stack(pool_size=4) as (server, client):
            conns = []
            try:
                # hold several concurrent WebSockets open to drain the idle pool
                for _ in range(8):
                    reader, writer = await asyncio.open_connection("127.0.0.1", server.public_port)
                    writer.write(_ws_upgrade(client.hostname))
                    await writer.drain()
                    await reader.readuntil(b"\r\n\r\n")
                    conns.append((reader, writer))
                # the pool must grow past its baseline to serve the surge
                for _ in range(500):
                    if client._live > client._pool_size:
                        break
                    await asyncio.sleep(0.01)
                peak = client._live
                assert peak > client._pool_size, f"pool did not grow: live={peak}"
            finally:
                for _, w in conns:
                    w.close()
            # once the surge is gone, it drains back toward the baseline
            for _ in range(500):
                if client._live <= client._pool_size + 2:
                    break
                await asyncio.sleep(0.02)
            assert client._live <= client._pool_size + 2, (
                f"pool did not shrink: live={client._live} peak={peak}"
            )

    run(scenario())


def test_drain_finishes_active_transfers_then_stops() -> None:
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
            await reader.readuntil(b"\r\n\r\n")

            drain = asyncio.ensure_future(server.drain(grace=10))
            await asyncio.sleep(0.1)
            # listeners are closed: new connections are refused...
            with pytest.raises(OSError):
                _, w2 = await asyncio.open_connection("127.0.0.1", server.public_port)
                w2.close()
            # ...but the in-flight WebSocket still works
            writer.write(b"still-alive")
            await writer.drain()
            assert await reader.readexactly(len(b"still-alive")) == b"still-alive"

            writer.close()  # transfer ends → drain completes
            await asyncio.wait_for(drain, timeout=5)

    run(scenario())
