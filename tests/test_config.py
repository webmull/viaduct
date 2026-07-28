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


def test_loads_string_values_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _write_config(tmp_path, 'server = "1.2.3.4:4443"\ntoken = "sekrit"\nretries = 3\n')
    assert config.load() == {"server": "1.2.3.4:4443", "token": "sekrit"}


def test_invalid_toml_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _write_config(tmp_path, "server = = nope")
    with pytest.raises(config.ConfigError):
        config.load()
