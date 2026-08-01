"""Update-check logic: version compare, daily cache, opt-out (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from viaduct import update


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # isolate the cache and config so tests never touch the real machine/network
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("VIADUCT_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("VIADUCT_AUTO_UPGRADE", raising=False)
    monkeypatch.delenv(update._UPGRADED_SENTINEL, raising=False)


@pytest.mark.parametrize(
    ("a", "b", "newer"),
    [
        ("0.2.0", "0.1.9", True),
        ("v1.0.0", "0.9.9", True),
        ("0.1.0", "0.1.0", False),
        ("0.1.0", "0.1.1", False),
        ("1.2.10", "1.2.9", True),
    ],
)
def test_is_newer(a: str, b: str, newer: bool) -> None:
    assert update.is_newer(a, b) is newer


def test_check_returns_version_when_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "installed_version", lambda: "0.1.0")
    monkeypatch.setattr(update, "fetch_stable_version", lambda timeout=1.5: "0.2.0")
    assert update.check_for_update(force=True) == "0.2.0"


def test_check_none_when_current(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "installed_version", lambda: "0.2.0")
    monkeypatch.setattr(update, "fetch_stable_version", lambda timeout=1.5: "0.2.0")
    assert update.check_for_update(force=True) is None


def test_opt_out_disables_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIADUCT_NO_UPDATE_CHECK", "1")

    def _boom(timeout: float = 1.5) -> str:
        raise AssertionError("must not hit the network when opted out")

    monkeypatch.setattr(update, "fetch_stable_version", _boom)
    assert update.check_for_update() is None


def test_daily_cache_avoids_second_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "installed_version", lambda: "0.1.0")
    calls = {"n": 0}

    def _fetch(timeout: float = 1.5) -> str:
        calls["n"] += 1
        return "0.2.0"

    monkeypatch.setattr(update, "fetch_stable_version", _fetch)
    assert update.check_for_update() == "0.2.0"  # first: fetches + caches
    assert update.check_for_update() == "0.2.0"  # second: served from cache
    assert calls["n"] == 1


def test_auto_enabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert update.auto_enabled() is False
    monkeypatch.setenv("VIADUCT_AUTO_UPGRADE", "1")
    assert update.auto_enabled() is True


def test_auto_disabled_after_reexec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIADUCT_AUTO_UPGRADE", "1")
    monkeypatch.setenv(update._UPGRADED_SENTINEL, "1")
    assert update.auto_enabled() is False  # the re-exec sentinel wins


def test_maybe_auto_upgrade_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> bool:
        raise AssertionError("must not upgrade when auto is off")

    monkeypatch.setattr(update, "run_upgrade", _boom)
    assert update.maybe_auto_upgrade() is None
