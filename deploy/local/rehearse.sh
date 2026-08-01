#!/usr/bin/env bash
# Local rehearsal of the viaduct production topology, minus Let's Encrypt/DNS-01.
#
# Stands up the exact request path the droplet uses:
#
#   curl --HTTPS--> Caddy (:8443, internal CA) --plaintext--> viaductd (:8080)
#                                                                  |
#                                                    tunnel (:4443, TLS)
#                                                                  |
#                                                       viaduct client
#                                                                  |
#                                                       demo app (:3000)
#
# and proves a real HTTPS request to a server-assigned *.viaduct.test name
# round-trips all the way to the local app. High ports are used so no sudo is
# needed; production binds 80/443 instead (via CAP_NET_BIND_SERVICE in systemd).
#
# Re-runnable. Everything lives under deploy/local/.run and .cache (gitignored).
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="$REPO/.venv/bin"
HERE="$REPO/deploy/local"
RUN="$HERE/.run"
CACHE="$HERE/.cache"
CADDY="$CACHE/caddy"

APP_PORT=3000 PUB_PORT=8080 TUN_PORT=4443 HTTPS_PORT=8443 BASE=viaduct.test
KEEP="${1:-}"   # pass "keep" to leave the stack running for browser testing

pids=()
cleanup() { for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; }
trap cleanup EXIT
fail() { echo; echo "REHEARSAL FAILED: $*"; echo "--- viaductd ---"; tail -5 "$RUN/viaductd.log" 2>/dev/null; echo "--- caddy ---"; tail -5 "$RUN/caddy.log" 2>/dev/null; echo "--- client ---"; tail -5 "$RUN/client.log" 2>/dev/null; exit 1; }

[ -x "$VENV/viaductd" ] || { echo "install the CLIs first: python3 -m venv .venv && .venv/bin/pip install ."; exit 1; }
rm -rf "$RUN"; mkdir -p "$RUN" "$CACHE"

wait_port() { for _ in $(seq 1 100); do (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&-; return 0; }; sleep 0.1; done; return 1; }

echo "== 1. caddy binary (standard build; internal CA needs no plugins) =="
if [ ! -x "$CADDY" ]; then
  case "$(uname -m)" in arm64) A=arm64;; x86_64) A=amd64;; *) A=amd64;; esac
  echo "downloading caddy (darwin/$A)..."
  curl -fsSL "https://caddyserver.com/api/download?os=darwin&arch=$A" -o "$CADDY" || fail "caddy download failed"
  chmod +x "$CADDY"
fi
"$CADDY" version | head -1

echo "== 2. self-signed cert for viaductd's tunnel port =="
CERT="$RUN/tunnel.crt"; KEY="$RUN/tunnel.key"
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$KEY" -out "$CERT" -days 2 \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1 || fail "openssl"

echo "== 3. demo app on :$APP_PORT =="
mkdir -p "$RUN/webroot"; echo "viaduct local rehearsal OK" > "$RUN/webroot/index.html"
( cd "$RUN/webroot" && exec python3 -m http.server "$APP_PORT" ) >/dev/null 2>&1 & pids+=($!)

echo "== 4. viaductd: public :$PUB_PORT, tunnel :$TUN_PORT (TLS), base-domain $BASE =="
"$VENV/viaductd" --bind 127.0.0.1 --public-port "$PUB_PORT" --tunnel-port "$TUN_PORT" \
  --base-domain "$BASE" --tls-cert "$CERT" --tls-key "$KEY" >"$RUN/viaductd.log" 2>&1 & pids+=($!)

echo "== 5. caddy: HTTPS :$HTTPS_PORT (internal CA) -> viaductd :$PUB_PORT =="
cat > "$RUN/Caddyfile" <<EOF
{
	admin off
	storage file_system {
		root $RUN/caddy-data
	}
	auto_https disable_redirects
}

*.$BASE:$HTTPS_PORT, $BASE:$HTTPS_PORT {
	tls internal
	reverse_proxy 127.0.0.1:$PUB_PORT
}
EOF
"$CADDY" run --config "$RUN/Caddyfile" --adapter caddyfile >"$RUN/caddy.log" 2>&1 & pids+=($!)

wait_port "$APP_PORT" || fail "demo app never listened"
wait_port "$PUB_PORT" || fail "viaductd public port never listened"
wait_port "$TUN_PORT" || fail "viaductd tunnel port never listened"
wait_port "$HTTPS_PORT" || fail "caddy never listened"

ROOT_CA="$RUN/caddy-data/pki/authorities/local/root.crt"
for _ in $(seq 1 100); do [ -s "$ROOT_CA" ] && break; sleep 0.1; done
[ -s "$ROOT_CA" ] || fail "caddy internal root CA never appeared"

echo "== 6. viaduct client: tunnel :$TUN_PORT (TLS) exposing :$APP_PORT =="
NO_COLOR=1 "$VENV/viaduct" http "$APP_PORT" --server "127.0.0.1:$TUN_PORT" \
  --tls --tls-ca "$CERT" --pool-size 5 >"$RUN/client.log" 2>&1 & pids+=($!)

HOST=""
for _ in $(seq 1 100); do HOST=$(grep -oE "[a-z0-9-]+\.$BASE" "$RUN/client.log" | head -1); [ -n "$HOST" ] && break; sleep 0.1; done
[ -n "$HOST" ] || fail "client never received a hostname"
echo "   server assigned: https://$HOST:$HTTPS_PORT/"

echo "== 7. PROOF: real HTTPS request through the whole stack =="
body=""; code=""
for _ in $(seq 1 60); do
  body=$(curl -sS --cacert "$ROOT_CA" --resolve "$HOST:$HTTPS_PORT:127.0.0.1" "https://$HOST:$HTTPS_PORT/" 2>/dev/null)
  echo "$body" | grep -q "viaduct local rehearsal OK" && break
  sleep 0.2
done
code=$(curl -sS -o /dev/null -w '%{http_code}' --cacert "$ROOT_CA" --resolve "$HOST:$HTTPS_PORT:127.0.0.1" "https://$HOST:$HTTPS_PORT/" 2>/dev/null)
echo "   GET https://$HOST/            -> HTTP $code   body: '$body'"

code404=$(curl -sS -o /dev/null -w '%{http_code}' --cacert "$ROOT_CA" --resolve "nope-nobody.$BASE:$HTTPS_PORT:127.0.0.1" "https://nope-nobody.$BASE:$HTTPS_PORT/" 2>/dev/null)
echo "   GET https://nope-nobody.$BASE/ -> HTTP $code404   (want 404, proves wildcard routing)"

echo
if echo "$body" | grep -q "viaduct local rehearsal OK" && [ "$code" = 200 ] && [ "$code404" = 404 ]; then
  echo "✅ REHEARSAL PASSED — HTTPS -> Caddy -> viaductd -> tunnel -> app works end to end,"
  echo "   with a browser-valid cert chain (verified against Caddy's internal CA)."
else
  fail "unexpected results (code=$code code404=$code404)"
fi

if [ "$KEEP" = keep ]; then
  echo
  echo "Leaving the stack up (Ctrl-C to stop). To reach it yourself:"
  echo "  curl --cacert '$ROOT_CA' --resolve '$HOST:$HTTPS_PORT:127.0.0.1' https://$HOST:$HTTPS_PORT/"
  echo "  or in a browser: add '127.0.0.1 $HOST' to /etc/hosts, trust the CA at"
  echo "  $ROOT_CA, then open https://$HOST:$HTTPS_PORT/"
  wait
fi
