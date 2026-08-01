"""Shared test fixtures."""

from __future__ import annotations

import pytest

from viaduct import dns


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never hit the real network for CNAME resolution in tests.

    Custom-domain tests override this with their own mapping; everything else
    gets 'no CNAME' so a non-wildcard Host resolves to no tunnel without a lookup.
    """

    async def _none(name: str) -> str | None:
        return None

    monkeypatch.setattr(dns, "resolve_cname", _none)
