"""Unit tests for the SQLite reservation store."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from viaduct.store import Store, SubdomainTaken, hash_token, valid_subdomain


def test_hash_token_is_sha256_hex() -> None:
    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


def test_create_and_reload(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    store = Store(db)
    created = store.create_reservation("pmesh", hash_token("tok"))
    assert created.last_seen is None
    store.close()

    reopened = Store(db)
    res = reopened.get("pmesh")
    assert res is not None
    assert res.token_hash == hash_token("tok")
    assert res.created_at == created.created_at
    reopened.close()


def test_duplicate_reservation_rejected(tmp_path: Path) -> None:
    store = Store(tmp_path / "v.db")
    store.create_reservation("pmesh", "h1")
    with pytest.raises(SubdomainTaken):
        store.create_reservation("pmesh", "h2")
    store.close()


def test_touch_persists_last_seen(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    store = Store(db)
    store.create_reservation("pmesh", "h")
    store.touch("pmesh")
    res = store.get("pmesh")
    assert res is not None and res.last_seen is not None
    seen = res.last_seen
    store.close()

    reopened = Store(db)
    res = reopened.get("pmesh")
    assert res is not None and res.last_seen == seen
    reopened.close()


def test_touch_unknown_subdomain_is_noop(tmp_path: Path) -> None:
    store = Store(tmp_path / "v.db")
    store.touch("ghost")
    store.close()


def test_wal_mode_enabled(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    Store(db).close()
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


@pytest.mark.parametrize(
    ("subdomain", "ok"),
    [
        ("pmesh", True),
        ("a", True),
        ("a-b2", True),
        ("x" * 63, True),
        ("x" * 64, False),
        ("", False),
        ("UPPER", False),
        ("has space", False),
        ("-leading", False),
        ("trailing-", False),
        ("dotted.name", False),
    ],
)
def test_valid_subdomain(subdomain: str, ok: bool) -> None:
    assert valid_subdomain(subdomain) is ok
