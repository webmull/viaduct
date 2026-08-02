"""Machine-local registry of running tunnels, for ``viaduct list`` / ``viaduct kill``.

Each ``viaduct http`` process drops a small JSON record under the state dir while
it holds a tunnel, keyed by pid, and removes it on exit. Nothing here is shared
with the server or across machines: it is purely a convenience for finding and
stopping the tunnels you started on this box. A process that dies without
cleaning up (a crash, SIGKILL) leaves a stale record, which the next read prunes
once it sees the pid is gone.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
from pathlib import Path
from typing import Any


def _state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME", "")
    root = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return root / "viaduct" / "tunnels"


def _record_path(pid: int) -> Path:
    return _state_dir() / f"{pid}.json"


def register(*, port: int, subdomain: str, url: str, server: str, started: float) -> None:
    """Record this process's tunnel. Best-effort: never raises into the tunnel."""
    pid = os.getpid()
    rec = {
        "pid": pid,
        "port": port,
        "subdomain": subdomain,
        "url": url,
        "server": server,
        "started": started,
    }
    with contextlib.suppress(OSError):
        _state_dir().mkdir(parents=True, exist_ok=True)
        tmp = _record_path(pid).with_suffix(".tmp")
        tmp.write_text(json.dumps(rec))
        tmp.replace(_record_path(pid))  # atomic swap into place


def deregister() -> None:
    with contextlib.suppress(OSError):
        _record_path(os.getpid()).unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


def active() -> list[dict[str, Any]]:
    """Live tunnel records for this machine, oldest first; prunes dead ones."""
    out: list[dict[str, Any]] = []
    d = _state_dir()
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid = rec.get("pid")
        if not isinstance(pid, int) or not _alive(pid):
            with contextlib.suppress(OSError):
                f.unlink()
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get("started", 0.0))
    return out


def terminate(pid: int) -> bool:
    """SIGTERM a tunnel process (it drains and exits). True if the signal was sent."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    return True
