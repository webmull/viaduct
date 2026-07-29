"""Load test: N concurrent WebSocket connections through the tunnel.

Measures time-to-established per connection (TCP connect → 101 response),
which mirrors an audience scanning a QR code simultaneously — the failure
mode that matters. Stdlib only; run against viaductd's public port (or Caddy).

    python scripts/load_test.py --port 18080 --hostname pmesh.localhost \
        --connections 500
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import secrets
import statistics
import sys
import time


async def establish_one(
    host: str, port: int, hostname: str, deadline: float
) -> tuple[float, str | None]:
    """Return (seconds-to-established, error) for one WebSocket upgrade."""
    start = time.perf_counter()
    try:
        async with asyncio.timeout(deadline):
            reader, writer = await asyncio.open_connection(host, port)
            key = base64.b64encode(secrets.token_bytes(16)).decode()
            writer.write(
                f"GET /ws HTTP/1.1\r\n"
                f"Host: {hostname}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n".encode()
            )
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            elapsed = time.perf_counter() - start
            status = head.split(b"\r\n", 1)[0].decode("latin-1")
            if b" 101 " not in head.split(b"\r\n", 1)[0] + b" ":
                return elapsed, f"non-101 response: {status}"
            writer.close()
            return elapsed, None
    except TimeoutError:
        return time.perf_counter() - start, "timeout"
    except OSError as exc:
        return time.perf_counter() - start, f"{type(exc).__name__}: {exc}"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Address of the public listener")
    parser.add_argument("--port", type=int, default=8080, help="Public port")
    parser.add_argument("--hostname", required=True, help="Host header, e.g. pmesh.viaduct.sh")
    parser.add_argument("--connections", type=int, default=500)
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-connection deadline")
    args = parser.parse_args()

    print(f"opening {args.connections} concurrent WebSocket connections ...")
    wall_start = time.perf_counter()
    results = await asyncio.gather(
        *(
            establish_one(args.host, args.port, args.hostname, args.timeout)
            for _ in range(args.connections)
        )
    )
    wall = time.perf_counter() - wall_start

    ok = sorted(t for t, err in results if err is None)
    failures: dict[str, int] = {}
    for _, err in results:
        if err is not None:
            failures[err] = failures.get(err, 0) + 1

    print(f"\nestablished:  {len(ok)}/{args.connections} in {wall:.2f}s wall time")
    if ok:
        q = statistics.quantiles(ok, n=100) if len(ok) >= 2 else [ok[0]] * 99
        print(
            f"time-to-established  p50={q[49] * 1000:.0f}ms  p95={q[94] * 1000:.0f}ms  "
            f"p99={q[98] * 1000:.0f}ms  max={ok[-1] * 1000:.0f}ms"
        )
    for err, count in sorted(failures.items(), key=lambda kv: -kv[1]):
        print(f"failed ({count}): {err}")
    return 0 if len(ok) == args.connections else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
