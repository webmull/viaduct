"""Viaduct wire protocol: length-prefixed JSON frames, handshake only.

A frame is a 4-byte big-endian unsigned length followed by that many bytes of
UTF-8 JSON. The JSON value is always an object with a string ``"type"`` key.

Frames are exchanged only during a connection's handshake phase. After the
handshake completes, the same connection carries raw bytes with no framing —
the protocol deliberately gets out of the way of the data path. ``read_frame``
consumes exactly one frame from the stream, so any bytes that follow it remain
buffered in the reader for the raw pipe to pick up.

Control connection::

    -> {"type": "hello", "local_port": ...}
    <- {"type": "ok", "hostname": ...}  |  {"type": "error", "reason": ...}
    <-> {"type": "ping"} / {"type": "pong"}   every HEARTBEAT_INTERVAL seconds

The server assigns a random subdomain and returns it in ``ok.hostname``; the
client reads the leading label back off that hostname to tag its data
connections.

Data connection::

    -> {"type": "data_hello", "subdomain": ...}
    (server holds it idle; on assignment, raw bytes follow immediately)

Read timeouts are the caller's concern (wrap calls in ``asyncio.timeout``).
"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any, Final

Frame = dict[str, Any]

#: Handshake frames are tiny (a subdomain, a port); anything larger is a
#: protocol violation, not a big message.
MAX_FRAME: Final = 16 * 1024

#: Ping cadence on the control connection, both directions (seconds).
HEARTBEAT_INTERVAL: Final = 20.0

#: Consider the peer dead after this much silence on the control connection.
#: Both sides ping every HEARTBEAT_INTERVAL, so this is many heartbeats of grace
#: — long enough to ride out a phone sleeping or a brief network drop without
#: tearing the tunnel down (which would reassign a new subdomain on reconnect).
#: A cleanly closed connection is still detected immediately; this only bounds
#: how long a silently-vanished peer lingers.
DEAD_PEER_TIMEOUT: Final = 300.0  # 5 minutes

_HEADER = struct.Struct(">I")


class ProtocolError(Exception):
    """The bytes on the wire (or a frame about to be sent) violate the protocol."""


class ConnectionClosed(ProtocolError):
    """The peer closed the connection while a frame was expected."""


def encode_frame(msg: Frame) -> bytes:
    payload = json.dumps(msg, separators=(",", ":")).encode()
    if len(payload) > MAX_FRAME:
        raise ProtocolError(f"frame too large: {len(payload)} bytes")
    return _HEADER.pack(len(payload)) + payload


async def write_frame(writer: asyncio.StreamWriter, msg: Frame) -> None:
    writer.write(encode_frame(msg))
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> Frame:
    """Read exactly one frame, leaving any following bytes in the reader.

    Raises ConnectionClosed if the peer hangs up (cleanly or mid-frame) and
    ProtocolError for anything that is not a well-formed frame.
    """
    try:
        header = await reader.readexactly(_HEADER.size)
        (length,) = _HEADER.unpack(header)
        if length > MAX_FRAME:
            raise ProtocolError(f"frame too large: {length} bytes")
        payload = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        detail = "mid-frame" if exc.partial else "at frame boundary"
        raise ConnectionClosed(f"connection closed {detail}") from exc
    except ConnectionError as exc:
        raise ConnectionClosed(str(exc)) from exc
    try:
        msg = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("frame payload is not valid JSON") from exc
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("frame is not an object with a string 'type'")
    return msg


def hello(local_port: int) -> Frame:
    return {"type": "hello", "local_port": local_port}


def data_hello(subdomain: str) -> Frame:
    return {"type": "data_hello", "subdomain": subdomain}


def ok(**fields: Any) -> Frame:
    return {"type": "ok", **fields}


def error(reason: str) -> Frame:
    return {"type": "error", "reason": reason}


def ping() -> Frame:
    return {"type": "ping"}


def pong() -> Frame:
    return {"type": "pong"}


def require_str(frame: Frame, key: str) -> str:
    value = frame.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"missing or invalid {key!r}")
    return value


def require_int(frame: Frame, key: str) -> int:
    value = frame.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"missing or invalid {key!r}")
    return value
