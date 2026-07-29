"""Integration tests for custom domains: wire ops, routing precedence, tls-check."""

from __future__ import annotations

import pytest

from support import TOKEN, bare_server, http_get, run, tunnel_stack
from viaduct import protocol
from viaduct.client import TunnelError, domain_request
from viaduct.server import TunnelServer
from viaduct.store import hash_token


async def _op(server: TunnelServer, frame: protocol.Frame) -> protocol.Frame:
    return await domain_request("127.0.0.1", server.tunnel_port, None, frame)


def test_domain_add_routes_and_survives_remove() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, _client):
            reply = await _op(server, protocol.domain_add(TOKEN, "pmesh", "Demo.Example.COM."))
            assert reply["hostname"] == "demo.example.com"  # normalized
            assert reply["target"] == "pmesh.viaduct.test"

            resp = await http_get(server.public_port, "demo.example.com", "/via-domain")
            assert resp.startswith(b"HTTP/1.1 200"), resp[:80]
            assert b"echo:/via-domain" in resp

            await _op(server, protocol.domain_remove(TOKEN, "demo.example.com"))
            resp = await http_get(server.public_port, "demo.example.com", "/via-domain")
            assert resp.startswith(b"HTTP/1.1 404"), resp[:80]

    run(scenario())


def test_domain_list_scoped_to_token() -> None:
    async def scenario() -> None:
        async with bare_server("pmesh", "other") as server:
            await _op(server, protocol.domain_add(TOKEN, "pmesh", "a.example.com"))
            await _op(server, protocol.domain_add(TOKEN, "pmesh", "b.example.com"))
            reply = await _op(server, protocol.domain_list(TOKEN))
            hostnames = sorted(d["hostname"] for d in reply["domains"])
            assert hostnames == ["a.example.com", "b.example.com"]

    run(scenario())


def test_domain_op_rejections() -> None:
    async def scenario() -> None:
        async with bare_server("pmesh") as server:
            with pytest.raises(TunnelError, match="bad_token"):
                await _op(server, protocol.domain_add("wrong", "pmesh", "a.example.com"))
            with pytest.raises(TunnelError, match="unknown_subdomain"):
                await _op(server, protocol.domain_add(TOKEN, "ghost", "a.example.com"))
            with pytest.raises(TunnelError, match="invalid_hostname"):
                await _op(server, protocol.domain_add(TOKEN, "pmesh", "not_a_host"))
            with pytest.raises(TunnelError, match="hostname_under_base_domain"):
                await _op(server, protocol.domain_add(TOKEN, "pmesh", "evil.viaduct.test"))
            await _op(server, protocol.domain_add(TOKEN, "pmesh", "a.example.com"))
            with pytest.raises(TunnelError, match="domain_taken"):
                await _op(server, protocol.domain_add(TOKEN, "pmesh", "a.example.com"))
            with pytest.raises(TunnelError, match="unknown_domain"):
                await _op(server, protocol.domain_remove(TOKEN, "never.example.com"))
            with pytest.raises(TunnelError, match="bad_token"):
                await _op(server, protocol.domain_list("wrong"))

    run(scenario())


def test_domain_remove_requires_owning_token() -> None:
    async def scenario() -> None:
        async with bare_server("pmesh") as server:
            other_token = "other-token"
            server.store.create_reservation("other", hash_token(other_token))
            await _op(server, protocol.domain_add(TOKEN, "pmesh", "a.example.com"))
            with pytest.raises(TunnelError, match="unknown_domain"):
                await _op(server, protocol.domain_remove(other_token, "a.example.com"))

    run(scenario())


def test_tls_check_endpoint() -> None:
    async def scenario() -> None:
        async with bare_server("pmesh") as server:
            path = "/_viaduct/tls-check?domain=demo.example.com"
            resp = await http_get(server.public_port, "localhost", path)
            assert resp.startswith(b"HTTP/1.1 404"), resp[:80]

            await _op(server, protocol.domain_add(TOKEN, "pmesh", "demo.example.com"))
            resp = await http_get(server.public_port, "localhost", path)
            assert resp.startswith(b"HTTP/1.1 200"), resp[:80]

            resp = await http_get(server.public_port, "localhost", "/_viaduct/tls-check")
            assert resp.startswith(b"HTTP/1.1 404"), resp[:80]

    run(scenario())


def test_tls_check_does_not_shadow_tunnel_paths() -> None:
    async def scenario() -> None:
        async with tunnel_stack() as (server, _client):
            resp = await http_get(
                server.public_port, "pmesh.viaduct.test", "/_viaduct/tls-check?domain=x"
            )
            assert resp.startswith(b"HTTP/1.1 200"), resp[:80]
            assert b"echo:/_viaduct/tls-check" in resp  # reached the local app, not the gate

    run(scenario())
