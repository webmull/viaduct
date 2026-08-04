"""Unit tests for the wire protocol framing."""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from typing import Any

import pytest

from viaduct import protocol, server


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class _SockWriter:
    """Minimal StreamWriter stand-in exposing a real socket via get_extra_info."""

    def __init__(self, sock: socket.socket | None) -> None:
        self._sock = sock

    def get_extra_info(self, name: str) -> Any:
        return self._sock if name == "socket" else None


def test_enable_keepalive_sets_socket_options() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(srv.getsockname())
    accepted, _ = srv.accept()
    try:
        protocol.enable_keepalive(_SockWriter(client))
        # enabled reads back non-zero (1 on Linux, the option bit 8 on macOS)
        assert client.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        # whichever name this platform uses for the idle time must be set
        idle_opt = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
        if idle_opt is not None:
            assert client.getsockopt(socket.IPPROTO_TCP, idle_opt) == protocol.KEEPALIVE_IDLE
    finally:
        client.close()
        accepted.close()
        srv.close()


def test_enable_keepalive_no_socket_is_noop() -> None:
    # a transport with no underlying socket (e.g. some SSL/pipe transports) must
    # not raise; the pool still works, just without the tuning
    protocol.enable_keepalive(_SockWriter(None))


async def read_from(data: bytes) -> protocol.Frame:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return await protocol.read_frame(reader)


class FakeWriter:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf += data

    async def drain(self) -> None:
        pass


def test_round_trip() -> None:
    frame = protocol.hello(3000)
    assert run(read_from(protocol.encode_frame(frame))) == frame


def test_hello_omits_pin_by_default() -> None:
    assert "pin" not in protocol.hello(3000)


def test_hello_includes_pin_when_given() -> None:
    assert protocol.hello(3000, "abc123")["pin"] == "abc123"


def test_length_prefix_is_big_endian() -> None:
    data = protocol.encode_frame(protocol.ping())
    assert data[:4] == struct.pack(">I", len(data) - 4)


def test_two_frames_read_sequentially() -> None:
    async def read_two() -> tuple[protocol.Frame, protocol.Frame]:
        reader = asyncio.StreamReader()
        reader.feed_data(protocol.encode_frame(protocol.ping()))
        reader.feed_data(protocol.encode_frame(protocol.pong()))
        reader.feed_eof()
        return await protocol.read_frame(reader), await protocol.read_frame(reader)

    assert run(read_two()) == (protocol.ping(), protocol.pong())


def test_trailing_raw_bytes_stay_in_reader() -> None:
    """After the handshake frame, raw bytes must survive for the data path."""
    raw = b"GET / HTTP/1.1\r\nHost: pmesh.viaduct.sh\r\n\r\n"

    async def read_then_rest() -> tuple[protocol.Frame, bytes]:
        reader = asyncio.StreamReader()
        reader.feed_data(protocol.encode_frame(protocol.ok(hostname="pmesh.viaduct.sh")) + raw)
        reader.feed_eof()
        frame = await protocol.read_frame(reader)
        return frame, await reader.read()

    frame, rest = run(read_then_rest())
    assert frame == protocol.ok(hostname="pmesh.viaduct.sh")
    assert rest == raw


def test_write_frame_writes_encoded_bytes() -> None:
    writer = FakeWriter()
    run(protocol.write_frame(writer, protocol.ping()))  # type: ignore[arg-type]
    assert bytes(writer.buf) == protocol.encode_frame(protocol.ping())


def test_encode_rejects_oversized_payload() -> None:
    frame = {"type": "hello", "pad": "x" * protocol.MAX_FRAME}
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_frame(frame)


def test_read_rejects_oversized_length() -> None:
    data = struct.pack(">I", protocol.MAX_FRAME + 1) + b"x"
    with pytest.raises(protocol.ProtocolError) as excinfo:
        run(read_from(data))
    assert not isinstance(excinfo.value, protocol.ConnectionClosed)


def test_clean_eof_raises_connection_closed() -> None:
    with pytest.raises(protocol.ConnectionClosed):
        run(read_from(b""))


def test_truncated_frame_raises_connection_closed() -> None:
    data = protocol.encode_frame(protocol.hello(3000))
    with pytest.raises(protocol.ConnectionClosed):
        run(read_from(data[: len(data) // 2]))


def test_invalid_json_raises_protocol_error() -> None:
    payload = b"{not json"
    with pytest.raises(protocol.ProtocolError):
        run(read_from(struct.pack(">I", len(payload)) + payload))


def test_non_object_payload_rejected() -> None:
    payload = b'["hello"]'
    with pytest.raises(protocol.ProtocolError):
        run(read_from(struct.pack(">I", len(payload)) + payload))


def test_missing_type_rejected() -> None:
    payload = b'{"token": "t"}'
    with pytest.raises(protocol.ProtocolError):
        run(read_from(struct.pack(">I", len(payload)) + payload))


def test_require_str() -> None:
    frame = protocol.data_hello("pmesh")
    assert protocol.require_str(frame, "subdomain") == "pmesh"
    for bad in (
        {"type": "data_hello"},
        {"type": "data_hello", "subdomain": ""},
        {"type": "data_hello", "subdomain": 7},
    ):
        with pytest.raises(protocol.ProtocolError):
            protocol.require_str(bad, "subdomain")


def test_require_int() -> None:
    frame = protocol.hello(3000)
    assert protocol.require_int(frame, "local_port") == 3000
    for bad in (
        {"type": "hello"},
        {"type": "hello", "local_port": "3000"},
        {"type": "hello", "local_port": True},
    ):
        with pytest.raises(protocol.ProtocolError):
            protocol.require_int(bad, "local_port")


def test_hello_carries_client_version() -> None:
    assert protocol.hello(3000, client="1.4.0")["client"] == "1.4.0"
    assert "client" not in protocol.hello(3000)  # omitted when not given


def test_error_carries_extra_fields() -> None:
    assert protocol.error("client_too_old", min_client="1.5.0") == {
        "type": "error",
        "reason": "client_too_old",
        "min_client": "1.5.0",
    }
    assert protocol.error("nope") == {"type": "error", "reason": "nope"}


class _CaptureWriter:
    """StreamWriter stand-in that records written bytes; enough for _reject."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, b: bytes) -> None:
        self.buf.extend(b)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    def get_extra_info(self, key: str) -> Any:
        return ("1.2.3.4", 0) if key == "peername" else None


def _first_frame(buf: bytes) -> dict:
    (length,) = struct.unpack(">I", buf[:4])
    return json.loads(bytes(buf[4 : 4 + length]))


def test_server_rejects_client_below_min() -> None:
    srv = server.TunnelServer(base_domain="localhost")
    w = _CaptureWriter()
    run(srv._handle_control(protocol.hello(3000, client="0.9.0"), None, w))
    msg = _first_frame(w.buf)
    assert msg["type"] == "error"
    assert msg["reason"] == "client_too_old"
    assert msg["min_client"] == server.MIN_CLIENT_VERSION


def test_min_client_gate() -> None:
    # a client at (or above) the floor passes; anything below is gated
    from viaduct import update

    assert not update.is_newer(server.MIN_CLIENT_VERSION, server.MIN_CLIENT_VERSION)
    assert update.is_newer(server.MIN_CLIENT_VERSION, "1.4.0")  # below the floor -> gated
    assert update.is_newer(server.MIN_CLIENT_VERSION, "0.9.0")
