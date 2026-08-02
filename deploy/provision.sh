#!/usr/bin/env bash
# Provision a viaduct server. The SAME script runs on a DigitalOcean droplet and
# in the local container rehearsal; behaviour is controlled by environment:
#
#   BASE_DOMAIN     domain tunnels hang off        (default: viaduct.sh)
#   TLS_MODE        letsencrypt | internal         (default: letsencrypt)
#   DO_API_TOKEN    Caddy DNS-01 token; prompted for if unset in letsencrypt mode
#   SETUP_FIREWALL  yes | no                        (default: yes; use no in containers)
#   TUNNEL_ALLOW_IPS  comma/space-separated IPs allowed on the no-auth tunnel
#                     port 4443 (default: unset = open to the internet, warned)
#   REPO_DIR        checked-out repo                (default: this script's repo)
#
# Caddy is downloaded prebuilt from caddyserver.com (with the DigitalOcean DNS
# plugin baked in for letsencrypt mode), no Go build, so no xcaddy and no OOM
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
  # Pass the token via a curl config on stdin, never on the command line (which
  # ps / /proc would expose). printf is a bash builtin, so the token never lands
  # in any process's argv either.
  [ "$(printf 'header = "Authorization: Bearer %s"\n' "${1:-}" \
        | curl -sS -o /dev/null -w '%{http_code}' -K - \
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
  # Liveness check: confirm we fetched a runnable binary, not an HTML error page.
  # (The on-demand DNS-plugin build publishes no checksum to pin, so this is not
  # an integrity guarantee; the download is HTTPS from caddyserver.com.)
  /usr/local/bin/caddy version >/dev/null 2>&1 \
    || { echo "downloaded caddy is not a runnable binary" >&2; return 1; }
}

do_users() {
  id caddy   >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/caddy --shell /usr/sbin/nologin caddy
  id viaduct >/dev/null 2>&1 || useradd --system --home-dir /var/lib/viaduct --shell /usr/sbin/nologin viaduct
  install -d -o caddy   -g caddy   /etc/caddy /var/www/viaduct-site
  install -d -o viaduct -g viaduct /etc/viaduct/certs
}

