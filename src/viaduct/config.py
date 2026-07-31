"""Client configuration: ~/.config/viaduct/config.toml (server address, TLS)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path


class ConfigError(Exception):
    """The config file exists but cannot be read or parsed."""


def config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(xdg) if xdg else Path.home() / ".config"
    return root / "viaduct" / "config.toml"


def load() -> dict[str, str | bool]:
    """Return the string/bool keys of the config file, or {} if it doesn't exist."""
    path = config_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    return {key: value for key, value in data.items() if isinstance(value, str | bool)}
