"""CLI tests for `viaductd token create`."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from viaduct.server import app
from viaduct.store import Store, hash_token

runner = CliRunner()


def test_token_create_prints_token_and_stores_only_hash(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    result = runner.invoke(app, ["token", "create", "--subdomain", "pmesh", "--db", str(db)])
    assert result.exit_code == 0, result.output

    store = Store(db)
    res = store.get("pmesh")
    store.close()
    assert res is not None
    # One printed line must hash to the stored value — and the raw token must
    # not itself appear anywhere in the database.
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    tokens = [line for line in lines if hash_token(line) == res.token_hash]
    assert len(tokens) == 1
    assert tokens[0] not in db.read_bytes().decode("latin-1")


def test_token_create_rejects_duplicate(tmp_path: Path) -> None:
    db = str(tmp_path / "v.db")
    first = runner.invoke(app, ["token", "create", "--subdomain", "pmesh", "--db", db])
    assert first.exit_code == 0
    second = runner.invoke(app, ["token", "create", "--subdomain", "pmesh", "--db", db])
    assert second.exit_code == 1


def test_token_create_rejects_invalid_subdomain(tmp_path: Path) -> None:
    db = str(tmp_path / "v.db")
    result = runner.invoke(app, ["token", "create", "--subdomain", "Bad_Name", "--db", db])
    assert result.exit_code != 0
