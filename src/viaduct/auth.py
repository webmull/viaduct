"""HTTP auth for tunnels: Basic, Bearer, and an IP allowlist, enforced server-side.

The client hashes its credentials and sends an opaque payload in the hello frame
(never the plaintext); viaductd reconstructs a :class:`TunnelAuth` and checks each
request at the edge, before it reaches a data connection or the owner's machine.
Owner-supplied messages are HTML-escaped here before they go into the error page.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import html
import ipaddress
import re
import time

from viaduct import routing

#: cap on owner-supplied message length (chars).
_MSG_MAX = 200
#: control characters (incl. CR/LF) are stripped from owner-supplied strings so
#: none can inject into a response header (the realm) or the branded error page.
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
#: failed-auth throttle: this many failures from one IP within the window blocks
#: further attempts for a bit (a light brute-force speed bump).
_FAIL_WINDOW = 60.0
_FAIL_MAX = 10

_DEFAULT_AUTH = (
    "This tunnel is password-protected. Ask the person who shared this link for "
    "the username and password."
)
_DEFAULT_BEARER = "This tunnel requires a valid bearer token."
_DEFAULT_DENY = "This tunnel only accepts requests from approved addresses."

IPAddr = ipaddress.IPv4Address | ipaddress.IPv6Address


def client_payload(
    basic_auth: str | None = None,
    bearer: str | None = None,
    allow_ips: list[str] | None = None,
    auth_message: str | None = None,
    deny_message: str | None = None,
    realm: str | None = None,
) -> dict | None:
    """Build the opaque auth payload the client puts in the hello frame. Only
    hashes of credentials travel, never the plaintext password or token."""
    payload: dict = {}
    if basic_auth:
        payload["basic"] = hashlib.sha256(
            b"Basic " + base64.b64encode(basic_auth.encode("utf-8"))
        ).hexdigest()
    if bearer:
        payload["bearer"] = hashlib.sha256(b"Bearer " + bearer.encode("utf-8")).hexdigest()
    if allow_ips:
        payload["allow_ips"] = list(allow_ips)
    if auth_message:
        payload["auth_message"] = auth_message
    if deny_message:
        payload["deny_message"] = deny_message
    if realm:
        payload["realm"] = realm
    return payload or None


def _clip(value: object, limit: int = _MSG_MAX) -> str | None:
    if not isinstance(value, str):
        return None
    return _CTRL.sub("", value[:limit]).strip() or None


def resolve_ip(head: bytes, peer_ip: str | None, trust_peer: bool) -> IPAddr | None:
    """The visitor IP: the last X-Forwarded-For hop the trusted front set, or the
    direct socket peer when the operator opted into --trust-peer-ip (no front)."""
    ip = routing.forwarded_for_ip(head)
    if ip is None and trust_peer and peer_ip:
        with contextlib.suppress(ValueError):
            ip = ipaddress.ip_address(peer_ip)
    return ip


class TunnelAuth:
    """Server-side auth for one tunnel, reconstructed from the client's payload."""

    def __init__(self, payload: dict) -> None:
        self.basic = payload["basic"] if isinstance(payload.get("basic"), str) else None
        self.bearer = payload["bearer"] if isinstance(payload.get("bearer"), str) else None
        nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for item in payload.get("allow_ips") or []:
            if isinstance(item, str):
                with contextlib.suppress(ValueError):
                    nets.append(ipaddress.ip_network(item, strict=False))
        self.allow_nets = nets or None
        self.auth_message = _clip(payload.get("auth_message"))
        self.deny_message = _clip(payload.get("deny_message"))
        self.realm = (_clip(payload.get("realm"), 64) or "viaduct").replace('"', "")
        #: per-visitor-IP failed-auth timestamps, for the brute-force throttle.
        self._fails: dict[str, list[float]] = {}

    def enforced(self) -> bool:
        """True if this actually gates anything (used for the ok-frame handshake)."""
        return bool(self.basic or self.bearer or self.allow_nets)

    def _throttled(self, ip: IPAddr | None) -> bool:
        if ip is None:
            return False
        now = time.monotonic()
        recent = [t for t in self._fails.get(str(ip), []) if now - t < _FAIL_WINDOW]
        self._fails[str(ip)] = recent
        return len(recent) >= _FAIL_MAX

    def _record_fail(self, ip: IPAddr | None) -> None:
        if ip is not None:
            self._fails.setdefault(str(ip), []).append(time.monotonic())

    def check(
        self, head: bytes, ip: IPAddr | None
    ) -> tuple[str, str, str, dict[str, str] | None] | None:
        """Return (status, title, detail, extra_headers) if the request is denied,
        else None. *detail* is HTML-escaped where it is owner-supplied."""
        if self.allow_nets is not None and (
            ip is None or not any(ip in net for net in self.allow_nets)
        ):
            return (
                "403 Forbidden",
                "Not allowed",
                html.escape(self.deny_message) if self.deny_message else _DEFAULT_DENY,
                None,
            )
        if self.basic is None and self.bearer is None:
            return None
        if self._throttled(ip):
            return (
                "429 Too Many Requests",
                "Slow down",
                "Too many failed attempts. Wait a moment and try again.",
                {"Retry-After": "30"},
            )
        got = routing.header_value(head, b"authorization") or b""
        digest = hashlib.sha256(got).hexdigest()
        if (self.basic and hmac.compare_digest(digest, self.basic)) or (
            self.bearer and hmac.compare_digest(digest, self.bearer)
        ):
            return None
        self._record_fail(ip)
        detail = html.escape(self.auth_message) if self.auth_message else None
        if self.basic is not None:
            return (
                "401 Unauthorized",
                "Password required",
                detail or _DEFAULT_AUTH,
                {"WWW-Authenticate": f'Basic realm="{self.realm}"'},
            )
        return (
            "401 Unauthorized",
            "Token required",
            detail or _DEFAULT_BEARER,
            {"WWW-Authenticate": "Bearer"},
        )
