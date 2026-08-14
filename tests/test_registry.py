"""Tests for the machine-local tunnel registry (viaduct list / kill)."""

from __future__ import annotations

import json
import os

from viaduct import registry


def test_register_active_deregister(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    registry.register(
        port=8080,
        subdomain="funny-otter",
        url="https://funny-otter.viaduct.sh",
        server="viaduct.sh:4443",
        started=100.0,
    )
    active = registry.active()
    assert len(active) == 1
    rec = active[0]
    assert rec["pid"] == os.getpid()  # our own (alive) process
    assert rec["subdomain"] == "funny-otter"
    assert rec["port"] == 8080
    assert rec["url"] == "https://funny-otter.viaduct.sh"

    registry.deregister()
    assert registry.active() == []


def test_active_prunes_records_whose_process_is_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    tunnels = tmp_path / "viaduct" / "tunnels"
    tunnels.mkdir(parents=True)
    dead_pid = 999_999  # above macOS's default max pid; not a live process
    stale = tunnels / f"{dead_pid}.json"
    stale.write_text(
        json.dumps(
            {
                "pid": dead_pid,
                "port": 1,
                "subdomain": "ghost",
                "url": "u",
                "server": "s",
                "started": 1.0,
            }
        )
    )
    assert registry.active() == []  # dead pid is filtered out
    assert not stale.exists()  # and its stale record is pruned


def test_active_ignores_unreadable_files(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    tunnels = tmp_path / "viaduct" / "tunnels"
    tunnels.mkdir(parents=True)
    (tunnels / "garbage.json").write_text("not json {")
    assert registry.active() == []


def test_terminate_missing_process_returns_false():
    assert registry.terminate(999_999) is False


def test_force_kill_missing_process_returns_false():
    assert registry.force_kill(999_999) is False


def test_is_alive_missing_process_returns_false():
    assert registry.is_alive(999_999) is False
