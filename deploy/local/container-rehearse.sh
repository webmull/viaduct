#!/usr/bin/env bash
# High-fidelity rehearsal: run the SAME deploy/provision.sh the droplet uses,
# inside a systemd container, then prove the full stack end to end.
#
# This reproduces the droplet's systemd + multi-user + file-permission
# environment — including the caddy/viaduct permission split behind the earlier
# 502 — which the Mac-native rehearsal (deploy/local/rehearse.sh) cannot.
#
# Requires a Docker daemon (e.g. Colima: `colima start`). TLS uses Caddy's
# internal CA over a fake viaduct.test domain, so no public DNS or DO token.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE=viaduct-rehearse:latest
NAME=viaduct-rehearse
BASE=viaduct.test

docker info >/dev/null 2>&1 || { echo "no docker daemon — run 'colima start' (or start Docker) first"; exit 1; }

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "== build systemd image =="
docker build -q -t "$IMAGE" -f "$REPO/deploy/local/Dockerfile" "$REPO/deploy/local" || exit 1

echo "== boot systemd container (repo mounted at /opt/viaduct) =="
docker run -d --name "$NAME" --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  -v "$REPO":/opt/viaduct:ro \
  --tmpfs /run --tmpfs /run/lock \
  "$IMAGE" >/dev/null || exit 1

for _ in $(seq 1 60); do docker exec "$NAME" systemctl list-units >/dev/null 2>&1 && break; sleep 1; done
docker exec "$NAME" systemctl list-units >/dev/null 2>&1 || { echo "systemd never came up"; docker logs "$NAME" | tail; exit 1; }

echo "== run provision.sh (BASE_DOMAIN=$BASE TLS_MODE=internal) =="
docker exec \
  -e BASE_DOMAIN="$BASE" -e TLS_MODE=internal -e SETUP_FIREWALL=no -e REPO_DIR=/opt/viaduct \
  "$NAME" bash /opt/viaduct/deploy/provision.sh || { echo "provision failed"; exit 1; }

echo "== wait for caddy + viaductd to be active =="
for svc in caddy viaductd; do
  ok=0
  for _ in $(seq 1 60); do docker exec "$NAME" systemctl is-active --quiet "$svc" && { ok=1; break; }; sleep 1; done
  if [ "$ok" != 1 ]; then
    echo "FAIL: $svc not active"; docker exec "$NAME" systemctl status "$svc" --no-pager -l | head -30
    echo "--- $svc journal ---"; docker exec "$NAME" journalctl -u "$svc" -n 30 --no-pager
    exit 1
  fi
  echo "   $svc: active"
done

echo "== in-container smoke test (demo app + client + HTTPS through Caddy) =="
docker exec "$NAME" bash -s <<'SMOKE'
set -uo pipefail
BASE=viaduct.test
ROOT=/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
mkdir -p /tmp/webroot; echo "viaduct container rehearsal OK" > /tmp/webroot/index.html
( cd /tmp/webroot && python3 -m http.server 3000 >/tmp/app.log 2>&1 & )
grep -q "$BASE" /etc/hosts || echo "127.0.0.1 $BASE" >> /etc/hosts
NO_COLOR=1 /usr/local/bin/viaduct http 3000 --server "$BASE:4443" --tls --tls-ca "$ROOT" --pool-size 5 >/tmp/client.log 2>&1 &
HOST=""
for _ in $(seq 1 100); do HOST=$(grep -oE "[a-z0-9-]+\.$BASE" /tmp/client.log | head -1); [ -n "$HOST" ] && break; sleep 0.1; done
[ -n "$HOST" ] || { echo "FAIL: client got no hostname"; cat /tmp/client.log; exit 1; }
echo "   server assigned: https://$HOST/"
body=""
for _ in $(seq 1 60); do
  body=$(curl -sS --cacert "$ROOT" --resolve "$HOST:443:127.0.0.1" "https://$HOST/" 2>/dev/null)
  echo "$body" | grep -q "viaduct container rehearsal OK" && break
  sleep 0.3
done
code=$(curl -sS -o /dev/null -w '%{http_code}' --cacert "$ROOT" --resolve "$HOST:443:127.0.0.1" "https://$HOST/" 2>/dev/null)
code404=$(curl -sS -o /dev/null -w '%{http_code}' --cacert "$ROOT" --resolve "nope-nobody.$BASE:443:127.0.0.1" "https://nope-nobody.$BASE/" 2>/dev/null)
echo "   GET https://$HOST/            -> HTTP $code   body: '$body'"
echo "   GET https://nope-nobody.$BASE/ -> HTTP $code404 (want 404)"
echo "$body" | grep -q "viaduct container rehearsal OK" && [ "$code" = 200 ] && [ "$code404" = 404 ]
SMOKE
rc=$?
[ "$rc" = 0 ] || { echo "❌ internal-mode smoke test failed"; exit 1; }

echo "== validate the DigitalOcean (letsencrypt) config path — no live ACME =="
docker exec -e TLS_MODE=letsencrypt -e BASE_DOMAIN=viaduct.sh "$NAME" bash -c '
  set -e
  arch=$(dpkg --print-architecture)
  curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=${arch}&p=github.com/caddy-dns/digitalocean" -o /usr/local/bin/caddy-do
  chmod +x /usr/local/bin/caddy-do
  /usr/local/bin/caddy-do list-modules 2>/dev/null | grep -q dns.providers.digitalocean && echo "   DO DNS plugin present in prebuilt Caddy"
  source /opt/viaduct/deploy/provision.sh   # loads functions only (does not provision)
  do_caddyfile                              # generate the letsencrypt Caddyfile exactly as a droplet would
  /usr/local/bin/caddy-do adapt --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null \
    && echo "   letsencrypt Caddyfile is valid (dns digitalocean directive recognized)"
' || { echo "❌ letsencrypt config validation failed"; exit 1; }

echo
echo "✅ CONTAINER REHEARSAL PASSED"
echo "   internal mode:    full HTTPS -> Caddy -> viaductd -> tunnel -> app, under systemd."
echo "   letsencrypt mode: DO DNS plugin + Caddyfile validated (only live LE issuance needs a real droplet)."
