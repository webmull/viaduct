# Viaduct — Build Prompt

Paste this into Claude Code (or similar) at the root of an empty repo.

---

## Amendment (2026-07-31): ephemeral subdomains, no custom domains, no auth

This supersedes the reservation, custom-domain, and auth parts of the original
spec below. Adam simplified the model in steps:

- **No user-chosen subdomains.** The server assigns a random friendly name
  (`adjective-animal`, e.g. `funny-otter`) to each tunnel at connect time and
  frees it on disconnect. Names are ephemeral (a reconnect gets a new one) and
  never persisted. They never collide between concurrent tunnels.
- **No custom domains** — the `domains` table, `viaduct domain` commands, and
  the `/_viaduct/tls-check` on-demand-TLS endpoint are gone. Caddy serves the
  wildcard `*.viaduct.sh` (DNS-01) and the apex landing page.
- **No auth at all.** This overrides the original "auth token is always
  required" non-goal. There are no tokens and no `token create`; any client
  that reaches the tunnel port gets a tunnel. Restrict that port at the
  firewall (source IPs) since it is otherwise an open relay.

There is **no persistent state and no database** — the store is gone entirely.
Everything else about the architecture (connection-pool data path, TLS on 4443
to encrypt tunneled traffic in transit, hardening, deploy) stands.

---

## Task

Build **Viaduct**, a self-hosted reverse tunnel in Python — a minimal alternative to ngrok/frp. It exposes a service running on a local machine at a public HTTPS hostname, without any inbound ports or NAT configuration on the local network.

Target deployment: **one** DigitalOcean droplet, one region, a handful of trusted users.

## Non-goals — do not build these

These are deliberately out of scope. Do not add them, do not add abstractions "ready for" them:

- Multi-region, load balancing, or DNS steering
- Stream multiplexing over a single connection (see connection pool below)
- UDP or raw TCP forwarding — HTTP and WebSocket only
- Web dashboard, signup flow, billing, user accounts
- Metrics/observability beyond structured logging
- Anonymous tunnels — auth token is always required

Anything not listed under Milestones is out of scope.

## Constraints

- Python 3.11+, `asyncio`, no framework
- Stdlib-first. Permitted third-party: `typer` (CLI), `rich` (output). Justify anything else.
- Two entry points: `viaduct` (client) and `viaductd` (server)
- Type hints throughout, `ruff` clean
- No secrets in the repo or in logs

---

## Architecture

```
phone ──HTTPS──> Caddy :443 ──plaintext──> viaductd :8080
                                                │
                                    (control connection, outbound)
                                                │
                                          viaduct client
                                                │
                                          localhost:3000
```

TLS terminates at Caddy. `viaductd` only ever sees plaintext on localhost. This is deliberate — it keeps CPU-bound crypto out of the Python process.

### Connection pool model

**Do not build a multiplexer.** Stream framing over a single connection is where this stops being tractable. Instead:

1. Client opens one **control connection** to the server and authenticates.
2. Client pre-opens a **pool** of idle data connections (default 20) to the server.
3. A public request arrives. Server picks an idle connection from that tunnel's pool and writes the raw bytes to it.
4. Client sees activity on that connection, opens a fresh connection to `localhost:<port>`, and pipes bidirectionally until either side closes.
5. Both connections are discarded. Client replenishes the pool.

Core of the data path is two of these under `asyncio.gather`:

```python
async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
```

### Wire protocol

Handshake only, then raw bytes. Frame = 4-byte big-endian length + UTF-8 JSON.

Control connection:
```
→ {"type":"hello","token":"...","subdomain":"pmesh","local_port":3000}
← {"type":"ok","hostname":"pmesh.viaduct.sh"}
← {"type":"error","reason":"subdomain_taken"}
↔ {"type":"ping"} / {"type":"pong"}      every 20s, both directions
```

Data connection:
```
→ {"type":"data_hello","token":"...","subdomain":"pmesh"}
[server holds it idle; on assignment, raw bytes follow immediately]
```

### Routing

Server routes on the `Host` header of the incoming request:

1. Exact match in `domains` table (custom domains)
2. Else parse subdomain from `*.viaduct.sh` and match `reservations`
3. Else 404

