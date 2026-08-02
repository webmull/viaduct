"""--host-header: the client-side Host rewriter in relay.rewrite_request_host."""

from __future__ import annotations

import asyncio

from viaduct import relay


class _FakeWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, b: bytes) -> None:
        self.data.extend(b)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


def _reader(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


def _run(stream: bytes, host: bytes = b"localhost", initial: bytes = b"") -> bytes:
    async def go() -> bytes:
        w = _FakeWriter()
        await relay.rewrite_request_host(_reader(stream), w, host, None, initial)
        assert w.closed
        return bytes(w.data)

    return asyncio.run(go())


def test_rewrites_single_request() -> None:
    req = b"GET / HTTP/1.1\r\nHost: funny-otter.viaduct.sh\r\nAccept: */*\r\n\r\n"
    out = _run(req)
    assert b"Host: localhost\r\n" in out
    assert b"funny-otter" not in out
    assert b"Accept: */*" in out  # other headers untouched


def test_rewrites_both_keepalive_requests() -> None:
    body = b"hello"
    req = (
        b"POST /a HTTP/1.1\r\nHost: pub.viaduct.sh\r\nContent-Length: 5\r\n\r\n" + body
        + b"GET /b HTTP/1.1\r\nHost: pub.viaduct.sh\r\n\r\n"
    )
    out = _run(req)
    assert out.count(b"Host: localhost\r\n") == 2  # both requests rewritten
    assert b"pub.viaduct.sh" not in out
    assert body in out  # request body preserved verbatim


def test_first_head_from_initial_is_rewritten() -> None:
    # the server hands the first request head over as `initial`, not on the reader
    initial = b"GET / HTTP/1.1\r\nHost: pub.viaduct.sh\r\n\r\n"
    out = _run(b"", initial=initial)
    assert b"Host: localhost\r\n" in out and b"pub.viaduct.sh" not in out


def test_websocket_upgrade_then_raw() -> None:
    handshake = (
        b"GET /ws HTTP/1.1\r\nHost: pub.viaduct.sh\r\n"
        b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
    )
    frames = b"\x81\x05hello"  # opaque ws bytes must pass through untouched
    out = _run(handshake + frames)
    assert b"Host: localhost\r\n" in out  # handshake host rewritten
    assert out.endswith(frames)


def test_injects_host_when_absent() -> None:
    out = _run(b"GET / HTTP/1.1\r\nAccept: */*\r\n\r\n")
    assert b"Host: localhost\r\n" in out
