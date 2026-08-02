# viaduct

Self-hosted reverse tunneling in a couple thousand lines of Python. Give a service
running on your machine a public HTTPS URL with one command, no inbound ports,
port forwarding, or NAT configuration.

```console
$ viaduct http 8080
         _           _            _
  __   _(_) __ _  __| |_   _  ___| |_
  \ \ / / |/ _` |/ _` | | | |/ __| __|
   \ V /| | (_| | (_| | |_| | (__| |_
    \_/ |_|\__,_|\__,_|\__,_|\___|\__|  .sh
  self-hosted reverse tunnel

╭───────────────────────────────────────────────────────────────────────╮
│ Forwarding   https://funny-otter.viaduct.sh  →  http://127.0.0.1:8080  │
╰───────────────────────────────────────────────────────────────────────╯
  Ctrl+C to stop
```

The `viaduct` client dials out to a `viaductd` server you run on a small VPS.
The server terminates public HTTPS at [Caddy](https://caddyserver.com) and pipes
raw bytes back down the tunnel to your `localhost`. It is a deliberately small
alternative to ngrok and frp: standard-library Python, no database, no accounts,
and only `typer` and `rich` as runtime dependencies.

## Why viaduct

- **Self-hosted and yours.** One droplet, your domain, your data. Nothing routes
  through a third party.
- **Small enough to read.** The whole client and server are plain asyncio and the
  standard library. If it does something surprising, you can go read why.
- **No accounts, by design.** No tokens, no signup, no database. You control who
  can reach it at the firewall instead.
- **Fast where it counts.** A tunnel to a nearby server is a single hop, so
  latency stays low for interactive use and demos.

## Features

- HTTP and WebSocket tunneling over a single outbound connection
- A random, friendly subdomain per tunnel (for example `funny-otter.viaduct.sh`),
  freed on disconnect
- Automatic public TLS via Caddy and Let's Encrypt (DNS-01 wildcard)
- Graceful drain on server restart and automatic client reconnect with backoff
- A per-source-IP tunnel cap as an abuse safeguard for a no-auth server
- Self-update tracking PyPI releases, off by default

## Install

Requires Python 3.11 or newer. [pipx](https://pipx.pypa.io) keeps the CLI in its
own isolated environment and puts `viaduct` on your `PATH`:

```sh
pipx install viaduct-sh
```

The PyPI distribution is named `viaduct-sh` (the bare `viaduct` name was already
taken), and it installs both the `viaduct` client and the `viaductd` server. To
track the latest commit instead of a release, install straight from git with
`pipx install git+https://github.com/webmull/viaduct`.

The client targets the hosted `viaduct.sh` server by default, so you can go
straight to:

```sh
viaduct http 8080
```

## Usage

Point `viaduct http` at any local port. It prints a public HTTPS URL and forwards
traffic to that port until you stop it with Ctrl+C:

```sh
viaduct http 8080
# tunnel up: https://funny-otter.viaduct.sh → http://127.0.0.1:8080
```

The client dials out, so it never needs your machine's IP or any open inbound
port. If nothing is listening on the port yet, it warns but still opens the
tunnel, and serves `502` until your app comes up.

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--server host:port` | `viaduct.sh:4443` | The `viaductd` server to dial |
| `--region lon\|nyc\|sg\|syd\|blr` | none | Pick a server region (shortcut for `--server`) |
| `--tls` / `--no-tls` | on, off for localhost | TLS to the tunnel port |
| `--tls-ca PATH` | none | Extra CA bundle to trust (for a self-signed server) |
| `--pool-size N` | 40 | Idle data connections kept ready for incoming requests |
| `--inspect` | off | Log each request: method, path, status, and time |
| `--pin` | off | Keep the same public URL across reconnects (stable subdomain) |
| `--host-header HOST` | off | Rewrite the `Host` header your app sees (e.g. `localhost`), for dev servers that reject unknown hosts |

By default every reconnect gets a fresh random URL. `--pin` keeps the same URL
for a given local port across reconnects, which is handy for a webhook endpoint
or a long-lived demo. The name is still server-assigned (you can't choose the
string); it is derived from a random secret stored once at
`~/.config/viaduct/pin.key`, so it stays stable without any server-side state.

Flags can live in `~/.config/viaduct/config.toml` instead:

```toml
server = "my-server.example.com:4443"
# tls  = true                 # override the on-except-localhost default if needed
# host_header = "localhost"   # rewrite the Host header sent to your local app
```

Some dev servers (Vite, Next.js, Django, Rails, …) keep an allow-list of hostnames
and reject a request whose `Host` is the public tunnel URL with an error like
"Invalid Host header" or "Blocked request". `--host-header localhost` makes your
app see the request as if it arrived on `localhost`, so it stops rejecting them,
while visitors still use the public URL.

### Managing tunnels

Each `viaduct http` runs in its own process. To see and stop the tunnels you have
open on this machine, from any shell:

```sh
viaduct list                 # name, port, URL, uptime, pid
viaduct kill funny-otter     # stop one by name (or pid)
viaduct kill --all           # stop them all
```

These are machine-local: they only see the tunnels you started on this box.

## From your code

Open a tunnel without the CLI. Handy for tests that need a real public URL
(webhooks) and for scripts.

```python
import viaduct

async with viaduct.tunnel(8080) as t:      # async
    print(t.url)                           # https://funny-otter.viaduct.sh

with viaduct.tunnel_sync(8080) as t:       # sync (scripts, notebooks)
    print(t.url)
```

Installing `viaduct-sh` also registers a **pytest fixture**, `viaduct_tunnel`,
that gives a test a real public URL and tears it down afterwards:

```python
def test_stripe_webhook(viaduct_tunnel):
    url = viaduct_tunnel(8000)             # your local test server is now public
    stripe.WebhookEndpoint.create(url=url + "/hook")
    ...                                    # torn down automatically
```

There is a **Node client** too (zero dependencies), published as `viaduct-sh` on
npm and living in [`node/`](node/):

```js
import { tunnel } from "viaduct-sh";
const t = await tunnel(3000);
console.log(t.url);
// or, no install:  npx viaduct-sh 3000
```

## Run your own server

The client defaults to the hosted `viaduct.sh`, but the point of viaduct is that
you run your own. One Ubuntu droplet runs Caddy (public HTTPS) and `viaductd`.
This is the condensed path; [`deploy/setup.md`](deploy/setup.md) has the exhaustive
version, and you can rehearse the exact same script locally first with
[`deploy/local/`](deploy/local/).

**1. Create a droplet and point DNS at it.** An Ubuntu 24.04 droplet with 1 GB of
RAM is plenty.

```
A      your-domain.com    -> <droplet-ip>
CNAME  *.your-domain.com  -> your-domain.com
```

Create a DigitalOcean API token with DNS write scope. It is used for ACME DNS-01
wildcard certificates and lives only on the droplet, never in the repo.

**2. Provision it (as root).**

```sh
apt update && apt install -y git
git clone https://github.com/webmull/viaduct /opt/viaduct
BASE_DOMAIN=your-domain.com /opt/viaduct/deploy/provision.sh
```

The script prompts for the API token (input hidden) and validates it against the
DigitalOcean API before continuing, so a bad paste fails immediately instead of
turning into a certificate loop. It then installs the CLIs, downloads Caddy
prebuilt with the DigitalOcean DNS plugin (no Go build, so no out-of-memory on a
small box), creates the service users, writes the Caddyfile and systemd units,
tunes limits, opens the firewall, and starts everything. `viaductd` comes up once
Caddy has the wildcard certificate, a minute or two on first boot.

**3. Connect.** Point the client at your server and open a tunnel:

```sh
viaduct http 3000 --server your-domain.com:4443
```

There is no auth, so restrict the tunnel port to trusted IPs at the firewall if
that matters:

```sh
ufw delete allow 4443/tcp
ufw allow from <your-ip> to any port 4443 proto tcp
```

`viaductd` drains active connections gracefully on restart, so `systemctl restart
viaductd` and the monthly certificate refresh are safe during live traffic.

## How it works

```
visitor ──HTTPS──▶ Caddy ──▶ viaductd ──tunnel──▶ viaduct ──▶ your app
(public)           (TLS)     (server)   (1 hop)   (client)    127.0.0.1:8080
```

The client opens one control connection to the server and keeps a small pool of
idle data connections ready. When a request arrives, the server hands it to a
free data connection, and the client splices it to your local port as raw bytes.
There is no HTTP parsing or multiplexing on the tunnel itself, which keeps the
code small and the path fast. The pool grows under bursts and drains back down
when they pass.

## Updating

```sh
viaduct --version
viaduct upgrade          # reinstall the latest release from PyPI
```

`viaduct upgrade` reinstalls the latest `viaduct-sh` release from PyPI via pipx.
On an interactive terminal the client also prints a one-line notice, at most once
a day, when a newer release is out (silence it with `VIADUCT_NO_UPDATE_CHECK=1`).
Automatic upgrades are off by default; set `VIADUCT_AUTO_UPGRADE=1` (or
`auto_upgrade = true` in `config.toml`) to have the client jump to the latest
release on startup.

## Development

```sh
git clone https://github.com/webmull/viaduct && cd viaduct
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

python -m pytest          # tests
python -m ruff check      # lint
```

Everything can run on one machine, no TLS, base domain `localhost`:

```sh
# server: public traffic on :8080, tunnel connections on :4443
viaductd --base-domain localhost

# in another shell: expose something and open a tunnel to the local server
python3 -m http.server 3000
viaduct http 3000 --server 127.0.0.1:4443

# reach it through the tunnel (pass the Host header the client printed)
curl -H 'Host: funny-otter.localhost' http://127.0.0.1:8080/
```

## Scope and non-goals

viaduct is intentionally minimal and means to stay that way. It does not aim to
be a hosted multi-tenant service, it does not multiplex many streams over one
connection, it has no user accounts or dashboards, and it targets a single server
serving a handful of trusted users rather than a global edge network. If you need
those, ngrok and frp exist and do them well.

## Releasing

To promote a commit to the `stable` channel, bump `version` in `pyproject.toml`
(that string, read at the `stable` ref, is how clients decide they are behind),
then move the tag:

```sh
git tag -f stable <commit>
git push -f origin stable
```

## License

Released under the [MIT License](LICENSE).
