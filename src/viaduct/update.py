"""Self-update against the ``stable`` git tag (the prod channel).

The client never silently runs newer code by default: it can *notify* that a
newer stable release exists, and ``viaduct upgrade`` applies it via pipx. Truly
automatic upgrades are opt-in (``VIADUCT_AUTO_UPGRADE=1`` or ``auto_upgrade`` in
config.toml) and only ever jump to whatever the ``stable`` tag points at.

"Behind" is decided by the version string in ``pyproject.toml`` at the ``stable``
ref, so promoting a release means: bump ``version``, move the ``stable`` tag onto
that commit, and push it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tomllib
import urllib.request
from importlib import metadata
from pathlib import Path

from viaduct import config

REPO_SLUG = "webmull/viaduct"
STABLE_REF = "stable"
RAW_PYPROJECT = f"https://raw.githubusercontent.com/{REPO_SLUG}/{STABLE_REF}/pyproject.toml"
#: what ``viaduct upgrade`` / auto-upgrade installs (pinned to the moving tag)
GIT_TARGET = f"git+https://github.com/{REPO_SLUG}@{STABLE_REF}"

CHECK_INTERVAL = 86_400.0  # notify at most once a day
FETCH_TIMEOUT = 1.5
#: set in the child after a re-exec so an auto-upgrade can't loop
_UPGRADED_SENTINEL = "_VIADUCT_UPGRADED"


def installed_version() -> str:
    try:
        return metadata.version("viaduct")
    except metadata.PackageNotFoundError:
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
    """Version declared in pyproject.toml at the ``stable`` tag, or None."""
    try:
        req = urllib.request.Request(RAW_PYPROJECT, headers={"User-Agent": "viaduct"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = tomllib.loads(resp.read().decode("utf-8"))
        version = data["project"]["version"]
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
    """Reinstall from the ``stable`` tag via pipx. Returns True on success."""
    try:
        subprocess.run(
            ["pipx", "install", "--force", GIT_TARGET],
            check=True,
            timeout=300,  # a hung clone must not block tunnel startup forever
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
