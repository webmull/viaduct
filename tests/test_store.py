"""Unit tests for the SQLite token store."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from viaduct.store import Store, hash_token


def test_hash_token_is_sha256_hex() -> None:
    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


def test_create_and_lookup(tmp_path: Path) -> None:
    store = Store(tmp_path / "v.db")
    store.create_token(hash_token("sekrit"), label="adam-laptop")
    assert store.get_by_token("sekrit") is not None
    assert store.get_by_token("sekrit").label == "adam-laptop"
    assert store.get_by_token("wrong") is None
    store.close()


def test_tokens_survive_reload(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    store = Store(db)
    store.create_token(hash_token("tok"))
    store.close()

    reopened = Store(db)
    tok = reopened.get_by_token("tok")
    assert tok is not None and tok.token_hash == hash_token("tok")
    reopened.close()


def test_raw_token_never_stored(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    store = Store(db)
    store.create_token(hash_token("super-secret-value"))
    store.close()
    assert "super-secret-value" not in db.read_bytes().decode("latin-1")


def test_touch_persists_last_seen(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    store = Store(db)
    store.create_token(hash_token("tok"))
    store.touch(hash_token("tok"))
    seen = store.get_by_token("tok").last_seen
    assert seen is not None
    store.close()

    reopened = Store(db)
    assert reopened.get_by_token("tok").last_seen == seen
    reopened.close()


def test_touch_unknown_token_is_noop(tmp_path: Path) -> None:
    store = Store(tmp_path / "v.db")
    store.touch(hash_token("ghost"))
    store.close()


def test_wal_mode_enabled(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    Store(db).close()
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
