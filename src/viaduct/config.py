"""Client configuration: ~/.config/viaduct/config.toml (server address, TLS)."""

from __future__ import annotations

import contextlib
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
    secret = _read_pin_secret(path) or _create_pin_secret(path)
    return hashlib.sha256(f"{secret}:{local_port}".encode()).hexdigest()


def _read_pin_secret(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""  # not created yet
    except OSError as exc:
        raise ConfigError(f"cannot read pin key {path}: {exc}") from exc


def _create_pin_secret(path: Path) -> str:
    """Create the pin key atomically at mode 0600, tolerating a concurrent race.

    ``O_CREAT | O_EXCL`` closes the TOCTOU window (no readable-then-chmod gap)
    and makes concurrent first-runs safe: the loser reuses the winner's secret.
    An empty/corrupt leftover (e.g. an interrupted first write) is dropped and
    regenerated rather than silently yielding a predictable ``sha256(":port")``.
    """
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot create config dir {path.parent}: {exc}") from exc
    secret = secrets.token_hex(32)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = _read_pin_secret(path)
            if existing:
                return existing  # another process won the race; use its secret
            with contextlib.suppress(OSError):
                path.unlink()  # empty/corrupt leftover; drop it and retry once
            continue
        except OSError as exc:
            raise ConfigError(f"cannot create pin key {path}: {exc}") from exc
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(secret)
        except OSError as exc:
            raise ConfigError(f"cannot write pin key {path}: {exc}") from exc
        return secret
    raise ConfigError(f"cannot initialise pin key {path}")


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
