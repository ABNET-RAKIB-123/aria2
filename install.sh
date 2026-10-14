#!/usr/bin/env bash
#
# aria2 + web panel (downloads, server files, Google Drive) one-shot
# installer for Ubuntu/Debian.
#
#   Install:    sudo bash install.sh
#   Uninstall:  sudo bash install.sh --uninstall
#
# Optional environment variables:
#   DOMAIN=example.com       enable HTTPS with Let's Encrypt for this domain
#                            (DOMAIN=none switches back to IP + HTTP)
#   EMAIL=you@example.com    email for Let's Encrypt (required with DOMAIN)
#   DOWNLOAD_DIR=/srv/downloads
#
# DOMAIN, EMAIL and DOWNLOAD_DIR are remembered in /etc/aria2-install.conf,
# so re-running the script (e.g. to update) keeps them without retyping.
#   PANEL_PASSWORD=...       web panel password (auto-generated on first install)
#   REF=main                 git branch/tag to fetch panel files from when the
#                            script is run without a local checkout
#
set -euo pipefail

# Root-owned on purpose: /etc/aria2 belongs to the aria2 user, and this file
# feeds values into root-run commands.
SAVED_CONF="/etc/aria2-install.conf"
saved() { { [[ -f "$SAVED_CONF" ]] && sed -n "s/^$1=//p" "$SAVED_CONF" | head -n1; } || true; }

DOWNLOAD_DIR="${DOWNLOAD_DIR:-$(saved DOWNLOAD_DIR)}"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-/srv/downloads}"
if [[ "${DOMAIN:-}" == "none" ]]; then
  DOMAIN=""
  EMAIL=""
else
  DOMAIN="${DOMAIN:-$(saved DOMAIN)}"
  EMAIL="${EMAIL:-$(saved EMAIL)}"
fi
PANEL_PASSWORD="${PANEL_PASSWORD:-}"
REF="${REF:-main}"
REPO_RAW="https://raw.githubusercontent.com/ABNET-RAKIB-123/aria2/${REF}"

ARIA2_USER="aria2"
CONF_DIR="/etc/aria2"
PANEL_DIR="/opt/aria2-panel"
PANEL_CONF="${CONF_DIR}/panel.json"
CRED_FILE="/root/aria2-web-credentials.txt"
TRACKER_URL="https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/trackers_best.txt"

