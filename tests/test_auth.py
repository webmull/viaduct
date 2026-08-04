"""Tests for tunnel auth (Basic + Bearer + IP allowlist), enforced server-side."""

from __future__ import annotations

import base64
import ipaddress

from viaduct import auth


def _head(**headers: str) -> bytes:
    lines = [b"GET / HTTP/1.1"]
    for name, value in headers.items():
        lines.append(name.replace("_", "-").encode() + b": " + value.encode())
    return b"\r\n".join(lines) + b"\r\n\r\n"


def _auth(**kwargs) -> auth.TunnelAuth:
    return auth.TunnelAuth(auth.client_payload(**kwargs) or {})


def _basic(user_pass: str) -> str:
    return "Basic " + base64.b64encode(user_pass.encode()).decode()


def test_client_payload_hashes_credentials():
    payload = auth.client_payload(
        basic_auth="alice:secret",
        bearer="tok",
        allow_ips=["10.0.0.0/8"],
        auth_message="am",
        deny_message="dm",
        realm="r",
    )
    assert set(payload) == {"basic", "bearer", "allow_ips", "auth_message", "deny_message", "realm"}
    assert payload["basic"] != "alice:secret" and len(payload["basic"]) == 64  # sha256 hex
    assert "secret" not in str(payload) and payload["bearer"] != "tok"
    assert payload["allow_ips"] == ["10.0.0.0/8"]
    assert auth.client_payload() is None  # nothing requested


def test_basic_auth_pass_and_fail():
    a = _auth(basic_auth="alice:secret")
    assert a.check(_head(Authorization=_basic("alice:secret")), None) is None
    blocked = a.check(_head(Authorization=_basic("alice:nope")), None)
    assert blocked[0] == "401 Unauthorized"
    assert blocked[3]["WWW-Authenticate"] == 'Basic realm="viaduct"'
    assert a.check(_head(), None)[0] == "401 Unauthorized"  # no header at all


def test_bearer_token():
    a = _auth(bearer="tok123")
    assert a.check(_head(Authorization="Bearer tok123"), None) is None
    blocked = a.check(_head(Authorization="Bearer wrong"), None)
    assert blocked[0] == "401 Unauthorized" and blocked[1] == "Token required"
    assert blocked[3] == {"WWW-Authenticate": "Bearer"}


def test_ip_allowlist():
    a = _auth(allow_ips=["1.2.3.0/24"])
    assert a.check(_head(), ipaddress.ip_address("1.2.3.4")) is None
    assert a.check(_head(), ipaddress.ip_address("9.9.9.9"))[0] == "403 Forbidden"
    assert a.check(_head(), None)[0] == "403 Forbidden"  # fail-closed with no IP


def test_custom_messages_are_escaped():
    a = _auth(basic_auth="a:b", auth_message="<b>hi</b>")
    detail = a.check(_head(), None)[2]
    assert "&lt;b&gt;hi&lt;/b&gt;" in detail and "<b>hi</b>" not in detail

    d = _auth(allow_ips=["1.0.0.0/8"], deny_message="office only")
    assert "office only" in d.check(_head(), ipaddress.ip_address("9.9.9.9"))[2]


def test_custom_realm():
    a = _auth(basic_auth="a:b", realm='My "Realm"')
    # quotes stripped so the header stays well-formed
    assert a.check(_head(), None)[3]["WWW-Authenticate"] == 'Basic realm="My Realm"'


def test_rate_limit_after_repeated_failures():
    a = _auth(basic_auth="a:b")
    ip = ipaddress.ip_address("5.5.5.5")
    for _ in range(10):
        assert a.check(_head(Authorization="Basic bad"), ip)[0] == "401 Unauthorized"
    assert a.check(_head(Authorization="Basic bad"), ip)[0] == "429 Too Many Requests"


def test_enforced():
    assert _auth(basic_auth="a:b").enforced()
    assert _auth(bearer="t").enforced()
    assert _auth(allow_ips=["1.0.0.0/8"]).enforced()
    assert auth.TunnelAuth({}).enforced() is False  # messages only, nothing to gate


def test_resolve_ip():
    xff = _head(X_Forwarded_For="1.1.1.1, 2.2.2.2")
    assert str(auth.resolve_ip(xff, "9.9.9.9", False)) == "2.2.2.2"  # last (front-set) hop
    bare = _head()
    assert auth.resolve_ip(bare, "3.3.3.3", False) is None  # no XFF, no trust -> unknown
    assert str(auth.resolve_ip(bare, "3.3.3.3", True)) == "3.3.3.3"  # --trust-peer-ip
