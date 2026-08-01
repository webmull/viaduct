"""Server/TLS resolution for the client CLI (ngrok-like defaults)."""

from __future__ import annotations

from pathlib import Path

import pytest

from viaduct.client import _connection_settings


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
