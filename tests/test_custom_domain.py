"""Custom domains via CNAME: DNS parsing, routing, and the on-demand-TLS ask."""

from __future__ import annotations

import asyncio
import struct

import support
from support import BASE_DOMAIN, bare_server, http_get, make_client, run
from viaduct import dns, routing


def test_request_target() -> None:
    head = b"GET /_viaduct/tls-check?domain=x HTTP/1.1\r\nHost: y\r\n\r\n"
    assert routing.request_target(head) == "/_viaduct/tls-check?domain=x"
    assert routing.request_target(b"nonsense") == ""


def test_cname_parser_reads_answer() -> None:
    # Craft a DNS response: 1 question, 1 CNAME answer, verify the parser.
    header = struct.pack(">HHHHHH", dns._QUERY_ID, 0x8180, 1, 1, 0, 0)
    question = dns._encode_qname("demo.example.com") + struct.pack(">HH", 5, 1)
    rdata = dns._encode_qname("funny-otter.viaduct.sh")
    answer = b"\xc0\x0c" + struct.pack(">HHIH", 5, 1, 60, len(rdata)) + rdata  # name = ptr to qname
    assert dns._cname_targets(header + question + answer) == ["funny-otter.viaduct.sh"]
    # a mismatched transaction id is ignored
    assert dns._cname_targets(struct.pack(">HHHHHH", 0x1234, 0x8180, 0, 0, 0, 0)) == []


async def _ask(port: int, domain: str) -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET /_viaduct/tls-check?domain={domain} HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()
    data = await reader.read(-1)
    writer.close()
    return int(data.split(b" ")[1])


def test_custom_host_routes_via_cname(monkeypatch) -> None:
    async def scenario() -> None:
        async with bare_server() as server:
            local = await asyncio.start_server(support.local_app, "127.0.0.1", 0)
            port = local.sockets[0].getsockname()[1]
            client = make_client(server, local_port=port, pool_size=2)
            try:
                await client.start()
                await support.wait_for_idle(server, client.subdomain)
                target = f"{client.subdomain}.{BASE_DOMAIN}"

                async def fake(name: str) -> str | None:
                    return target if name == "demo.example.test" else None

                monkeypatch.setattr(dns, "resolve_cname", fake)
                resp = await http_get(server.public_port, "demo.example.test", "/hi")
                assert resp.startswith(b"HTTP/1.1 200"), resp[:80]
                assert b"echo:/hi" in resp
                # a custom domain that CNAMEs nowhere useful still 404s
                resp = await http_get(server.public_port, "random.example.test")
                assert resp.startswith(b"HTTP/1.1 404"), resp[:80]
            finally:
                await client.stop()
                local.close()

    run(scenario())


def test_tls_check_gates_on_live_tunnel(monkeypatch) -> None:
    async def scenario() -> None:
        async with bare_server() as server:
            client = make_client(server, pool_size=0)
            await client.start()
            sub = client.subdomain
            try:

                async def fake(name: str) -> str | None:
                    return {
                        "demo.example.test": f"{sub}.{BASE_DOMAIN}",
                        "ghost.example.test": "no-such-tunnel.viaduct.test",
                    }.get(name)

                monkeypatch.setattr(dns, "resolve_cname", fake)
                # live wildcard, CNAME->live, CNAME->dead tunnel, and no CNAME
                assert await _ask(server.public_port, f"{sub}.{BASE_DOMAIN}") == 200
                assert await _ask(server.public_port, "demo.example.test") == 200
                assert await _ask(server.public_port, "ghost.example.test") == 404
                assert await _ask(server.public_port, "random.example.test") == 404
            finally:
                await client.stop()

    run(scenario())
