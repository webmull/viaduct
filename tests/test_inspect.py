"""--inspect request-line and status parsing."""

from __future__ import annotations

from viaduct.client import _req_line, _status_code


def test_req_line_basic() -> None:
    assert _req_line(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n") == ("GET", "/health")


def test_req_line_post_with_query() -> None:
    assert _req_line(b"POST /hook?id=5 HTTP/1.1\r\n") == ("POST", "/hook?id=5")


def test_req_line_partial_or_garbage() -> None:
    assert _req_line(b"GET") == ("?", "?")
    assert _req_line(b"") == ("?", "?")


def test_status_code_ok() -> None:
    assert _status_code(b"HTTP/1.1 200 OK\r\n") == "200"
    assert _status_code(b"HTTP/2 404 Not Found\r\n") == "404"


def test_status_code_websocket_upgrade() -> None:
    assert _status_code(b"HTTP/1.1 101 Switching Protocols\r\n") == "101"


def test_status_code_non_http() -> None:
    assert _status_code(b"\x00\x01binary junk") == "?"
