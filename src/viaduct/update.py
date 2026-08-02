"""Self-update against the ``viaduct-sh`` release on PyPI.

The client never silently runs newer code by default: it can *notify* that a
newer release exists on PyPI, and ``viaduct upgrade`` applies it via pipx. Truly
automatic upgrades are opt-in (``VIADUCT_AUTO_UPGRADE=1`` or ``auto_upgrade`` in
config.toml) and only ever jump to the latest published release.

"Behind" is decided by the latest version on PyPI, so cutting a release means:
bump ``version`` and push a ``vX.Y.Z`` tag; CI publishes it to PyPI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from importlib import metadata
from pathlib import Path

from viaduct import config

#: PyPI distribution and its JSON API (the release channel)
PYPI_NAME = "viaduct-sh"
PYPI_JSON = f"https://pypi.org/pypi/{PYPI_NAME}/json"
#: how ``viaduct upgrade`` / auto-upgrade reinstalls the latest release
UPGRADE_CMD = ["pipx", "install", "--force", PYPI_NAME]
#: the same command as a copy-pasteable hint for error messages
UPGRADE_HINT = " ".join(UPGRADE_CMD)

CHECK_INTERVAL = 86_400.0  # notify at most once a day
FETCH_TIMEOUT = 1.5
#: set in the child after a re-exec so an auto-upgrade can't loop
_UPGRADED_SENTINEL = "_VIADUCT_UPGRADED"


def installed_version() -> str:
    # PyPI distribution is "viaduct-sh"; "viaduct" covers older source installs.
    for dist in ("viaduct-sh", "viaduct"):
        try:
            return metadata.version(dist)
        except metadata.PackageNotFoundError:
            continue
    return "0.0.0"


def _ver_tuple(v: str) -> tuple[int, ...]:
    """Lenient dotted-numeric parse: 'v0.3.1' -> (0, 3, 1); junk -> 0."""
    parts: list[int] = []
    for chunk in v.strip().lstrip("vV").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    return _ver_tuple(candidate) > _ver_tuple(current)


def fetch_stable_version(timeout: float = FETCH_TIMEOUT) -> str | None:
    """Latest ``viaduct-sh`` version published on PyPI, or None on any failure."""
    try:
        req = urllib.request.Request(PYPI_JSON, headers={"User-Agent": "viaduct"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        version = data["info"]["version"]
        return version if isinstance(version, str) else None
    except Exception:  # network, parse, missing key; never fatal
        return None


def _cache_path() -> Path:
    root = os.environ.get("XDG_CACHE_HOME")
    base = Path(root) if root else Path.home() / ".cache"
    return base / "viaduct" / "update.json"


def _read_cache() -> dict[str, object]:
    import json

    try:
        return json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_cache(checked: float, latest: str) -> None:
    import json

    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked": checked, "latest": latest}))
    except OSError:
        pass  # a missing cache just means we check again next time


def _env_true(name: str) -> bool:
    """True only for explicit truthy values, so e.g. ``NAME=0`` reads as off."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def check_for_update(force: bool = False) -> str | None:
    """Return the stable version if it's newer than installed, else None.

    Rate-limited to once a day via a small cache unless *force*. Disabled by
    ``VIADUCT_NO_UPDATE_CHECK=1``. Never raises.
    """
    if _env_true("VIADUCT_NO_UPDATE_CHECK"):
        return None
    now = time.time()
    cache = _read_cache()
    latest: str | None
    last = cache.get("checked", 0)
    if not force and isinstance(last, int | float) and 0 <= now - last < CHECK_INTERVAL:
        latest = cache.get("latest") if isinstance(cache.get("latest"), str) else None
    else:
        latest = fetch_stable_version()
        if latest:
            _write_cache(now, latest)
    if latest and is_newer(latest, installed_version()):
        return latest
    return None


def auto_enabled() -> bool:
    if os.environ.get(_UPGRADED_SENTINEL):
        return False  # we just re-exec'd; don't check again
    if _env_true("VIADUCT_AUTO_UPGRADE"):
        return True
    try:
        return config.load().get("auto_upgrade") is True
    except config.ConfigError:
        return False


def run_upgrade() -> bool:
    """Reinstall the latest release from PyPI via pipx. Returns True on success."""
    try:
        subprocess.run(
            UPGRADE_CMD,
            check=True,
            timeout=300,  # a hung install must not block tunnel startup forever
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def maybe_auto_upgrade() -> str | None:
    """If auto-upgrade is on and we're behind, upgrade and re-exec.

    Returns the target version (for the caller to announce) when it is about to
    upgrade; on success this never returns because the process is replaced.
    """
    if not auto_enabled():
        return None
    latest = check_for_update(force=False)
    if not latest:
        return None
    if run_upgrade():
        os.environ[_UPGRADED_SENTINEL] = "1"
        os.execvp(sys.argv[0], sys.argv)  # replaces this process
    return latest  # upgrade failed; let the caller carry on with a warning
