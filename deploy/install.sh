#!/usr/bin/env bash
# AlgoDesk installer for Ubuntu/Debian VPS.
#
#   sudo bash deploy/install.sh                       # HTTPS on <ip>.sslip.io
#   sudo bash deploy/install.sh --domain algodesk.example.com --email you@example.com
#   sudo bash deploy/install.sh --proxy traefik       # pakai Traefik (Docker) yang sudah ada
#
# --proxy auto (default) memakai Traefik bila container Traefik sedang berjalan,
# selain itu memasang Caddy.
#
# Safe to re-run: it updates the app, keeps the database and settings.
set -euo pipefail

APP_USER=algodesk
PREFIX=/opt/algodesk
DATA=/var/lib/algodesk
ENV_FILE=/etc/algodesk.env
PORT=8000
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DOMAIN=""
EMAIL=""
PROXY="auto"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --email)  EMAIL="$2"; shift 2 ;;
    --proxy)  PROXY="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Opsi tidak dikenal: $1" >&2; exit 1 ;;
  esac
done

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "Jalankan dengan sudo." >&2; exit 1; }
command -v apt-get >/dev/null || { echo "Skrip ini untuk Ubuntu/Debian (apt)." >&2; exit 1; }
[[ -f "$SRC/backend/algodesk/app.py" ]] || { echo "Jalankan dari dalam folder repo algodesk." >&2; exit 1; }

# Reuse settings from a previous install unless overridden.
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  DOMAIN="${DOMAIN:-${ALGODESK_DOMAIN:-}}"
  EMAIL="${EMAIL:-${ALGODESK_EMAIL:-}}"
fi

traefik_container() {
  command -v docker >/dev/null || return 0
  docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null | awk '$2 ~ /(^|\/)traefik(:|$)/ {print $1; exit}'
}
TRAEFIK="$(traefik_container)"
case "$PROXY" in
  auto) [[ -n "$TRAEFIK" ]] && PROXY=traefik || PROXY=caddy ;;
  traefik) [[ -n "$TRAEFIK" ]] || { echo "Container Traefik tidak ditemukan." >&2; exit 1; } ;;
  caddy) ;;
  *) echo "--proxy harus auto, caddy, atau traefik" >&2; exit 1 ;;
esac

say "Memasang paket sistem"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl gnupg debian-keyring debian-archive-keyring apt-transport-https >/dev/null
if [[ "$PROXY" == caddy ]] && ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy >/dev/null
fi

if [[ -z "$DOMAIN" ]]; then
  IP="$(curl -4 -fsS https://api.ipify.org || true)"
  [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "IP publik tidak terdeteksi. Pakai --domain." >&2; exit 1; }
  DOMAIN="${IP//./-}.sslip.io"
  warn "Tanpa domain sendiri; memakai $DOMAIN (sertifikat HTTPS tetap otomatis)."
fi

say "Menyiapkan pengguna dan folder"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$DATA" "$DATA/backups"
install -d -m 755 "$PREFIX"

cat > "$ENV_FILE" <<EOF
ALGODESK_DB=$DATA/algodesk.db
ALGODESK_COOKIE_SECURE=1
ALGODESK_DOMAIN=$DOMAIN
ALGODESK_EMAIL=$EMAIL
EOF
chmod 640 "$ENV_FILE"
chgrp "$APP_USER" "$ENV_FILE"

say "Memasang aplikasi ke $PREFIX"
rm -rf "$PREFIX/app.new"
cp -a "$SRC/backend" "$PREFIX/app.new"
rm -rf "$PREFIX/app.new/tests" "$PREFIX/app.new/.pytest_cache"
find "$PREFIX/app.new" -name __pycache__ -prune -exec rm -rf {} +
[[ -d "$PREFIX/venv" ]] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q -r "$PREFIX/app.new/requirements.txt"
rm -rf "$PREFIX/app.old"
[[ -d "$PREFIX/app" ]] && mv "$PREFIX/app" "$PREFIX/app.old"
mv "$PREFIX/app.new" "$PREFIX/app"
install -m 755 "$SRC/deploy/backup.py" "$PREFIX/backup.py"
chown -R root:root "$PREFIX"

say "Mendaftarkan layanan systemd"
cat > /etc/systemd/system/algodesk.service <<EOF
[Unit]
Description=AlgoDesk backend
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
EnvironmentFile=$ENV_FILE
WorkingDirectory=$PREFIX/app
ExecStart=$PREFIX/venv/bin/python -m uvicorn algodesk.app:create_app --factory --host 127.0.0.1 --port $PORT --no-server-header
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/algodesk-backup.service <<EOF
[Unit]
Description=AlgoDesk database backup

[Service]
Type=oneshot
User=$APP_USER
EnvironmentFile=$ENV_FILE
ExecStart=$PREFIX/venv/bin/python $PREFIX/backup.py $DATA/backups 14
EOF

cat > /etc/systemd/system/algodesk-backup.timer <<EOF
[Unit]
Description=Daily AlgoDesk database backup

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now algodesk.service algodesk-backup.timer >/dev/null
systemctl restart algodesk.service

