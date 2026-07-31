"""Unit tests for HTTP head reading and host-based routing."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from viaduct import routing

HEAD = b"GET /hi HTTP/1.1\r\nHost: pmesh.viaduct.sh\r\n\r\n"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_read_head_leaves_body_in_reader() -> None:
    async def scenario() -> tuple[bytes, bytes]:
        reader = asyncio.StreamReader()
        reader.feed_data(HEAD + b"BODYBYTES")
        reader.feed_eof()
        head = await routing.read_head(reader)
        return head, await reader.read()

    head, rest = run(scenario())
    assert head == HEAD
    assert rest == b"BODYBYTES"


def test_read_head_rejects_truncated_input() -> None:
    async def scenario() -> bytes:
        reader = asyncio.StreamReader()
        reader.feed_data(b"GET / HTTP/1.1\r\nHo")
        reader.feed_eof()
        return await routing.read_head(reader)

    with pytest.raises(routing.BadRequest):
        run(scenario())


def test_extract_host_basic() -> None:
    assert routing.extract_host(HEAD) == "pmesh.viaduct.sh"


def test_extract_host_is_case_insensitive() -> None:
    head = b"GET / HTTP/1.1\r\nhOsT: PMesh.Viaduct.SH\r\n\r\n"
    assert routing.extract_host(head) == "pmesh.viaduct.sh"


def test_extract_host_strips_port() -> None:
    head = b"GET / HTTP/1.1\r\nHost: pmesh.viaduct.sh:8080\r\n\r\n"
    assert routing.extract_host(head) == "pmesh.viaduct.sh"


def test_extract_host_ipv6_literal() -> None:
    head = b"GET / HTTP/1.1\r\nHost: [::1]:8080\r\n\r\n"
    assert routing.extract_host(head) == "[::1]"


def test_extract_host_missing() -> None:
    assert routing.extract_host(b"GET / HTTP/1.1\r\nAccept: */*\r\n\r\n") is None


def test_extract_host_empty_value() -> None:
    assert routing.extract_host(b"GET / HTTP/1.1\r\nHost:\r\n\r\n") is None


def test_extract_host_ignores_request_line() -> None:
    assert routing.extract_host(b"GET /host:8080 HTTP/1.1\r\n\r\n") is None


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("funny-otter.viaduct.sh", "funny-otter"),
        ("pmesh.viaduct.sh", "pmesh"),
        # only a single label routes; multi-label prefixes are rejected
        ("a.b.viaduct.sh", None),
        ("viaduct.sh", None),
        ("notviaduct.sh", None),
        ("example.com", None),
        (".viaduct.sh", None),
    ],
)
def test_subdomain_for_host(host: str, expected: str | None) -> None:
    assert routing.subdomain_for_host(host, "viaduct.sh") == expected


def test_plain_response_framing() -> None:
    data = routing.plain_response("404 Not Found", "nope\n")
    head, _, body = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 404 Not Found\r\n")
    assert f"Content-Length: {len(body)}".encode() in head
    assert body == b"nope\n"
