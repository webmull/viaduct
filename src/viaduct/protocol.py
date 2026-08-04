"""Viaduct wire protocol: length-prefixed JSON frames, handshake only.

A frame is a 4-byte big-endian unsigned length followed by that many bytes of
UTF-8 JSON. The JSON value is always an object with a string ``"type"`` key.

Frames are exchanged only during a connection's handshake phase. After the
handshake completes, the same connection carries raw bytes with no framing —
the protocol deliberately gets out of the way of the data path. ``read_frame``
consumes exactly one frame from the stream, so any bytes that follow it remain
buffered in the reader for the raw pipe to pick up.

Control connection::

    -> {"type": "hello", "local_port": ...[, "pin": ...]}
    <- {"type": "ok", "hostname": ..., "token": ...}  |  {"type": "error", "reason": ...}
    <-> {"type": "ping"} / {"type": "pong"}   every HEARTBEAT_INTERVAL seconds

The server assigns a random subdomain and returns it in ``ok.hostname``; the
client reads the leading label back off that hostname to tag its data
connections.

Data connection::

    -> {"type": "data_hello", "subdomain": ..., "token": ...}
    (server holds it idle; on assignment, raw bytes follow immediately)

Read timeouts are the caller's concern (wrap calls in ``asyncio.timeout``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import struct
from typing import Any, Final

Frame = dict[str, Any]

#: Handshake frames are tiny (a subdomain, a port); anything larger is a
#: protocol violation, not a big message.
MAX_FRAME: Final = 16 * 1024

#: Ping cadence on the control connection, both directions (seconds).
HEARTBEAT_INTERVAL: Final = 20.0

#: Hard ceiling on control-connection silence before the read times out. In
#: practice the acknowledged-heartbeat check (below) fires first at ~60s, so this
#: mainly bounds a half-open link where the read would otherwise block forever.
#: A cleanly closed connection is still detected immediately.
DEAD_PEER_TIMEOUT: Final = 300.0  # 5 minutes

#: Acknowledged heartbeats: each side counts its own pings that have not been
#: ponged. This many unanswered in a row means the peer is gone. It catches a
#: half-open link (where frames still arrive one way, so DEAD_PEER_TIMEOUT never
#: fires) and detects death in ~HEARTBEAT_MAX_MISSED x HEARTBEAT_INTERVAL
#: (~60s) instead of 5 minutes. Trade-off: a peer that goes fully silent (a
#: laptop asleep with the lid shut) is also dropped after ~60s and reconnects on
#: wake; --pin keeps the same URL across that reconnect.
HEARTBEAT_MAX_MISSED: Final = 2

#: Deadline for a fresh connection to send its first (hello / data_hello) frame.
#: Bounds slowloris: an idle connection that never speaks is closed, not parked.
HANDSHAKE_TIMEOUT: Final = 10.0

#: TCP keepalive for pooled data connections. An idle data connection carries no
#: application traffic, so a stateful middlebox (NAT, conntrack, cloud firewall)
#: can silently evict its mapping after a few minutes; the socket is then
#: half-open and a request spliced into it black-holes. Probes keep the mapping
#: warm and turn a genuinely dead peer into a detectable reset. Idle is set below
#: common NAT idle timeouts (~1-5 min).
KEEPALIVE_IDLE: Final = 20  # seconds idle before the first probe (below common NAT idle)
KEEPALIVE_INTVL: Final = 10  # seconds between probes
KEEPALIVE_COUNT: Final = 3  # unanswered probes before the socket is reset

_HEADER = struct.Struct(">I")


def enable_keepalive(writer: asyncio.StreamWriter) -> None:
    """Turn on TCP keepalive with a sub-NAT-timeout idle on a stream's socket.

    Best-effort: a socket-less transport is a no-op, and per-connection options
    the platform does not expose are skipped. The idle/interval/count knobs are
    named differently per OS (Linux: ``TCP_KEEPIDLE``; macOS: ``TCP_KEEPALIVE``),
    so we set whichever exist. ``SO_KEEPALIVE`` alone still helps where they do
    not, using the system defaults.
    """
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    idle_opt = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    for opt, value in (
        (idle_opt, KEEPALIVE_IDLE),
        (getattr(socket, "TCP_KEEPINTVL", None), KEEPALIVE_INTVL),
        (getattr(socket, "TCP_KEEPCNT", None), KEEPALIVE_COUNT),
    ):
        if opt is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)


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


def hello(local_port: int, pin: str | None = None, auth: dict | None = None) -> Frame:
    frame: Frame = {"type": "hello", "local_port": local_port}
    if pin:
        frame["pin"] = pin  # opaque --pin seed; the server derives a stable name
    if auth:
        frame["auth"] = auth  # opaque auth payload: credential hashes + allowlist + messages
    return frame


def data_hello(subdomain: str, token: str | None = None) -> Frame:
    frame: Frame = {"type": "data_hello", "subdomain": subdomain}
    if token:
        frame["token"] = token  # per-tunnel capability; binds this conn to its owner
    return frame


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
