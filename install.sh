#!/usr/bin/env bash
#
# aria2 + web panel (downloads, video sites, server files, cloud storage,
# Telegram bot, RSS) one-shot installer for Ubuntu/Debian.
#
#   Install / update:  sudo bash install.sh
#   Uninstall:         sudo bash install.sh --uninstall
#
# Optional environment variables:
#   DOMAIN=example.com       enable HTTPS with Let's Encrypt for this domain
#                            (DOMAIN=none switches back to IP + HTTP)
#   EMAIL=you@example.com    email for Let's Encrypt (required with DOMAIN)
#   TUNNEL=1                 free HTTPS link through Cloudflare Tunnel, no domain
#                            or open ports needed (TUNNEL=0 turns it off)
#   DOWNLOAD_DIR=/srv/downloads
#   PANEL_PASSWORD=...       sets the 'admin' user's password (auto-generated
#                            on first install)
#   REF=main                 git branch/tag to fetch panel files from when the
#                            script is run without a local checkout
#
# DOMAIN, EMAIL, TUNNEL and DOWNLOAD_DIR are remembered in
# /etc/aria2-install.conf, so re-running the script (e.g. to update) keeps
# them without retyping.
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
TUNNEL="${TUNNEL:-$(saved TUNNEL)}"
TUNNEL="${TUNNEL:-0}"
PANEL_PASSWORD="${PANEL_PASSWORD:-}"
REF="${REF:-main}"
REPO_RAW="https://raw.githubusercontent.com/ABNET-RAKIB-123/aria2/${REF}"

ARIA2_USER="aria2"
ARIA2_HOME="/var/lib/aria2"
CONF_DIR="/etc/aria2"
PANEL_DIR="/opt/aria2-panel"
PANEL_CONF="${CONF_DIR}/panel.json"
CRED_FILE="/root/aria2-web-credentials.txt"
TRACKER_URL="https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/trackers_best.txt"
TUNNEL_METRICS="127.0.0.1:45678"

green()  { printf '\033[1;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
red()    { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
step()   { echo; green "==> $*"; }

[[ $EUID -eq 0 ]] || { red "Run as root: sudo bash $0"; exit 1; }
command -v apt-get >/dev/null || { red "This script supports Ubuntu/Debian only."; exit 1; }

case "$(uname -m)" in
  x86_64 | amd64) ARCH="amd64" ;;
  aarch64 | arm64) ARCH="arm64" ;;
  *) ARCH="other" ;;
esac

# Leftovers from earlier versions of this script.
remove_legacy() {
  systemctl disable --now filebrowser aria2-trackers.timer 2>/dev/null || true
  rm -f /etc/systemd/system/filebrowser.service /usr/local/bin/filebrowser "${CONF_DIR}/filebrowser.db" \
        /etc/systemd/system/aria2-trackers.service /etc/systemd/system/aria2-trackers.timer
  rm -rf /var/www/ariang
}

remove_tunnel() {
  systemctl disable --now aria2-tunnel 2>/dev/null || true
  rm -f /etc/systemd/system/aria2-tunnel.service
}

uninstall() {
  step "Removing aria2 web setup"
  systemctl disable --now aria2 aria2-panel aria2-weekly.timer 2>/dev/null || true
  remove_tunnel
  remove_legacy
  rm -f /etc/systemd/system/aria2.service \
        /etc/systemd/system/aria2-panel.service \
        /etc/systemd/system/aria2-weekly.service \
        /etc/systemd/system/aria2-weekly.timer \
        /usr/local/bin/aria2-update-trackers \
        /usr/local/bin/aria2-weekly \
        /usr/local/bin/aria2-panel \
        /usr/local/bin/yt-dlp \
        /etc/nginx/sites-enabled/aria2 \
        /etc/nginx/sites-available/aria2 \
        "$SAVED_CONF"
  rm -rf "$PANEL_DIR" "$CONF_DIR" "${ARIA2_HOME}/thumbs"
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
[[ "$TUNNEL" == "1" ]] || TUNNEL=0

step "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y aria2 nginx python3 unzip curl openssl ca-certificates ffmpeg p7zip-full
# Optional extras: newer 7-Zip and unrar handle RAR5 archives better, and Noto
# fonts let burned-in subtitles render Bangla and other scripts. Not every
# release has them (unrar lives in multiverse/non-free), so failures are fine.
for pkg in 7zip unrar unattended-upgrades fonts-noto-core; do
  apt-get install -y "$pkg" >/dev/null 2>&1 || yellow "Optional package ${pkg} not available, skipping."
done

if ! command -v rclone >/dev/null; then
  step "Installing rclone (cloud storage)"
  curl -fsSL https://rclone.org/install.sh | bash
fi

step "Installing yt-dlp (YouTube, Facebook and 1000+ sites)"
case "$ARCH" in
  amd64) YTDLP_ASSET="yt-dlp_linux" ;;
  arm64) YTDLP_ASSET="yt-dlp_linux_aarch64" ;;
  *) YTDLP_ASSET="yt-dlp" ;;
