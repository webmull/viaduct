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

## Amendment (2026-08-01): opt-in stable subdomain (`--pin`)

Relaxes "never persisted" from the 2026-07-31 amendment for one opt-in case,
without reintroducing a database or user-chosen names.

- **`viaduct http PORT --pin`** gives the tunnel a stable subdomain, the same on
  every reconnect, instead of a fresh random one. It is for someone who wants a
  durable URL for a webhook endpoint or a long-lived demo. Without `--pin`,
  behaviour is unchanged (a fresh ephemeral name each time).
- **The name is still server-derived, never user-chosen.** The client stores a
  random secret once at `~/.config/viaduct/pin.key` (mode 0600) and sends
  `sha256(secret:local_port)` in the hello frame. The server derives an
  `adjective-animal-xxxx` name deterministically from that seed. The user cannot
  choose the string, so squatting or impersonation (`login`, a brand name) stays
  impossible. Different local ports yield different stable names.
- **Still no server state and no database.** The server persists nothing: the
  same seed always derives the same name, so stability is a pure function of the
  client's secret. If the derived name is already held by another live tunnel,
  the server rejects with `pin_in_use` and the client reports it and exits.

## Amendment (2026-08-02): data-connection capability token

Closes a hijack hole in the no-auth model without adding user auth. On
registration the server generates a random per-tunnel token and returns it to
the owning client in the `ok` frame (over the TLS control channel). Every
`data_hello` must then present it, so knowing a tunnel's public subdomain is not
enough to attach a data connection and intercept or serve its traffic. This is
an internal binding, not a user credential: there is still no signup, no
`token create`, and no database.

## Amendment (2026-08-02): custom domains via CNAME (bring your own domain)

Re-introduces custom domains from the original spec, but stateless: no `domains`
table, no `viaduct domain` command, no client flag. The mapping lives entirely in
the user's DNS.

- **Setup.** Pin a tunnel (`viaduct http 8080 --pin` gives a stable
  `name.BASE_DOMAIN`), then CNAME your domain to it:
  `demo.example.com  CNAME  name.BASE_DOMAIN`.
- **Routing.** For a Host that is not `*.BASE_DOMAIN`, viaductd resolves the
  host's CNAME chain (a small stdlib DNS query in `dns.py`, cached ~60s) back to
  a `name.BASE_DOMAIN` label and routes to that tunnel. An A record straight to
  the droplet does not work, there must be a CNAME to follow.
- **TLS.** Caddy on-demand TLS issues a cert for the custom domain on first hit,
  gated by an `ask` endpoint (`GET /_viaduct/tls-check`) that viaductd answers
  200 only when the domain resolves to a live tunnel, so it is not an open cert
  mill. Issuance uses HTTP-01, which only succeeds if the domain already points
  at the droplet, so you can only serve domains whose DNS you control.
- **Cert pre-warm.** The first time the ask endpoint sees a live custom domain,
  viaductd opens one throwaway TLS handshake to the local Caddy for it (deduped
  per domain for a short window). Caddy locks issuance per name, so this drives
  the ACME round-trip to completion from a stable in-process client, and the
  first real visitor gets a ready cert instead of eating the multi-second wait.
- **Still no state.** The binding is the user's CNAME plus the live in-memory
  tunnel; nothing is persisted, and a removed CNAME simply stops resolving.

## Amendment (2026-08-04): opt-in per-tunnel access control (Basic / Bearer / IP)

Adds optional access control that a tunnel owner puts on their own tunnel. This
does **not** revive the original "auth token is always required" non-goal:
tunnels are still anonymous and public by default. This is a gate the owner
chooses to add to a shared link, not a login to the service. Still no accounts,
no `token create`, no database.

