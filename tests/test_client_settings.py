"""Server/TLS resolution for the client CLI (ngrok-like defaults)."""

from __future__ import annotations

from pathlib import Path

import pytest

from viaduct import auth
from viaduct.client import _auth_summary, _connection_settings, _expand_allow_ips


@pytest.fixture(autouse=True)
def _empty_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # point config loading at an empty dir so only defaults/flags apply
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def _write_config(tmp_path: Path, text: str) -> None:
    path = tmp_path / "viaduct" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text)


def test_default_targets_hosted_server_with_tls() -> None:
    host, port, ssl_ctx = _connection_settings(None, None, None)
    assert (host, port) == ("viaduct.sh", 4443)
    assert ssl_ctx is not None  # TLS on for the real server


def test_localhost_defaults_to_plaintext() -> None:
    _, _, ssl_ctx = _connection_settings("127.0.0.1:4443", None, None)
    assert ssl_ctx is None
    _, _, ssl_ctx = _connection_settings("localhost:8443", None, None)
    assert ssl_ctx is None


def test_explicit_tls_flag_overrides_host_default() -> None:
    _, _, ssl_ctx = _connection_settings("127.0.0.1:4443", True, None)
    assert ssl_ctx is not None  # forced on locally
    _, _, ssl_ctx = _connection_settings("viaduct.sh:4443", False, None)
    assert ssl_ctx is None  # forced off for a real host


def test_config_tls_false_overrides_auto_for_real_host(tmp_path: Path) -> None:
    _write_config(tmp_path, "tls = false\n")
    _, _, ssl_ctx = _connection_settings("example.com:4443", None, None)
    assert ssl_ctx is None  # config beats the on-by-default heuristic


def test_config_server_used_when_no_flag(tmp_path: Path) -> None:
    _write_config(tmp_path, 'server = "example.com:9000"\n')
    host, port, ssl_ctx = _connection_settings(None, None, None)
    assert (host, port) == ("example.com", 9000)
    assert ssl_ctx is not None  # real host -> TLS on


def _summary(**kwargs: object) -> str | None:
    return _auth_summary(auth.client_payload(**kwargs))


def test_auth_summary_labels_the_banner() -> None:
    assert _summary(basic_auth="a:b") == "Basic auth"
    assert _summary(bearer="t") == "bearer token"
    assert _summary(allow_ips=["1.2.3.4"]) == "IP allowlist (1 rule)"
    assert _summary(allow_ips=["1.2.3.4", "10.0.0.0/8"]) == "IP allowlist (2 rules)"
    assert _summary(basic_auth="a:b", allow_ips=["1.2.3.4"]) == "Basic auth + IP allowlist (1 rule)"


def test_auth_summary_none_when_nothing_gates() -> None:
    # a message with no actual gate must not claim the tunnel is protected
    assert _summary(auth_message="hi") is None
    assert _summary() is None
    assert _auth_summary(None) is None


def test_expand_allow_ips_accepts_repeats_and_commas() -> None:
    assert _expand_allow_ips(None) == []
    assert _expand_allow_ips(["1.2.3.4"]) == ["1.2.3.4"]
    assert _expand_allow_ips(["1.2.3.4", "5.6.7.8"]) == ["1.2.3.4", "5.6.7.8"]  # repeated flag
    assert _expand_allow_ips(["1.2.3.4,5.6.7.8"]) == ["1.2.3.4", "5.6.7.8"]  # comma-separated
    # a mix, with stray whitespace and empty parts
    assert _expand_allow_ips(["1.2.3.4, 10.0.0.0/8", "", "9.9.9.9 ,"]) == [
        "1.2.3.4",
        "10.0.0.0/8",
        "9.9.9.9",
    ]
