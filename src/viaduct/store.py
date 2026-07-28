"""Persistence: subdomain reservations (and, from M4, custom domains).

Only reservations and custom domains persist — runtime tunnel state must not.
Rows load into memory at startup; every change writes through to SQLite
immediately. Tokens are stored only as sha256 hex digests, never raw.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path("/var/lib/viaduct/viaduct.db")

_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
  subdomain   TEXT PRIMARY KEY,
  token_hash  TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  last_seen   INTEGER
);

CREATE TABLE IF NOT EXISTS domains (
  hostname    TEXT PRIMARY KEY,
  subdomain   TEXT NOT NULL REFERENCES reservations(subdomain),
  verified    INTEGER NOT NULL DEFAULT 0,
  created_at  INTEGER NOT NULL
);
"""


class SubdomainTaken(Exception):
    """A reservation for this subdomain already exists."""


def hash_token(token: str) -> str:
    """sha256 hex digest — the only form of a token that is ever stored."""
    return hashlib.sha256(token.encode()).hexdigest()


def valid_subdomain(subdomain: str) -> bool:
    """True if *subdomain* is a usable DNS label (lowercase, alnum + hyphens)."""
    return _SUBDOMAIN_RE.fullmatch(subdomain) is not None


@dataclass
class Reservation:
    subdomain: str
    token_hash: str
    created_at: int
    last_seen: int | None


class Store:
    """SQLite-backed reservations, mirrored in memory for lock-free reads."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)  # autocommit
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self.reservations: dict[str, Reservation] = {
            row[0]: Reservation(*row)
            for row in self._conn.execute(
                "SELECT subdomain, token_hash, created_at, last_seen FROM reservations"
            )
        }

    def close(self) -> None:
        self._conn.close()

    def get(self, subdomain: str) -> Reservation | None:
        return self.reservations.get(subdomain)

    def create_reservation(self, subdomain: str, token_hash: str) -> Reservation:
        if subdomain in self.reservations:
            raise SubdomainTaken(subdomain)
        res = Reservation(subdomain, token_hash, created_at=int(time.time()), last_seen=None)
        try:
            self._conn.execute(
                "INSERT INTO reservations (subdomain, token_hash, created_at, last_seen)"
                " VALUES (?, ?, ?, ?)",
                (res.subdomain, res.token_hash, res.created_at, res.last_seen),
            )
        except sqlite3.IntegrityError as exc:  # raced by another process
            raise SubdomainTaken(subdomain) from exc
        self.reservations[subdomain] = res
        return res

    def touch(self, subdomain: str) -> None:
        """Record that this reservation's tunnel was just seen alive."""
        res = self.reservations.get(subdomain)
        if res is None:
            return
        res.last_seen = int(time.time())
        self._conn.execute(
            "UPDATE reservations SET last_seen = ? WHERE subdomain = ?",
            (res.last_seen, subdomain),
        )
