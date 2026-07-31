"""CLI tests for `viaductd token create`."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from viaduct.server import app
from viaduct.store import Store

runner = CliRunner()


def test_token_create_prints_token_and_stores_only_hash(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    result = runner.invoke(app, ["token", "create", "--db", str(db)])
    assert result.exit_code == 0, result.output

    store = Store(db)
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    minted = [line for line in lines if store.get_by_token(line) is not None]
    store.close()
    assert len(minted) == 1
    # the raw token must not itself appear anywhere in the database
    assert minted[0] not in db.read_bytes().decode("latin-1")


def test_token_create_records_label(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    result = runner.invoke(app, ["token", "create", "--label", "adam", "--db", str(db)])
    assert result.exit_code == 0, result.output
    token = next(
        line.strip() for line in result.output.splitlines() if line.strip() and " " not in line
    )
    store = Store(db)
    tok = store.get_by_token(token)
    store.close()
    assert tok is not None and tok.label == "adam"


def test_each_token_create_is_distinct(tmp_path: Path) -> None:
    db = str(tmp_path / "v.db")
    first = runner.invoke(app, ["token", "create", "--db", db])
    second = runner.invoke(app, ["token", "create", "--db", db])
    assert first.exit_code == 0 and second.exit_code == 0
    t1 = first.output.splitlines()[0].strip()
    t2 = second.output.splitlines()[0].strip()
    assert t1 != t2
    store = Store(Path(db))
    assert store.get_by_token(t1) is not None
    assert store.get_by_token(t2) is not None
    store.close()
