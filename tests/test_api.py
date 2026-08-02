"""Embeddable API: viaduct.tunnel() opens a live tunnel that serves requests."""

from __future__ import annotations

import asyncio

import pytest

import support
import viaduct
from support import BASE_DOMAIN, bare_server, http_get, run


def test_tunnel_context_serves() -> None:
    async def scenario() -> None:
        async with bare_server() as server:
            local = await asyncio.start_server(support.local_app, "127.0.0.1", 0)
            port = local.sockets[0].getsockname()[1]
            try:
                async with viaduct.tunnel(
                    port, server=f"127.0.0.1:{server.tunnel_port}", tls=False
                ) as t:
                    assert t.url == f"https://{t.hostname}"
                    assert t.hostname.endswith("." + BASE_DOMAIN)
                    assert t.subdomain and "." not in t.subdomain
                    await support.wait_for_idle(server, t.subdomain)
                    resp = await http_get(server.public_port, t.hostname, "/hi")
                    assert resp.startswith(b"HTTP/1.1 200"), resp[:80]
                    assert b"echo:/hi" in resp
            finally:
                local.close()

    run(scenario())


def test_unknown_region_raises() -> None:
    async def scenario() -> None:
        with pytest.raises(ValueError, match="unknown region"):
            async with viaduct.tunnel(1, region="mars"):
                pass

    run(scenario())


def test_server_and_region_conflict() -> None:
    async def scenario() -> None:
        with pytest.raises(ValueError, match="either server or region"):
            async with viaduct.tunnel(1, server="x:4443", region="lon"):
                pass

    run(scenario())
