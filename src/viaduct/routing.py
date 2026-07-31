"""HTTP head reading and Host-based routing helpers.

The server reads only the request line and headers ("the head") to route,
then replays those bytes down the tunnel followed by the rest of the stream.
Bodies are never parsed or buffered; WebSocket upgrades pass through untouched.
"""

from __future__ import annotations

import asyncio

#: Cap on request-line + headers. Enforced by passing this as the StreamReader
#: ``limit`` on the public listener — ``readuntil`` fails beyond it.
MAX_HEAD = 64 * 1024

_HEAD_END = b"\r\n\r\n"


class BadRequest(Exception):
    """The bytes on the public socket do not begin with a usable HTTP head."""


async def read_head(reader: asyncio.StreamReader) -> bytes:
    """Read through the end of the headers, leaving any body bytes in the reader."""
    try:
        return await reader.readuntil(_HEAD_END)
    except asyncio.IncompleteReadError as exc:
        raise BadRequest("connection closed before end of headers") from exc
    except asyncio.LimitOverrunError as exc:
        raise BadRequest("headers exceed size limit") from exc


def extract_host(head: bytes) -> str | None:
    """Return the lowercased Host header value without any port, or None."""
    for line in head.split(b"\r\n")[1:]:
        name, sep, value = line.partition(b":")
        if sep and name.strip().lower() == b"host":
            host = _strip_port(value.strip().decode("latin-1").lower())
            return host or None
    return None


def _strip_port(host: str) -> str:
    if host.startswith("["):  # IPv6 literal, e.g. [::1]:8080
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    base, _, port = host.rpartition(":")
    if base and port.isdigit():
        return base
    return host


def subdomain_for_host(host: str, base_domain: str) -> str | None:
    """Return the leading label of *host* under *base_domain*, or None.

    Only a single label is accepted (``pmesh.viaduct.sh`` -> ``pmesh``); a
    multi-label prefix returns None since generated names are single labels.
    """
    suffix = "." + base_domain
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    if not label or "." in label:
        return None
    return label


def plain_response(status: str, body: str) -> bytes:
    """A complete, correctly-framed HTTP/1.1 text response ending the connection."""
    payload = body.encode()
    head = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    return head.encode("ascii") + payload
