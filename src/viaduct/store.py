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


class DomainTaken(Exception):
    """This custom domain is already registered."""


class UnknownSubdomain(Exception):
    """A domain was added against a subdomain that has no reservation."""


def hash_token(token: str) -> str:
    """sha256 hex digest — the only form of a token that is ever stored."""
    return hashlib.sha256(token.encode()).hexdigest()


def valid_subdomain(subdomain: str) -> bool:
    """True if *subdomain* is a usable DNS label (lowercase, alnum + hyphens)."""
    return _SUBDOMAIN_RE.fullmatch(subdomain) is not None


def valid_hostname(hostname: str) -> bool:
    """True if *hostname* is a usable lowercase FQDN (two or more valid labels)."""
    if not 1 <= len(hostname) <= 253:
        return False
    labels = hostname.split(".")
    if len(labels) < 2:
        return False
    return all(_SUBDOMAIN_RE.fullmatch(label) for label in labels)


@dataclass
class Reservation:
    subdomain: str
    token_hash: str
    created_at: int
    last_seen: int | None


@dataclass
class Domain:
    hostname: str
    subdomain: str
    verified: int
    created_at: int


class Store:
    """SQLite-backed reservations, mirrored in memory for lock-free reads."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)  # autocommit
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self.reservations: dict[str, Reservation] = {
            row[0]: Reservation(*row)
            for row in self._conn.execute(
                "SELECT subdomain, token_hash, created_at, last_seen FROM reservations"
            )
        }
        self.domains: dict[str, Domain] = {
            row[0]: Domain(*row)
            for row in self._conn.execute(
                "SELECT hostname, subdomain, verified, created_at FROM domains"
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

    def get_domain(self, hostname: str) -> Domain | None:
        return self.domains.get(hostname)

    def domains_for(self, subdomain: str) -> list[Domain]:
        return [d for d in self.domains.values() if d.subdomain == subdomain]

    def domain_routes(self) -> dict[str, str]:
        """hostname -> subdomain mapping for the routing hot path."""
        return {hostname: d.subdomain for hostname, d in self.domains.items()}

    def add_domain(self, hostname: str, subdomain: str) -> Domain:
        if hostname in self.domains:
            raise DomainTaken(hostname)
        if subdomain not in self.reservations:
            raise UnknownSubdomain(subdomain)
        domain = Domain(hostname, subdomain, verified=0, created_at=int(time.time()))
        try:
            self._conn.execute(
                "INSERT INTO domains (hostname, subdomain, verified, created_at)"
                " VALUES (?, ?, ?, ?)",
                (domain.hostname, domain.subdomain, domain.verified, domain.created_at),
            )
        except sqlite3.IntegrityError as exc:  # raced by another process
            raise DomainTaken(hostname) from exc
        self.domains[hostname] = domain
        return domain

    def remove_domain(self, hostname: str) -> bool:
        if self.domains.pop(hostname, None) is None:
            return False
        self._conn.execute("DELETE FROM domains WHERE hostname = ?", (hostname,))
        return True
