#!/usr/bin/env bash
#
# aria2 + AriaNg + File Browser one-shot installer for Ubuntu/Debian.
#
#   Install:    sudo bash install.sh
#   Uninstall:  sudo bash install.sh --uninstall
#
# Optional environment variables:
#   DOMAIN=example.com     enable HTTPS with Let's Encrypt for this domain
#   EMAIL=you@example.com  email for Let's Encrypt (required with DOMAIN)
#   DOWNLOAD_DIR=/srv/downloads
#   RPC_SECRET=...         aria2 RPC token (auto-generated if empty)
#   FB_PASSWORD=...        File Browser admin password (auto-generated if empty)
#
set -euo pipefail

DOWNLOAD_DIR="${DOWNLOAD_DIR:-/srv/downloads}"
DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
RPC_SECRET="${RPC_SECRET:-}"
FB_PASSWORD="${FB_PASSWORD:-}"

ARIA2_USER="aria2"
CONF_DIR="/etc/aria2"
WEB_DIR="/var/www/ariang"
FB_DB="${CONF_DIR}/filebrowser.db"
CRED_FILE="/root/aria2-web-credentials.txt"
ARIANG_FALLBACK_VERSION="1.3.7"
TRACKER_URL="https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/trackers_best.txt"

green()  { printf '\033[1;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
red()    { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
step()   { echo; green "==> $*"; }

[[ $EUID -eq 0 ]] || { red "Run as root: sudo bash $0"; exit 1; }
command -v apt-get >/dev/null || { red "This script supports Ubuntu/Debian only."; exit 1; }

uninstall() {
  step "Removing aria2 web setup"
  systemctl disable --now aria2 filebrowser aria2-trackers.timer 2>/dev/null || true
  rm -f /etc/systemd/system/aria2.service \
        /etc/systemd/system/filebrowser.service \
        /etc/systemd/system/aria2-trackers.service \
        /etc/systemd/system/aria2-trackers.timer \
        /usr/local/bin/aria2-update-trackers \
        /usr/local/bin/filebrowser \
        /etc/nginx/sites-enabled/aria2 \
        /etc/nginx/sites-available/aria2
  rm -rf "$WEB_DIR" "$CONF_DIR"
  systemctl daemon-reload
  if [[ ! -e /etc/nginx/sites-enabled/default && -e /etc/nginx/sites-available/default ]]; then
    ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
  fi
  systemctl reload nginx 2>/dev/null || true
  green "Done. Downloaded files in ${DOWNLOAD_DIR} were kept."
  exit 0
}

[[ "${1:-}" == "--uninstall" ]] && uninstall

if [[ -n "$DOMAIN" && -z "$EMAIL" ]]; then
  red "EMAIL is required when DOMAIN is set."; exit 1
fi

# AriaNg takes the token base64-encoded inside a URL path, so make sure the
# encoded form has no '/', '+' or '=' characters.
gen_rpc_secret() {
  local s
  while :; do
    s="$(openssl rand -hex 12)"
    [[ "$(printf '%s' "$s" | base64)" =~ ^[A-Za-z0-9]+$ ]] && { echo "$s"; return; }
  done
}

step "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y aria2 nginx unzip curl openssl ca-certificates

step "Creating user and directories"
id "$ARIA2_USER" &>/dev/null || useradd -r -m -d /var/lib/aria2 -s /usr/sbin/nologin "$ARIA2_USER"
mkdir -p "$CONF_DIR" "$DOWNLOAD_DIR" "$WEB_DIR"
touch "${CONF_DIR}/aria2.session"

# Keep the existing token on re-runs so saved AriaNg settings keep working.
if [[ -z "$RPC_SECRET" && -f "${CONF_DIR}/aria2.conf" ]]; then
  RPC_SECRET="$(sed -n 's/^rpc-secret=//p' "${CONF_DIR}/aria2.conf")"
fi
[[ -n "$RPC_SECRET" ]] || RPC_SECRET="$(gen_rpc_secret)"

step "Writing aria2 config"
cat > "${CONF_DIR}/aria2.conf" <<EOF
## Files
dir=${DOWNLOAD_DIR}
continue=true
file-allocation=none
disk-cache=64M
auto-file-renaming=true

## Speed
max-concurrent-downloads=5
max-connection-per-server=16
split=16
min-split-size=1M
max-overall-download-limit=0
max-overall-upload-limit=0

## RPC (only reachable through nginx)
enable-rpc=true
rpc-listen-all=false
rpc-listen-port=6800
rpc-allow-origin-all=true
rpc-secret=${RPC_SECRET}
rpc-max-request-size=16M

## Session (resume after restart)
input-file=${CONF_DIR}/aria2.session
save-session=${CONF_DIR}/aria2.session
save-session-interval=30
force-save=false

## BitTorrent
listen-port=6881-6999
dht-listen-port=6881-6999
enable-dht=true
enable-dht6=false
enable-peer-exchange=true
bt-enable-lpd=true
bt-max-peers=128
bt-save-metadata=true
bt-load-saved-metadata=true
follow-torrent=true
seed-ratio=1.0
seed-time=0
EOF

step "Installing tracker updater (better torrent speed)"
cat > /usr/local/bin/aria2-update-trackers <<EOF
#!/usr/bin/env bash
set -euo pipefail
list="\$(curl -fsSL --max-time 30 "${TRACKER_URL}" | grep -v '^\s*\$' | paste -sd, -)" || exit 0
[[ -n "\$list" ]] || exit 0
conf="${CONF_DIR}/aria2.conf"
sed -i '/^bt-tracker=/d' "\$conf"
echo "bt-tracker=\$list" >> "\$conf"
systemctl try-restart aria2 || true
EOF
chmod +x /usr/local/bin/aria2-update-trackers

cat > /etc/systemd/system/aria2-trackers.service <<'EOF'
[Unit]
Description=Update aria2 BitTorrent tracker list
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/aria2-update-trackers
EOF

cat > /etc/systemd/system/aria2-trackers.timer <<'EOF'
[Unit]
Description=Weekly aria2 tracker list update

[Timer]
OnCalendar=weekly
Persistent=true

[Install]
WantedBy=timers.target
EOF

/usr/local/bin/aria2-update-trackers || yellow "Tracker list download failed, skipping."

step "Creating aria2 service"
cat > /etc/systemd/system/aria2.service <<EOF
[Unit]
Description=aria2 Download Manager
After=network-online.target
Wants=network-online.target

[Service]
User=${ARIA2_USER}
Group=${ARIA2_USER}
UMask=0002
ExecStart=/usr/bin/aria2c --conf-path=${CONF_DIR}/aria2.conf
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

step "Installing AriaNg web UI"
ARIANG_VERSION="$(curl -fsSL --max-time 20 https://api.github.com/repos/mayswind/AriaNg/releases/latest \
  | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1 || true)"
ARIANG_VERSION="${ARIANG_VERSION:-$ARIANG_FALLBACK_VERSION}"
tmp="$(mktemp -d)"
curl -fsSL -o "${tmp}/ariang.zip" \
  "https://github.com/mayswind/AriaNg/releases/download/${ARIANG_VERSION}/AriaNg-${ARIANG_VERSION}.zip"
rm -rf "${WEB_DIR:?}"/*
unzip -q "${tmp}/ariang.zip" -d "$WEB_DIR"
rm -rf "$tmp"
green "AriaNg ${ARIANG_VERSION} installed."

step "Installing File Browser (upload / download files)"
if ! command -v filebrowser >/dev/null; then
  curl -fsSL https://raw.githubusercontent.com/filebrowser/get/master/get.sh | bash
fi
FB_BIN="$(command -v filebrowser)"

FB_NEW=0
if [[ ! -f "$FB_DB" ]]; then
  FB_NEW=1
  [[ -n "$FB_PASSWORD" ]] || FB_PASSWORD="$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 16)"
  "$FB_BIN" -d "$FB_DB" config init >/dev/null
  "$FB_BIN" -d "$FB_DB" config set \
    --address 127.0.0.1 --port 8080 --root "$DOWNLOAD_DIR" --baseurl /files >/dev/null
  "$FB_BIN" -d "$FB_DB" users add admin "$FB_PASSWORD" --perm.admin >/dev/null
elif [[ -n "$FB_PASSWORD" ]]; then
  "$FB_BIN" -d "$FB_DB" users update admin --password "$FB_PASSWORD" >/dev/null
fi

cat > /etc/systemd/system/filebrowser.service <<EOF
[Unit]
Description=File Browser
After=network.target

[Service]
User=${ARIA2_USER}
Group=${ARIA2_USER}
UMask=0002
ExecStart=${FB_BIN} -d ${FB_DB}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chown -R "$ARIA2_USER:$ARIA2_USER" "$CONF_DIR" "$DOWNLOAD_DIR"
chmod 600 "${CONF_DIR}/aria2.conf"

step "Configuring nginx"
SERVER_NAME="${DOMAIN:-_}"
cat > /etc/nginx/sites-available/aria2 <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name ${SERVER_NAME};

    client_max_body_size 0;
    proxy_request_buffering off;

    location / {
        root ${WEB_DIR};
        index index.html;
    }

    location /jsonrpc {
        proxy_pass http://127.0.0.1:6800/jsonrpc;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 1d;
    }

    location /files {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 1d;
    }
}
EOF
ln -sf /etc/nginx/sites-available/aria2 /etc/nginx/sites-enabled/aria2
rm -f /etc/nginx/sites-enabled/default
nginx -t

step "Starting services"
systemctl daemon-reload
systemctl enable --now aria2 filebrowser aria2-trackers.timer
systemctl restart aria2 filebrowser
systemctl reload nginx

SCHEME="http"
PORT=80
if [[ -n "$DOMAIN" ]]; then
  step "Getting HTTPS certificate for ${DOMAIN}"
  apt-get install -y certbot python3-certbot-nginx
  if certbot --nginx --non-interactive --agree-tos --redirect -m "$EMAIL" -d "$DOMAIN"; then
    SCHEME="https"; PORT=443
  else
    yellow "certbot failed. Make sure ${DOMAIN} points to this server. Continuing with HTTP."
  fi
fi

step "Firewall"
if command -v ufw >/dev/null; then
  ufw allow OpenSSH >/dev/null || true
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw allow 6881:6999/tcp >/dev/null
  ufw allow 6881:6999/udp >/dev/null
  if ufw status | grep -q "Status: active"; then
    green "ufw rules added."
  else
    yellow "ufw is installed but inactive. Rules were added; enable it yourself with: sudo ufw enable"
  fi
fi

HOST="${DOMAIN:-$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')}"
WS="ws"; [[ "$SCHEME" == "https" ]] && WS="wss"
SECRET_B64="$(printf '%s' "$RPC_SECRET" | base64)"
ARIANG_URL="${SCHEME}://${HOST}/#!/settings/rpc/set/${WS}/${HOST}/${PORT}/jsonrpc/${SECRET_B64}"
FB_URL="${SCHEME}://${HOST}/files"

{
  echo "aria2 web setup - $(date)"
  echo
  echo "AriaNg (auto-configured link): ${ARIANG_URL}"
  echo "AriaNg plain URL:              ${SCHEME}://${HOST}/"
  echo "  RPC host:  ${HOST}"
  echo "  RPC port:  ${PORT}"
  echo "  RPC path:  /jsonrpc"
  echo "  RPC token: ${RPC_SECRET}"
  echo
  echo "File Browser: ${FB_URL}"
  echo "  user: admin"
  if [[ $FB_NEW -eq 1 || -n "$FB_PASSWORD" ]]; then
    echo "  pass: ${FB_PASSWORD}"
  else
    echo "  pass: (unchanged from previous install)"
  fi
  echo
  echo "Download folder: ${DOWNLOAD_DIR}"
} > "$CRED_FILE"
chmod 600 "$CRED_FILE"

echo
green "=============================================="
green "  Setup complete!"
green "=============================================="
cat "$CRED_FILE"
echo
yellow "These details are saved in ${CRED_FILE}"
yellow "Open the AriaNg auto-configured link once; it saves the RPC settings in your browser."
