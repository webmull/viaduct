"""HTTP head reading and Host-based routing helpers.

The server reads only the request line and headers ("the head") to route,
then replays those bytes down the tunnel followed by the rest of the stream.
Bodies are never parsed or buffered; WebSocket upgrades pass through untouched.
"""

from __future__ import annotations

import asyncio
import base64
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
_MARK_PATH = (
    "m511 191.9-3.3 1.5-5.2 2.4-5.8 2.8-6.4 3-10.3 4.8-9.2 4.3-5.6 2.6-6.9 3.3-4.5 2-10.6 5-12 5.7-6.4 3-26.1 12.3-5.2 2.4-5.2 2.4-3 1.4-5.8 2.7-4.8 2.2-1.9.8-2.6 1.2-5.4 2.5-11.5 5.4-5.2 2.4-7.5 3.5-4.8 2.2L324 279l-5.5 2.6a416.5 416.5 0 0 1-20.2 9.5l-4.1 1.8-11.4 5.3c-1.8 1-4.1 2-5.1 2.5l-14.2 6.5-10.3 4.9-9.7 4.5-4.3 2-8.5 3.8-3.8 1.8-1.7 1-.3 2.2c-.4 3.7-.1 489 .3 489.5q.4.8 10-5.1l5-2.9 7.9-4.6 9.7-5.8a83 83 0 0 0 7.5-4.5 1532 1532 0 0 0 31-18.2l11.6-6.9 14.6-8.7 16-9.4c.5-.4 5.5-3.5 11-6.7l37.5-22.7a171 171 0 0 1 10.7-6.4c.4-.4.5-17.4.5-109 0-111.6.1-117.9 1.3-119.8q.3-.5.5-2.2a82 82 0 0 1 5.4-19.3c2-5.5 2.2-6 6-13l2-3.8c4-7.7 14.7-20.6 23.1-27.9 11.6-10 27.3-18.5 41.8-22.8l3.4-1 4.1-1 4.7-1 4.5-.7c14.3-1.6 29.2-1.2 40.2 1.2l4.3.9 2.2.5 2.9.8c2.4.6 7.9 2.5 10.2 3.5l1.9.8 2.5 1A106 106 0 0 1 582 416a94 94 0 0 1 9.6 8.3c1.4 1.2 10 11 11.9 13.5a119 119 0 0 1 14 24.9c1.5 2.7 3.6 9.3 5.1 15.9 3.4 14.6 3.1 3.3 3.3 127.2 0 59.6.2 108.8.3 109.1q0 .8 3.6 2.6a87 87 0 0 1 8.5 5.2l5.5 3.2 2 1.2 1.9 1.2 3.8 2.3 4.1 2.6 1.4.8 7.1 4.1a84 84 0 0 1 8.1 5q.3 0 1.7 1l1.6 1 2 1.3 2.6 1.5 2.8 1.6 4.8 3c.6.3 1.4.7 1.6 1l6.2 3.6 6.2 3.7a1740 1740 0 0 0 46.1 27l16.7 10.3 2.8 1.6a101 101 0 0 1 10.9 6.2l1.5.9a392 392 0 0 0 19.2 10.8c.3-.2.4-463.5.2-481.6l-.2-10.7-5.4-2.6a102 102 0 0 0-9.8-4.5l-11.2-5.3-2.8-1.2-7.4-3.5-22.5-10.4-3.6-1.7-5-2.3-10.7-5c-2.4-1-3.2-1.4-7.5-3.5l-5.7-2.6-4.3-2-4.2-2-13.5-6.2-9.1-4.2a207 207 0 0 0-14-6.5l-6.4-3-3.6-1.7-3.2-1.4q-3.9-1.7-10-4.7l-5.2-2.4-8.6-4-3.4-1.5-10.3-4.8-11-5.2-5.8-2.8-5-2.3-3-1.3-10.5-5-11.4-5.3-4.9-2.3-5.5-2.5-4.9-2.3-4.5-2.1-5.3-2.5-6.4-3-8-3.8c-11.9-5.8-13.1-6.3-14.3-5.9m-38 370.4c0 .3 1.6.7 2.8.7s.2-.6-1.4-.8q-1.3-.1-1.4.1m7.1 2.5q1 .7 1.7 1c.4 0 1.5.5 2.5.9l2.9 1a173 173 0 0 1 16.3 8.3c1.3 0 9 5.9 13.9 10.7a36 36 0 0 1 11.2 18c.8 2.2.8 10.6 0 13.8a67 67 0 0 1-16.6 28.8 219 219 0 0 1-29.4 27.9l-5.4 4.6-2 1.7-4.1 3.4-2.5 2a145 145 0 0 0-8.9 7L456 697l-5.2 4.2-5.2 4.1-9.8 7.8a53 53 0 0 1-5.5 4.4l-1.8 1.4a381 381 0 0 0-21 17.1c-.9.6-3.2 2.6-5.2 4.3L392 749l-3.7 3.2-4.1 3.6-6.9 6a250 250 0 0 0-13.5 12.3 502 502 0 0 0-50.8 55.2l-.8 1.1c-3.5 3.7-4.8 6.3-3.6 7 1 .5 192.8.4 193.5-.1q.5-.6 1.2-2.6l3.7-12.4 2.5-7.3a91 91 0 0 1 5.5-14.1l2.2-4.9a383 383 0 0 1 18.8-35.3l3.9-6.4 3.1-5.4c0-.2.8-1.5 6.6-10.8a49 49 0 0 0 3.2-5.6l3.2-5.2 5.2-8.8 4-7 2-3.7a262 262 0 0 0 14.2-30.3 190 190 0 0 0 5-17l1.3-7.8c.5-3.6.2-14.9-.5-18-1.5-7-2.1-8.8-4.5-13.7a71 71 0 0 0-26.6-28.8l-2.6-1.7c-3.5-2.3-9-5.3-13.5-7.3l-4.7-2.1a37 37 0 0 0-8.1-3.4c-.5-.3-4.7-1.8-11.4-4l-9.1-3-3.8-1.2-2-.5-1.1-.3q-.4-.4-2-.6l-3.5-.9c-4.7-1.4-11-2.8-13.3-3l-1.8-.2z"  # noqa: E501
)
#: Favicon (the mark), theme-adaptive so it shows on light and dark browser chrome.
_FAVICON = "data:image/svg+xml;base64," + base64.b64encode(
    (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="97.9 100.4 828.2 828.2">'
        "<style>path{fill:#14171f}@media(prefers-color-scheme:dark){path{fill:#f2f4f8}}</style>"
        f'<path fill-rule="evenodd" d="{_MARK_PATH}"/></svg>'
    ).encode()
).decode()
#: The mark rendered inline in the page body, always solid black on the white ground.
_MARK_SVG = (
    '<svg class="mark" viewBox="97.9 100.4 828.2 828.2" aria-hidden="true">'
    f'<path fill="#14171f" fill-rule="evenodd" d="{_MARK_PATH}"/></svg>'
)
_ERROR_CSS = (
    "*{box-sizing:border-box}html,body{margin:0;height:100%}"
    "body{background:#ffffff;color:#1f2328;font-family:ui-sans-serif,-apple-system,"
    "BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;display:grid;"
    "place-items:center;text-align:center;padding:3rem 1.5rem;line-height:1.6}"
    "main{max-width:28rem}.mark{width:3.5rem;height:3.5rem;display:block;margin:0 auto .85rem}"
    ".m{font-weight:600;letter-spacing:-.01em;font-size:1.3rem;color:#14171f}"
    ".m b{color:#14171f;font-weight:600}.code{font-size:3.5rem;font-weight:800;line-height:1;"
    "color:#14171f;margin-top:1.4rem;font-variant-numeric:tabular-nums}"
    "h1{font-size:1.35rem;font-weight:650;margin:.6rem 0 0}p{color:#57606a;margin:.6rem auto 0;"
    "max-width:24rem;font-size:.95rem}.a{margin-top:1.6rem;display:flex;gap:.75rem;"
    "justify-content:center;align-items:center;flex-wrap:wrap}"
    ".lnk{display:inline-block;color:#57606a;text-decoration:none;font-size:.85rem;"
    "border:1px solid rgba(0,0,0,.16);padding:.5rem 1rem;border-radius:.55rem}"
    ".lnk:hover{color:#14171f;border-color:#14171f}.ct{color:#57606a;font-size:.85rem}"
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
        f'<link rel="icon" href="{_FAVICON}">'
        f"<title>{title} · viaduct.sh</title><style>{_ERROR_CSS}</style></head>"
        f'<body><main>{_MARK_SVG}<div class="m">viaduct<b>.sh</b></div>'
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