esac
if curl -fsSL -o /usr/local/bin/yt-dlp.new \
     "https://github.com/yt-dlp/yt-dlp/releases/latest/download/${YTDLP_ASSET}"; then
  chmod 755 /usr/local/bin/yt-dlp.new
  mv /usr/local/bin/yt-dlp.new /usr/local/bin/yt-dlp
else
  rm -f /usr/local/bin/yt-dlp.new
  yellow "yt-dlp download failed; video-site downloads will not work until the next update."
fi

step "Server tuning (swap, automatic security updates)"
MEM_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
ROOT_FREE_GB="$(df -Pk / | awk 'NR==2 {print int($4/1024/1024)}')"
if [[ -z "$(swapon --show 2>/dev/null)" && "$MEM_MB" -lt 2048 && "$ROOT_FREE_GB" -gt 6 && ! -e /swapfile ]]; then
  if fallocate -l 2G /swapfile 2>/dev/null && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile; then
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    green "Added 2 GB swap (RAM is ${MEM_MB} MB)."
  else
    rm -f /swapfile
    yellow "Could not add swap, skipping."
  fi
fi
if dpkg -s unattended-upgrades >/dev/null 2>&1 && [[ ! -f /etc/apt/apt.conf.d/20auto-upgrades ]]; then
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
    > /etc/apt/apt.conf.d/20auto-upgrades
  green "Automatic security updates enabled."
fi

step "Creating user and directories"
id "$ARIA2_USER" &>/dev/null || useradd -r -m -d "$ARIA2_HOME" -s /usr/sbin/nologin "$ARIA2_USER"
mkdir -p "$CONF_DIR" "$DOWNLOAD_DIR" "$PANEL_DIR" "${ARIA2_HOME}/thumbs"
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

## Speed (the panel's Settings tab can change the limits at runtime)
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

step "Installing weekly updater (torrent trackers, yt-dlp)"
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

cat > /usr/local/bin/aria2-weekly <<'EOF'
#!/usr/bin/env bash
/usr/local/bin/aria2-update-trackers || true
[[ -x /usr/local/bin/yt-dlp ]] && /usr/local/bin/yt-dlp -U || true
EOF
chmod +x /usr/local/bin/aria2-weekly

cat > /etc/systemd/system/aria2-weekly.service <<'EOF'
[Unit]
Description=Weekly aria2 maintenance (tracker list, yt-dlp update)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/aria2-weekly
EOF

cat > /etc/systemd/system/aria2-weekly.timer <<'EOF'
[Unit]
Description=Weekly aria2 maintenance

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
fetch_file() {
  if [[ -n "$SRC_DIR" && -f "${SRC_DIR}/$1" ]]; then
    cp "${SRC_DIR}/$1" "$2"
  else
    curl -fsSL -o "$2" "${REPO_RAW}/$1"
  fi
}
fetch_file panel/server.py "${PANEL_DIR}/server.py"
fetch_file panel/index.html "${PANEL_DIR}/index.html"
fetch_file panel/app.css "${PANEL_DIR}/app.css"
fetch_file panel/app.js "${PANEL_DIR}/app.js"
fetch_file panel/sw.js "${PANEL_DIR}/sw.js"
fetch_file panel/manifest.webmanifest "${PANEL_DIR}/manifest.webmanifest"
fetch_file panel/icon.svg "${PANEL_DIR}/icon.svg"
fetch_file aria2-panel /usr/local/bin/aria2-panel
chmod 755 /usr/local/bin/aria2-panel

# Write panel.json, keeping users and the session key from earlier runs.
# PANEL_PASSWORD (or a fresh install) sets the 'admin' password; the new
# password is printed so the summary can show it.
NEW_PASSWORD="$(PANEL_CONF="$PANEL_CONF" PANEL_PASSWORD="$PANEL_PASSWORD" DOWNLOAD_DIR="$DOWNLOAD_DIR" \
  RPC_SECRET="$RPC_SECRET" CONF_DIR="$CONF_DIR" DATA_DIR="$ARIA2_HOME" TUNNEL_METRICS="$TUNNEL_METRICS" \
  python3 - <<'PY'
import hashlib, json, os, secrets
path = os.environ["PANEL_CONF"]
try:
    with open(path) as f:
        cfg = json.load(f)
except (OSError, ValueError):
    cfg = {}
users = cfg.setdefault("users", {})
if "password_hash" in cfg:
    users.setdefault("admin", {"salt": cfg.pop("password_salt"), "hash": cfg.pop("password_hash"), "role": "admin"})
    cfg.pop("password_hash", None)
    cfg.pop("password_salt", None)
