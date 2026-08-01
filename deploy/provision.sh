#!/usr/bin/env bash
# Provision a viaduct server. The SAME script runs on a DigitalOcean droplet and
# in the local container rehearsal; behaviour is controlled by environment:
#
#   BASE_DOMAIN     domain tunnels hang off        (default: viaduct.sh)
#   TLS_MODE        letsencrypt | internal         (default: letsencrypt)
#   DO_API_TOKEN    Caddy DNS-01 token; prompted for if unset in letsencrypt mode
#   SETUP_FIREWALL  yes | no                        (default: yes; use no in containers)
#   REPO_DIR        checked-out repo                (default: this script's repo)
#
# Caddy is downloaded prebuilt from caddyserver.com (with the DigitalOcean DNS
# plugin baked in for letsencrypt mode) — no Go build, so no xcaddy and no OOM
# on a 1 GB droplet.
set -euo pipefail

BASE_DOMAIN="${BASE_DOMAIN:-viaduct.sh}"
TLS_MODE="${TLS_MODE:-letsencrypt}"
SETUP_FIREWALL="${SETUP_FIREWALL:-yes}"
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"

# ---- pretty output (falls back to plain when not a TTY, e.g. the container) ----
if [ -t 1 ] && [ "${TERM:-dumb}" != dumb ]; then
  R=$'\033[0m'; B=$'\033[1m'; D=$'\033[2m'; A=$'\033[38;5;214m'; G=$'\033[32m'; E=$'\033[31m'; TTY=1
else
  R=; B=; D=; A=; G=; E=; TTY=0
fi
TOTAL=8; [ "$SETUP_FIREWALL" = yes ] && TOTAL=9
STEP=0

