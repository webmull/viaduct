# viaduct.sh

Open-source reverse tunneling for local development. Expose a service running on
your machine at a public HTTPS hostname with one command, no inbound ports or
NAT configuration required.

The `viaduct` client dials out to a `viaductd` server you run on a small VPS.
The server terminates public HTTPS at Caddy and pipes traffic back down the
tunnel to your `localhost`.

- HTTP and WebSocket tunneling
- A random friendly subdomain (e.g. `funny-otter.viaduct.sh`) assigned per tunnel
- No auth and no database — dead simple. Anyone who can reach the tunnel port
  gets a tunnel, so restrict that port at the firewall (see below)
- Self-hosted, designed for one DigitalOcean droplet and a handful of trusted users

Full design is in [`SPEC.md`](SPEC.md); the complete droplet runbook is in
[`deploy/setup.md`](deploy/setup.md).

## Requirements

- Python 3.11 or newer
- Runtime dependencies `typer` and `rich` (installed automatically)

## Run locally

Everything runs on one machine for development: no TLS, base domain `localhost`.

```sh
# 1. Install the CLIs from a clone of this repo
python3 -m venv .venv && . .venv/bin/activate
pip install .

# 2. Start the server: public traffic on :8080, tunnel connections on :4443
viaductd --base-domain localhost

# 3. In another shell, start something to expose and open the tunnel.
#    The server assigns a random name and the client prints the URL, e.g.
#    "tunnel up funny-otter.localhost -> 127.0.0.1:3000"
python3 -m http.server 3000
viaduct http 3000

# 4. Reach it through the tunnel (use the name the client printed)
curl -H 'Host: funny-otter.localhost' http://127.0.0.1:8080/
```

Instead of passing flags, the client reads `~/.config/viaduct/config.toml`:

```toml
server = "127.0.0.1:4443"
# tls  = true            # enable when talking to a real server over 4443
```

## Provision on DigitalOcean

One Ubuntu droplet runs Caddy (public HTTPS) and `viaductd`. This section is the
condensed path; [`deploy/setup.md`](deploy/setup.md) has the exhaustive version
including the Caddy build and certificate paths.

### 1. Create the droplet and DNS

- Create an Ubuntu 24.04 droplet (1 GB is plenty).
- Point DNS at its IP (DigitalOcean DNS or your registrar):
  ```
  A      viaduct.sh    -> <droplet-ip>
  CNAME  *.viaduct.sh  -> viaduct.sh
  ```
- Create a DigitalOcean API token with DNS write scope. It is used for ACME
  DNS-01 wildcard certificates and lives only on the droplet, never in the repo.

### 2. Provision the droplet (run as root)

SSH in as root (or `sudo -i`) and run the same script the local rehearsal uses:

```sh
apt update && apt install -y git
git clone https://github.com/webmull/viaduct /opt/viaduct
/opt/viaduct/deploy/provision.sh          # prompts for your DO API token
```

It prompts for your DigitalOcean API token (input hidden) and **validates it
against the DO API before continuing**, so a bad or truncated paste fails right
there instead of turning into a Caddy certificate loop. (You can also pass it
non-interactively: `DO_API_TOKEN=... /opt/viaduct/deploy/provision.sh`.)

That single script installs the CLIs, downloads Caddy prebuilt with the
DigitalOcean DNS plugin (no Go build, so no OOM on a 1 GB box), creates the
`caddy`/`viaduct` users, writes the Caddyfile and env files, installs the systemd
units, tunes limits, opens the firewall, and starts everything. viaductd comes up
once Caddy has obtained the wildcard cert (a minute or two on first boot; it
retries automatically).

Rehearse the exact same script locally first — see
[`deploy/local/`](deploy/local/). [`deploy/setup.md`](deploy/setup.md) annotates
what each step does.

The firewall it sets up opens the tunnel port (4443) to the internet. Since there
is no auth, restrict it to your users' IPs by hand afterward if that matters
(`ufw delete allow 4443/tcp` then `ufw allow from <ip> to any port 4443 proto tcp`).

### 3. Connect

There are no tokens to issue. On the local machine, put the server and
`tls = true` in `~/.config/viaduct/config.toml`, then:

```sh
viaduct http 3000
# tunnel up: https://funny-otter.viaduct.sh -> localhost:3000
```

The server assigns a fresh random name for each tunnel and frees it when the
tunnel drops, so reconnecting gives a new URL. Names never collide between
concurrent tunnels.

`viaductd` drains active connections gracefully on restart, so
`systemctl restart viaductd` and the monthly cert-refresh timer are safe during
live traffic; clients reconnect automatically with backoff.