password = os.environ.get("PANEL_PASSWORD", "")
if not password and not users:
    password = secrets.token_urlsafe(12)
if password:
    salt = secrets.token_hex(16)
    users["admin"] = {"salt": salt, "role": "admin",
                      "hash": hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000).hex()}
cfg.setdefault("session_key", secrets.token_hex(32))
cfg.pop("remote", None)
cfg.update(
    download_dir=os.environ["DOWNLOAD_DIR"],
    rpc_secret=os.environ["RPC_SECRET"],
    rclone_config=os.path.join(os.environ["CONF_DIR"], "rclone.conf"),
    state_file=os.path.join(os.environ["CONF_DIR"], "panel-state.json"),
    data_dir=os.environ["DATA_DIR"],
    tunnel_metrics=os.environ["TUNNEL_METRICS"],
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
Environment=HOME=${ARIA2_HOME}
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 ${PANEL_DIR}/server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

chown -R "$ARIA2_USER:$ARIA2_USER" "$CONF_DIR" "$DOWNLOAD_DIR" "$ARIA2_HOME"
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
systemctl enable --now aria2 aria2-panel aria2-weekly.timer
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

TUNNEL_URL=""
if [[ "$TUNNEL" == "1" ]]; then
  step "Setting up Cloudflare Tunnel (free HTTPS link)"
  if ! command -v cloudflared >/dev/null; then
    if [[ "$ARCH" == "other" ]]; then
      yellow "Cloudflare Tunnel is only set up automatically on amd64/arm64 servers."
    else
      tmp_deb="$(mktemp --suffix=.deb)"
      curl -fsSL -o "$tmp_deb" \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb"
      dpkg -i "$tmp_deb" >/dev/null
      rm -f "$tmp_deb"
    fi
  fi
  if command -v cloudflared >/dev/null; then
    # After certbot the port-80 server only redirects the domain, so point the
    # tunnel at the HTTPS server instead.
    ORIGIN="http://127.0.0.1:80"
    EXTRA=""
    if [[ "$SCHEME" == "https" ]]; then
      ORIGIN="https://127.0.0.1:443"
      EXTRA="--no-tls-verify"
    fi
    cat > /etc/systemd/system/aria2-tunnel.service <<EOF
[Unit]
Description=Cloudflare quick tunnel for the aria2 panel
After=network-online.target nginx.service
Wants=network-online.target

[Service]
User=${ARIA2_USER}
ExecStart=$(command -v cloudflared) tunnel --no-autoupdate --metrics ${TUNNEL_METRICS} --url ${ORIGIN} ${EXTRA}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable aria2-tunnel >/dev/null
    systemctl restart aria2-tunnel
    for _ in $(seq 1 30); do
      TUNNEL_URL="$(curl -fsS --max-time 2 "http://${TUNNEL_METRICS}/quicktunnel" 2>/dev/null \
        | sed -n 's/.*"hostname":"\([^"]*\)".*/https:\/\/\1/p' || true)"
      [[ -n "$TUNNEL_URL" ]] && break
      sleep 1
    done
    [[ -n "$TUNNEL_URL" ]] || yellow "Tunnel started but its link is not ready yet. Check later with: sudo aria2-panel url"
  fi
else
  remove_tunnel
  systemctl daemon-reload
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

printf 'DOWNLOAD_DIR=%s\nDOMAIN=%s\nEMAIL=%s\nTUNNEL=%s\n' "$DOWNLOAD_DIR" "$DOMAIN" "$EMAIL" "$TUNNEL" > "$SAVED_CONF"
chmod 600 "$SAVED_CONF"

HOST="${DOMAIN:-$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')}"

{
  echo "aria2 web panel - $(date)"
  echo
  echo "Panel URL: ${SCHEME}://${HOST}/"
  [[ -n "$TUNNEL_URL" ]] && echo "HTTPS URL: ${TUNNEL_URL}   (Cloudflare Tunnel, changes after a restart)"
  echo "Username:  admin"
  if [[ -n "$NEW_PASSWORD" ]]; then
    echo "Password:  ${NEW_PASSWORD}"
  else
    echo "Password:  (unchanged from previous install)"
  fi
  echo
  echo "Download folder: ${DOWNLOAD_DIR}"
  echo "Health check:    sudo aria2-panel status"
} > "$CRED_FILE"
chmod 600 "$CRED_FILE"

echo
green "=============================================="
green "  Setup complete!"
green "=============================================="
cat "$CRED_FILE"
echo
yellow "Saved in ${CRED_FILE}. Change the password from the panel's Settings tab."
yellow "Run 'sudo aria2-panel status' any time to check that everything is working."
