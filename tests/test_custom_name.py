"""Handshake tests for custom subdomains (--name): the server validates and pins."""

from __future__ import annotations

import asyncio

import pytest

from support import BASE_DOMAIN, bare_server, make_client, run
from viaduct import profanity
from viaduct.client import TunnelError


def test_custom_name_assigned_when_free() -> None:
    async def go() -> None:
        async with bare_server() as server:
            client = make_client(server, name="myfriendlysubdomain")
            try:
                hostname = await client.start()
                assert hostname == f"myfriendlysubdomain.{BASE_DOMAIN}"
                assert "myfriendlysubdomain" in server.tunnels
            finally:
                await client.stop()

    run(go())


def test_custom_name_is_normalised() -> None:
    async def go() -> None:
        async with bare_server() as server:
            client = make_client(server, name="My-App")
            try:
                assert await client.start() == f"my-app.{BASE_DOMAIN}"
            finally:
                await client.stop()

    run(go())


def test_reserved_name_rejected() -> None:
    async def go() -> None:
        async with bare_server() as server:
            client = make_client(server, name="nyc")
            with pytest.raises(TunnelError) as ei:
                await client.start()
            assert str(ei.value) == "name_invalid"
            await client.stop()

    run(go())


def test_taken_name_rejected() -> None:
    async def go() -> None:
        async with bare_server() as server:
            first = make_client(server, name="acme-staging")
            second = make_client(server, name="acme-staging")
            try:
                await first.start()
                with pytest.raises(TunnelError) as ei:
                    await second.start()
                assert str(ei.value) == "name_taken"
            finally:
                await first.stop()
                await second.stop()

    run(go())


def test_profanity_name_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # screen a benign fixture token so no real slur appears in the test
    monkeypatch.setattr(profanity, "blocks", lambda label: label == "wibbleword")

    async def go() -> None:
        async with bare_server() as server:
            client = make_client(server, name="wibbleword")
            with pytest.raises(TunnelError) as ei:
                await client.start()
            assert str(ei.value) == "name_rejected"
            await client.stop()

    run(go())


def test_fragmented_name_rejected() -> None:
    # a hyphen-obfuscated word never reaches the profanity screen: syntax refuses it
    async def go() -> None:
        async with bare_server() as server:
            client = make_client(server, name="d-a-m-n")
            with pytest.raises(TunnelError) as ei:
                await client.start()
            assert str(ei.value) == "name_invalid"
            await client.stop()

    run(go())


def test_name_freed_on_disconnect_and_reclaimable() -> None:
    async def go() -> None:
        async with bare_server() as server:
            first = make_client(server, name="reclaimme")
            await first.start()
            assert "reclaimme" in server.tunnels
            await first.stop()
            for _ in range(300):  # wait for the server to process the drop
                if "reclaimme" not in server.tunnels:
                    break
                await asyncio.sleep(0.01)
            assert "reclaimme" not in server.tunnels  # freed
            assert not server._ip_tunnels  # per-IP count decremented to empty
            second = make_client(server, name="reclaimme")  # reclaimable by anyone
            try:
                assert await second.start() == f"reclaimme.{BASE_DOMAIN}"
            finally:
                await second.stop()

    run(go())
