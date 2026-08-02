"""Region selection (client) and multi-base-domain routing (server)."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from viaduct import client
from viaduct.server import TunnelServer


def test_multibase_match_subdomain() -> None:
    s = TunnelServer(base_domain="viaduct.test,lon.viaduct.test")
    assert s.base_domain == "viaduct.test"  # primary names new tunnels
    assert s.base_domains == ["viaduct.test", "lon.viaduct.test"]
    # the same tunnel is reachable under the primary and the alias
    assert s._match_subdomain("funny-otter.viaduct.test") == "funny-otter"
    assert s._match_subdomain("funny-otter.lon.viaduct.test") == "funny-otter"
    # a different region's name, or no host, does not match
    assert s._match_subdomain("funny-otter.nyc.viaduct.test") is None
    assert s._match_subdomain(None) is None


def test_region_resolves_to_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    host, port, ctx = client._connection_settings(None, None, None, region="nyc")
    assert (host, port) == ("nyc.viaduct.sh", 4443)
    assert ctx is not None  # TLS on for a real host


def test_region_unknown_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(typer.BadParameter):
        client._connection_settings(None, None, None, region="mars")


def test_region_and_server_conflict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(typer.BadParameter):
        client._connection_settings("x:4443", None, None, region="nyc")
