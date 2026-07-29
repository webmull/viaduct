# Droplet setup — M3 (TLS)

One Ubuntu droplet runs Caddy (public HTTPS) and viaductd. Systemd units,
ulimit/sysctl tuning and the renewal-restart timer land in M5; this document
covers DNS, firewall, and TLS.

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
ufw allow 4443/tcp   # viaduct tunnel listener (TLS, terminated by viaductd)
ufw enable
```

Port 4443 is the one departure from the original 22/80/443 plan: client tunnel
connections are not HTTP, so they cannot ride through Caddy. viaductd
terminates TLS on this port itself (stdlib `ssl`) so tokens never cross the
internet in plaintext.

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
token = "<from: viaductd token create --subdomain pmesh>"
tls = true
```

Then:

```sh
viaduct http 3000 --subdomain pmesh
# -> tunnel up pmesh.viaduct.sh — public URL https://pmesh.viaduct.sh
```
