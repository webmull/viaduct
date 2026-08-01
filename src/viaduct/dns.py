"""Minimal DNS CNAME resolution (stdlib only), for custom-domain routing.

Python's ``socket`` layer cannot reliably return a host's CNAME target
(``AI_CANONNAME`` follows a chain on some resolvers and not others), so this
sends a small CNAME query to the system resolver over UDP and parses the answer.
It is just enough DNS to follow a custom domain back to its ``*.BASE_DOMAIN``
tunnel alias, nothing more: no EDNS, no DNSSEC, no TCP fallback (CNAME answers
are tiny). A stingy or unreachable resolver simply yields ``None``.
"""

from __future__ import annotations

import asyncio
import socket
import struct

_TYPE_CNAME = 5
_CLASS_IN = 1
_QUERY_ID = 0x7A11  # fixed: we send one query per socket and match on the answer
_TIMEOUT = 2.0  # seconds per resolver before giving up


def _resolvers() -> list[str]:
    servers: list[str] = []
    try:
        with open("/etc/resolv.conf") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    servers.append(parts[1])
    except OSError:
        pass
    return servers or ["1.1.1.1", "8.8.8.8"]


def _encode_qname(name: str) -> bytes:
    out = bytearray()
    for label in name.rstrip(".").split("."):
        encoded = label.encode("ascii", "ignore")[:63]
        out += bytes([len(encoded)]) + encoded
    out.append(0)
    return bytes(out)


def _build_query(name: str) -> bytes:
    header = struct.pack(">HHHHHH", _QUERY_ID, 0x0100, 1, 0, 0, 0)  # RD=1, 1 question
    question = _encode_qname(name) + struct.pack(">HH", _TYPE_CNAME, _CLASS_IN)
    return header + question


def _decode_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a (possibly compressed) DNS name; return (name, offset_after)."""
    labels: list[str] = []
    end = offset
    jumped = False
    for _ in range(128):  # bound the loop against a malicious pointer cycle
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                end = offset
            break
        if length & 0xC0 == 0xC0:  # compression pointer
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                end = offset + 2
            offset = pointer
            jumped = True
            continue
        offset += 1
        labels.append(data[offset : offset + length].decode("ascii", "replace"))
        offset += length
    return ".".join(labels), end


def _cname_targets(resp: bytes) -> list[str]:
    if len(resp) < 12 or struct.unpack(">H", resp[:2])[0] != _QUERY_ID:
        return []
    _, _, qd, an, _, _ = struct.unpack(">HHHHHH", resp[:12])
    offset = 12
    for _ in range(qd):
        _, offset = _decode_name(resp, offset)
        offset += 4  # qtype + qclass
    targets: list[str] = []
    for _ in range(an):
        _, offset = _decode_name(resp, offset)
        rtype, _rclass, _ttl, rdlength = struct.unpack(">HHIH", resp[offset : offset + 10])
        offset += 10
        if rtype == _TYPE_CNAME:
            target, _ = _decode_name(resp, offset)
            targets.append(target.rstrip(".").lower())
        offset += rdlength
    return targets


def _query_blocking(name: str, timeout: float) -> str | None:
    query = _build_query(name)
    for server in _resolvers()[:2]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                sock.sendto(query, (server, 53))
                resp, _ = sock.recvfrom(4096)
        except OSError:
            continue
        targets = _cname_targets(resp)
        if targets:
            return targets[-1]  # end of any chain returned in one answer
    return None


async def resolve_cname(name: str) -> str | None:
    """Return *name*'s CNAME target (lowercased, no trailing dot), or None.

    Runs the blocking UDP query in a thread so it never stalls the event loop.
    """
    return await asyncio.to_thread(_query_blocking, name, _TIMEOUT)
