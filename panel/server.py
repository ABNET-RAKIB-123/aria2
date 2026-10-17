#!/usr/bin/env python3
"""aria2 web panel.

Downloads (aria2 + yt-dlp), server files, cloud storage (rclone), a Telegram
bot and RSS auto-download. Uses only the Python standard library.
Configuration comes from the JSON file named by PANEL_CONFIG (default
/etc/aria2/panel.json), written by install.sh.
"""
import base64
import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.1"
CONFIG_PATH = os.environ.get("PANEL_CONFIG", "/etc/aria2/panel.json")
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

with open(CONFIG_PATH) as f:
    CFG = json.load(f)

ROOT = os.path.realpath(CFG["download_dir"])
RCLONE_CONF = CFG["rclone_config"]
STATE_PATH = CFG["state_file"]
RSS_SEEN_PATH = os.path.join(os.path.dirname(STATE_PATH), "rss-seen.json")
DATA_DIR = CFG.get("data_dir", "/var/lib/aria2")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
SESSION_KEY = CFG["session_key"].encode()
TUNNEL_METRICS = CFG.get("tunnel_metrics", "127.0.0.1:45678")
SESSION_TTL = 30 * 86400
CHUNK = 1024 * 1024
HIDDEN_SUFFIXES = (".aria2", ".uploading")
ROLES = {"viewer": 1, "user": 2, "admin": 3}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

VIDEO_HOST_RE = re.compile(
    r"^https?://([\w-]+\.)*(youtube\.com|youtu\.be|facebook\.com|fb\.watch|instagram\.com|tiktok\.com|"
    r"twitter\.com|x\.com|vimeo\.com|dailymotion\.com|soundcloud\.com|reddit\.com|twitch\.tv|bilibili\.com)/",
    re.I)
DRIVE_LINK_RE = re.compile(r"^https?://(drive|docs)\.google\.com/", re.I)
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
VIDEO_EXT = (".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".flv", ".wmv", ".ts")
TAR_EXT = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
ARCHIVE_EXT = (".zip", ".rar", ".7z") + TAR_EXT
SUB_EXT = (".srt", ".ass", ".ssa", ".vtt")
IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
MP4_LIKE = (".mp4", ".m4v", ".mov")
LANG_RE = re.compile(r"^[a-z]{2,3}$")
SUB_LANGS_RE = re.compile(r"^[A-Za-z0-9,.*-]{1,60}$")

YTDLP_FORMATS = {
    "best": "bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "720": "bv*[height<=720]+ba/b[height<=720]/b",
    "480": "bv*[height<=480]+ba/b[height<=480]/b",
    "audio": "ba/b",
}

REMOTE_TYPES = {"drive": "Google Drive", "dropbox": "Dropbox", "onedrive": "OneDrive", "mega": "Mega"}
REMOTE_PREFIX = {"drive": "gdrive", "dropbox": "dropbox", "onedrive": "onedrive", "mega": "mega"}
OAUTH_TYPES = ("drive", "dropbox", "onedrive")

DEFAULT_STATE = {
    "autoupload": False,
    "autoupload_remote": "",
    "autoupload_dest": "aria2",
    "autoupload_move": False,
    "disk_guard_gb": 2.0,
    "speed_down_kb": 0,
    "speed_up_kb": 0,
    "schedule_enabled": False,
    "schedule_start": "01:00",
    "schedule_end": "08:00",
    "tz_offset_min": 0,
    "telegram_token": "",
    "telegram_chat": "",
    "telegram_notify": True,
    "rss_feeds": [],
    "rss_dest": "rss",
    "rss_interval_min": 15,
    "tunnel_last": "",
}

SETTING_TYPES = {
    "autoupload": bool, "autoupload_remote": str, "autoupload_dest": str, "autoupload_move": bool,
    "disk_guard_gb": float, "speed_down_kb": int, "speed_up_kb": int,
    "schedule_enabled": bool, "schedule_start": str, "schedule_end": str, "tz_offset_min": int,
    "telegram_token": str, "telegram_chat": str, "telegram_notify": bool,
    "rss_dest": str, "rss_interval_min": int,
}

CONFIG_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- config / state

def write_json(path, data, mode=0o600):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def save_config():
    with CONFIG_LOCK:
        write_json(CONFIG_PATH, CFG)


def migrate_config():
    """Older installs stored a single password; turn it into the 'admin' user."""
    if "users" in CFG:
        return
    CFG["users"] = {}
    if "password_hash" in CFG:
        CFG["users"]["admin"] = {"salt": CFG.pop("password_salt"), "hash": CFG.pop("password_hash"),
                                 "role": "admin"}
    save_config()


def load_state():
    state = json.loads(json.dumps(DEFAULT_STATE))
    try:
        with open(STATE_PATH) as f:
            state.update(json.load(f))
    except (OSError, ValueError):
        pass
    return state


def update_state(changes):
    with STATE_LOCK:
        state = load_state()
        state.update(changes)
        write_json(STATE_PATH, state)
        return state


# ---------------------------------------------------------------- helpers

def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000).hex()


def sign(data):
    return hmac.new(SESSION_KEY, data.encode(), hashlib.sha256).hexdigest()


