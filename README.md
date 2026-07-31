# viaduct.sh

Open-source reverse tunneling for local development. Expose a service running on
your machine at a public HTTPS hostname with one command, no inbound ports or
NAT configuration required.

The `viaduct` client dials out to a `viaductd` server you run on a small VPS.
The server terminates public HTTPS at Caddy and pipes traffic back down the
tunnel to your `localhost`.

- HTTP and WebSocket tunneling
- Auth tokens, stored only as sha256 hashes (one token can open many tunnels)
- A random friendly subdomain (e.g. `funny-otter.viaduct.sh`) assigned per tunnel
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

# 2. Mint an auth token (printed once; not tied to any subdomain)
viaductd token create --db ./viaduct.db

# 3. Start the server: public traffic on :8080, tunnel connections on :4443
viaductd --db ./viaduct.db --base-domain localhost

# 4. In another shell, start something to expose and open the tunnel.
#    The server assigns a random name and the client prints the URL, e.g.
#    "tunnel up funny-otter.localhost -> 127.0.0.1:3000"
python3 -m http.server 3000
viaduct http 3000 --token <token-from-step-2>

# 5. Reach it through the tunnel (use the name the client printed)
curl -H 'Host: funny-otter.localhost' http://127.0.0.1:8080/
```

Instead of passing flags, the client reads `~/.config/viaduct/config.toml`:

```toml
server = "127.0.0.1:4443"
token  = "<token>"
# tls   = true            # enable when talking to a real server over 4443
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

SSH in as root (or `sudo -i`), then run the steps below top to bottom.

```sh
# packages
apt update && apt install -y git python3-venv python3-pip ufw curl golang-go

# code + CLIs
git clone https://github.com/webmull/viaduct /opt/viaduct
cd /opt/viaduct
python3 -m venv .venv
.venv/bin/pip install .
ln -sf /opt/viaduct/.venv/bin/viaductd /usr/local/bin/viaductd
ln -sf /opt/viaduct/.venv/bin/viaduct  /usr/local/bin/viaduct

# firewall: default deny, SSH source-restricted, plus 80/443 and the tunnel port
ufw default deny incoming
ufw allow from <your-ip> to any port 22 proto tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 4443/tcp
ufw --force enable

# open-file limit and listen backlog (survives a burst of connections)
printf '*  soft  nofile  65535\n*  hard  nofile  65535\n' >> /etc/security/limits.conf
echo 'net.core.somaxconn = 1024' > /etc/sysctl.d/90-viaduct.conf
sysctl --system

# build Caddy with the DigitalOcean DNS plugin
go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
~/go/bin/xcaddy build --with github.com/caddy-dns/digitalocean --output /usr/local/bin/caddy
useradd --system --create-home --home-dir /var/lib/caddy --shell /usr/sbin/nologin caddy
install -d -o caddy -g caddy /etc/caddy
cp deploy/Caddyfile /etc/caddy/Caddyfile

# service account for viaductd, with read access to Caddy's managed certs
useradd --system --home-dir /var/lib/viaduct --shell /usr/sbin/nologin viaduct
usermod -aG caddy viaduct
install -d -o viaduct -g viaduct /var/lib/viaduct

# secrets and cert paths (see deploy/setup.md for the exact wildcard cert paths)
mkdir -p /etc/viaduct
cat > /etc/viaduct/viaductd.env <<'EOF'
DO_API_TOKEN=<do-api-token>
TLS_CERT=/var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory/wildcard_.viaduct.sh/wildcard_.viaduct.sh.crt
TLS_KEY=/var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory/wildcard_.viaduct.sh/wildcard_.viaduct.sh.key
EOF
chmod 640 /etc/viaduct/viaductd.env && chown root:viaduct /etc/viaduct/viaductd.env
cp /etc/viaduct/viaductd.env /etc/viaduct/caddy.env && chown root:caddy /etc/viaduct/caddy.env

# landing page (served by Caddy at https://viaduct.sh)
mkdir -p /var/www/viaduct-site && cp site/* /var/www/viaduct-site/

# systemd units + monthly cert-refresh restart timer
cp deploy/caddy.service deploy/viaductd.service \
   deploy/viaductd-restart.service deploy/viaductd-restart.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now caddy viaductd viaductd-restart.timer
```

### 3. Issue tokens and connect

On the droplet, mint a token per user (the `--label` is just a reminder note):

```sh
viaductd token create --label alice --db /var/lib/viaduct/viaduct.db
```

On the local machine, put the server, token, and `tls = true` in
`~/.config/viaduct/config.toml`, then:

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