Read only the request line and headers to extract `Host`, then replay those bytes down the tunnel followed by the rest of the stream. Do not buffer the whole request. Do not parse the body. WebSocket upgrades must pass through untouched.

---

## State

**Runtime state is in memory and must not persist.** `dict[str, TunnelConn]` mapping subdomain to live connection. If the process restarts, all clients redial — a persisted binding to a dead socket is worse than nothing.

**Only reservations and custom domains persist.** SQLite at `/var/lib/viaduct/viaduct.db`, WAL mode. Load into memory at startup, write through on change.

```sql
CREATE TABLE reservations (
  subdomain   TEXT PRIMARY KEY,
  token_hash  TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  last_seen   INTEGER
);

CREATE TABLE domains (
  hostname    TEXT PRIMARY KEY,
  subdomain   TEXT NOT NULL REFERENCES reservations(subdomain),
  verified    INTEGER NOT NULL DEFAULT 0,
  created_at  INTEGER NOT NULL
);
```

Store `sha256` of tokens, never the token itself.

---

## CLI

Client config at `~/.config/viaduct/config.toml` (server address, token).

```
viaduct http 3000 --subdomain pmesh
    Open a tunnel. Blocks. Reconnects on drop with exponential backoff
    (1s, 2s, 4s ... capped at 30s). Prints the public URL on connect.

viaduct domain add demo.adamdavis.co.uk --subdomain pmesh
    Register a custom domain. Prints the exact CNAME record the user
    must create, and warns that apex domains need ALIAS/ANAME.

viaduct domain list
viaduct domain remove demo.adamdavis.co.uk

viaduct status
    Show configured server, token presence, active reservations.
```

Server:
```
viaductd --config /etc/viaduct/server.toml
viaductd token create --subdomain pmesh    # prints token once, stores hash
```

---

## TLS

Caddy in front, built with the DigitalOcean DNS plugin:

```
xcaddy build --with github.com/caddy-dns/digitalocean
```

Wildcard cert for `*.viaduct.sh` via DNS-01. Custom domains use **on-demand TLS** with a mandatory `ask` endpoint:

```
on_demand_tls {
    ask http://localhost:8080/_viaduct/tls-check
}
```

`viaductd` must expose `GET /_viaduct/tls-check?domain=<host>` returning **200 only if that hostname exists in the `domains` table**, 404 otherwise. Without this the service is an open certificate mill and Let's Encrypt will rate-limit it within the hour. This is not optional.

---

## Milestones

Build in this order. Each must work before starting the next.

**M1 — the pipe.** Hardcoded subdomain, hardcoded token, no DB, no TLS. Client connects, server proxies plain HTTP to `localhost:3000`. Prove bytes flow both ways and a WebSocket upgrade survives.

**M2 — persistence and auth.** SQLite, token hashing, `viaductd token create`, reservation lookup, reject unknown tokens.

**M3 — TLS.** Caddy in front, wildcard cert, real hostname.

**M4 — custom domains.** `domains` table, `viaduct domain add`, host-header routing precedence, `/_viaduct/tls-check`.

**M5 — hardening.** Heartbeat with dead-peer detection, reconnect backoff, pool replenishment, per-token connection cap, idle timeout, graceful drain on SIGTERM.

---

## Deployment

Produce `deploy/` containing:

- `viaductd.service` — systemd unit with `Restart=always`, `RestartSec=5`, `StateDirectory=viaduct`, `DO_API_TOKEN` via `EnvironmentFile`
- `viaduct.service` — client unit, same restart policy
- `Caddyfile`
- `setup.md` — droplet bootstrap: `ufw` default-deny with only 22 (source-restricted), 80, 443 open; `ulimit -n 65535`; `net.core.somaxconn` raised from the default 128

---

## Testing

- Unit tests for framing, host-header extraction, routing precedence
- Integration test: real server + real client on localhost, assert an HTTP request round-trips and a WebSocket echo works
- **Load test script**: open 500 concurrent WebSocket connections, measure time-to-established. This mirrors an audience scanning a QR code simultaneously and is the failure mode that matters.

---

## Start here

Begin with M1 only. Show me the layout and the protocol module before writing the data path, and stop at the end of each milestone for review.
