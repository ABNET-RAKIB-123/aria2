#!/usr/bin/env python3
"""aria2 web panel: downloads (aria2), server files and Google Drive (rclone).

Uses only the Python standard library. Configuration comes from the JSON
file named by PANEL_CONFIG (default /etc/aria2/panel.json), written by
install.sh.
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATH = os.environ.get("PANEL_CONFIG", "/etc/aria2/panel.json")
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

with open(CONFIG_PATH) as f:
    CFG = json.load(f)

ROOT = os.path.realpath(CFG["download_dir"])
REMOTE = CFG.get("remote", "gdrive")
RCLONE_CONF = CFG["rclone_config"]
STATE_PATH = CFG["state_file"]
SESSION_KEY = CFG["session_key"].encode()
SESSION_TTL = 30 * 86400
SHARE_TTL = 7 * 86400
CHUNK = 1024 * 1024
HIDDEN_SUFFIXES = (".aria2", ".uploading")

DEFAULT_STATE = {"autoupload": False, "autoupload_dest": "aria2", "autoupload_move": False}


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- helpers

def load_state():
    state = dict(DEFAULT_STATE)
    try:
        with open(STATE_PATH) as f:
            state.update(json.load(f))
    except (OSError, ValueError):
        pass
    return state


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def save_config():
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(CFG, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000).hex()


def sign(data):
    return hmac.new(SESSION_KEY, data.encode(), hashlib.sha256).hexdigest()


def b64e(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def local_path(rel):
    rel = (rel or "").strip("/")
    path = os.path.realpath(os.path.join(ROOT, rel))
    if path != ROOT and not path.startswith(ROOT + os.sep):
        raise ApiError(400, "Invalid path")
    return path


def rel_path(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/").lstrip(".")


def drive_path(rel):
    return "%s:%s" % (REMOTE, (rel or "").strip("/"))


def check_name(name):
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise ApiError(400, "Invalid name")
    return name


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists("%s (%d)%s" % (base, n, ext)):
        n += 1
    return "%s (%d)%s" % (base, n, ext)


def rclone(*args, timeout=120):
    try:
        r = subprocess.run(["rclone", "--config", RCLONE_CONF] + list(args),
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ApiError(500, "rclone is not installed")
    except subprocess.TimeoutExpired:
        raise ApiError(504, "rclone timed out")
    if r.returncode != 0:
        lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
        raise ApiError(502, lines[-1] if lines else "rclone failed")
    return r.stdout


def drive_connected():
    try:
        with open(RCLONE_CONF) as f:
            return ("[%s]" % REMOTE) in f.read()
    except OSError:
        return False


def require_drive():
    if not drive_connected():
        raise ApiError(400, "Google Drive is not connected")


# ---------------------------------------------------------------- aria2

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
        raise ApiError(502, "aria2 is not running")
    if "error" in reply:
        raise ApiError(400, reply["error"].get("message", "aria2 error"))
    return reply["result"]


# ---------------------------------------------------------------- jobs (rclone transfers)

JOBS = {}
JOB_LIMIT = 50
STATS_RE = re.compile(r"([\d.]+ ?\w*B) / ([\d.]+ ?\w*B), (\d+)%, ([^,]*), ETA (\S+)")


def start_job(title, args):
    job = {"id": secrets.token_hex(4), "title": title, "status": "running", "progress": 0,
           "info": "", "error": "", "started": time.time()}
    cmd = ["rclone", "--config", RCLONE_CONF, "--stats", "2s", "--stats-one-line",
           "--stats-log-level", "NOTICE"] + args
    job["_proc"] = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    text=True, errors="replace")
    JOBS[job["id"]] = job
    finished = [j for j in JOBS.values() if j["status"] != "running"]
    for old in sorted(finished, key=lambda j: j["started"])[:max(0, len(JOBS) - JOB_LIMIT)]:
        JOBS.pop(old["id"], None)
    threading.Thread(target=watch_job, args=(job,), daemon=True).start()
    return public_job(job)


def watch_job(job):
    proc = job["_proc"]
    last_error = ""
    for line in proc.stderr:
        m = STATS_RE.search(line)
        if m:
            job["progress"] = int(m.group(3))
            job["info"] = "%s / %s · %s · ETA %s" % (m.group(1), m.group(2), m.group(4).strip(), m.group(5))
        elif "ERROR" in line:
            last_error = line.split("ERROR", 1)[1].strip(" :\n")
    rc = proc.wait()
    if job["status"] == "cancelled":
        return
    if rc == 0:
        job["status"], job["progress"] = "done", 100
    else:
        job["status"], job["error"] = "error", last_error or "rclone exited with code %d" % rc


def public_job(job):
    return {k: v for k, v in job.items() if not k.startswith("_")}


# ---------------------------------------------------------------- auto upload to Drive

def autoupload_loop():
    seen = None
    while True:
        time.sleep(5)
        try:
            stopped = aria2_rpc("aria2.tellStopped", [0, 1000, ["gid", "status", "files", "dir", "followedBy"]])
        except ApiError:
            continue
        if seen is None:
            seen = {d["gid"] for d in stopped}
            continue
        state = load_state()
        for d in stopped:
            if d["gid"] in seen:
                continue
            seen.add(d["gid"])
            if d["status"] != "complete" or d.get("followedBy") or not d.get("files"):
                continue
            if not state["autoupload"] or not drive_connected():
                continue
            try:
                first = os.path.realpath(d["files"][0]["path"])
                top = os.path.relpath(first, ROOT).split(os.sep)[0]
                src = local_path(top)
            except (ApiError, KeyError, ValueError):
                continue
            if not os.path.exists(src):
                continue
            dest = state["autoupload_dest"].strip("/")
            verb = "move" if state["autoupload_move"] else "copy"
            if os.path.isdir(src):
                args = [verb, src, drive_path(dest + "/" + top)]
            else:
                args = [verb, src, drive_path(dest)]
            start_job("Auto upload: %s → Drive/%s" % (top, dest), args)


# ---------------------------------------------------------------- Google Drive connect

AUTH = {"proc": None, "out": []}
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
    raise ApiError(400, "Could not read the token. Paste everything rclone printed.")


def create_remote(token):
    if drive_connected():
        rclone("config", "delete", REMOTE)
    rclone("config", "create", REMOTE, "drive", "scope", "drive", "token", token, "--non-interactive")
    rclone("lsjson", "--max-depth", "1", drive_path(""), timeout=60)


def auth_start():
    with AUTH_LOCK:
        if AUTH["proc"] and AUTH["proc"].poll() is None:
            AUTH["proc"].kill()
        out = []
        proc = subprocess.Popen(["rclone", "--config", RCLONE_CONF, "authorize", "drive",
                                 "--auth-no-open-browser"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        AUTH.update(proc=proc, out=out)

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
            raise ApiError(502, "rclone did not start the login flow")
        conn = http.client.HTTPConnection("127.0.0.1", 53682, timeout=10)
        conn.request("GET", local.split("53682", 1)[1])
        resp = conn.getresponse()
        location = resp.getheader("Location")
        resp.read()
        if not location:
            raise ApiError(502, "Could not get the Google login link; use the token method")
        return location


def auth_finish(redirect_url):
    query = urllib.parse.urlsplit((redirect_url or "").strip()).query
    if "code=" not in query:
        raise ApiError(400, "Paste the full address from the browser bar (it contains code=)")
    proc = AUTH["proc"]
    if not proc or proc.poll() is not None:
        raise ApiError(400, "Login expired, press Connect again")
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 53682, timeout=30)
        conn.request("GET", "/?" + query)
        conn.getresponse().read()
    except OSError:
        raise ApiError(502, "Could not reach rclone login helper")
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise ApiError(504, "Google login timed out")
    time.sleep(0.2)
    create_remote(parse_token("".join(AUTH["out"])))


# ---------------------------------------------------------------- routes

ROUTES = {}


def route(method, path, auth=True):
    def deco(fn):
        ROUTES[(method, path)] = (fn, auth)
        return fn
    return deco


@route("POST", "/api/login", auth=False)
def api_login(h):
    body = h.json()
    expected = CFG["password_hash"]
    if not hmac.compare_digest(hash_password(body.get("password", ""), CFG["password_salt"]), expected):
        time.sleep(1)
        raise ApiError(403, "Wrong password")
    exp = str(int(time.time()) + SESSION_TTL)
    secure = "; Secure" if h.headers.get("X-Forwarded-Proto") == "https" else ""
    h.extra_headers.append(("Set-Cookie", "panel=%s.%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s"
                            % (exp, sign(exp), SESSION_TTL, secure)))
    return {"ok": True}


@route("POST", "/api/logout")
def api_logout(h):
    h.extra_headers.append(("Set-Cookie", "panel=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"))
    return {"ok": True}


@route("POST", "/api/password")
def api_password(h):
    body = h.json()
    if not hmac.compare_digest(hash_password(body.get("old", ""), CFG["password_salt"]), CFG["password_hash"]):
        raise ApiError(403, "Current password is wrong")
    new = body.get("new", "")
    if len(new) < 8:
        raise ApiError(400, "New password must be at least 8 characters")
    CFG["password_salt"] = secrets.token_hex(16)
    CFG["password_hash"] = hash_password(new, CFG["password_salt"])
    save_config()
    return {"ok": True}


@route("GET", "/api/info")
def api_info(h):
    du = shutil.disk_usage(ROOT)
    return {"disk_total": du.total, "disk_free": du.free, "drive": drive_connected(),
            "state": load_state()}


@route("POST", "/api/settings")
def api_settings(h):
    body = h.json()
    state = load_state()
    for key in DEFAULT_STATE:
        if key in body:
            state[key] = type(DEFAULT_STATE[key])(body[key])
    state["autoupload_dest"] = state["autoupload_dest"].strip().strip("/") or "aria2"
    save_state(state)
    return state


@route("POST", "/api/aria2")
def api_aria2(h):
    body = h.json()
    method = body.get("method", "")
    params = body.get("params") or []
    if method == "system.multicall":
        if not all(str(c.get("methodName", "")).startswith("aria2.") for c in (params[0] if params else [])):
            raise ApiError(400, "Method not allowed")
    elif not method.startswith("aria2."):
        raise ApiError(400, "Method not allowed")
    return {"result": aria2_rpc(method, params)}


@route("GET", "/api/files")
def api_files(h):
    path = local_path(h.query.get("path"))
    if not os.path.isdir(path):
        raise ApiError(404, "Folder not found")
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


@route("PUT", "/api/upload")
def api_upload(h):
    folder = local_path(h.query.get("dir"))
    if not os.path.isdir(folder):
        raise ApiError(404, "Folder not found")
    dest = unique_path(os.path.join(folder, check_name(h.query.get("name"))))
    length = int(h.headers.get("Content-Length") or -1)
    if length < 0:
        raise ApiError(411, "Content-Length required")
    tmp = dest + ".uploading"
    try:
        with open(tmp, "wb") as out:
            remaining = length
            while remaining > 0:
                chunk = h.rfile.read(min(CHUNK, remaining))
                if not chunk:
                    raise ApiError(400, "Upload interrupted")
                out.write(chunk)
                remaining -= len(chunk)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"ok": True, "name": os.path.basename(dest)}


@route("GET", "/api/download")
def api_download(h):
    path = local_path(h.query.get("path"))
    if not os.path.isfile(path):
        raise ApiError(404, "File not found")
    h.serve_file(path, inline=h.query.get("inline") == "1")


@route("POST", "/api/share")
def api_share(h):
    path = local_path(h.json().get("path"))
    if not os.path.isfile(path):
        raise ApiError(404, "File not found")
    payload = b64e(json.dumps({"p": rel_path(path), "e": int(time.time()) + SHARE_TTL}).encode())
    token = "%s.%s" % (payload, sign(payload)[:32])
    return {"url": "/d/%s/%s" % (token, urllib.parse.quote(os.path.basename(path))), "days": SHARE_TTL // 86400}


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
        raise ApiError(400, "Cannot rename the root folder")
    dest = os.path.join(os.path.dirname(src), check_name(body.get("name")))
    if os.path.exists(dest):
        raise ApiError(409, "A file with that name already exists")
    os.rename(src, dest)
    return {"ok": True}


@route("POST", "/api/fs/delete")
def api_delete(h):
    path = local_path(h.json().get("path"))
    if path == ROOT:
        raise ApiError(400, "Cannot delete the root folder")
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.remove(path)
    if os.path.exists(path + ".aria2"):
        os.remove(path + ".aria2")
    return {"ok": True}


@route("GET", "/api/drive/list")
def api_drive_list(h):
    require_drive()
    rel = (h.query.get("path") or "").strip("/")
    items = json.loads(rclone("lsjson", "--max-depth", "1", drive_path(rel)) or "[]")
    items = [{"name": i["Name"], "dir": i["IsDir"], "size": max(0, i.get("Size") or 0),
              "mtime": i.get("ModTime", "")} for i in items]
    items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
    return {"path": rel, "items": items}


@route("GET", "/api/drive/about")
def api_drive_about(h):
    require_drive()
    try:
        return json.loads(rclone("about", "--json", drive_path(""), timeout=30))
    except (ApiError, ValueError):
        return {}


@route("GET", "/api/drive/download")
def api_drive_download(h):
    require_drive()
    rel = (h.query.get("path") or "").strip("/")
    if not rel:
        raise ApiError(400, "No file given")
    proc = subprocess.Popen(["rclone", "--config", RCLONE_CONF, "cat", drive_path(rel)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    first = proc.stdout.read(CHUNK)
    if not first and proc.wait() != 0:
        raise ApiError(502, "Could not read file from Drive")
    name = rel.rsplit("/", 1)[-1]
    h.send_response(200)
    h.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
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


@route("PUT", "/api/drive/put")
def api_drive_put(h):
    require_drive()
    folder = (h.query.get("dir") or "").strip("/")
    name = check_name(h.query.get("name"))
    length = int(h.headers.get("Content-Length") or -1)
    if length < 0:
        raise ApiError(411, "Content-Length required")
    dest = drive_path((folder + "/" + name).strip("/"))
    proc = subprocess.Popen(["rclone", "--config", RCLONE_CONF, "rcat", "--size", str(length), dest],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        remaining = length
        while remaining > 0:
            chunk = h.rfile.read(min(CHUNK, remaining))
            if not chunk:
                proc.kill()
                raise ApiError(400, "Upload interrupted")
            proc.stdin.write(chunk)
            remaining -= len(chunk)
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace").strip()
        if proc.wait() != 0:
            raise ApiError(502, err.splitlines()[-1] if err else "Drive upload failed")
    except BrokenPipeError:
        proc.wait()
        raise ApiError(502, "Drive upload failed")
    return {"ok": True}


@route("POST", "/api/drive/mkdir")
def api_drive_mkdir(h):
    require_drive()
    body = h.json()
    folder = (body.get("dir") or "").strip("/")
    rclone("mkdir", drive_path((folder + "/" + check_name(body.get("name"))).strip("/")))
    return {"ok": True}


@route("POST", "/api/drive/delete")
def api_drive_delete(h):
    require_drive()
    body = h.json()
    rel = (body.get("path") or "").strip("/")
    if not rel:
        raise ApiError(400, "Cannot delete Drive root")
    rclone("purge" if body.get("dir") else "deletefile", drive_path(rel), timeout=600)
    return {"ok": True}


@route("POST", "/api/drive/upload")
def api_drive_upload(h):
    """Server file/folder -> Drive (background job)."""
    require_drive()
    body = h.json()
    src = local_path(body.get("path"))
    if src == ROOT or not os.path.exists(src):
        raise ApiError(400, "Choose a file or folder")
    dest = (body.get("dest") or "").strip("/")
    name = os.path.basename(src)
    verb = "move" if body.get("move") else "copy"
    if os.path.isdir(src):
        args = [verb, src, drive_path((dest + "/" + name).strip("/"))]
    else:
        args = [verb, src, drive_path(dest)]
    return start_job("Server → Drive: %s" % name, args)


@route("POST", "/api/drive/fetch")
def api_drive_fetch(h):
    """Drive file/folder -> server (background job)."""
    require_drive()
    body = h.json()
    rel = (body.get("path") or "").strip("/")
    if not rel:
        raise ApiError(400, "Choose a file or folder")
    dest = local_path(body.get("dest"))
    name = rel.rsplit("/", 1)[-1]
    if body.get("dir"):
        args = ["copy", drive_path(rel), os.path.join(dest, name)]
    else:
        args = ["copy", drive_path(rel), dest]
    return start_job("Drive → Server: %s" % name, args)


@route("POST", "/api/drive/link")
def api_drive_link(h):
    """Google Drive share link -> server or Drive (background job)."""
    require_drive()
    body = h.json()
    url = (body.get("url") or "").strip()
    m = re.search(r"/folders/([\w-]{10,})", url)
    is_folder = bool(m)
    if not m:
        m = re.search(r"/d/([\w-]{10,})", url) or re.search(r"[?&]id=([\w-]{10,})", url)
    if not m:
        raise ApiError(400, "This is not a Google Drive link")
    fid = m.group(1)
    to_drive = body.get("target") == "drive"
    if to_drive:
        base = drive_path((body.get("dest") or "").strip("/"))
    else:
        base = local_path(body.get("dest"))
    if is_folder:
        dest = (base.rstrip("/") + "/drive-folder-" + fid[:8]) if to_drive else os.path.join(base, "drive-folder-" + fid[:8])
        args = ["copy", "--drive-root-folder-id", fid, drive_path(""), dest]
    else:
        args = ["backend", "copyid", drive_path(""), fid, base.rstrip("/") + "/"]
    return start_job("Drive link → %s: %s" % ("Drive" if to_drive else "Server", fid[:12]), args)


@route("POST", "/api/drive/copyurl")
def api_drive_copyurl(h):
    """Direct internet link -> straight into Drive (background job)."""
    require_drive()
    body = h.json()
    urls = [u.strip() for u in (body.get("urls") or []) if u.strip().startswith(("http://", "https://"))]
    if not urls:
        raise ApiError(400, "Only http/https links can go straight to Drive")
    dest = drive_path((body.get("dest") or "").strip("/")).rstrip("/") + "/"
    jobs = []
    for u in urls:
        jobs.append(start_job("Link → Drive: %s" % u[:80], ["copyurl", "--auto-filename", u, dest]))
    return {"jobs": jobs}


@route("GET", "/api/drive/status")
def api_drive_status(h):
    return {"connected": drive_connected()}


@route("POST", "/api/drive/auth/start")
def api_drive_auth_start(h):
    return {"url": auth_start()}


@route("POST", "/api/drive/auth/finish")
def api_drive_auth_finish(h):
    auth_finish(h.json().get("url"))
    return {"ok": True}


@route("POST", "/api/drive/token")
def api_drive_token(h):
    create_remote(parse_token(h.json().get("token")))
    return {"ok": True}


@route("POST", "/api/drive/disconnect")
def api_drive_disconnect(h):
    if drive_connected():
        rclone("config", "delete", REMOTE)
    return {"ok": True}


@route("GET", "/api/jobs")
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
        url = urllib.parse.urlsplit(self.path)
        self.query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if method == "GET" and url.path in ("/", "/index.html"):
                return self.serve_static("index.html")
            if method == "GET" and url.path.startswith("/d/"):
                return self.serve_share(url.path)
            entry = ROUTES.get((method, url.path))
            if not entry:
                raise ApiError(404, "Not found")
            fn, auth = entry
            if method != "GET" and self.headers.get("X-Panel") != "1":
                raise ApiError(403, "Missing X-Panel header")
            if auth and not self.authed():
                raise ApiError(401, "Login required")
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
            self.send_json({"error": "Server error: %s" % e}, 500)

    def authed(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "panel":
                exp, _, sig = value.partition(".")
                if exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, sign(exp)):
                    return True
        return False

    def json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 32 * 1024 * 1024:
            raise ApiError(413, "Request too large")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ApiError(400, "Invalid JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "Invalid JSON")
        return data

    def send_json(self, data, code=200):
        if self.sent:
            return
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

    def serve_static(self, name):
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.sent = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def serve_share(self, path):
        parts = path.split("/")
        if len(parts) < 3:
            raise ApiError(404, "Not found")
        payload, _, sig = parts[2].partition(".")
        if not hmac.compare_digest(sig, sign(payload)[:32]):
            raise ApiError(403, "Invalid link")
        data = json.loads(b64d(payload))
        if data["e"] < time.time():
            raise ApiError(410, "This link has expired")
        file_path = local_path(data["p"])
        if not os.path.isfile(file_path):
            raise ApiError(404, "File no longer exists")
        self.serve_file(file_path, inline=False)

    def serve_file(self, path, inline):
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
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
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
    threading.Thread(target=autoupload_loop, daemon=True).start()
    host = CFG.get("listen", "127.0.0.1")
    port = int(CFG.get("port", 8090))
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print("aria2 panel listening on %s:%d" % (host, port), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
