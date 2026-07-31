# Droplet setup

One Ubuntu droplet runs Caddy (public HTTPS) and viaductd. This document
covers DNS, firewall, TLS, limits, and the systemd units.

## 1. DNS (DigitalOcean)

```
A      viaduct.sh    -> <droplet-ip>
CNAME  *.viaduct.sh  -> viaduct.sh
```

Create a DigitalOcean API token scoped to DNS writes (used for ACME DNS-01
challenges). It lives only on the droplet — never in this repo.

## 2. Firewall

```sh
ufw default deny incoming
ufw allow from <your-ip> to any port 22 proto tcp   # SSH, source-restricted
ufw allow 80/tcp     # ACME HTTP fallback + redirect
ufw allow 443/tcp    # public HTTPS (Caddy)
ufw allow from <trusted-ip> to any port 4443 proto tcp   # tunnel port (see note)
ufw enable
```

Port 4443 is the one departure from the original 22/80/443 plan: client tunnel
connections are not HTTP, so they cannot ride through Caddy. viaductd terminates
TLS on this port itself (stdlib `ssl`) so tunneled traffic is encrypted in
transit. There is **no auth** on the tunnel: anyone who can reach 4443 can open
a tunnel, so restrict it to your users' source IPs where you can. Use plain
`ufw allow 4443/tcp` only if you accept an internet-open tunnel port.

## 3. Caddy with the DigitalOcean DNS plugin

```sh
xcaddy build --with github.com/caddy-dns/digitalocean
install -m 755 caddy /usr/local/bin/caddy
```

Put the API token in `/etc/viaduct/caddy.env` (`root:caddy`, mode 640):

```
DO_API_TOKEN=<token>
```

Run Caddy with `deploy/Caddyfile`. The first request for any `*.viaduct.sh`
host triggers wildcard issuance via DNS-01; nothing else to do.

## 4. TLS on the tunnel listener (port 4443)

viaductd reuses the wildcard certificate Caddy manages. Caddy stores it under
its data directory, e.g.:

```
/var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory/wildcard_.viaduct.sh/wildcard_.viaduct.sh.crt
/var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory/wildcard_.viaduct.sh/wildcard_.viaduct.sh.key
```

- Add the `viaduct` user to the `caddy` group so the key is readable.
- Start the server with:

```sh
viaductd --bind 0.0.0.0 --base-domain viaduct.sh \
    --tls-cert <path>.crt --tls-key <path>.key
```

(Public port stays bound to localhost via Caddy's `reverse_proxy 127.0.0.1:8080`;
`--bind 0.0.0.0` is needed so the tunnel listener is reachable. If binding both
to all interfaces bothers you, front 8080 with a localhost-only rule in ufw —
it is already not exposed since ufw only opens 80/443/4443.)

Let's Encrypt rotates certs roughly every 60 days. viaductd loads the cert at
startup, so restart it after renewal — M5 adds a monthly systemd restart timer.
A stale cert shows up client-side as a certificate-expired error.

## 5. Client machine

`~/.config/viaduct/config.toml`:

```toml
server = "viaduct.sh:4443"
tls = true
```

Then:

```sh
viaduct http 3000
# -> tunnel up funny-otter.viaduct.sh — public URL https://funny-otter.viaduct.sh
```

The server picks a fresh random name for each tunnel and frees it on
disconnect, so reconnecting yields a new URL. Names never collide between
concurrent tunnels.

## 6. Limits and kernel tuning

The failure mode that matters is an audience scanning a QR code at once —
hundreds of TCP connections arriving in a burst.

```sh
# File descriptors: the systemd units set LimitNOFILE=65535 for the daemons.
# For interactive shells too:
cat >> /etc/security/limits.conf <<'EOF'
*  soft  nofile  65535
*  hard  nofile  65535
EOF

# Listen backlog: the kernel default of 128 drops connections under burst.
# viaductd asks for a backlog of 512; somaxconn caps it, so raise it.
cat > /etc/sysctl.d/90-viaduct.conf <<'EOF'
net.core.somaxconn = 1024
EOF
sysctl --system
```

## 7. Systemd units

```sh
useradd --system --home-dir /var/lib/viaduct --shell /usr/sbin/nologin viaduct
usermod -aG caddy viaduct   # read access to Caddy's managed certificates

install -m 644 deploy/caddy.service deploy/viaductd.service \
    deploy/viaductd-restart.service deploy/viaductd-restart.timer \
    /etc/systemd/system/
mkdir -p /etc/viaduct

# viaductd only needs the cert paths (no auth, no DB)
cat > /etc/viaduct/viaductd.env <<'EOF'
TLS_CERT=/var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory/wildcard_.viaduct.sh/wildcard_.viaduct.sh.crt
TLS_KEY=/var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory/wildcard_.viaduct.sh/wildcard_.viaduct.sh.key
EOF
chmod 640 /etc/viaduct/viaductd.env && chown root:viaduct /etc/viaduct/viaductd.env

# Caddy needs the DigitalOcean API token for DNS-01
echo 'DO_API_TOKEN=<token>' > /etc/viaduct/caddy.env
chmod 640 /etc/viaduct/caddy.env && chown root:caddy /etc/viaduct/caddy.env

systemctl daemon-reload
systemctl enable --now caddy viaductd viaductd-restart.timer
```

`viaduct.service` is for the *local* machine running the client — install it
there, edit the port in ExecStart, and put server/tls in that user's
`~/.config/viaduct/config.toml`.

viaductd drains gracefully on SIGTERM: it stops accepting, gives in-flight
transfers 30s to finish, then exits, so `systemctl restart viaductd` (and the
monthly cert-refresh timer) is safe during live traffic; clients reconnect
with 1s to 30s backoff.

## 8. Landing page

The coming-soon page in `site/` is served by Caddy at `https://viaduct.sh`
(`www` redirects to the apex):

```sh
mkdir -p /var/www/viaduct-site
cp site/* /var/www/viaduct-site/
```

## 9. Load test

From any machine (500 concurrent WebSocket upgrades, time-to-established). Use
whatever name `viaduct http` printed as the Host header:

```sh
python scripts/load_test.py --host <droplet-ip-or-127.0.0.1> --port 8080 \
    --hostname funny-otter.viaduct.sh --connections 500
```