banner() {
  printf '%s' "$A"
  cat <<'ART'
         _           _            _
  __   _(_) __ _  __| |_   _  ___| |_
  \ \ / / |/ _` |/ _` | | | |/ __| __|
   \ V /| | (_| | (_| | |_| | (__| |_
    \_/ |_|\__,_|\__,_|\__,_|\___|\__|  .sh
ART
  printf '%s%s  self-hosted reverse tunnel · installer%s\n\n' "$R" "$D" "$R"
}

step() {  # step "label" cmd args...
  STEP=$((STEP + 1)); local label="$1"; shift
  local logf; logf="$(mktemp)"
  if [ "$TTY" = 1 ]; then
    printf '  %s▸%s [%d/%d] %s ' "$A" "$R" "$STEP" "$TOTAL" "$label"
    ( "$@" ) >"$logf" 2>&1 &
    local pid=$! sp='|/-\' i=0 rc=0
    while kill -0 "$pid" 2>/dev/null; do i=$(((i + 1) % 4)); printf '\b%s' "${sp:$i:1}"; sleep 0.1; done
    wait "$pid" || rc=$?
    if [ "$rc" = 0 ]; then
      printf '\r  %s✔%s [%d/%d] %s%*s\n' "$G" "$R" "$STEP" "$TOTAL" "$label" 6 ''
    else
      printf '\r  %s✗%s [%d/%d] %s\n\n' "$E" "$R" "$STEP" "$TOTAL" "$label"; sed 's/^/    /' "$logf"; rm -f "$logf"; exit "$rc"
    fi
  else
    printf '  [%d/%d] %s\n' "$STEP" "$TOTAL" "$label"
    local rc=0; ( "$@" ) >"$logf" 2>&1 || rc=$?
    [ "$rc" = 0 ] || { sed 's/^/    /' "$logf"; rm -f "$logf"; exit "$rc"; }
  fi
  rm -f "$logf"
}

note() { printf '  %s%s%s\n' "$D" "$1" "$R"; }

verify_do_token() {
  [ "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${1:-}" \
        https://api.digitalocean.com/v2/account 2>/dev/null || echo 000)" = 200 ]
}

# ---- step bodies ----
do_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3-venv python3-pip curl ca-certificates
  [ "$SETUP_FIREWALL" = yes ] && apt-get install -y -qq ufw
  return 0
}

do_clis() {
  python3 -m venv /opt/viaduct-venv
  /opt/viaduct-venv/bin/pip install -q --upgrade pip
  /opt/viaduct-venv/bin/pip install -q "$REPO_DIR"
  ln -sf /opt/viaduct-venv/bin/viaductd /usr/local/bin/viaductd
  ln -sf /opt/viaduct-venv/bin/viaduct  /usr/local/bin/viaduct
}

do_caddy() {
  local url="https://caddyserver.com/api/download?os=linux&arch=${ARCH}"
  [ "$TLS_MODE" = letsencrypt ] && url="${url}&p=github.com/caddy-dns/digitalocean"
  curl -fsSL "$url" -o /usr/local/bin/caddy
  chmod +x /usr/local/bin/caddy
}

do_users() {
  id caddy   >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/caddy --shell /usr/sbin/nologin caddy
  id viaduct >/dev/null 2>&1 || useradd --system --home-dir /var/lib/viaduct --shell /usr/sbin/nologin viaduct
  install -d -o caddy   -g caddy   /etc/caddy /var/www/viaduct-site
  install -d -o viaduct -g viaduct /etc/viaduct/certs
}

do_site() { cp -r "$REPO_DIR"/site/* /var/www/viaduct-site/ 2>/dev/null || true; }

do_caddyfile() {
  if [ "$TLS_MODE" = letsencrypt ]; then
    cat > /etc/caddy/Caddyfile <<EOF
{
	email admin@${BASE_DOMAIN}
}
${BASE_DOMAIN} {
	tls {
		dns digitalocean {env.DO_API_TOKEN}
	}
	root * /var/www/viaduct-site
	header Cache-Control "no-cache"
	file_server
	handle_errors {
		rewrite * /404.html
		file_server {
			status 404
		}
	}
}
www.${BASE_DOMAIN} {
	tls {
		dns digitalocean {env.DO_API_TOKEN}
	}
	redir https://${BASE_DOMAIN}{uri} permanent
}
*.${BASE_DOMAIN} {
	tls {
		dns digitalocean {env.DO_API_TOKEN}
	}
	reverse_proxy 127.0.0.1:8080
}
EOF
  else
    cat > /etc/caddy/Caddyfile <<EOF
{
	email admin@${BASE_DOMAIN}
}
${BASE_DOMAIN} {
	tls internal
	root * /var/www/viaduct-site
	header Cache-Control "no-cache"
	file_server
	handle_errors {
		rewrite * /404.html
		file_server {
			status 404
		}
	}
}
*.${BASE_DOMAIN} {
	tls internal
	reverse_proxy 127.0.0.1:8080
}
EOF
  fi
}

do_env() {
  : > /etc/viaduct/caddy.env
  [ "$TLS_MODE" = letsencrypt ] && echo "DO_API_TOKEN=${DO_API_TOKEN}" > /etc/viaduct/caddy.env
  chown root:caddy /etc/viaduct/caddy.env && chmod 640 /etc/viaduct/caddy.env
  cat > /etc/viaduct/viaductd.env <<EOF
BASE_DOMAIN=${BASE_DOMAIN}
TLS_CERT=/etc/viaduct/certs/tls.crt
TLS_KEY=/etc/viaduct/certs/tls.key
EOF
  chown root:viaduct /etc/viaduct/viaductd.env && chmod 640 /etc/viaduct/viaductd.env
}

do_services() {
  install -m 755 "$REPO_DIR"/deploy/viaductd-sync-cert /usr/local/sbin/viaductd-sync-cert
  install -m 644 "$REPO_DIR"/deploy/caddy.service \
                 "$REPO_DIR"/deploy/viaductd.service \
                 "$REPO_DIR"/deploy/viaductd-restart.service \
                 "$REPO_DIR"/deploy/viaductd-restart.timer \
                 /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable caddy viaductd viaductd-restart.timer
  systemctl start caddy viaductd-restart.timer
  # viaductd's first start races Caddy obtaining the cert; Restart=always brings
  # it up once the cert exists, so don't fail provisioning on it.
  systemctl start viaductd 2>/dev/null || true
  return 0
}

do_firewall() {
  grep -q '# viaduct' /etc/security/limits.conf 2>/dev/null || \
    printf '*  soft  nofile  65535  # viaduct\n*  hard  nofile  65535  # viaduct\n' >> /etc/security/limits.conf
  echo 'net.core.somaxconn = 1024' > /etc/sysctl.d/90-viaduct.conf
  sysctl --system >/dev/null 2>&1 || true
  # SSH open (key auth); 443/udp is HTTP/3; 4443 is the (no-auth) tunnel port.
  ufw --force default deny incoming
  ufw allow 22/tcp; ufw allow 80/tcp; ufw allow 443/tcp; ufw allow 443/udp; ufw allow 4443/tcp
  ufw --force enable
}

# ---- run ----
main() {
banner
step "Installing system packages" do_packages

if [ "$TLS_MODE" = letsencrypt ]; then
  if [ -z "${DO_API_TOKEN:-}" ] && [ -t 0 ]; then
    for _ in 1 2 3; do
      printf '  %sDigitalOcean API token%s (DNS write scope, hidden): ' "$B" "$R"
      read -rs DO_API_TOKEN; echo
      DO_API_TOKEN="$(printf '%s' "${DO_API_TOKEN:-}" | tr -d '[:space:]')"
      [ -n "$DO_API_TOKEN" ] && verify_do_token "$DO_API_TOKEN" && { note "token verified with DigitalOcean ✔"; break; }
      printf '  %srejected by DigitalOcean (401) — likely a truncated paste; try again%s\n' "$E" "$R"
      DO_API_TOKEN=""
    done
  fi
  [ -n "${DO_API_TOKEN:-}" ] || { echo "no DO_API_TOKEN — set it in the environment or run interactively" >&2; exit 1; }
  verify_do_token "$DO_API_TOKEN" || { echo "DO_API_TOKEN rejected by the DigitalOcean API (401)" >&2; exit 1; }
fi

step "Installing viaduct CLIs"        do_clis
step "Downloading Caddy ($TLS_MODE)"  do_caddy
step "Creating service users"         do_users
step "Installing landing page"        do_site
step "Writing Caddy config"           do_caddyfile
step "Writing service config"         do_env
step "Starting services (systemd)"    do_services
[ "$SETUP_FIREWALL" = yes ] && step "Tuning host and firewall" do_firewall

echo
printf '  %s✔ viaduct is provisioned%s  %s(%s, %s)%s\n' "$G$B" "$R" "$D" "$BASE_DOMAIN" "$TLS_MODE" "$R"
if [ "$TLS_MODE" = letsencrypt ]; then
  note "Caddy is fetching the TLS certificate now — https://${BASE_DOMAIN} goes live in a minute or two."
  note "viaductd starts automatically once that cert exists (it retries every 5s)."
fi
}

# Run only when executed directly; sourcing (e.g. from tests) just loads the
# functions so they can be reused without provisioning. (Uses an if-block, not
# `&& main`, so a source doesn't return non-zero and trip the caller's set -e.)
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then main "$@"; fi