do_site() { cp -r "$REPO_DIR"/site/* /var/www/viaduct-site/ 2>/dev/null || true; }

do_caddyfile() {
  # BASE_DOMAIN may be comma-separated (primary + aliases): primary names the
  # tunnels and the site; apex/wild expand to all names for one multi-SAN cert.
  local primary="${BASE_DOMAIN%%,*}"
  local apex wild
  apex=$(printf '%s' "$BASE_DOMAIN" | sed 's/,/, /g')
  wild=$(printf '%s' "$BASE_DOMAIN" | awk -F, '{for(i=1;i<=NF;i++) printf (i>1?", ":"") "*." $i}')
  if [ "$TLS_MODE" = letsencrypt ]; then
    cat > /etc/caddy/Caddyfile <<EOF
{
	email admin@${primary}
	on_demand_tls {
		ask http://127.0.0.1:8080/_viaduct/tls-check
	}
}
${apex} {
	tls {
		dns digitalocean {env.DO_API_TOKEN}
	}
	reverse_proxy /_viaduct/health 127.0.0.1:8080
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
www.${primary} {
	tls {
		dns digitalocean {env.DO_API_TOKEN}
	}
	redir https://${primary}{uri} permanent
}
${wild} {
	tls {
		dns digitalocean {env.DO_API_TOKEN}
	}
	reverse_proxy 127.0.0.1:8080
}
https:// {
	tls {
		on_demand
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
	reverse_proxy /_viaduct/health 127.0.0.1:8080
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
  # Create both files with their FINAL perms up front (install from /dev/null),
  # so the DO token is never written into a world-readable file that is only
  # tightened afterwards. printf is a bash builtin, so the token stays out of argv.
  install -m 640 -o root -g caddy /dev/null /etc/viaduct/caddy.env
  [ "$TLS_MODE" = letsencrypt ] \
    && printf 'DO_API_TOKEN=%s\n' "${DO_API_TOKEN}" >> /etc/viaduct/caddy.env
  install -m 640 -o root -g viaduct /dev/null /etc/viaduct/viaductd.env
  cat >> /etc/viaduct/viaductd.env <<EOF
BASE_DOMAIN=${BASE_DOMAIN}
TLS_CERT=/etc/viaduct/certs/tls.crt
TLS_KEY=/etc/viaduct/certs/tls.key
EOF
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
  # SSH open (key auth); 443/udp is HTTP/3. 4443 is the no-auth tunnel port:
  # restrict it to TUNNEL_ALLOW_IPS when given, else open it (with a warning).
  ufw --force default deny incoming
  ufw allow 22/tcp; ufw allow 80/tcp; ufw allow 443/tcp; ufw allow 443/udp
  if [ -n "${TUNNEL_ALLOW_IPS:-}" ]; then
    for _ip in ${TUNNEL_ALLOW_IPS//,/ }; do
      ufw allow from "$_ip" to any port 4443 proto tcp
    done
  else
    echo "  WARNING: TUNNEL_ALLOW_IPS unset; opening tunnel port 4443 to the whole internet (it has no auth)." >&2
    ufw allow 4443/tcp
  fi
  ufw --force enable
}

# ---- run ----
main() {
banner
printf '  %sPrerequisites%s\n' "$B" "$R"
note "Ubuntu droplet, run as root (1 GB RAM is plenty)."
if [ "$TLS_MODE" = letsencrypt ]; then
  note "DNS: A ${BASE_DOMAIN} -> this droplet's IP, and CNAME *.${BASE_DOMAIN} -> ${BASE_DOMAIN}."
  note "A DigitalOcean API token with DNS write scope, for wildcard TLS (prompted for below)."
fi
note "BASE_DOMAIN set to your domain (now: ${BASE_DOMAIN}). Optional: TUNNEL_ALLOW_IPS."
printf '\n'
step "Installing system packages" do_packages

if [ "$TLS_MODE" = letsencrypt ]; then
  if [ -z "${DO_API_TOKEN:-}" ] && [ -t 0 ]; then
    for _ in 1 2 3; do
      printf '  %sDigitalOcean API token%s (DNS write scope, hidden): ' "$B" "$R"
      read -rs DO_API_TOKEN; echo
      DO_API_TOKEN="$(printf '%s' "${DO_API_TOKEN:-}" | tr -d '[:space:]')"
      [ -n "$DO_API_TOKEN" ] && verify_do_token "$DO_API_TOKEN" && { note "token verified with DigitalOcean ✔"; break; }
      printf '  %srejected by DigitalOcean (401), likely a truncated paste; try again%s\n' "$E" "$R"
      DO_API_TOKEN=""
    done
  fi
  [ -n "${DO_API_TOKEN:-}" ] || { echo "no DO_API_TOKEN, set it in the environment or run interactively" >&2; exit 1; }
  verify_do_token "$DO_API_TOKEN" || { echo "DO_API_TOKEN rejected by the DigitalOcean API (401)" >&2; exit 1; }
fi

# The tunnel port (4443) has no auth, so offer to restrict it during setup, the
# same way the DO token is prompted for. Preset TUNNEL_ALLOW_IPS to skip this.
if [ "$SETUP_FIREWALL" = yes ] && [ -z "${TUNNEL_ALLOW_IPS:-}" ] && [ -t 0 ]; then
  printf '  %sAllow-list for tunnel port 4443%s (comma-separated IPs; blank = open to the internet): ' "$B" "$R"
  read -r TUNNEL_ALLOW_IPS || TUNNEL_ALLOW_IPS=""
  export TUNNEL_ALLOW_IPS
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
  note "Caddy is fetching the TLS certificate now, https://${BASE_DOMAIN} goes live in a minute or two."
  note "viaductd starts automatically once that cert exists (it retries every 5s)."
fi
}

# Run only when executed directly; sourcing (e.g. from tests) just loads the
# functions so they can be reused without provisioning. (Uses an if-block, not
# `&& main`, so a source doesn't return non-zero and trip the caller's set -e.)
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then main "$@"; fi