- **Flags on `viaduct http`:** `--basic-auth USER:PASS` (HTTP Basic), `--bearer
  TOKEN` (Authorization: Bearer), and `--allow-ip CIDR` (repeatable IP/CIDR
  allowlist). They compose; any combination gates the tunnel. Also
  `--auth-message` / `--deny-message` (custom 401 / 403 body copy) and
  `--auth-realm`. Credentials can come from the flag, `VIADUCT_BASIC_AUTH` /
  `VIADUCT_BEARER`, or `~/.config/viaduct/config.toml`; `--basic-auth USER` with
  no password prompts for it rather than putting it in argv.
- **Enforced server-side, at the edge.** viaductd checks each request in
  `_handle_public` *before* it acquires a data connection, so an unauthorised
  request is answered with a branded 401/403 and never reaches the owner's
  machine. The check reuses the head viaductd already parses to route by Host, so
  the data path is untouched (no cost once authorised).
- **Only hashes travel; nothing is persisted.** The client puts an opaque `auth`
  dict in the hello frame carrying `sha256` of the Basic/Bearer credential (never
  plaintext) plus the allowlist and messages. viaductd reconstructs a
  `TunnelAuth` held only on the live in-memory tunnel; it stores no secret and
  writes nothing to disk. Compares are constant-time (`hmac.compare_digest`).
- **Version-skew safety.** The `ok` frame echoes `auth_enforced: true` when a
  tunnel is actually gated; a client that requested auth but does not see the
  echo raises `auth_unsupported` and refuses to run, so a new client can never
  sit unprotected in front of an old server.
- **IP allowlist and the trusted front.** The visitor address is the last
  `X-Forwarded-For` hop set by Caddy (spoof-resistant behind the front). With no
  XFF it fails closed. An operator self-hosting viaductd with no trusted front
  passes `viaductd --trust-peer-ip` to use the direct socket address instead.
- **Extras:** owner-supplied messages are HTML-escaped into the branded error
  page; a light per-IP failed-auth throttle (429) slows brute force. All of this
  lives in `auth.py`; there is still no database and no user accounts.

## Amendment (2026-08-07): opt-in user-chosen subdomain (`--name`)

Reverses "no user-chosen subdomains" from the 2026-07-31 amendment for one
opt-in case, with all validation server-side and still no database.

- **`viaduct http PORT --name SUBDOMAIN`** requests a specific subdomain and pins
  it (stable across reconnects, like `--pin`) if it is free. It is for someone
  who wants a memorable URL they picked. Without `--name`, behaviour is unchanged
  (a random ephemeral name, or a server-derived one with `--pin`). `--name` and
  `--pin` are mutually exclusive.
- **All validation is server-side and authoritative; the client ships no
  wordlist.** The client puts the requested `name` in the hello frame. viaductd
  then, in order: normalises and checks syntax (a single DNS label, 3 to 63 chars
  of `[a-z0-9-]`, no leading, trailing, or double hyphen, not all-numeric);
  rejects a small reserved set (region codes, `www/api/admin/...`); screens for
  profanity; and checks availability. Failures return `name_invalid`,
  `name_rejected`, or `name_taken`, all fatal; the client reports and exits.
- **Profanity screen.** Words from a public multi-language list
  (github.com/censor-text/profanity-list) ship only as peppered hashes
  (`src/viaduct/data/profanity.txt`, regenerated by `tools/build_profanity.py`),
  so no plaintext slur list lives in the repo; the hashing is obfuscation, not
  security. A requested name is "exploded" into hyphen segments and substrings,
  each hashed and looked up. Substring matching is deliberately blunt and can flag
  an innocent name that merely contains a bad word; a minimum substring length
  curbs the worst of it.
- **Availability is per server, not fleet-wide.** A name is unique on the viaductd
  you connect to; the same name may exist on another region (whose host is
  region-suffixed anyway). Reserving names globally across regions was considered
  and deliberately left out, to avoid introducing inter-region coordination.
- **Still no persistence.** The reservation is just the live tunnel occupying the
  name in the server's in-memory table, freed on disconnect. Nothing is written to
  disk and there is no ownership or accounts model.

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