def b64e(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024


def local_path(rel):
    rel = (rel or "").strip("/")
    path = os.path.realpath(os.path.join(ROOT, rel))
    if path != ROOT and not path.startswith(ROOT + os.sep):
        raise ApiError(400, "ভুল পাথ")
    return path


def rel_path(path):
    rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
    return "" if rel == "." else rel


def check_name(name):
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise ApiError(400, "ভুল নাম")
    return name


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    if base.endswith(".tar"):
        base, ext = base[:-4], ".tar" + ext
    n = 1
    while os.path.exists("%s (%d)%s" % (base, n, ext)):
        n += 1
    return "%s (%d)%s" % (base, n, ext)


def clean_folder(folder):
    return "/".join(p for p in (folder or "").strip().split("/") if p and p not in (".", ".."))


def rclone(*args, timeout=120):
    try:
        r = subprocess.run(["rclone", "--config", RCLONE_CONF] + list(args),
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise ApiError(500, "rclone ইনস্টল নেই")
    except subprocess.TimeoutExpired:
        raise ApiError(504, "rclone সময়মতো উত্তর দেয়নি")
    if r.returncode != 0:
        lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
        raise ApiError(502, lines[-1] if lines else "rclone ব্যর্থ হয়েছে")
    return r.stdout


def in_window(start, end, offset_min):
    now = time.gmtime(time.time() + offset_min * 60)
    n = now.tm_hour * 60 + now.tm_min
    s = int(start[:2]) * 60 + int(start[3:])
    e = int(end[:2]) * 60 + int(end[3:])
    if s == e:
        return True
    return s <= n < e if s < e else (n >= s or n < e)


# ---------------------------------------------------------------- cloud remotes

_REMOTES = {"mtime": None, "list": []}


def remotes():
    try:
        mtime = os.path.getmtime(RCLONE_CONF)
    except OSError:
        return []
    if _REMOTES["mtime"] != mtime:
        result = []
        try:
            out = rclone("listremotes", "--long", timeout=20)
        except ApiError:
            out = ""
        for line in out.splitlines():
            name, _, typ = line.partition(":")
            if name.strip():
                typ = typ.strip()
                result.append({"name": name.strip(), "type": typ, "label": REMOTE_TYPES.get(typ, typ)})
        _REMOTES.update(mtime=mtime, list=result)
    return list(_REMOTES["list"])


def get_remote(name="", typ=None):
    found = [r for r in remotes() if typ is None or r["type"] == typ]
    if name:
        for r in found:
            if r["name"] == name:
                return r
    elif found:
        return found[0]
    if typ:
        raise ApiError(400, "%s কানেক্ট করা নেই" % REMOTE_TYPES.get(typ, typ))
    raise ApiError(400, "Cloud একাউন্ট পাওয়া যায়নি, আগে কানেক্ট করুন")


def rpath(remote, rel):
    return "%s:%s" % (remote, clean_folder(rel))


def rdir(remote, folder):
    """Remote folder path that ends in ':' or '/', for commands that need a directory target."""
    p = rpath(remote, folder)
    return p if p.endswith(":") else p + "/"


def new_remote_name(typ):
    names = {r["name"] for r in remotes()}
    base = REMOTE_PREFIX[typ]
    if base not in names:
        return base
    n = 2
    while "%s%d" % (base, n) in names:
        n += 1
    return "%s%d" % (base, n)


def create_remote(typ, params, obscure=False):
    name = new_remote_name(typ)
    args = ["config", "create", name, typ] + params + ["--non-interactive"]
    if obscure:
        args.append("--obscure")
    rclone(*args)
    try:
        rclone("lsjson", "--max-depth", "1", name + ":", timeout=90)
    except ApiError as e:
        try:
            rclone("config", "delete", name)
        except ApiError:
            pass
        raise ApiError(502, "কানেক্ট হয়নি: %s" % e)
    return name


AUTH = {"proc": None, "out": [], "type": None}
AUTH_LOCK = threading.Lock()


def parse_token(blob):
    blob = (blob or "").strip()
    m = re.search(r"--->\s*(.+?)\s*<---", blob, re.S)
    if m:
        blob = m.group(1).strip()
    candidates = [blob]
    try:
        candidates.append(b64d(blob.replace("+", "-").replace("/", "_")).decode())
    except Exception:
        pass
    for cand in candidates:
        try:
            data = json.loads(cand)
        except ValueError:
            continue
        if isinstance(data, dict):
            if "access_token" in data:
                return json.dumps(data)
            tok = data.get("token")
            if isinstance(tok, str) and tok:
                return tok
            if isinstance(tok, dict):
                return json.dumps(tok)
    raise ApiError(400, "টোকেন পড়া যায়নি। rclone যা দেখিয়েছে পুরোটা পেস্ট করুন।")


def oauth_params(typ, token):
    params = ["token", token]
    if typ == "drive":
        params += ["scope", "drive"]
    return params


def auth_start(typ):
    if typ not in OAUTH_TYPES:
        raise ApiError(400, "এই ধরনের একাউন্ট সাপোর্ট করে না")
    with AUTH_LOCK:
        if AUTH["proc"] and AUTH["proc"].poll() is None:
            AUTH["proc"].kill()
        out = []
        proc = subprocess.Popen(["rclone", "--config", RCLONE_CONF, "authorize", typ, "--auth-no-open-browser"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                stdin=subprocess.DEVNULL)
        AUTH.update(proc=proc, out=out, type=typ)

        def reader():
            for line in proc.stdout:
                out.append(line)
        threading.Thread(target=reader, daemon=True).start()

        local = None
        for _ in range(150):
            m = re.search(r"http://127\.0\.0\.1:53682/auth\?state=[^\s\"']+", "".join(out))
            if m:
                local = m.group(0)
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if not local:
            raise ApiError(502, "rclone লগইন শুরু করতে পারেনি")
        conn = http.client.HTTPConnection("127.0.0.1", 53682, timeout=10)
        conn.request("GET", local.split("53682", 1)[1])
        resp = conn.getresponse()
        location = resp.getheader("Location")
        resp.read()
        if not location:
            raise ApiError(502, "লগইন লিংক পাওয়া যায়নি, টোকেন পদ্ধতি ব্যবহার করুন")
        return location


def auth_finish(redirect_url):
    proc = AUTH["proc"]
    if not proc:
        raise ApiError(400, "লগইনের মেয়াদ শেষ, আবার লিংক তৈরি করুন")
    # Normal flow: the rclone helper is still waiting, so hand it the code from
    # the pasted redirect URL. When the panel is opened on the same machine the
    # browser reaches 127.0.0.1:53682 itself, so rclone already has the code and
    # has exited ("All done") -- then skip straight to reading the token.
    if proc.poll() is None:
        query = urllib.parse.urlsplit((redirect_url or "").strip()).query
        if "code=" not in query:
            raise ApiError(400, "address bar-এর পুরো লিংক পেস্ট করুন (এতে code= থাকে)")
        try:
            conn = http.client.HTTPConnection("127.0.0.1", 53682, timeout=30)
            conn.request("GET", "/?" + query)
            conn.getresponse().read()
        except OSError:
            raise ApiError(502, "rclone লগইন হেল্পারে পৌঁছানো যায়নি")
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise ApiError(504, "লগইনের সময় শেষ")
    time.sleep(0.2)
    typ = AUTH["type"]
    try:
        token = parse_token("".join(AUTH["out"]))
    except ApiError:
        raise ApiError(400, "লগইন সম্পূর্ণ হয়নি। আবার 'লগইন লিংক তৈরি করো' দিয়ে চেষ্টা করুন।")
    return create_remote(typ, oauth_params(typ, token))


# ---------------------------------------------------------------- aria2

ARIA2_READ = {"aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped", "aria2.tellStatus",
              "aria2.getGlobalStat", "aria2.getGlobalOption", "aria2.getVersion", "aria2.getFiles",
              "aria2.getPeers", "aria2.getUris", "aria2.getOption"}


def aria2_rpc(method, params=None):
    token = "token:" + CFG["rpc_secret"]
    if method == "system.multicall":
        calls = params[0] if params else []
        for call in calls:
            call["params"] = [token] + list(call.get("params") or [])
        params = [calls]
    else:
        params = [token] + list(params or [])
    body = json.dumps({"jsonrpc": "2.0", "id": "panel", "method": method, "params": params})
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 6800, timeout=30)
        conn.request("POST", "/jsonrpc", body, {"Content-Type": "application/json"})
        reply = json.loads(conn.getresponse().read())
    except (OSError, ValueError):
        raise ApiError(502, "aria2 চলছে না")
    if "error" in reply:
        raise ApiError(400, reply["error"].get("message", "aria2 error"))
    return reply["result"]


def dl_name(d):
    bt = d.get("bittorrent") or {}
    if (bt.get("info") or {}).get("name"):
        return bt["info"]["name"]
    files = d.get("files") or []
    if files and files[0].get("path"):
        return os.path.basename(files[0]["path"])
    if files and files[0].get("uris"):
        return files[0]["uris"][0]["uri"]
    return d.get("gid", "?")


# ---------------------------------------------------------------- Telegram

def tg_api(token, method, params=None, timeout=20):
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (token, method), data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def tg_send(token, chat, text):
    try:
        tg_api(token, "sendMessage", {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"})
        return True
    except Exception:
        return False


def notify(text):
    st = load_state()
    if st["telegram_token"] and st["telegram_chat"] and st["telegram_notify"]:
        threading.Thread(target=tg_send, args=(st["telegram_token"], st["telegram_chat"], text),
                         daemon=True).start()


TG_HELP = ("🤖 কমান্ড:\n"
           "লিংক পাঠান → ডাউনলোড শুরু (Direct, Magnet, YouTube, Drive লিংক)\n"
           ".torrent ফাইল পাঠান → টরেন্ট শুরু\n"
           "/status → এখন কী চলছে\n"
           "/disk → ডিস্কের জায়গা\n"
           "/pause → সব থামাও\n"
           "/resume → সব চালাও\n"
           "/url → প্যানেলের লিংক\n"
           "/id → আপনার Chat ID")


def tg_handle(token, st, msg):
    chat = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or msg.get("caption") or "").strip()
    reply = lambda t: tg_send(token, chat, t)  # noqa: E731
    if not chat:
        return
    if chat != st["telegram_chat"]:
        if text.startswith(("/start", "/id")):
            reply("আপনার Chat ID: %s\nএটা প্যানেলের ⚙ সেটিং → Telegram-এ বসিয়ে সেভ করুন।" % chat)
        return
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if cmd in ("/start", "/help"):
        return reply(TG_HELP)
    if cmd == "/id":
        return reply("আপনার Chat ID: %s" % chat)
    if cmd == "/status":
        stat = aria2_rpc("aria2.getGlobalStat")
        active = aria2_rpc("aria2.tellActive", [["gid", "totalLength", "completedLength", "downloadSpeed",
                                                  "files", "bittorrent"]])
        lines = ["⬇ %s/s  ⬆ %s/s  চলছে: %s  অপেক্ষায়: %s" % (
            fmt_size(stat["downloadSpeed"]), fmt_size(stat["uploadSpeed"]), stat["numActive"], stat["numWaiting"])]
        for d in active[:15]:
            total = int(d["totalLength"] or 0)
            pct = int(int(d["completedLength"]) * 100 / total) if total else 0
            lines.append("• %s — %d%% (%s/s)" % (dl_name(d)[:60], pct, fmt_size(d["downloadSpeed"])))
        running = [j for j in JOBS.values() if j["status"] == "running"]
        for j in running[:10]:
            lines.append("• %s — %d%%" % (j["title"][:60], j["progress"]))
        return reply("\n".join(lines))
    if cmd == "/disk":
        du = shutil.disk_usage(ROOT)
        return reply("💾 খালি %s / মোট %s" % (fmt_size(du.free), fmt_size(du.total)))
    if cmd == "/pause":
        aria2_rpc("aria2.pauseAll")
        return reply("⏸ সব থামানো হয়েছে")
    if cmd == "/resume":
        aria2_rpc("aria2.unpauseAll")
        return reply("▶ সব চালু হয়েছে")
    if cmd == "/url":
        url = tunnel_url()
        return reply(("🔗 " + url) if url else "Tunnel চালু নেই। সার্ভারের IP বা ডোমেইন দিয়ে প্যানেল খুলুন।")
    doc = msg.get("document")
    if doc and (doc.get("file_name") or "").lower().endswith(".torrent"):
        info = tg_api(token, "getFile", {"file_id": doc["file_id"]})
        path = info["result"]["file_path"]
        with urllib.request.urlopen("https://api.telegram.org/file/bot%s/%s" % (token, path), timeout=60) as r:
            data = r.read(10 * 1024 * 1024)
        aria2_rpc("aria2.addTorrent", [base64.b64encode(data).decode(), [], {}])
        return reply("✅ টরেন্ট যোগ হয়েছে: %s" % doc["file_name"])
    urls = re.findall(r"(magnet:\?\S+|https?://\S+)", text)
    if urls:
        res = add_links(urls, target="server")
        parts = []
        if res["aria2"]:
            parts.append("⬇ %d টা ডাউনলোড শুরু" % res["aria2"])
        if res["video"]:
            parts.append("🎬 %d টা ভিডিও নামানো শুরু" % res["video"])
        if res["cloud"]:
            parts.append("☁ %d টা Drive লিংক শুরু" % res["cloud"])
        parts += ["❌ " + e for e in res["errors"]]
        return reply("\n".join(parts) or "কিছু যোগ হয়নি")
    reply("বুঝতে পারিনি। /help দেখুন।")


def telegram_loop():
    offset = 0
    last_token = None
    while True:
        st = load_state()
        token = st["telegram_token"]
        if not token:
            time.sleep(10)
            continue
        if token != last_token:
            offset, last_token = 0, token
        try:
            res = tg_api(token, "getUpdates", {"timeout": 30, "offset": offset}, timeout=45)
        except Exception:
            time.sleep(15)
            continue
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if not msg:
                continue
            try:
                tg_handle(token, load_state(), msg)
            except Exception as e:
                chat = str((msg.get("chat") or {}).get("id", ""))
                tg_send(token, chat, "❌ সমস্যা: %s" % e)


# ---------------------------------------------------------------- jobs (rclone / yt-dlp / archives)

JOBS = {}
JOB_LIMIT = 100
RCLONE_STATS_RE = re.compile(r"([\d.]+ ?\w*B) / ([\d.]+ ?\w*B), (\d+)%, ([^,]*), ETA (\S+)")
YTDLP_RE = re.compile(r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*(\S+)(?:\s+at\s+(\S+))?(?:\s+ETA\s+(\S+))?")
YTDLP_ITEM_RE = re.compile(r"Downloading (?:item|video) (\d+) of (\d+)")
PERCENT_RE = re.compile(r"^\s*(\d{1,3})%")


def parse_rclone(job, line):
    m = RCLONE_STATS_RE.search(line)
    if m:
        job["progress"] = int(m.group(3))
        job["info"] = "%s / %s · %s · ETA %s" % (m.group(1), m.group(2), m.group(4).strip(), m.group(5))


def parse_ytdlp(job, line):
    m = YTDLP_RE.search(line)
    if m:
        job["progress"] = int(float(m.group(1)))
        info = [m.group(2)]
        if m.group(3):
            info.append(m.group(3))
        if m.group(4):
            info.append("ETA " + m.group(4))
        if job.get("_item"):
            info.insert(0, job["_item"])
        job["info"] = " · ".join(info)
        return
    m = YTDLP_ITEM_RE.search(line)
    if m:
        job["_item"] = "%s/%s" % (m.group(1), m.group(2))
    elif "[download] Destination:" in line:
        job["title"] = "🎬 " + os.path.basename(line.split("Destination:", 1)[1].strip())
    elif line.startswith(("[Merger]", "[ExtractAudio]", "[VideoConvertor]")):
        job["info"] = "জোড়া লাগানো / কনভার্ট হচ্ছে..."


def parse_percent(job, line):
    m = PERCENT_RE.match(line)
    if m:
        job["progress"] = min(100, int(m.group(1)))


def iter_lines(stream):
    buf = b""
    while True:
        chunk = stream.read1(65536)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for p in parts:
            if p.strip():
                yield p.decode("utf-8", "replace")
    if buf.strip():
        yield buf.decode("utf-8", "replace")


def start_job(title, cmd, parser=None, kind="cloud", on_done=None, always=None):
    job = {"id": secrets.token_hex(4), "title": title, "kind": kind, "status": "running", "progress": 0,
           "info": "", "error": "", "started": time.time()}
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise ApiError(500, "%s ইনস্টল নেই" % cmd[0])
    job["_proc"] = proc
    JOBS[job["id"]] = job
    finished = sorted((j for j in JOBS.values() if j["status"] != "running"), key=lambda j: j["started"])
    for old in finished[:max(0, len(JOBS) - JOB_LIMIT)]:
        JOBS.pop(old["id"], None)
    threading.Thread(target=watch_job, args=(job, parser, on_done, always), daemon=True).start()
    return public_job(job)


FFMPEG_PROGRESS_LINE = re.compile(r"^[a-z_]+=\S*$")


def watch_job(job, parser, on_done, always=None):
    try:
        _watch_job(job, parser, on_done)
    finally:
        if always:
            try:
                always(job)
            except Exception:
                traceback.print_exc()


def _watch_job(job, parser, on_done):
    proc = job["_proc"]
    tail = []
    last_error = ""
    for line in iter_lines(proc.stdout):
        if parser:
            try:
                parser(job, line)
            except Exception:
                pass
        if "ERROR" in line:
            last_error = line.split("ERROR", 1)[1].strip(" :")
        if not FFMPEG_PROGRESS_LINE.match(line):
            tail = (tail + [line])[-5:]
    rc = proc.wait()
    if job["status"] == "cancelled":
        return
    if rc == 0:
        job["status"], job["progress"] = "done", 100
        notify("✅ শেষ: %s" % job["title"])
        if on_done:
            try:
                on_done(job)
            except Exception:
                traceback.print_exc()
    else:
        job["status"] = "error"
        job["error"] = last_error or (tail[-1] if tail else "exit code %d" % rc)
        notify("❌ ব্যর্থ: %s\n%s" % (job["title"], job["error"]))


def public_job(job):
    return {k: v for k, v in job.items() if not k.startswith("_")}


def rclone_cmd(*args):
    return ["rclone", "--config", RCLONE_CONF, "--stats", "2s", "--stats-one-line",
            "--stats-log-level", "NOTICE"] + list(args)


def ytdlp_job(url, folder="", quality="best", playlist=False, subs=""):
    if not shutil.which("yt-dlp"):
        raise ApiError(500, "yt-dlp ইনস্টল নেই")
    if not url.startswith(("http://", "https://")):
        raise ApiError(400, "ভিডিও লিংক http/https হতে হবে")
    dest = local_path(folder)
    os.makedirs(dest, exist_ok=True)
    cmd = ["yt-dlp", "--newline", "--no-colors", "-P", dest, "-o", "%(title).150B [%(id)s].%(ext)s"]
    if quality == "audio":
        cmd += ["-f", YTDLP_FORMATS["audio"], "-x", "--audio-format", "mp3"]
    else:
        cmd += ["-f", YTDLP_FORMATS.get(quality, YTDLP_FORMATS["best"]), "--merge-output-format", "mp4"]
        if subs:
            if not SUB_LANGS_RE.match(subs):
                raise ApiError(400, "সাবটাইটেলের ভাষা এভাবে লিখুন: bn,en")
            cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", subs, "--embed-subs"]
    cmd += ["--yes-playlist" if playlist else "--no-playlist", "--", url]
    return start_job("🎬 %s" % url[:80], cmd, parse_ytdlp, kind="video")


def drive_link_job(url, target, folder, remote_name=""):
    m = re.search(r"/folders/([\w-]{10,})", url)
    is_folder = bool(m)
    if not m:
        m = re.search(r"/d/([\w-]{10,})", url) or re.search(r"[?&]id=([\w-]{10,})", url)
    if not m:
        raise ApiError(400, "এটা Google Drive লিংক না")
    fid = m.group(1)
    remote = get_remote("", "drive")["name"]
    if target == "cloud":
        base = rdir(get_remote(remote_name)["name"], folder)
    else:
        base = local_path(folder)
        os.makedirs(base, exist_ok=True)
        base = base.rstrip("/") + "/"
    if is_folder:
        args = ["copy", "--drive-root-folder-id", fid, remote + ":", base + "drive-folder-" + fid[:8]]
    else:
        args = ["backend", "copyid", remote + ":", fid, base]
    return start_job("Drive লিংক → %s: %s" % ("Cloud" if target == "cloud" else "সার্ভার", fid[:12]),
                     rclone_cmd(*args), parse_rclone)


def add_links(urls, target="server", folder="", quality="best", playlist=False, remote="", subs=""):
    folder = clean_folder(folder)
    result = {"aria2": 0, "video": 0, "cloud": 0, "errors": []}
    opts = {}
    if target != "cloud" and folder:
        dest = local_path(folder)
        os.makedirs(dest, exist_ok=True)
        opts = {"dir": dest}
    for u in [u.strip() for u in urls if u.strip()]:
        try:
            if DRIVE_LINK_RE.match(u):
                drive_link_job(u, target, folder, remote)
                result["cloud"] += 1
            elif target == "video" or (target == "server" and VIDEO_HOST_RE.match(u)):
                ytdlp_job(u, folder, quality, playlist, subs)
                result["video"] += 1
            elif target == "cloud":
                if VIDEO_HOST_RE.match(u):
                    raise ApiError(400, "ভিডিও সাইটের লিংক আগে সার্ভারে নামান (অটো আপলোড চালু রাখলে Cloud-এ যাবে)")
                if not u.startswith(("http://", "https://")):
                    raise ApiError(400, "Magnet/Torrent সরাসরি Cloud-এ যায় না, আগে সার্ভারে নামান")
                r = get_remote(remote)
                start_job("লিংক → %s: %s" % (r["name"], u[:80]),
                          rclone_cmd("copyurl", "--auto-filename", u, rdir(r["name"], folder)), parse_rclone)
                result["cloud"] += 1
            else:
                aria2_rpc("aria2.addUri", [[u], opts])
                result["aria2"] += 1
        except ApiError as e:
            result["errors"].append("%s: %s" % (u[:60], e))
    return result


# ---------------------------------------------------------------- background monitor

MON = {"sched": None, "disk_low": False, "tunnel": None}


def autoupload(d, st):
    if not st["autoupload"]:
        return
    try:
        remote = get_remote(st["autoupload_remote"])["name"]
        first = os.path.realpath(d["files"][0]["path"])
        top = os.path.relpath(first, ROOT).split(os.sep)[0]
        src = local_path(top)
    except (ApiError, KeyError, IndexError, ValueError):
        return
    if src == ROOT or not os.path.exists(src):
        return
    dest = clean_folder(st["autoupload_dest"])
    verb = "move" if st["autoupload_move"] else "copy"
    target = rpath(remote, (dest + "/" + top) if os.path.isdir(src) else dest)
    start_job("অটো আপলোড: %s → %s" % (top, remote), rclone_cmd(verb, src, target), parse_rclone)


def on_download_stopped(d, st):
    name = dl_name(d)
    if d["status"] == "complete" and not d.get("followedBy"):
        notify("✅ ডাউনলোড শেষ: %s (%s)" % (name, fmt_size(d.get("totalLength"))))
        autoupload(d, st)
    elif d["status"] == "error":
        notify("❌ ডাউনলোড ব্যর্থ: %s\n%s" % (name, d.get("errorMessage", "")))


def maintenance(st):
    want = {"max-overall-download-limit": str(max(0, int(st["speed_down_kb"])) * 1024),
            "max-overall-upload-limit": str(max(0, int(st["speed_up_kb"])) * 1024)}
    cur = aria2_rpc("aria2.getGlobalOption")
    diff = {k: v for k, v in want.items() if cur.get(k) != v}
    if diff:
        aria2_rpc("aria2.changeGlobalOption", [diff])

    if st["schedule_enabled"]:
        allowed = in_window(st["schedule_start"], st["schedule_end"], int(st["tz_offset_min"]))
        if allowed != MON["sched"]:
            aria2_rpc("aria2.unpauseAll" if allowed else "aria2.pauseAll")
            MON["sched"] = allowed
    else:
        if MON["sched"] is False:
            aria2_rpc("aria2.unpauseAll")
        MON["sched"] = None

    guard = float(st["disk_guard_gb"])
    free = shutil.disk_usage(ROOT).free
    low = guard > 0 and free < guard * 1024 ** 3
    if low:
        if int(aria2_rpc("aria2.getGlobalStat").get("numActive", 0)) > 0:
            aria2_rpc("aria2.pauseAll")
        if not MON["disk_low"]:
            notify("⚠️ ডিস্কে জায়গা কম (%s খালি)। ডাউনলোড থামানো হয়েছে।" % fmt_size(free))
    elif MON["disk_low"]:
        notify("💾 এখন জায়গা আছে (%s খালি)। প্যানেল থেকে ▶ চালু করুন।" % fmt_size(free))
    MON["disk_low"] = low


def tunnel_url():
    host, _, port = TUNNEL_METRICS.partition(":")
    try:
        conn = http.client.HTTPConnection(host, int(port), timeout=2)
        conn.request("GET", "/quicktunnel")
        data = json.loads(conn.getresponse().read() or b"{}")
        hostname = data.get("hostname") or ""
        return ("https://" + hostname) if hostname else ""
    except (OSError, ValueError):
        return ""


def watch_tunnel():
    url = tunnel_url()
    if url and url != load_state()["tunnel_last"]:
        update_state({"tunnel_last": url})
        notify("🔗 প্যানেলের নতুন লিংক: %s" % url)
    MON["tunnel"] = url


def monitor_loop():
    seen = None
    tick = 0
    while True:
        time.sleep(5)
        tick += 1
        st = load_state()
        try:
            stopped = aria2_rpc("aria2.tellStopped", [0, 1000, ["gid", "status", "files", "dir", "followedBy",
                                                                "totalLength", "errorMessage", "bittorrent"]])
            if seen is not None:
                for d in stopped:
                    if d["gid"] not in seen:
                        on_download_stopped(d, st)
            seen = {d["gid"] for d in stopped}
        except ApiError:
            pass
        except Exception:
            traceback.print_exc()
        for every, fn, args in ((2, maintenance, (st,)), (12, watch_tunnel, ())):
            if tick % every == 0:
                try:
                    fn(*args)
                except ApiError:
                    pass
                except Exception:
                    traceback.print_exc()


# ---------------------------------------------------------------- RSS

RSS_STATUS = {}
RSS_LOCK = threading.Lock()


def xml_local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def parse_feed(data):
    root = ET.fromstring(data)
    items = []
    for it in root.iter():
        if xml_local(it.tag) not in ("item", "entry"):
            continue
        title, link, guid, magnet, enclosure = "", "", "", "", ""
        for el in it.iter():
            name = xml_local(el.tag)
            text = (el.text or "").strip()
            if text.startswith("magnet:"):
                magnet = magnet or text
            if name == "title" and not title:
                title = text
            elif name == "link":
                href = el.get("href") or text
                if href.startswith("magnet:"):
                    magnet = magnet or href
                elif el.get("rel") == "enclosure" or (el.get("type") or "").endswith("bittorrent"):
                    enclosure = enclosure or href
                else:
                    link = link or href
            elif name == "enclosure":
                enclosure = enclosure or el.get("url", "")
            elif name in ("guid", "id"):
                guid = guid or text
        target = magnet or enclosure or link
        if target:
            items.append({"title": title, "url": target, "key": guid or target})
    return items


def rss_matches(title, flt):
    t = title.lower()
    for word in (flt or "").lower().split():
        if word.startswith("-") and len(word) > 1:
            if word[1:] in t:
                return False
        elif word not in t:
            return False
    return True


def rss_check():
    with RSS_LOCK:
        st = load_state()
        try:
            with open(RSS_SEEN_PATH) as f:
                seen = json.load(f)
        except (OSError, ValueError):
            seen = {}
        dest = local_path(clean_folder(st["rss_dest"]))
        added_total = 0
        for feed in st["rss_feeds"]:
            fid = feed.get("id")
            status = {"checked": time.time(), "error": "", "added": 0}
            RSS_STATUS[fid] = status
            if not feed.get("enabled", True):
                status["error"] = "বন্ধ"
                continue
            try:
                req = urllib.request.Request(feed["url"], headers={"User-Agent": "aria2-panel/" + VERSION})
                with urllib.request.urlopen(req, timeout=30) as r:
                    items = parse_feed(r.read(5 * 1024 * 1024))
            except Exception as e:
                status["error"] = str(e)[:200]
                continue
            keys = seen.get(fid)
            if keys is None:
                seen[fid] = [i["key"] for i in items][-500:]
                status["error"] = "প্রথমবার: পুরনো %d টা আইটেম বাদ দেওয়া হয়েছে, এখন থেকে নতুনগুলো নামবে" % len(items)
                continue
            keyset = set(keys)
            for item in items:
                if item["key"] in keyset:
                    continue
                keys.append(item["key"])
                keyset.add(item["key"])
                if not rss_matches(item["title"], feed.get("filter", "")):
                    continue
                try:
                    os.makedirs(dest, exist_ok=True)
                    aria2_rpc("aria2.addUri", [[item["url"]], {"dir": dest}])
                    status["added"] += 1
                    added_total += 1
                    notify("📡 RSS থেকে নতুন: %s" % item["title"])
                except ApiError as e:
                    status["error"] = str(e)
            seen[fid] = keys[-500:]
        valid = {f.get("id") for f in st["rss_feeds"]}
        write_json(RSS_SEEN_PATH, {k: v for k, v in seen.items() if k in valid})
        return added_total


def rss_loop():
    last = 0
    while True:
        time.sleep(30)
        st = load_state()
        if not st["rss_feeds"]:
            continue
        if time.time() - last >= max(5, int(st["rss_interval_min"])) * 60:
            last = time.time()
            try:
                rss_check()
            except Exception:
                traceback.print_exc()


# ---------------------------------------------------------------- login protection

FAILS = {}
FAIL_LIMIT = 5
FAIL_WINDOW = 900


def client_ip(h):
    ip = h.headers.get("X-Real-IP") or h.client_address[0]
    if ip in ("127.0.0.1", "::1") and h.headers.get("Cf-Connecting-Ip"):
        ip = h.headers["Cf-Connecting-Ip"]
    return ip


def session_payload(user, exp):
    return "%s|%s|%s" % (user, exp, CFG["users"][user]["hash"][:16])


def make_session(h, user):
    exp = str(int(time.time()) + SESSION_TTL)
    value = "%s.%s.%s" % (b64e(user.encode()), exp, sign(session_payload(user, exp)))
    secure = "; Secure" if h.headers.get("X-Forwarded-Proto") == "https" else ""
    h.extra_headers.append(("Set-Cookie", "panel=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s"
                            % (value, SESSION_TTL, secure)))


def admin_count():
    return sum(1 for u in CFG["users"].values() if u["role"] == "admin")


# ---------------------------------------------------------------- routes

ROUTES = {}


def route(method, path, role="user"):
    def deco(fn):
        ROUTES[(method, path)] = (fn, role)
        return fn
    return deco


@route("POST", "/api/login", role=None)
def api_login(h):
    ip = client_ip(h)
    now = time.time()
    fail = FAILS.get(ip)
    if fail and fail[2] > now:
        raise ApiError(429, "অনেকবার ভুল পাসওয়ার্ড। %d মিনিট পরে চেষ্টা করুন।" % (int(fail[2] - now) // 60 + 1))
    body = h.json()
    user = (body.get("username") or "admin").strip()
    info = CFG["users"].get(user)
    if not info or not hmac.compare_digest(hash_password(body.get("password", ""), info["salt"]), info["hash"]):
        time.sleep(1)
        if not fail or now - fail[1] > FAIL_WINDOW:
            fail = [0, now, 0]
        fail[0] += 1
        if fail[0] >= FAIL_LIMIT:
            fail = [0, now, now + FAIL_WINDOW]
            notify("⚠️ %s থেকে বারবার ভুল পাসওয়ার্ড দেওয়া হয়েছে, ১৫ মিনিটের জন্য ব্লক করা হলো।" % ip)
        FAILS[ip] = fail
        raise ApiError(403, "ইউজারনেম বা পাসওয়ার্ড ভুল")
    FAILS.pop(ip, None)
    make_session(h, user)
    return {"ok": True}


@route("POST", "/api/logout", role="viewer")
def api_logout(h):
    h.extra_headers.append(("Set-Cookie", "panel=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"))
    return {"ok": True}


@route("POST", "/api/password", role="viewer")
def api_password(h):
    body = h.json()
    info = CFG["users"][h.user]
    if not hmac.compare_digest(hash_password(body.get("old", ""), info["salt"]), info["hash"]):
        raise ApiError(403, "বর্তমান পাসওয়ার্ড ভুল")
    new = body.get("new", "")
    if len(new) < 8:
        raise ApiError(400, "নতুন পাসওয়ার্ড কমপক্ষে ৮ অক্ষরের হতে হবে")
    info["salt"] = secrets.token_hex(16)
    info["hash"] = hash_password(new, info["salt"])
    save_config()
    make_session(h, h.user)
    return {"ok": True}


@route("GET", "/api/users", role="admin")
def api_users(h):
    return {"users": [{"name": n, "role": u["role"]} for n, u in sorted(CFG["users"].items())]}


@route("POST", "/api/users/save", role="admin")
def api_users_save(h):
    body = h.json()
    name = (body.get("name") or "").strip()
    role = body.get("role", "user")
    password = body.get("password") or ""
    if not USERNAME_RE.match(name):
        raise ApiError(400, "ইউজারনেম শুধু ইংরেজি অক্ষর, সংখ্যা, _ . - দিয়ে (১-৩২ অক্ষর)")
    if role not in ROLES:
        raise ApiError(400, "ভুল role")
    existing = CFG["users"].get(name)
    if not existing and len(password) < 8:
        raise ApiError(400, "নতুন ইউজারের পাসওয়ার্ড কমপক্ষে ৮ অক্ষর")
    if password and len(password) < 8:
        raise ApiError(400, "পাসওয়ার্ড কমপক্ষে ৮ অক্ষর")
    if existing and existing["role"] == "admin" and role != "admin" and admin_count() <= 1:
        raise ApiError(400, "অন্তত একজন admin থাকতে হবে")
    info = existing or {}
    info["role"] = role
    if password:
        info["salt"] = secrets.token_hex(16)
        info["hash"] = hash_password(password, info["salt"])
    CFG["users"][name] = info
    save_config()
    if name == h.user and password:
        make_session(h, name)
    return {"ok": True}


@route("POST", "/api/users/delete", role="admin")
def api_users_delete(h):
    name = h.json().get("name", "")
    if name == h.user:
        raise ApiError(400, "নিজেকে মুছতে পারবেন না")
    if name in CFG["users"]:
        if CFG["users"][name]["role"] == "admin" and admin_count() <= 1:
            raise ApiError(400, "অন্তত একজন admin থাকতে হবে")
        del CFG["users"][name]
        save_config()
    return {"ok": True}


@route("GET", "/api/info", role="viewer")
def api_info(h):
    du = shutil.disk_usage(ROOT)
    st = load_state()
    if h.role != "admin":
        st = {k: st[k] for k in ("autoupload", "autoupload_remote", "autoupload_dest")}
    else:
        st.pop("rss_feeds", None)
    return {
        "version": VERSION, "user": h.user, "role": h.role,
        "disk_total": du.total, "disk_free": du.free, "disk_low": MON["disk_low"],
        "remotes": remotes(), "state": st, "tunnel": tunnel_url(),
        "schedule_paused": MON["sched"] is False,
        "tools": {"ytdlp": bool(shutil.which("yt-dlp")), "ffmpeg": bool(shutil.which("ffmpeg")),
                  "archive": bool(shutil.which("7zz") or shutil.which("7z"))},
        "server_time": int(time.time()),
    }


@route("POST", "/api/settings", role="admin")
def api_settings(h):
    body = h.json()
    changes = {}
    for key, typ in SETTING_TYPES.items():
        if key in body:
            try:
                changes[key] = typ(body[key])
            except (TypeError, ValueError):
                raise ApiError(400, "ভুল মান: %s" % key)
    for key in ("schedule_start", "schedule_end"):
        if key in changes and not TIME_RE.match(changes[key]):
            raise ApiError(400, "সময় HH:MM আকারে দিন (যেমন 01:30)")
    for key in ("speed_down_kb", "speed_up_kb", "disk_guard_gb"):
        if key in changes and changes[key] < 0:
            raise ApiError(400, "মান ০ বা তার বেশি হতে হবে")
    if "rss_interval_min" in changes:
        changes["rss_interval_min"] = max(5, changes["rss_interval_min"])
    if "telegram_token" in changes:
        changes["telegram_token"] = changes["telegram_token"].strip()
        if changes["telegram_token"] and not re.match(r"^\d+:[\w-]+$", changes["telegram_token"]):
            raise ApiError(400, "Telegram বট টোকেন ঠিক নেই")
    if "telegram_chat" in changes:
        changes["telegram_chat"] = changes["telegram_chat"].strip()
    for key in ("autoupload_dest", "rss_dest"):
        if key in changes:
            changes[key] = clean_folder(changes[key])
    state = update_state(changes)
    try:
        maintenance(state)
    except ApiError:
        pass
    state.pop("rss_feeds", None)
    return state


@route("POST", "/api/telegram/test", role="admin")
def api_telegram_test(h):
    st = load_state()
    if not (st["telegram_token"] and st["telegram_chat"]):
        raise ApiError(400, "আগে বট টোকেন আর Chat ID সেভ করুন")
    if not tg_send(st["telegram_token"], st["telegram_chat"], "✅ প্যানেল থেকে পরীক্ষার মেসেজ। সব ঠিক আছে!"):
        raise ApiError(502, "মেসেজ যায়নি। টোকেন/Chat ID দেখুন, আর বটকে একবার /start পাঠান।")
    return {"ok": True}


@route("GET", "/api/rss", role="admin")
def api_rss(h):
    st = load_state()
    return {"feeds": st["rss_feeds"], "status": RSS_STATUS, "dest": st["rss_dest"],
            "interval": st["rss_interval_min"]}


@route("POST", "/api/rss/save", role="admin")
def api_rss_save(h):
    feeds = []
    for f in h.json().get("feeds") or []:
        url = (f.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ApiError(400, "RSS লিংক http/https হতে হবে")
        feeds.append({"id": f.get("id") or secrets.token_hex(4), "url": url,
                      "name": (f.get("name") or "").strip()[:80], "filter": (f.get("filter") or "").strip()[:200],
                      "enabled": bool(f.get("enabled", True))})
    update_state({"rss_feeds": feeds})
    return {"feeds": feeds}


@route("POST", "/api/rss/check", role="admin")
def api_rss_check(h):
    return {"added": rss_check(), "status": RSS_STATUS}


# Where the options dict sits in each method's params (the panel strips the
# secret, so these are 0-based on the client-sent params). addUri/addTorrent let
# the caller pick a download directory, so pin it inside ROOT and keep "out" a
# plain filename -- otherwise a non-admin could make aria2 write anywhere it can
# (e.g. overwrite /etc/aria2/panel.json and take over the panel).
ARIA2_OPT_INDEX = {
    "aria2.addUri": 1, "aria2.addTorrent": 2, "aria2.addMetalink": 1,
    "aria2.changeOption": 1, "aria2.changeGlobalOption": 0,
}


def sanitize_aria2_options(method, params):
    idx = ARIA2_OPT_INDEX.get(method)
    if idx is None or idx >= len(params) or not isinstance(params[idx], dict):
        return
    opts = params[idx]
    if "dir" in opts:
        d = os.path.realpath(str(opts["dir"]))
        if d != ROOT and not d.startswith(ROOT + os.sep):
            raise ApiError(400, "ভুল ডাউনলোড ফোল্ডার")
        opts["dir"] = d
    if "out" in opts and ("/" in str(opts["out"]) or str(opts["out"]) in ("", ".", "..")):
        raise ApiError(400, "ভুল ফাইলের নাম")


@route("POST", "/api/aria2", role="viewer")
def api_aria2(h):
    body = h.json()
    method = body.get("method", "")
    params = body.get("params") or []
    calls = (params[0] if params else []) if method == "system.multicall" else None
    methods = [str(c.get("methodName", "")) for c in calls] if calls is not None else [method]
    if not all(m.startswith("aria2.") for m in methods):
        raise ApiError(400, "এই কমান্ড চলবে না")
    if ROLES[h.role] < ROLES["user"] and not all(m in ARIA2_READ for m in methods):
        raise ApiError(403, "অনুমতি নেই")
    if calls is not None:
        for c in calls:
            sanitize_aria2_options(str(c.get("methodName", "")), c.get("params") or [])
    else:
        sanitize_aria2_options(method, params)
    return {"result": aria2_rpc(method, params)}


@route("POST", "/api/add")
def api_add(h):
    body = h.json()
    urls = body.get("urls") or []
    if not urls:
        raise ApiError(400, "আগে লিংক দিন")
    return add_links(urls, target=body.get("target", "server"), folder=body.get("folder", ""),
                     quality=body.get("quality", "best"), playlist=bool(body.get("playlist")),
                     remote=body.get("remote", ""), subs=(body.get("subs") or "").strip())


@route("GET", "/api/files", role="viewer")
def api_files(h):
    path = local_path(h.query.get("path"))
    if not os.path.isdir(path):
        raise ApiError(404, "ফোল্ডার পাওয়া যায়নি")
    items = []
    for entry in os.scandir(path):
        if entry.name.endswith(HIDDEN_SUFFIXES):
            continue
        try:
            st = entry.stat()
            is_dir = entry.is_dir()
        except OSError:
            continue
        items.append({"name": entry.name, "dir": is_dir, "size": 0 if is_dir else st.st_size,
                      "mtime": int(st.st_mtime)})
    items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
    return {"path": rel_path(path), "items": items}


@route("GET", "/api/search", role="viewer")
def api_search(h):
    words = (h.query.get("q") or "").lower().split()
    if not words:
        raise ApiError(400, "কী খুঁজবেন লিখুন")
    results = []
    deadline = time.time() + 8
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(dirpath, d))]
        for name in dirs + files:
            if name.endswith(HIDDEN_SUFFIXES) or not all(w in name.lower() for w in words):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            is_dir = os.path.isdir(full)
            results.append({"name": name, "path": rel_path(full), "dir": is_dir,
                            "size": 0 if is_dir else st.st_size, "mtime": int(st.st_mtime)})
            if len(results) >= 300:
                return {"items": results, "truncated": True}
        if time.time() > deadline:
            return {"items": results, "truncated": True}
    return {"items": results, "truncated": False}


@route("PUT", "/api/upload")
def api_upload(h):
    folder = local_path(h.query.get("dir"))
    if not os.path.isdir(folder):
        raise ApiError(404, "ফোল্ডার পাওয়া যায়নি")
    dest = unique_path(os.path.join(folder, check_name(h.query.get("name"))))
    length = int(h.headers.get("Content-Length") or -1)
    if length < 0:
        raise ApiError(411, "Content-Length দরকার")
    tmp = dest + ".uploading"
    try:
        with open(tmp, "wb") as out:
            remaining = length
            while remaining > 0:
                chunk = h.rfile.read(min(CHUNK, remaining))
                if not chunk:
                    raise ApiError(400, "আপলোড মাঝপথে থেমে গেছে")
                out.write(chunk)
                remaining -= len(chunk)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"ok": True, "name": os.path.basename(dest)}


@route("GET", "/api/download", role="viewer")
def api_download(h):
    path = local_path(h.query.get("path"))
    if not os.path.isfile(path):
        raise ApiError(404, "ফাইল পাওয়া যায়নি")
    h.serve_file(path, inline=h.query.get("inline") == "1")


@route("GET", "/api/zip", role="viewer")
def api_zip(h):
    paths = [local_path(p) for p in h.query_all.get("path", [])]
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        raise ApiError(400, "কিছু সিলেক্ট করা নেই")
    name = (os.path.basename(paths[0]) or "files") if len(paths) == 1 else "files"
    h.stream_zip(paths, name + ".zip")


THUMB_SEM = threading.Semaphore(2)


@route("GET", "/api/thumb", role="viewer")
def api_thumb(h):
    path = local_path(h.query.get("path"))
    low = path.lower()
    if not os.path.isfile(path) or not low.endswith(IMAGE_EXT + VIDEO_EXT):
        raise ApiError(404, "ছবি নেই")
    if not shutil.which("ffmpeg"):
        if low.endswith(IMAGE_EXT):
            return h.serve_file(path, inline=True, cache=86400)
        raise ApiError(404, "ffmpeg নেই")
    st = os.stat(path)
    key = hashlib.sha1(("%s|%s|%s" % (path, st.st_mtime, st.st_size)).encode()).hexdigest()
    out = os.path.join(THUMB_DIR, key + ".jpg")
    if not os.path.exists(out):
        os.makedirs(THUMB_DIR, exist_ok=True)
        with THUMB_SEM:
            tmp = out + ".tmp.jpg"
            seeks = (["-ss", "5"], ["-ss", "0"]) if low.endswith(VIDEO_EXT) else ([],)
            for seek in seeks:
                try:
                    subprocess.run(["ffmpeg", "-v", "error"] + seek + ["-i", path, "-frames:v", "1",
                                    "-vf", "scale=320:-2", "-y", tmp], timeout=60,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except subprocess.TimeoutExpired:
                    pass
                if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                    os.replace(tmp, out)
                    break
        if not os.path.exists(out):
            raise ApiError(404, "ছবি বানানো যায়নি")
    h.serve_file(out, inline=True, cache=86400)


@route("POST", "/api/share")
def api_share(h):
    body = h.json()
    path = local_path(body.get("path"))
    if path == ROOT or not os.path.exists(path):
        raise ApiError(404, "ফাইল পাওয়া যায়নি")
    days = int(body.get("days") or 7)
    if days not in (1, 7, 30, 365):
        days = 7
    payload = b64e(json.dumps({"p": rel_path(path), "e": int(time.time()) + days * 86400}).encode())
    token = "%s.%s" % (payload, sign(payload)[:32])
    name = os.path.basename(path) + (".zip" if os.path.isdir(path) else "")
    return {"url": "/d/%s/%s" % (token, urllib.parse.quote(name)), "days": days}


@route("POST", "/api/fs/mkdir")
def api_mkdir(h):
    body = h.json()
    parent = local_path(body.get("dir"))
    os.makedirs(os.path.join(parent, check_name(body.get("name"))), exist_ok=True)
    return {"ok": True}


@route("POST", "/api/fs/rename")
def api_rename(h):
    body = h.json()
    src = local_path(body.get("path"))
    if src == ROOT:
        raise ApiError(400, "মূল ফোল্ডারের নাম বদলানো যাবে না")
    dest = os.path.join(os.path.dirname(src), check_name(body.get("name")))
    if os.path.exists(dest):
        raise ApiError(409, "এই নামে আগে থেকেই আছে")
    os.rename(src, dest)
    return {"ok": True}


@route("POST", "/api/fs/move")
def api_move(h):
    """Move or copy files/folders to another folder inside the download root."""
    body = h.json()
    paths = body.get("paths") or ([body["path"]] if body.get("path") else [])
    dest_dir = local_path(clean_folder(body.get("dest")))
    os.makedirs(dest_dir, exist_ok=True)
    copy = bool(body.get("copy"))
    done = 0
    for rel in paths:
        src = local_path(rel)
        if src == ROOT:
            raise ApiError(400, "মূল ফোল্ডার সরানো যাবে না")
        target = unique_path(os.path.join(dest_dir, os.path.basename(src)))
        if copy:
            if os.path.isdir(src):
                shutil.copytree(src, target, symlinks=True)
            else:
                shutil.copy2(src, target)
        else:
            if os.path.isdir(src) and (dest_dir == src or dest_dir.startswith(src + os.sep)):
                raise ApiError(400, "ফোল্ডারকে তার নিজের ভেতরে সরানো যাবে না")
            shutil.move(src, target)
        done += 1
    return {"ok": True, "count": done}


@route("POST", "/api/fs/delete")
def api_delete(h):
    body = h.json()
    paths = body.get("paths") or [body.get("path")]
    for rel in paths:
        path = local_path(rel)
        if path == ROOT:
            raise ApiError(400, "মূল ফোল্ডার মোছা যাবে না")
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.remove(path)
        if os.path.exists(path + ".aria2"):
            os.remove(path + ".aria2")
    return {"ok": True}


@route("POST", "/api/fs/extract")
def api_extract(h):
    body = h.json()
    src = local_path(body.get("path"))
    name = os.path.basename(src)
    low = name.lower()
    if not os.path.isfile(src) or not low.endswith(ARCHIVE_EXT):
        raise ApiError(400, "এটা zip/rar/7z/tar ফাইল না")
    base = name
    for ext in sorted(ARCHIVE_EXT, key=len, reverse=True):
        if low.endswith(ext):
            base = name[:-len(ext)]
            break
    dest = unique_path(os.path.join(os.path.dirname(src), base or "extracted"))
    password = body.get("password") or ""
    sevenzip = shutil.which("7zz") or shutil.which("7z")
    if low.endswith(TAR_EXT):
        cmd = ["tar", "-xf", src, "-C", dest]
    elif low.endswith(".rar") and shutil.which("unrar"):
        cmd = ["unrar", "x", "-o+", "-p" + (password or "-"), src, dest + "/"]
    elif sevenzip:
        cmd = [sevenzip, "x", "-y", "-bsp1", "-bso0", "-o" + dest] + (["-p" + password] if password else []) + [src]
    else:
        raise ApiError(500, "7zip ইনস্টল নেই")
    os.makedirs(dest)
    return start_job("🗜 খোলা হচ্ছে: %s" % name, cmd, parse_percent, kind="archive")


def probe_video(path):
    if not shutil.which("ffprobe"):
        raise ApiError(500, "ffmpeg ইনস্টল নেই")
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:stream=index,codec_name,codec_type:stream_tags=language,title"
                            ":stream_disposition=default,forced,attached_pic", "-of", "json", path],
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ApiError(504, "ভিডিও পড়তে অনেক সময় লাগছে")
    if r.returncode != 0:
        raise ApiError(400, "ভিডিও ফাইল পড়া যায়নি")
    data = json.loads(r.stdout or "{}")
    subs, audio, video = [], 0, None
    for st in data.get("streams") or []:
        kind = st.get("codec_type")
        tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
        disp = st.get("disposition") or {}
        codec = st.get("codec_name", "")
        if kind == "subtitle":
            subs.append({"index": len(subs), "codec": codec, "language": tags.get("language", ""),
                         "title": tags.get("title", ""), "default": bool(disp.get("default")),
                         "forced": bool(disp.get("forced")), "image": codec in IMAGE_SUB_CODECS})
        elif kind == "audio":
            audio += 1
        elif kind == "video" and video is None and not disp.get("attached_pic"):
            video = codec
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        duration = 0
    return {"duration": duration, "subs": subs, "audio": audio, "video": video}


def fmt_clock(sec):
    sec = int(sec)
    return "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60) if sec >= 3600 else "%d:%02d" % (sec // 60, sec % 60)


def ffmpeg_parser(duration):
    def parse(job, line):
        key, _, value = line.partition("=")
        if key in ("out_time_us", "out_time_ms") and value.isdigit() and duration > 0:
            done = int(value) / 1e6
            job["progress"] = min(99, int(done * 100 / duration))
            job["_clock"] = "%s / %s" % (fmt_clock(done), fmt_clock(duration))
        elif key == "speed" and value not in ("N/A", ""):
            job["info"] = "%s · স্পিড %s" % (job.get("_clock", ""), value)
    return parse


def video_source(rel):
    src = local_path(rel)
    if not os.path.isfile(src) or not src.lower().endswith(VIDEO_EXT):
        raise ApiError(400, "এটা ভিডিও ফাইল না")
    if not shutil.which("ffmpeg"):
        raise ApiError(500, "ffmpeg ইনস্টল নেই")
    return src


def remove_on_failure(path):
    def cleanup(job):
        if job["status"] != "done" and os.path.exists(path):
            os.remove(path)
    return cleanup


@route("GET", "/api/subs/info", role="viewer")
def api_subs_info(h):
    src = video_source(h.query.get("path"))
    info = probe_video(src)
    folder = os.path.dirname(src)
    info["files"] = sorted(rel_path(os.path.join(folder, n)) for n in os.listdir(folder)
                           if n.lower().endswith(SUB_EXT) and os.path.isfile(os.path.join(folder, n)))
    return info


@route("POST", "/api/subs/edit")
def api_subs_edit(h):
    """Remove and/or add soft subtitle tracks without re-encoding (stream copy)."""
    body = h.json()
    src = video_source(body.get("path"))
    info = probe_video(src)
    subs = info["subs"]
    try:
        remove = sorted({int(i) for i in body.get("remove") or []})
    except (TypeError, ValueError):
        raise ApiError(400, "ভুল সাবটাইটেল নম্বর")
    if any(i < 0 or i >= len(subs) for i in remove):
        raise ApiError(400, "ভুল সাবটাইটেল নম্বর")
    add = []
    for a in body.get("add") or []:
        p = local_path(a.get("path"))
        if not os.path.isfile(p) or not p.lower().endswith(SUB_EXT):
            raise ApiError(400, "সাবটাইটেল ফাইল পাওয়া যায়নি (.srt/.ass/.vtt)")
        lang = (a.get("lang") or "").strip().lower()
        if lang and not LANG_RE.match(lang):
            raise ApiError(400, "ভুল ভাষার কোড: %s" % lang)
        add.append({"path": p, "lang": lang, "title": (a.get("title") or "").strip()[:80]})
    default = body.get("default") or {}
    kept = [i for i in range(len(subs)) if i not in remove]
    total = len(kept) + len(add)
    target = None
    if default.get("type") == "old" and int(default.get("index", -1)) in kept:
        target = kept.index(int(default["index"]))
    elif default.get("type") == "new" and 0 <= int(default.get("index", -1)) < len(add):
        target = len(kept) + int(default["index"])
    elif default.get("type") == "off":
        target = -1
    if not remove and not add and target is None:
        raise ApiError(400, "কিছুই বদলানো হয়নি")

    base, ext = os.path.splitext(src)
    out_ext = ext.lower() if ext.lower() in MP4_LIKE + (".mkv", ".webm") else ".mkv"
    mp4_like, webm = out_ext in MP4_LIKE, out_ext == ".webm"
    if (mp4_like or webm) and any(subs[i]["image"] for i in kept):
        raise ApiError(400, "এই ভিডিওতে ছবির (PGS/DVD) সাবটাইটেল আছে, যা MP4-এ রাখা যায় না। ওগুলো মুছে দিন।")
    out = unique_path(base + " [subs]" + out_ext)

    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", src]
    for a in add:
        cmd += ["-i", a["path"]]
    if mp4_like or webm:
        cmd += ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?"]
    else:
        cmd += ["-map", "0"]
    for i in remove:
        cmd += ["-map", "-0:s:%d" % i]
    for n in range(len(add)):
        cmd += ["-map", "%d:0" % (n + 1)]
    cmd += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]
    if mp4_like:
        cmd += ["-c:s", "mov_text"]
    elif webm:
        cmd += ["-c:s", "webvtt"]
    for n, a in enumerate(add):
        k = len(kept) + n
        if not (mp4_like or webm):
            cmd += ["-c:s:%d" % k, "ass" if a["path"].lower().endswith((".ass", ".ssa")) else "srt"]
        if a["lang"]:
            cmd += ["-metadata:s:s:%d" % k, "language=" + a["lang"]]
        if a["title"]:
            cmd += ["-metadata:s:s:%d" % k, "title=" + a["title"]]
    if target is not None:
        for k in range(total):
            cmd += ["-disposition:s:%d" % k, "default" if k == target else "0"]
    cmd += ["-progress", "pipe:1", "-nostats", out]

    replace = bool(body.get("replace"))

    def finish(job):
        if not replace:
            return
        final = base + out_ext
        os.remove(src)
        if final != src and os.path.exists(final):
            return
        os.replace(out, final)
        job["title"] = "🎞 সাবটাইটেল (আসল ফাইল বদলানো হয়েছে): %s" % os.path.basename(final)

    return start_job("🎞 সাবটাইটেল: %s" % os.path.basename(src), cmd, ffmpeg_parser(info["duration"]),
                     kind="video", on_done=finish, always=remove_on_failure(out))


@route("POST", "/api/subs/extract")
def api_subs_extract(h):
    """Save one subtitle track as a separate .srt/.ass file next to the video."""
    body = h.json()
    src = video_source(body.get("path"))
    subs = probe_video(src)["subs"]
    idx = int(body.get("index", -1))
    if not 0 <= idx < len(subs):
        raise ApiError(400, "ভুল সাবটাইটেল নম্বর")
    sub = subs[idx]
    if sub["image"]:
        raise ApiError(400, "এটা ছবির (PGS/DVD) সাবটাইটেল, লেখা হিসেবে বের করা যায় না")
    ext = ".ass" if sub["codec"] in ("ass", "ssa") else ".srt"
    base = os.path.splitext(src)[0]
    out = unique_path(base + (("." + sub["language"]) if LANG_RE.match(sub["language"]) else "") + ext)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", src, "-map", "0:s:%d" % idx, out]
    return start_job("🎞 সাবটাইটেল বের করা: %s" % os.path.basename(out), cmd, kind="video",
                     always=remove_on_failure(out))


@route("POST", "/api/subs/burn")
def api_subs_burn(h):
    """Render a subtitle into the picture (re-encodes the video to H.264/AAC MP4)."""
    body = h.json()
    src = video_source(body.get("path"))
    info = probe_video(src)
    source = body.get("source") or {}
    # libass filter arguments are hard to escape, so give ffmpeg simple symlinked paths.
    work = os.path.join(DATA_DIR, "work", secrets.token_hex(6))
    os.makedirs(work)
    video_link = os.path.join(work, "in" + os.path.splitext(src)[1].lower())
    os.symlink(src, video_link)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", video_link]
    try:
        if source.get("type") == "track":
            idx = int(source.get("index", -1))
            if not 0 <= idx < len(info["subs"]):
                raise ApiError(400, "ভুল সাবটাইটেল নম্বর")
            if info["subs"][idx]["image"]:
                cmd += ["-filter_complex", "[0:v:0][0:s:%d]overlay[v]" % idx, "-map", "[v]"]
            else:
                cmd += ["-map", "0:v:0", "-vf", "subtitles=%s:si=%d" % (video_link, idx)]
        elif source.get("type") == "file":
            sub = local_path(source.get("path"))
            if not os.path.isfile(sub) or not sub.lower().endswith(SUB_EXT):
                raise ApiError(400, "সাবটাইটেল ফাইল পাওয়া যায়নি")
            sub_link = os.path.join(work, "sub" + os.path.splitext(sub)[1].lower())
            os.symlink(sub, sub_link)
            cmd += ["-map", "0:v:0", "-vf", "subtitles=%s" % sub_link]
        else:
            raise ApiError(400, "কোন সাবটাইটেল বসাবেন বেছে নিন")
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    out = unique_path(os.path.splitext(src)[0] + " [hardsub].mp4")
    preset = "medium" if body.get("quality") == "good" else "veryfast"
    cmd += ["-map", "0:a?", "-c:v", "libx264", "-preset", preset, "-crf", "22", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-sn",
            "-progress", "pipe:1", "-nostats", out]

    def cleanup(job):
        shutil.rmtree(work, ignore_errors=True)
        remove_on_failure(out)(job)

    return start_job("🔥 সাবটাইটেল বসানো: %s" % os.path.basename(src), cmd, ffmpeg_parser(info["duration"]),
                     kind="video", always=cleanup)


@route("GET", "/api/cloud/list", role="viewer")
def api_cloud_list(h):
    remote = get_remote(h.query.get("remote", ""))["name"]
    rel = clean_folder(h.query.get("path"))
    items = json.loads(rclone("lsjson", "--max-depth", "1", rpath(remote, rel)) or "[]")
    items = [{"name": i["Name"], "dir": i["IsDir"], "size": max(0, i.get("Size") or 0),
              "mtime": i.get("ModTime", "")} for i in items]
    items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
    return {"remote": remote, "path": rel, "items": items}


@route("GET", "/api/cloud/about", role="viewer")
def api_cloud_about(h):
    remote = get_remote(h.query.get("remote", ""))["name"]
    try:
        return json.loads(rclone("about", "--json", remote + ":", timeout=30))
    except (ApiError, ValueError):
        return {}


@route("GET", "/api/cloud/download", role="viewer")
def api_cloud_download(h):
    remote = get_remote(h.query.get("remote", ""))["name"]
    rel = clean_folder(h.query.get("path"))
    if not rel:
        raise ApiError(400, "ফাইল বেছে নিন")
    proc = subprocess.Popen(["rclone", "--config", RCLONE_CONF, "cat", rpath(remote, rel)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    first = proc.stdout.read(CHUNK)
    if not first and proc.wait() != 0:
        raise ApiError(502, "Cloud থেকে পড়া যায়নি")
    name = rel.rsplit("/", 1)[-1]
    h.send_response(200)
    h.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
    h.send_header("X-Content-Type-Options", "nosniff")
    size = int(h.query.get("size") or 0)
    if size > 0:
        h.send_header("Content-Length", str(size))
    h.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
    h.end_headers()
    h.sent = True
    try:
        chunk = first
        while chunk:
            h.wfile.write(chunk)
            chunk = proc.stdout.read(CHUNK)
    finally:
        proc.kill()
        proc.wait()


@route("PUT", "/api/cloud/put")
def api_cloud_put(h):
    remote = get_remote(h.query.get("remote", ""))["name"]
    folder = clean_folder(h.query.get("dir"))
    name = check_name(h.query.get("name"))
    length = int(h.headers.get("Content-Length") or -1)
    if length < 0:
        raise ApiError(411, "Content-Length দরকার")
    proc = subprocess.Popen(["rclone", "--config", RCLONE_CONF, "rcat", "--size", str(length),
                             rpath(remote, (folder + "/" + name).strip("/"))],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        remaining = length
        while remaining > 0:
            chunk = h.rfile.read(min(CHUNK, remaining))
            if not chunk:
                proc.kill()
                raise ApiError(400, "আপলোড মাঝপথে থেমে গেছে")
            proc.stdin.write(chunk)
            remaining -= len(chunk)
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace").strip()
        if proc.wait() != 0:
            raise ApiError(502, err.splitlines()[-1] if err else "Cloud-এ আপলোড ব্যর্থ")
    except BrokenPipeError:
        proc.wait()
        raise ApiError(502, "Cloud-এ আপলোড ব্যর্থ")
    return {"ok": True}


@route("POST", "/api/cloud/mkdir")
def api_cloud_mkdir(h):
    body = h.json()
    remote = get_remote(body.get("remote", ""))["name"]
    folder = clean_folder(body.get("dir"))
    rclone("mkdir", rpath(remote, (folder + "/" + check_name(body.get("name"))).strip("/")))
    return {"ok": True}


@route("POST", "/api/cloud/delete")
def api_cloud_delete(h):
    body = h.json()
    remote = get_remote(body.get("remote", ""))["name"]
    rel = clean_folder(body.get("path"))
    if not rel:
        raise ApiError(400, "মূল ফোল্ডার মোছা যাবে না")
    rclone("purge" if body.get("dir") else "deletefile", rpath(remote, rel), timeout=600)
    return {"ok": True}


@route("POST", "/api/cloud/upload")
def api_cloud_upload(h):
    """Server file/folder -> cloud (background job)."""
    body = h.json()
    remote = get_remote(body.get("remote", ""))["name"]
    src = local_path(body.get("path"))
    if src == ROOT or not os.path.exists(src):
        raise ApiError(400, "ফাইল বা ফোল্ডার বেছে নিন")
    dest = clean_folder(body.get("dest"))
    name = os.path.basename(src)
    verb = "move" if body.get("move") else "copy"
    target = rpath(remote, (dest + "/" + name).strip("/") if os.path.isdir(src) else dest)
    return start_job("সার্ভার → %s: %s" % (remote, name), rclone_cmd(verb, src, target), parse_rclone)


@route("POST", "/api/cloud/fetch")
def api_cloud_fetch(h):
    """Cloud file/folder -> server (background job)."""
    body = h.json()
    remote = get_remote(body.get("remote", ""))["name"]
    rel = clean_folder(body.get("path"))
    if not rel:
        raise ApiError(400, "ফাইল বা ফোল্ডার বেছে নিন")
    dest = local_path(clean_folder(body.get("dest")))
    os.makedirs(dest, exist_ok=True)
    name = rel.rsplit("/", 1)[-1]
    target = os.path.join(dest, name) if body.get("dir") else dest
    return start_job("%s → সার্ভার: %s" % (remote, name), rclone_cmd("copy", rpath(remote, rel), target),
                     parse_rclone)


@route("POST", "/api/cloud/auth/start", role="admin")
def api_cloud_auth_start(h):
    return {"url": auth_start(h.json().get("type", "drive"))}


@route("POST", "/api/cloud/auth/finish", role="admin")
def api_cloud_auth_finish(h):
    return {"remote": auth_finish(h.json().get("url"))}


@route("POST", "/api/cloud/token", role="admin")
def api_cloud_token(h):
    body = h.json()
    typ = body.get("type", "drive")
    if typ not in OAUTH_TYPES:
        raise ApiError(400, "এই ধরনের একাউন্ট সাপোর্ট করে না")
    return {"remote": create_remote(typ, oauth_params(typ, parse_token(body.get("token"))))}


@route("POST", "/api/cloud/mega", role="admin")
def api_cloud_mega(h):
    body = h.json()
    user, password = (body.get("user") or "").strip(), body.get("pass") or ""
    if not user or not password:
        raise ApiError(400, "Mega-র ইমেইল আর পাসওয়ার্ড দিন")
    return {"remote": create_remote("mega", ["user", user, "pass", password], obscure=True)}


@route("POST", "/api/cloud/remove", role="admin")
def api_cloud_remove(h):
    name = h.json().get("remote", "")
    get_remote(name)
    rclone("config", "delete", name)
    return {"ok": True}


@route("GET", "/api/jobs", role="viewer")
def api_jobs(h):
    jobs = sorted(JOBS.values(), key=lambda j: j["started"], reverse=True)
    return {"jobs": [public_job(j) for j in jobs]}


@route("POST", "/api/jobs/cancel")
def api_jobs_cancel(h):
    job = JOBS.get(h.json().get("id"))
    if job and job["status"] == "running":
        job["status"] = "cancelled"
        job["_proc"].kill()
    return {"ok": True}


@route("POST", "/api/jobs/clear")
def api_jobs_clear(h):
    for jid in [j["id"] for j in JOBS.values() if j["status"] != "running"]:
        JOBS.pop(jid, None)
    return {"ok": True}


# ---------------------------------------------------------------- HTTP handler

class ZipOut:
    """Write-only file object for zipfile that streams to the HTTP response."""

    def __init__(self, wfile):
        self.wfile = wfile

    def write(self, data):
        self.wfile.write(data)
        return len(data)

    def flush(self):
        pass


CSP = ("default-src 'self'; img-src 'self' data:; "
       "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; media-src 'self'; object-src 'none'; "
       "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")

# URL path -> (filename beside server.py, Content-Type, Cache-Control)
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8", "no-cache"),
    "/index.html": ("index.html", "text/html; charset=utf-8", "no-cache"),
    "/app.css": ("app.css", "text/css; charset=utf-8", "no-cache"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8", "no-cache"),
    "/sw.js": ("sw.js", "application/javascript; charset=utf-8", "no-cache"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8", "no-cache"),
    "/icon.svg": ("icon.svg", "image/svg+xml", "public, max-age=86400"),
}


# Static error messages -> English, picked when the request carries X-Lang: en.
# Messages built with a value (%s/%d) stay Bangla (they won't match a key).
MSG_EN = {
    "ভুল পাথ": "Invalid path", "ভুল নাম": "Invalid name",
    "rclone ইনস্টল নেই": "rclone is not installed",
    "rclone সময়মতো উত্তর দেয়নি": "rclone did not respond in time",
    "rclone ব্যর্থ হয়েছে": "rclone failed",
    "Cloud একাউন্ট পাওয়া যায়নি, আগে কানেক্ট করুন": "No cloud account found, connect one first",
    "টোকেন পড়া যায়নি। rclone যা দেখিয়েছে পুরোটা পেস্ট করুন।": "Could not read the token. Paste everything rclone showed.",
    "এই ধরনের একাউন্ট সাপোর্ট করে না": "This account type is not supported",
    "rclone লগইন শুরু করতে পারেনি": "rclone could not start login",
    "লগইন লিংক পাওয়া যায়নি, টোকেন পদ্ধতি ব্যবহার করুন": "No login link; use the token method",
    "address bar-এর পুরো লিংক পেস্ট করুন (এতে code= থাকে)": "Paste the full address-bar link (it contains code=)",
    "লগইনের মেয়াদ শেষ, আবার লিংক তৈরি করুন": "Login expired, make a new link",
    "rclone লগইন হেল্পারে পৌঁছানো যায়নি": "Could not reach the rclone login helper",
    "লগইনের সময় শেষ": "Login timed out", "aria2 চলছে না": "aria2 is not running",
    "লগইন সম্পূর্ণ হয়নি। আবার 'লগইন লিংক তৈরি করো' দিয়ে চেষ্টা করুন।":
        "Login did not complete. Try again with 'Make a login link'.",
    "ভিডিও লিংক http/https হতে হবে": "The video link must be http/https",
    "yt-dlp ইনস্টল নেই": "yt-dlp is not installed",
    "সাবটাইটেলের ভাষা এভাবে লিখুন: bn,en": "Write the subtitle languages like: bn,en",
    "এটা Google Drive লিংক না": "This is not a Google Drive link",
    "ভিডিও সাইটের লিংক আগে সার্ভারে নামান (অটো আপলোড চালু রাখলে Cloud-এ যাবে)":
        "Download video-site links to the server first (auto-upload will send them to the cloud)",
    "Magnet/Torrent সরাসরি Cloud-এ যায় না, আগে সার্ভারে নামান":
        "Magnet/Torrent cannot go straight to the cloud; download to the server first",
    "ইউজারনেম বা পাসওয়ার্ড ভুল": "Wrong username or password",
    "বর্তমান পাসওয়ার্ড ভুল": "Current password is wrong",
    "নতুন পাসওয়ার্ড কমপক্ষে ৮ অক্ষরের হতে হবে": "The new password must be at least 8 characters",
    "ইউজারনেম শুধু ইংরেজি অক্ষর, সংখ্যা, _ . - দিয়ে (১-৩২ অক্ষর)":
        "Username may use only letters, digits, _ . - (1–32 chars)",
    "ভুল role": "Invalid role",
    "নতুন ইউজারের পাসওয়ার্ড কমপক্ষে ৮ অক্ষর": "A new user's password must be at least 8 characters",
    "পাসওয়ার্ড কমপক্ষে ৮ অক্ষর": "Password must be at least 8 characters",
    "অন্তত একজন admin থাকতে হবে": "At least one admin is required",
    "নিজেকে মুছতে পারবেন না": "You cannot delete yourself",
    "সময় HH:MM আকারে দিন (যেমন 01:30)": "Enter the time as HH:MM (e.g. 01:30)",
    "মান ০ বা তার বেশি হতে হবে": "The value must be 0 or more",
    "Telegram বট টোকেন ঠিক নেই": "The Telegram bot token is invalid",
    "আগে বট টোকেন আর Chat ID সেভ করুন": "Save the bot token and Chat ID first",
    "মেসেজ যায়নি। টোকেন/Chat ID দেখুন, আর বটকে একবার /start পাঠান।":
        "Message failed. Check the token/Chat ID and send /start to the bot once.",
    "RSS লিংক http/https হতে হবে": "The RSS link must be http/https",
    "এই কমান্ড চলবে না": "This command is not allowed", "অনুমতি নেই": "Not allowed",
    "এই কাজের অনুমতি আপনার নেই": "You don't have permission for this",
    "লগইন দরকার": "Login required", "X-Panel header নেই": "Missing X-Panel header",
    "পাওয়া যায়নি": "Not found", "ভুল JSON": "Invalid JSON",
    "অনেক বড় রিকোয়েস্ট": "Request too large", "আগে লিংক দিন": "Enter a link first",
    "ফোল্ডার পাওয়া যায়নি": "Folder not found", "কী খুঁজবেন লিখুন": "Type what to search for",
    "Content-Length দরকার": "Content-Length required",
    "আপলোড মাঝপথে থেমে গেছে": "Upload stopped midway",
    "ফাইল পাওয়া যায়নি": "File not found", "কিছু সিলেক্ট করা নেই": "Nothing selected",
    "ছবি নেই": "No image", "ffmpeg নেই": "ffmpeg is missing", "ছবি বানানো যায়নি": "Could not make the image",
    "ভুল ডাউনলোড ফোল্ডার": "Invalid download folder", "ভুল ফাইলের নাম": "Invalid file name",
    "মূল ফোল্ডারের নাম বদলানো যাবে না": "The root folder cannot be renamed",
    "এই নামে আগে থেকেই আছে": "Something with this name already exists",
    "মূল ফোল্ডার মোছা যাবে না": "The root folder cannot be deleted",
    "মূল ফোল্ডার সরানো যাবে না": "The root folder cannot be moved",
    "ফোল্ডারকে তার নিজের ভেতরে সরানো যাবে না": "A folder cannot be moved inside itself",
    "এটা zip/rar/7z/tar ফাইল না": "This is not a zip/rar/7z/tar file",
    "7zip ইনস্টল নেই": "7zip is not installed", "ffmpeg ইনস্টল নেই": "ffmpeg is not installed",
    "ভিডিও পড়তে অনেক সময় লাগছে": "Reading the video is taking too long",
    "ভিডিও ফাইল পড়া যায়নি": "Could not read the video file", "এটা ভিডিও ফাইল না": "This is not a video file",
    "ভুল সাবটাইটেল নম্বর": "Invalid subtitle number",
    "সাবটাইটেল ফাইল পাওয়া যায়নি (.srt/.ass/.vtt)": "Subtitle file not found (.srt/.ass/.vtt)",
    "কিছুই বদলানো হয়নি": "Nothing was changed",
    "এই ভিডিওতে ছবির (PGS/DVD) সাবটাইটেল আছে, যা MP4-এ রাখা যায় না। ওগুলো মুছে দিন।":
        "This video has image (PGS/DVD) subtitles that can't be kept in MP4. Remove them.",
    "এটা ছবির (PGS/DVD) সাবটাইটেল, লেখা হিসেবে বের করা যায় না":
        "This is an image (PGS/DVD) subtitle; it can't be extracted as text",
    "সাবটাইটেল ফাইল পাওয়া যায়নি": "Subtitle file not found",
    "কোন সাবটাইটেল বসাবেন বেছে নিন": "Choose which subtitle to burn in",
    "ফাইল বা ফোল্ডার বেছে নিন": "Choose a file or folder", "ফাইল বেছে নিন": "Choose a file",
    "Cloud থেকে পড়া যায়নি": "Could not read from the cloud", "Cloud-এ আপলোড ব্যর্থ": "Cloud upload failed",
    "ফাইলটা আর নেই": "The file is gone", "লিংক ঠিক নেই": "Invalid link",
    "এই লিংকের মেয়াদ শেষ": "This link has expired",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "aria2-panel"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def dispatch(self, method):
        self.sent = False
        self.extra_headers = []
        self.user = self.role = None
        self.lang = self.headers.get("X-Lang", "bn")
        url = urllib.parse.urlsplit(self.path)
        self.query_all = urllib.parse.parse_qs(url.query)
        self.query = {k: v[0] for k, v in self.query_all.items()}
        try:
            if method == "GET" and url.path in STATIC_FILES:
                return self.serve_static(url.path)
            if method == "GET" and url.path.startswith("/d/"):
                return self.serve_share(url.path)
            entry = ROUTES.get((method, url.path))
            if not entry:
                raise ApiError(404, "পাওয়া যায়নি")
            fn, role = entry
            if method != "GET" and self.headers.get("X-Panel") != "1":
                raise ApiError(403, "X-Panel header নেই")
            if role:
                self.user = self.current_user()
                if not self.user:
                    raise ApiError(401, "লগইন দরকার")
                self.role = CFG["users"][self.user]["role"]
                if ROLES[self.role] < ROLES[role]:
                    raise ApiError(403, "এই কাজের অনুমতি আপনার নেই")
            result = fn(self)
            if result is not None:
                self.send_json(result)
        except ApiError as e:
            self.send_json({"error": str(e)}, e.code)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as e:
            self.send_json({"error": e.strerror or str(e)}, 500)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": "সার্ভারে সমস্যা: %s" % e}, 500)

    def current_user(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name != "panel":
                continue
            pieces = value.split(".")
            if len(pieces) != 3:
                return None
            try:
                user = b64d(pieces[0]).decode()
            except Exception:
                return None
            exp, sig = pieces[1], pieces[2]
            if (user in CFG["users"] and exp.isdigit() and int(exp) > time.time()
                    and hmac.compare_digest(sig, sign(session_payload(user, exp)))):
                return user
        return None

    def json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 32 * 1024 * 1024:
            raise ApiError(413, "অনেক বড় রিকোয়েস্ট")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ApiError(400, "ভুল JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "ভুল JSON")
        return data

    def send_json(self, data, code=200):
        if self.sent:
            return
        if getattr(self, "lang", "bn") == "en" and isinstance(data, dict) and "error" in data:
            data = dict(data, error=MSG_EN.get(data["error"], data["error"]))
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in self.extra_headers:
            self.send_header(k, v)
        self.end_headers()
        self.sent = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def serve_static(self, route):
        fname, ctype, cache = STATIC_FILES[route]
        with open(os.path.join(STATIC_DIR, fname), "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if fname == "index.html":
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", CSP)
        if route == "/sw.js":
            self.send_header("Service-Worker-Allowed", "/")
        self.end_headers()
        self.sent = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def serve_share(self, path):
        parts = path.split("/")
        if len(parts) < 3:
            raise ApiError(404, "পাওয়া যায়নি")
        payload, _, sig = parts[2].partition(".")
        if not hmac.compare_digest(sig, sign(payload)[:32]):
            raise ApiError(403, "লিংক ঠিক নেই")
        data = json.loads(b64d(payload))
        if data["e"] < time.time():
            raise ApiError(410, "এই লিংকের মেয়াদ শেষ")
        target = local_path(data["p"])
        if target == ROOT or not os.path.exists(target):
            raise ApiError(404, "ফাইলটা আর নেই")
        if os.path.isdir(target):
            self.stream_zip([target], os.path.basename(target) + ".zip")
        else:
            self.serve_file(target, inline=False)

    def stream_zip(self, paths, name):
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()
        self.sent = True
        if self.command == "HEAD":
            return
        with zipfile.ZipFile(ZipOut(self.wfile), "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for p in paths:
                base = os.path.dirname(p)
                if os.path.isdir(p):
                    for dirpath, dirs, files in os.walk(p):
                        # Never follow symlinks out of ROOT (e.g. links planted by a
                        # malicious archive): skip linked sub-dirs and linked files.
                        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(dirpath, d))]
                        for fname in sorted(files):
                            if fname.endswith(HIDDEN_SUFFIXES):
                                continue
                            full = os.path.join(dirpath, fname)
                            if os.path.islink(full):
                                continue
                            if os.path.isfile(full):
                                zf.write(full, os.path.relpath(full, base))
                elif not os.path.islink(p):
                    zf.write(p, os.path.basename(p))

    def serve_file(self, path, inline, cache=0):
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                self.sent = True
                return
            status = 206
        name = os.path.basename(path)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        # Don't let the browser run an untrusted upload as active content in the
        # panel's origin; nosniff also stops an octet-stream being sniffed as HTML.
        if inline and ctype in ("text/html", "application/xhtml+xml", "image/svg+xml",
                                "text/xml", "application/xml"):
            inline = False
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if cache:
            self.send_header("Cache-Control", "private, max-age=%d" % cache)
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Disposition", "%s; filename*=UTF-8''%s"
                         % ("inline" if inline else "attachment", urllib.parse.quote(name)))
        self.end_headers()
        self.sent = True
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def main():
    migrate_config()
    for target in (monitor_loop, telegram_loop, rss_loop):
        threading.Thread(target=target, daemon=True).start()
    host = CFG.get("listen", "127.0.0.1")
    port = int(CFG.get("port", 8090))
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print("aria2 panel %s listening on %s:%d" % (VERSION, host, port), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
