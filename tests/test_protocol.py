"""Unit tests for the wire protocol framing."""

from __future__ import annotations

import asyncio
import struct
from typing import Any

import pytest

from viaduct import protocol


def run(coro: Any) -> Any:
    return asyncio.run(coro)


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
    frame = protocol.hello("tok-123", 3000)
    assert run(read_from(protocol.encode_frame(frame))) == frame


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
    data = protocol.encode_frame(protocol.hello("tok", 3000))
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
    frame = protocol.data_hello("tok", "pmesh")
    assert protocol.require_str(frame, "subdomain") == "pmesh"
    for bad in (
        {"type": "data_hello"},
        {"type": "data_hello", "subdomain": ""},
        {"type": "data_hello", "subdomain": 7},
    ):
        with pytest.raises(protocol.ProtocolError):
            protocol.require_str(bad, "subdomain")


def test_require_int() -> None:
    frame = protocol.hello("tok", 3000)
    assert protocol.require_int(frame, "local_port") == 3000
    for bad in (
        {"type": "hello"},
        {"type": "hello", "local_port": "3000"},
        {"type": "hello", "local_port": True},
    ):
        with pytest.raises(protocol.ProtocolError):
            protocol.require_int(bad, "local_port")


def test_redacted_masks_token_and_preserves_original() -> None:
    frame = protocol.hello("secret", 3000)
    safe = protocol.redacted(frame)
    assert safe["token"] == "[redacted]"
    assert frame["token"] == "secret"
    assert protocol.redacted(protocol.ping()) == protocol.ping()
