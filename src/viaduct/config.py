"""Client configuration: ~/.config/viaduct/config.toml (server address, TLS)."""

from __future__ import annotations

import hashlib
import os
import secrets
import tomllib
from pathlib import Path


class ConfigError(Exception):
    """The config file exists but cannot be read or parsed."""


def config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(xdg) if xdg else Path.home() / ".config"
    return root / "viaduct" / "config.toml"


def pin_secret_path() -> Path:
    return config_path().parent / "pin.key"


def pin_seed(local_port: int) -> str:
    """Return the ``--pin`` seed for this machine and port.

    Loads a random secret from ``~/.config/viaduct/pin.key`` (created on first
    use, mode 0600) and returns ``sha256(secret:local_port)``. The raw secret
    never leaves the machine; the server only ever sees this seed. The same
    machine and port always yield the same seed, so the derived subdomain is
    stable across reconnects, while different ports get different names.
    """
    path = pin_secret_path()
    try:
        secret = path.read_text().strip()
    except FileNotFoundError:
        secret = secrets.token_hex(32)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(secret)
            path.chmod(0o600)
        except OSError as exc:
            raise ConfigError(f"cannot write pin key {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read pin key {path}: {exc}") from exc
    return hashlib.sha256(f"{secret}:{local_port}".encode()).hexdigest()


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
