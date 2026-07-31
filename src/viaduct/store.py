"""Persistence: auth tokens.

Tokens are the only persistent state. Subdomains are assigned at connect time,
generated randomly, and live only in memory for the life of a tunnel, so they
are never stored. Tokens are kept only as sha256 hex digests, never raw.

Rows load into memory at startup; every change writes through to SQLite.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path("/var/lib/viaduct/viaduct.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
  token_hash  TEXT PRIMARY KEY,
  label       TEXT,
  created_at  INTEGER NOT NULL,
  last_seen   INTEGER
);
"""


def hash_token(token: str) -> str:
    """sha256 hex digest — the only form of a token that is ever stored."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Token:
    token_hash: str
    label: str | None
    created_at: int
    last_seen: int | None


class Store:
    """SQLite-backed auth tokens, mirrored in memory for lock-free reads."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)  # autocommit
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self.tokens: dict[str, Token] = {
            row[0]: Token(*row)
            for row in self._conn.execute(
                "SELECT token_hash, label, created_at, last_seen FROM tokens"
            )
        }

    def close(self) -> None:
        self._conn.close()

    def get_by_token(self, token: str) -> Token | None:
        """Return the Token for a presented secret, or None if unknown."""
        return self.tokens.get(hash_token(token))

    def create_token(self, token_hash: str, label: str | None = None) -> Token:
        tok = Token(token_hash, label, created_at=int(time.time()), last_seen=None)
        self._conn.execute(
            "INSERT INTO tokens (token_hash, label, created_at, last_seen) VALUES (?, ?, ?, ?)",
            (tok.token_hash, tok.label, tok.created_at, tok.last_seen),
        )
        self.tokens[token_hash] = tok
        return tok

    def touch(self, token_hash: str) -> None:
        """Record that a token was just used to open or close a tunnel."""
        tok = self.tokens.get(token_hash)
        if tok is None:
            return
        tok.last_seen = int(time.time())
        self._conn.execute(
            "UPDATE tokens SET last_seen = ? WHERE token_hash = ?",
            (tok.last_seen, token_hash),
        )
