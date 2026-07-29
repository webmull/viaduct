"""TLS on the tunnel listener: the client verifies the server certificate.

Uses a real self-signed cert (openssl) with SANs for localhost/127.0.0.1.
The public listener stays plaintext — Caddy terminates public TLS in prod.
"""

from __future__ import annotations

import ssl
import subprocess
from pathlib import Path

import pytest

from support import bare_server, http_get, make_client, run, tunnel_stack
from viaduct import protocol


@pytest.fixture(scope="module")
def certs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    tmp = tmp_path_factory.mktemp("certs")
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def _server_ctx(certs: tuple[Path, Path]) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certs[0], certs[1])
    return ctx


def _client_ctx(certs: tuple[Path, Path]) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(certs[0]))


def test_round_trip_over_tls(certs: tuple[Path, Path]) -> None:
    async def scenario() -> None:
        async with tunnel_stack(server_tls=_server_ctx(certs), client_ssl=_client_ctx(certs)) as (
            server,
            _client,
        ):
            resp = await http_get(server.public_port, "pmesh.viaduct.test", "/tls")
            assert resp.startswith(b"HTTP/1.1 200"), resp[:80]
            assert b"echo:/tls" in resp

    run(scenario())


def test_client_rejects_untrusted_cert(certs: tuple[Path, Path]) -> None:
    async def scenario() -> None:
        async with bare_server("pmesh", tls=_server_ctx(certs)) as server:
            client = make_client(server, ssl_ctx=ssl.create_default_context())
            try:
                with pytest.raises(ssl.SSLCertVerificationError):
                    await client.start()
            finally:
                await client.stop()

    run(scenario())


def test_plaintext_client_rejected_by_tls_server(certs: tuple[Path, Path]) -> None:
    async def scenario() -> None:
        async with bare_server("pmesh", tls=_server_ctx(certs)) as server:
            client = make_client(server)  # no ssl_ctx — sends raw frames
            try:
                with pytest.raises((protocol.ConnectionClosed, ConnectionError)):
                    await client.start()
            finally:
                await client.stop()

    run(scenario())
