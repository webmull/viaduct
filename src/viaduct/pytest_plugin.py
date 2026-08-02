"""pytest plugin: a ``viaduct_tunnel`` fixture that gives a test a real public URL.

    def test_stripe_webhook(viaduct_tunnel):
        url = viaduct_tunnel(8000)          # your local test server is now public
        stripe.WebhookEndpoint.create(url=url + "/hook")
        ...                                 # tunnel torn down automatically

Registered automatically once ``viaduct-sh`` is installed (pytest11 entry point).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

import viaduct


@pytest.fixture
def viaduct_tunnel() -> Iterator[Callable[..., str]]:
    """Open one or more tunnels during a test; returns each public URL. All are
    closed when the test finishes."""
    opened: list[viaduct.api._SyncTunnel] = []

    def _open(port: int, **kwargs: object) -> str:
        t = viaduct.tunnel_sync(port, **kwargs)
        t.__enter__()
        opened.append(t)
        assert t.url is not None
        return t.url

    yield _open
    for t in reversed(opened):
        t.__exit__(None, None, None)