green()  { printf '\033[1;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
red()    { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
step()   { echo; green "==> $*"; }

[[ $EUID -eq 0 ]] || { red "Run as root: sudo bash $0"; exit 1; }
command -v apt-get >/dev/null || { red "This script supports Ubuntu/Debian only."; exit 1; }

# Leftovers from the earlier AriaNg + File Browser version of this script.
remove_legacy() {
  systemctl disable --now filebrowser 2>/dev/null || true
  rm -f /etc/systemd/system/filebrowser.service /usr/local/bin/filebrowser "${CONF_DIR}/filebrowser.db"
  rm -rf /var/www/ariang
}

uninstall() {
  step "Removing aria2 web setup"
  systemctl disable --now aria2 aria2-panel aria2-trackers.timer 2>/dev/null || true
  remove_legacy
  rm -f /etc/systemd/system/aria2.service \
        /etc/systemd/system/aria2-panel.service \
        /etc/systemd/system/aria2-trackers.service \
        /etc/systemd/system/aria2-trackers.timer \
        /usr/local/bin/aria2-update-trackers \
        /etc/nginx/sites-enabled/aria2 \
        /etc/nginx/sites-available/aria2 \
        "$SAVED_CONF"
  rm -rf "$PANEL_DIR" "$CONF_DIR"
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
if [[ -n "$DOMAIN" && ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
  red "Invalid DOMAIN: ${DOMAIN}"; exit 1
fi
if [[ ! "$DOWNLOAD_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
  red "DOWNLOAD_DIR must be an absolute path without spaces: ${DOWNLOAD_DIR}"; exit 1
fi

step "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y aria2 nginx python3 unzip curl openssl ca-certificates

if ! command -v rclone >/dev/null; then
  step "Installing rclone (Google Drive)"
  curl -fsSL https://rclone.org/install.sh | bash
fi

step "Creating user and directories"
id "$ARIA2_USER" &>/dev/null || useradd -r -m -d /var/lib/aria2 -s /usr/sbin/nologin "$ARIA2_USER"
mkdir -p "$CONF_DIR" "$DOWNLOAD_DIR" "$PANEL_DIR"
touch "${CONF_DIR}/aria2.session" "${CONF_DIR}/rclone.conf"
remove_legacy

# Keep the existing token on re-runs.
RPC_SECRET=""
if [[ -f "${CONF_DIR}/aria2.conf" ]]; then
  RPC_SECRET="$(sed -n 's/^rpc-secret=//p' "${CONF_DIR}/aria2.conf")"
fi
[[ -n "$RPC_SECRET" ]] || RPC_SECRET="$(openssl rand -hex 16)"

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

## RPC (only used by the panel on localhost)
enable-rpc=true
rpc-listen-all=false
rpc-listen-port=6800
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

step "Installing web panel"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
for f in server.py index.html; do
  if [[ -n "$SRC_DIR" && -f "${SRC_DIR}/panel/${f}" ]]; then
    cp "${SRC_DIR}/panel/${f}" "${PANEL_DIR}/${f}"
  else
    curl -fsSL -o "${PANEL_DIR}/${f}" "${REPO_RAW}/panel/${f}"
  fi
done

# Write panel.json, keeping the password and session key from earlier runs
# unless PANEL_PASSWORD is given. Prints the new password when one is set.
NEW_PASSWORD="$(PANEL_CONF="$PANEL_CONF" PANEL_PASSWORD="$PANEL_PASSWORD" DOWNLOAD_DIR="$DOWNLOAD_DIR" \
  RPC_SECRET="$RPC_SECRET" CONF_DIR="$CONF_DIR" python3 - <<'PY'
import hashlib, json, os, secrets
path = os.environ["PANEL_CONF"]
try:
    with open(path) as f:
        cfg = json.load(f)
except (OSError, ValueError):
    cfg = {}
password = os.environ.get("PANEL_PASSWORD", "")
if not password and "password_hash" not in cfg:
    password = secrets.token_urlsafe(12)
if password:
    cfg["password_salt"] = secrets.token_hex(16)
    cfg["password_hash"] = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), cfg["password_salt"].encode(), 200000).hex()
cfg.setdefault("session_key", secrets.token_hex(32))
cfg.update(
    download_dir=os.environ["DOWNLOAD_DIR"],
    rpc_secret=os.environ["RPC_SECRET"],
    rclone_config=os.path.join(os.environ["CONF_DIR"], "rclone.conf"),
    state_file=os.path.join(os.environ["CONF_DIR"], "panel-state.json"),
    remote=cfg.get("remote", "gdrive"),
    listen="127.0.0.1",
    port=8090,
)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(password)
PY
)"

cat > /etc/systemd/system/aria2-panel.service <<EOF
[Unit]
Description=aria2 web panel
After=network.target aria2.service

[Service]
User=${ARIA2_USER}
Group=${ARIA2_USER}
UMask=0002
Environment=PANEL_CONFIG=${PANEL_CONF}
Environment=HOME=/var/lib/aria2
ExecStart=/usr/bin/python3 ${PANEL_DIR}/server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

chown -R "$ARIA2_USER:$ARIA2_USER" "$CONF_DIR" "$DOWNLOAD_DIR"
chmod 600 "${CONF_DIR}/aria2.conf" "$PANEL_CONF" "${CONF_DIR}/rclone.conf"
chmod 644 "${PANEL_DIR}"/*

step "Configuring nginx"
cat > /etc/nginx/sites-available/aria2 <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name ${DOMAIN:-_};

    client_max_body_size 0;

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_read_timeout 1d;
        proxy_send_timeout 1d;
    }
}
EOF
ln -sf /etc/nginx/sites-available/aria2 /etc/nginx/sites-enabled/aria2
rm -f /etc/nginx/sites-enabled/default
nginx -t

step "Starting services"
systemctl daemon-reload
systemctl enable --now aria2 aria2-panel aria2-trackers.timer
systemctl restart aria2 aria2-panel
systemctl reload nginx

SCHEME="http"
if [[ -n "$DOMAIN" ]]; then
  step "Getting HTTPS certificate for ${DOMAIN}"
  apt-get install -y certbot python3-certbot-nginx
  if certbot --nginx --non-interactive --agree-tos --redirect --keep-until-expiring -m "$EMAIL" -d "$DOMAIN"; then
    SCHEME="https"
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

printf 'DOWNLOAD_DIR=%s\nDOMAIN=%s\nEMAIL=%s\n' "$DOWNLOAD_DIR" "$DOMAIN" "$EMAIL" > "$SAVED_CONF"
chmod 600 "$SAVED_CONF"

HOST="${DOMAIN:-$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')}"

{
  echo "aria2 web panel - $(date)"
  echo
  echo "Panel URL: ${SCHEME}://${HOST}/"
  if [[ -n "$NEW_PASSWORD" ]]; then
    echo "Password:  ${NEW_PASSWORD}"
  else
    echo "Password:  (unchanged from previous install)"
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
yellow "Saved in ${CRED_FILE}. Change the password from the panel's Settings tab."
yellow "Connect Google Drive from the panel's 'Google Drive' tab."
