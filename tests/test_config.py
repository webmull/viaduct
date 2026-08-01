"""Unit tests for client config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from viaduct import config


def _write_config(root: Path, text: str) -> None:
    path = root / "viaduct" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text)


def test_missing_file_returns_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.load() == {}


def test_loads_string_and_bool_values_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _write_config(tmp_path, 'server = "1.2.3.4:4443"\ntoken = "sekrit"\ntls = true\nretries = 3\n')
    assert config.load() == {"server": "1.2.3.4:4443", "token": "sekrit", "tls": True}


def test_invalid_toml_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _write_config(tmp_path, "server = = nope")
    with pytest.raises(config.ConfigError):
        config.load()


def test_pin_seed_creates_key_0600(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    seed = config.pin_seed(8080)
    key = config.pin_secret_path()
    assert key.exists()
    assert key.stat().st_mode & 0o777 == 0o600
    assert isinstance(seed, str) and len(seed) == 64  # sha256 hex


def test_pin_seed_stable_across_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.pin_seed(8080) == config.pin_seed(8080)


def test_pin_seed_differs_by_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.pin_seed(8080) != config.pin_seed(3000)


def test_pin_seed_differs_by_machine_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "a"))
    first = config.pin_seed(8080)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "b"))
    assert config.pin_seed(8080) != first  # different secret file -> different seed


def test_pin_seed_regenerates_empty_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import hashlib

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    key = config.pin_secret_path()
    key.parent.mkdir(parents=True)
    key.write_text("")  # an interrupted first write leaves a 0-byte key
    seed = config.pin_seed(8080)
    assert seed != hashlib.sha256(b":8080").hexdigest()  # NOT the predictable empty-secret seed
    assert key.read_text().strip()  # a real secret was written