if [[ "$PROXY" == traefik ]]; then
  say "Mendaftarkan $DOMAIN ke Traefik ($TRAEFIK)"
  # Traefik must share the host network so it can reach 127.0.0.1:$PORT.
  [[ "$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$TRAEFIK")" == host ]] || {
    echo "Traefik tidak memakai network_mode host; AlgoDesk di 127.0.0.1:$PORT tidak terjangkau olehnya." >&2
    echo "Pakai --proxy caddy setelah Traefik dihentikan, atau hubungi pengembang." >&2
    exit 1
  }
  read -r RESOLVER ENTRY < <(docker inspect "$TRAEFIK" --format '{{json .Args}}' | python3 -c '
import json, re, sys
resolver, entry = "", ""
for a in json.load(sys.stdin) or []:
    m = re.match(r"--certificatesresolvers\.([^.=]+)\.", a, re.I)
    resolver = resolver or (m.group(1) if m else "")
    m = re.match(r"--entrypoints\.([^.=]+)\.address=.*:443$", a, re.I)
    entry = entry or (m.group(1) if m else "")
print(resolver or "-", entry or "-")')
  [[ "$RESOLVER" != "-" && "$ENTRY" != "-" ]] || {
    echo "Pengaturan Traefik (certresolver/entrypoint :443) tidak terbaca dari argumen container." >&2
    exit 1
  }
  # A placeholder container on the host network carries the routing labels;
  # Traefik resolves host-network containers to 127.0.0.1.
  docker rm -f algodesk-route >/dev/null 2>&1 || true
  docker run -d --name algodesk-route --restart unless-stopped --network host \
    -l traefik.enable=true \
    -l "traefik.http.routers.algodesk.rule=Host(\`$DOMAIN\`)" \
    -l "traefik.http.routers.algodesk.entrypoints=$ENTRY" \
    -l "traefik.http.routers.algodesk.tls.certresolver=$RESOLVER" \
    -l "traefik.http.services.algodesk.loadbalancer.server.port=$PORT" \
    busybox:1.36 sleep infinity >/dev/null
  if systemctl is-enabled caddy >/dev/null 2>&1; then
    systemctl disable --now caddy >/dev/null 2>&1 || true
    warn "Caddy dimatikan karena port 80/443 dipakai Traefik."
  fi
else
  say "Mengatur Caddy (HTTPS) untuk $DOMAIN"
  CADDYFILE=/etc/caddy/Caddyfile
  install -d -m 755 /etc/caddy
  if [[ -f "$CADDYFILE" ]] && ! grep -q '# managed by algodesk' "$CADDYFILE"; then
    cp "$CADDYFILE" "$CADDYFILE.bak.$(date +%Y%m%d%H%M%S)"
    warn "Caddyfile lama dicadangkan ke $CADDYFILE.bak.*"
  fi
  {
    echo "# managed by algodesk: dibuat ulang setiap install.sh dijalankan"
    [[ -n "$EMAIL" ]] && printf '{\n\temail %s\n}\n\n' "$EMAIL"
    cat <<EOF
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}
EOF
  } > "$CADDYFILE"
  caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
  fi
  systemctl enable caddy >/dev/null
  systemctl reload-or-restart caddy
fi

say "Menunggu server siap"
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || { journalctl -u algodesk -n 30 --no-pager; exit 1; }

# End-to-end check through the HTTPS proxy (-k: the certificate may still be issuing).
for _ in $(seq 1 20); do
  curl -fsSk --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/healthz" >/dev/null 2>&1 && { PROXY_OK=1; break; }
  sleep 2
done
[[ "${PROXY_OK:-}" == 1 ]] || warn "https://$DOMAIN belum menjawab lewat proxy $PROXY; periksa 'docker logs $TRAEFIK' atau 'journalctl -u caddy'."

cli() { (cd "$PREFIX/app" && runuser -u "$APP_USER" -- env ALGODESK_DB="$DATA/algodesk.db" "$PREFIX/venv/bin/python" -m algodesk.cli "$@"); }
count() { runuser -u "$APP_USER" -- "$PREFIX/venv/bin/python" -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('select count(*) from $1').fetchone()[0])" "$DATA/algodesk.db"; }

if [[ "$(count users)" == "0" ]]; then
  say "Membuat akun Super Admin pertama"
  read -rp "Nama pengguna admin [admin]: " ADMIN_USER </dev/tty
  cli create-user "${ADMIN_USER:-admin}" --role admin --name "Administrator" </dev/tty
fi

if [[ "$(count agents)" == "0" ]]; then
  say "Mendaftarkan agent MT5 (salin dua baris ini ke input AlgoDeskAgent)"
  cli create-agent vps-01
fi

say "Selesai"
cat <<EOF
Dashboard : https://$DOMAIN
Agent URL : https://$DOMAIN/api   (InpBackendUrl di AlgoDeskAgent)
WebRequest: tambahkan https://$DOMAIN di MT5 > Tools > Options > Expert Advisors
Log       : journalctl -u algodesk -f
Backup    : $DATA/backups (harian, 14 hari terakhir)
Update    : cd $SRC && sudo bash deploy/update.sh
EOF
