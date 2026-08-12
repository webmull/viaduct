"""HTTP head reading and Host-based routing helpers.

The server reads only the request line and headers ("the head") to route,
then replays those bytes down the tunnel followed by the rest of the stream.
Bodies are never parsed or buffered; WebSocket upgrades pass through untouched.
"""

from __future__ import annotations

import asyncio
import ipaddress

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
            # rstrip('.') normalizes a fully-qualified Host (e.g. "x.example.com.")
            host = _strip_port(value.strip().decode("latin-1").lower()).rstrip(".")
            return host or None
    return None


def header_value(head: bytes, name: bytes) -> bytes | None:
    """Return a request header's value (case-insensitive), or None."""
    want = name.lower()
    for line in head.split(b"\r\n")[1:]:
        n, sep, v = line.partition(b":")
        if sep and n.strip().lower() == want:
            return v.strip()
    return None


def forwarded_for_ip(head: bytes) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Visitor IP from the last X-Forwarded-For hop (the one the trusted front set)."""
    xff = header_value(head, b"x-forwarded-for")
    if not xff:
        return None
    parts = [p.strip() for p in xff.split(b",") if p.strip()]
    if not parts:
        return None
    try:
        return ipaddress.ip_address(parts[-1].decode("ascii", "ignore"))
    except ValueError:
        return None


def request_target(head: bytes) -> str:
    """The request target (path plus query) from the first request line, or ''."""
    first = head.split(b"\r\n", 1)[0]
    parts = first.split(b" ")
    return parts[1].decode("latin-1", "replace") if len(parts) >= 2 else ""


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


#: Self-contained error page. No external fonts/CSS so it renders instantly and
#: offline. Kept visually in sync with the reference at site/errors.html (see the
#: "error brand" rule in CLAUDE.md). status/title/detail are always internal
#: constants; never interpolate request data here (HTTP response-splitting).
_ERROR_CSS = (
    "*{box-sizing:border-box}html,body{margin:0;height:100%}"
    "body{background:radial-gradient(60% 50% at 50% 0%,rgba(245,181,68,.16),transparent 70%),"
    "#fbfbfd;color:#1f2328;font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"
    "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;display:grid;place-items:center;"
    "text-align:center;padding:3rem 1.5rem;line-height:1.6}main{max-width:28rem}"
    ".m{font-weight:600;letter-spacing:-.01em;font-size:.95rem;color:#57606a}"
    ".m b{color:#d97706;font-weight:600}.code{font-size:3.5rem;font-weight:800;line-height:1;"
    "color:#d97706;margin-top:1.75rem;font-variant-numeric:tabular-nums}"
    "h1{font-size:1.35rem;font-weight:650;margin:.6rem 0 0}p{color:#57606a;margin:.6rem auto 0;"
    "max-width:24rem;font-size:.95rem}.a{margin-top:1.6rem;display:flex;gap:.75rem;"
    "justify-content:center;align-items:center;flex-wrap:wrap}"
    ".lnk{display:inline-block;color:#57606a;text-decoration:none;font-size:.85rem;"
    "border:1px solid rgba(0,0,0,.16);padding:.5rem 1rem;border-radius:.55rem}"
    ".lnk:hover{color:#1f2328;border-color:#d97706}.ct{color:#57606a;font-size:.85rem}"
    ".ct b{color:#1f2328;font-weight:600}"
)


def error_response(
    status: str,
    title: str,
    detail: str,
    retry_after: int | None = None,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    """A branded HTML error page (see site/errors.html). Args are trusted constants.

    When *retry_after* is set (503), the page auto-refreshes and sends a
    ``Retry-After`` header, with a visible countdown and a "Retry now" link.
    """
    code = status.split(" ", 1)[0]
    if retry_after:
        meta = f'<meta http-equiv="refresh" content="{retry_after}">'
        actions = (
            '<div class="a"><a class="lnk" href="#" onclick="location.reload();return false">'
            "Retry now</a>"
            f'<span class="ct">retrying in <b id="c">{retry_after}</b>s</span></div>'
            f'<script>var n={retry_after},e=document.getElementById("c");'
            "setInterval(function(){n=n>0?n-1:0;e.textContent=n},1000)</script>"
        )
    else:
        meta = ""
        actions = (
            '<div class="a"><a class="lnk" href="https://viaduct.sh">What is viaduct?</a></div>'
        )
    html = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
        f"<title>{title} · viaduct.sh</title><style>{_ERROR_CSS}</style></head>"
        f'<body><main><div class="m">viaduct<b>.sh</b></div>'
        f'<div class="code">{code}</div><h1>{title}</h1><p>{detail}</p>'
        f"{actions}</main></body></html>"
    )
    payload = html.encode("utf-8")
    lines = [
        f"HTTP/1.1 {status}",
        "Content-Type: text/html; charset=utf-8",
        f"Content-Length: {len(payload)}",
    ]
    if retry_after:
        lines.append(f"Retry-After: {retry_after}")
    for _name, _value in (extra_headers or {}).items():
        lines.append(f"{_name}: {_value}")
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload
