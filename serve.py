#!/usr/bin/env python3
"""Phone chat origin for Hermes.

GET and HEAD for the page and its files (the STATIC table) are served from
this directory. Every other request is streamed to the Hermes API on
127.0.0.1:8642, including the Authorization header and the response body.
Every request must carry an allowed Tailscale login. Listens on 127.0.0.1 only.
"""

import datetime
import hmac
import json
import os
import re
import signal
import socket
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
# `python -I serve.py ...` (install, update check) leaves the script's own
# folder off sys.path; faceid.py lives beside this file.
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import faceid  # noqa: E402
# Per-install settings (port, Hermes home, alerts). Local and gitignored;
# install.sh writes it. Every key is optional; see config.example.json.
CONFIG_PATH = os.path.join(ROOT, "config.json")


def load_settings(path=None):
    try:
        with open(path or CONFIG_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


SETTINGS = load_settings()
LISTEN = ("127.0.0.1", int(os.environ.get("PHONE_CHAT_PORT") or SETTINGS.get("port") or 8643))
UPSTREAM = ("127.0.0.1", int(os.environ.get("PHONE_CHAT_UPSTREAM_PORT") or SETTINGS.get("upstream_port") or 8642))
HERMES_HOME = os.path.expanduser(SETTINGS.get("hermes_home") or "~/.hermes")


def app_look(settings):
    """Name and accent color for this install, so two installs on one phone
    (home and work) can be told apart. Bad values fall back to the defaults."""
    name = settings.get("app_name")
    name = re.sub(r"[\x00-\x1f\x7f<>&\"']", "", name).strip()[:30] if isinstance(name, str) else ""
    color = settings.get("accent_color")
    color = color.strip().lower() if isinstance(color, str) else ""
    if not re.fullmatch(r"#[0-9a-f]{6}|#[0-9a-f]{3}", color):
        color = ""
    return name or "Hermes", color


APP_NAME, ACCENT = app_look(SETTINGS)
# One Tailscale login per line. Tailscale Serve sets Tailscale-User-Login on
# every tailnet request and strips any copy the client sent, so the header can
# be trusted here only because this proxy listens on 127.0.0.1. A missing or
# empty file refuses everything: this is a lock, and a lock fails closed.
ALLOW_PATH = os.path.join(ROOT, "allowed-logins.txt")
PID_PATH = os.path.join(ROOT, "serve.pid")
LOG_PATH = os.path.join(ROOT, "serve.log")
HEADER_LIMIT = 64 * 1024
BODY_CHUNK = 64 * 1024
# Idle gap longer than the API server's 10s SSE keepalive.
STREAM_TIMEOUT = 180
CONNECT_TIMEOUT = 5

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "te",
    "trailer",
    "upgrade",
}


def log(message):
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def send_all(sock, data):
    view = memoryview(data)
    sent = 0
    while sent < len(view):
        count = sock.send(view[sent:])
        if count <= 0:
            raise ConnectionError("socket closed while sending")
        sent += count


def response_bytes(status, reason, body, content_type, extra=()):
    payload = body if isinstance(body, bytes) else body.encode("utf-8")
    head = (
        "HTTP/1.1 %d %s\r\n"
        "Content-Type: %s\r\n"
        "Content-Length: %d\r\n"
        "Cache-Control: no-cache\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "%s"
        "Connection: close\r\n"
        "\r\n"
    ) % (status, reason, content_type, len(payload), "".join(h + "\r\n" for h in extra))
    return head.encode("ascii") + payload


def send_response(sock, status, reason, body, content_type="text/plain; charset=utf-8", extra=()):
    send_all(sock, response_bytes(status, reason, body, content_type, extra))


def read_headers(sock):
    """Return (header_bytes, leftover) or (None, None) on a closed socket."""
    buf = bytearray()
    sock.settimeout(20)
    while b"\r\n\r\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            raise ValueError("header timeout")
        if not chunk:
            return None, None
        buf += chunk
        if len(buf) > HEADER_LIMIT:
            raise ValueError("headers too large")
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    return head, rest


def split_request(head):
    lines = head.split(b"\r\n")
    if not lines or not lines[0]:
        raise ValueError("empty request")
    parts = lines[0].decode("iso-8859-1").split(" ")
    if len(parts) != 3:
        raise ValueError("bad request line")
    method, target, _version = parts
    return method.upper(), target, lines[1:]


def origin_target(target):
    if target.startswith("http://") or target.startswith("https://"):
        mark = target.find("//")
        slash = target.find("/", mark + 2)
        return target[slash:] if slash >= 0 else "/"
    if not target.startswith("/"):
        raise ValueError("unsupported request target")
    return target


def header_map(lines):
    """Parse header lines. Returns (fields, connection_names, content_length, chunked)."""
    fields = []
    connection_names = set()
    content_length = None
    chunked = False
    for line in lines:
        if not line:
            continue
        if b":" not in line:
            raise ValueError("bad header")
        name, value = line.split(b":", 1)
        lname = name.decode("iso-8859-1").strip().lower()
        if not lname:
            raise ValueError("bad header")
        if lname == "connection":
            for part in value.decode("iso-8859-1").split(","):
                item = part.strip().lower()
                if item:
                    connection_names.add(item)
            continue
        if lname == "content-length":
            try:
                content_length = int(value.strip() or b"0")
            except ValueError:
                raise ValueError("bad content-length")
            if content_length < 0:
                raise ValueError("bad content-length")
        if lname == "transfer-encoding" and b"chunked" in value.lower():
            chunked = True
        fields.append((lname, name, value))
    return fields, connection_names, content_length, chunked


def mounted_target(target):
    """Strip a /phone mount. Tailscale usually does this itself; do it here too."""
    raw = origin_target(target)
    path, sep, query = raw.partition("?")
    if path == "/phone" or path.startswith("/phone/"):
        path = path[len("/phone"):] or "/"
    return path + (("?" + query) if sep else "")


def page_request(method, target):
    path = mounted_target(target).split("?", 1)[0]
    return method in ("GET", "HEAD") and path in STATIC


# The page and its files, by exact path. Nothing else on disk is reachable.
STATIC = {
    "/": ("page/index.html", "text/html; charset=utf-8"),
    "/app.js": ("page/app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("page/app.css", "text/css; charset=utf-8"),
    "/vendor/marked.umd.js": ("page/vendor/marked.umd.js", "text/javascript; charset=utf-8"),
    "/vendor/purify.min.js": ("page/vendor/purify.min.js", "text/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("page/manifest.webmanifest", "application/manifest+json"),
    "/icon-180.png": ("page/icon-180.png", "image/png"),
    "/theme.css": (None, "text/css; charset=utf-8"),
}


def theme_css(color):
    """The accent from config.json as a stylesheet (inline styles are blocked)."""
    if not color:
        return b"/* default accent */\n"
    hexes = color[1:] if len(color) == 7 else "".join(c * 2 for c in color[1:])
    r, g, b = (int(hexes[i:i + 2], 16) / 255 for i in (0, 2, 4))
    light = 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.45
    return (":root { --primary: %s; --primary-fg: %s; }\n"
            # The app name in the list header, so two installs differ at the top.
            ".wordmark-btn { color: var(--primary); }\n"
            % (color, "#161616" if light else "#ffffff")).encode("ascii")


def page_body(path, raw, name, color):
    """Put this install's name into the page and manifest; iOS takes the Home
    Screen label from them when the icon is added."""
    if path == "/theme.css":
        return theme_css(color)
    if name == "Hermes":
        return raw
    if path == "/":
        text = raw.decode("utf-8")
        text = text.replace('content="Hermes"', 'content="%s"' % name)
        text = text.replace(">Hermes<", ">%s<" % name)
        return text.encode("utf-8")
    if path == "/manifest.webmanifest":
        data = json.loads(raw.decode("utf-8"))
        data["name"] = data["short_name"] = name
        return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return raw

# No inline script or style is allowed, so injected markup cannot run code.
# CSSOM writes from app.js (element.style) are not affected by style-src.
CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "manifest-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


def serve_page(client, method, target):
    path = mounted_target(target).split("?", 1)[0]
    name, content_type = STATIC[path]
    raw = b""
    if name:
        with open(os.path.join(ROOT, name), "rb") as handle:
            raw = handle.read()
    body = page_body(path, raw, APP_NAME, ACCENT)
    payload = b"" if method == "HEAD" else body
    head = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: %s\r\n"
        "Content-Length: %d\r\n"
        "Cache-Control: no-cache\r\n"
        "Content-Security-Policy: %s\r\n"
        "Referrer-Policy: no-referrer\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "Connection: close\r\n"
        "\r\n"
    ) % (content_type, len(body), CSP)
    send_all(client, head.encode("ascii"))
    if payload:
        send_all(client, payload)


def pump(src, dst, remaining):
    while remaining:
        chunk = src.recv(min(BODY_CHUNK, remaining))
        if not chunk:
            raise ConnectionError("truncated request body")
        send_all(dst, chunk)
        remaining -= len(chunk)


def pump_chunked(client, upstream, initial):
    """Forward a chunked request body, framing included, until the terminal chunk."""
    buf = bytearray(initial)

    def fill(need):
        while len(buf) < need:
            chunk = client.recv(BODY_CHUNK)
            if not chunk:
                raise ConnectionError("truncated chunked body")
            buf.extend(chunk)

    while True:
        while b"\r\n" not in buf:
            fill(len(buf) + 1)
        line, _, rest = bytes(buf).partition(b"\r\n")
        size_token = line.split(b";", 1)[0].strip()
        try:
            size = int(size_token, 16)
        except ValueError:
            raise ValueError("bad chunk size")
        buf = bytearray(rest)
        need = size + 2
        fill(need)
        chunk = bytes(buf[:need])
        if not chunk.endswith(b"\r\n"):
            raise ValueError("bad chunk framing")
        send_all(upstream, line + b"\r\n" + chunk)
        del buf[:need]
        if size == 0:
            # Trailers end at a blank line. An empty trailer block is one CRLF.
            while True:
                if buf.startswith(b"\r\n"):
                    send_all(upstream, b"\r\n")
                    return
                if b"\r\n\r\n" in buf:
                    end = buf.find(b"\r\n\r\n") + 4
                    send_all(upstream, bytes(buf[:end]))
                    return
                fill(len(buf) + 1)


def proxy(client, head, leftover, upstream_addr, api_key=None):
    """Forward one request to Hermes. With *api_key* (Face ID mode) any
    Authorization or Cookie the phone sent is dropped and the key is added here."""
    method, target, lines = split_request(head)
    target = mounted_target(target)
    fields, connection_names, content_length, chunked = header_map(lines)
    # Origin is the browser's page address. Hermes rejects any Origin that is
    # not on its CORS list, including this same-origin phone page. Drop it.
    skip = HOP_BY_HOP | connection_names | {
        "host", "transfer-encoding", "content-length", "origin"}
    if api_key is not None:
        skip = skip | {"authorization", "cookie"}
    out = [("%s %s HTTP/1.1" % (method, target)).encode("ascii")]
    for lname, name, value in fields:
        if lname in skip:
            continue
        out.append(name + b":" + value)
    if api_key is not None:
        out.append(b"Authorization: Bearer " + api_key.encode("ascii"))
    out.append(b"Host: %s:%d" % (upstream_addr[0].encode("ascii"), upstream_addr[1]))
    out.append(b"Connection: close")
    if chunked:
        out.append(b"Transfer-Encoding: chunked")
    elif content_length is not None:
        out.append(b"Content-Length: %d" % content_length)

    upstream = socket.create_connection(upstream_addr, timeout=CONNECT_TIMEOUT)
    forwarded = False
    try:
        upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        upstream.settimeout(STREAM_TIMEOUT)
        send_all(upstream, b"\r\n".join(out) + b"\r\n\r\n")
        if chunked:
            pump_chunked(client, upstream, leftover)
        elif content_length:
            if leftover:
                take = leftover[:content_length]
                send_all(upstream, take)
                content_length -= len(take)
            if content_length:
                client.settimeout(STREAM_TIMEOUT)
                pump(client, upstream, content_length)
        client.settimeout(None)
        while True:
            try:
                chunk = upstream.recv(BODY_CHUNK)
            except socket.timeout:
                raise ConnectionError("upstream stalled")
            if not chunk:
                break
            forwarded = True
            send_all(client, chunk)
    except Exception:
        if not forwarded:
            raise
        log("client went away during response")
    finally:
        try:
            upstream.close()
        except Exception:
            pass


def allowed_logins():
    """Read per request so an edit takes effect without a restart."""
    try:
        with open(ALLOW_PATH, encoding="utf-8") as handle:
            return {
                line.strip().lower()
                for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            }
    except OSError:
        return set()


def request_login(lines):
    """Return the Tailscale login, or "" if absent or sent more than once."""
    found = []
    for line in lines:
        name, sep, value = line.partition(b":")
        if sep and name.strip().lower() == b"tailscale-user-login":
            found.append(value.strip().decode("iso-8859-1").lower())
    return found[0] if len(found) == 1 else ""


# --- Busy check -------------------------------------------------------------
# A reply started anywhere (desktop, CLI, this page) holds a turn lease in
# Hermes's state.db until it ends. The Hermes API does not expose it, so the
# page asks here, read-only, to know when to refresh a session it did not start.

BUSY_PATH = "/phone-api/busy"
SESSION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
STATE_DB = os.path.join(HERMES_HOME, "state.db")


def lease_root(conn, session_id):
    """Walk compression parents to the lease key, as Hermes does."""
    current = conn.execute(
        "SELECT id, parent_session_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    seen = {session_id}
    root = session_id
    while current and current[1] and current[1] not in seen:
        parent = conn.execute(
            "SELECT id, parent_session_id, end_reason FROM sessions WHERE id = ?", (current[1],)).fetchone()
        if not parent or parent[2] != "compression":
            break
        seen.add(parent[0])
        root = parent[0]
        current = parent
    return root


def holder_alive(holder):
    match = re.match(r"pid=(\d+):", holder or "")
    if not match:
        return True
    try:
        os.kill(int(match.group(1)), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


# --- Profiles -----------------------------------------------------------------
# Hermes serves every profile on the default listener under /p/<profile>/, and
# each profile checks its own API_SERVER_KEY. A profile is offered to the page
# when its .env has a usable key. Requests for it go out with that key, added
# here: in Face ID mode always, in API-key mode once the page's key (the
# default profile's) has been checked. The keys never reach the phone.

PROFILES_PATH = "/phone-api/profiles"
META_PATH = "/phone-api/meta"
PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PROFILE_PATH = re.compile(r"^/p/([^/]+)(/.*)?$")
MIN_KEY = 16


def profile_home(name):
    if name in (None, "", "default"):
        return HERMES_HOME
    if not PROFILE_NAME.match(name):
        return None
    return os.path.join(HERMES_HOME, "profiles", name)


def profile_key(name):
    if name in (None, "", "default"):
        return server_key()
    home = profile_home(name)
    if not home:
        return ""
    key = read_env(os.path.join(home, ".env"), ("API_SERVER_KEY",)).get("API_SERVER_KEY", "")
    return key if len(key) >= MIN_KEY and all(32 < ord(ch) < 127 for ch in key) else ""


def served_profiles():
    """"default" first, then each profile with its own usable API key, by name."""
    out = ["default"]
    try:
        names = sorted(os.listdir(os.path.join(HERMES_HOME, "profiles")))
    except OSError:
        names = []
    for name in names:
        if name != "default" and PROFILE_NAME.match(name) and profile_key(name):
            out.append(name)
    return out


def split_profile(local):
    """'/p/<name>/rest' -> (name, '/rest'); any other path -> ('default', path).
    An unknown or keyless profile -> (None, path)."""
    match = PROFILE_PATH.match(local)
    if not match:
        return "default", local
    name = match.group(1)
    if name == "default" or name not in served_profiles():
        return None, local
    return name, match.group(2) or "/"


def profile_file(name, filename, default_path):
    """A per-profile file. The default profile keeps the module paths (tests point them elsewhere)."""
    home = profile_home(name)
    if name in (None, "", "default") or not home:
        return default_path
    return os.path.join(home, filename)


def ro_connect(path):
    """Read-only. A WAL database whose -shm this user cannot map falls back to immutable."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(path), uri=True, timeout=2)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn
    except sqlite3.Error:
        return sqlite3.connect("file:%s?immutable=1" % urllib.parse.quote(path), uri=True, timeout=2)


def norm_path(path):
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path).strip()))).rstrip("/\\")


def project_folders(projects_db):
    """[(folder, name)] for live projects, longest folder first, as Hermes resolves them."""
    if not os.path.exists(projects_db):
        return []
    conn = ro_connect(projects_db)
    try:
        rows = conn.execute(
            "SELECT pf.path, p.name FROM project_folders pf JOIN projects p ON p.id = pf.project_id "
            "WHERE p.archived = 0").fetchall()
    finally:
        conn.close()
    out = [(norm_path(folder), name) for folder, name in rows if folder and name]
    out.sort(key=lambda item: len(item[0]), reverse=True)
    return out


def project_for(folders, cwd, repo_root):
    for path in (cwd, repo_root):
        if not path or not str(path).strip():
            continue
        target = norm_path(path)
        for folder, name in folders:
            if target == folder or target.startswith(folder + os.sep):
                return name
    return ""


ARCHIVED_LIMIT = 100


def session_meta(profile, ids, archived=False):
    """Project names for *ids*, and (with *archived*) the profile's archived
    sessions in the shape /api/sessions uses. Hermes's list API has neither."""
    folders = project_folders(profile_file(profile, "projects.db", os.path.join(HERMES_HOME, "projects.db")))
    conn = ro_connect(profile_file(profile, "state.db", STATE_DB))
    meta, rows = {}, []
    try:
        ids = [i for i in ids if SESSION_ID.match(i)][:200]
        if ids:
            marks = ",".join("?" * len(ids))
            for sid, cwd, root in conn.execute(
                    "SELECT id, cwd, git_repo_root FROM sessions WHERE id IN (%s)" % marks, ids):
                meta[sid] = {"project": project_for(folders, cwd, root)}
        if archived:
            # Roots only: a compression continuation is shown through its root.
            for row in conn.execute(
                    "SELECT s.id, s.title, s.source, s.started_at, "
                    "COALESCE(s.last_activity_at, s.ended_at, s.started_at), s.message_count, "
                    "s.input_tokens, s.output_tokens, s.estimated_cost_usd, s.actual_cost_usd, s.cwd, "
                    "s.git_repo_root, (SELECT substr(m.content, 1, 120) FROM messages m "
                    "WHERE m.session_id = s.id AND m.role = 'user' ORDER BY m.id LIMIT 1) "
                    "FROM sessions s LEFT JOIN sessions p ON p.id = s.parent_session_id "
                    "WHERE s.archived = 1 AND COALESCE(s.hidden, 0) = 0 "
                    "AND (s.parent_session_id IS NULL OR COALESCE(p.end_reason, '') != 'compression') "
                    "ORDER BY 5 DESC LIMIT ?", (ARCHIVED_LIMIT,)):
                project = project_for(folders, row[10], row[11])
                rows.append({
                    "id": row[0], "title": row[1], "source": row[2], "started_at": row[3],
                    "last_active": row[4], "message_count": row[5], "input_tokens": row[6] or 0,
                    "output_tokens": row[7] or 0, "estimated_cost_usd": row[8], "actual_cost_usd": row[9],
                    "preview": row[12] if isinstance(row[12], str) else "", "archived": True,
                    "pinned": False})
                meta[row[0]] = {"project": project}
    finally:
        conn.close()
    return {"meta": meta, "archived": rows if archived else None}


def session_busy(session_id, db_path=None):
    uri = "file:%s?mode=ro" % urllib.parse.quote(db_path or STATE_DB)
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        root = lease_root(conn, session_id)
        row = conn.execute(
            "SELECT holder, acquired_at, expires_at FROM session_turn_leases "
            "WHERE conversation_id = ? AND expires_at > ?", (root, time.time())).fetchone()
    finally:
        conn.close()
    if not row or not holder_alive(row[0]):
        return {"busy": False}
    platform = re.search(r"platform=([a-z_]+)", row[0] or "")
    # Only the platform leaves this process; the holder string has a pid and turn ids.
    return {"busy": True, "since": row[1], "platform": platform.group(1) if platform else ""}


ACTIVE_PATH = "/phone-api/active"
RUNS_DB = os.path.join(HERMES_HOME, "runs_idempotency.db")


def lineage_ids(conn, root):
    """The lease's root plus every compression continuation of it, so a list
    row holding any tip of the conversation gets the same status."""
    ids, frontier = [root], [root]
    while frontier and len(ids) < 64:
        nxt = []
        for sid in frontier:
            ended = conn.execute("SELECT end_reason FROM sessions WHERE id = ?", (sid,)).fetchone()
            if not ended or ended[0] != "compression":
                continue
            for (child,) in conn.execute("SELECT id FROM sessions WHERE parent_session_id = ?", (sid,)):
                if child not in ids:
                    ids.append(child)
                    nxt.append(child)
        frontier = nxt
    return ids


def waiting_sessions(runs_db):
    """Session ids of phone runs waiting for an approval. Only runs started by
    this page are in this store; desktop approvals are not visible here."""
    if not os.path.exists(runs_db):
        return set()
    out = set()
    for status in read_runs(runs_db).values():
        if status.get("status") == "waiting_for_approval" and status.get("session_id"):
            out.add(str(status["session_id"]))
    return out


def active_sessions(db_path=None, runs_db=None, approvals=None):
    """Every session with a live turn: {id: {state, since, platform}}.
    state is "approval" for a run waiting on the owner (a phone run, or a desktop
    approval the phone-approvals plugin reported), else "working".
    Each item of *approvals* gains "sessions": every id of its lineage."""
    uri = "file:%s?mode=ro" % urllib.parse.quote(db_path or STATE_DB)
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        leases = conn.execute(
            "SELECT conversation_id, holder, acquired_at FROM session_turn_leases WHERE expires_at > ?",
            (time.time(),)).fetchall()
        lineages = [(lineage_ids(conn, root), holder, since) for root, holder, since in leases
                    if holder_alive(holder)]
    finally:
        conn.close()
    try:
        waiting = waiting_sessions(runs_db or RUNS_DB)
    except sqlite3.Error:
        waiting = set()
    for item in approvals or ():
        key = item["session_key"]
        item["sessions"] = next((ids for ids, _h, _s in lineages if key in ids), [key])
        waiting.add(key)
    active = {}
    for ids, holder, since in lineages:
        platform = re.search(r"platform=([a-z_]+)", holder or "")
        # A run's status names the session it was started in; any tip of the lineage counts.
        state = "approval" if waiting.intersection(ids) else "working"
        entry = {"state": state, "since": since, "platform": platform.group(1) if platform else ""}
        for sid in ids:
            active[sid] = entry
    return active


# --- Desktop approvals ---------------------------------------------------------
# The phone-approvals Hermes plugin (plugin/phone-approvals) runs inside the
# desktop app's backend. It writes one marker per open approval prompt to
# pending/ and applies an answer this proxy writes to decisions/. Only "once"
# and "deny" are accepted from the phone; the plugin enforces that too.

APPROVALS_DIR = os.path.join(HERMES_HOME, "phone-approvals")
DECIDE_PATH = "/phone-api/approvals/decide"
PHONE_CHOICES = ("once", "deny")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
BODY_LIMIT = 4096


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (OSError, ValueError, TypeError):
        return True
    return True


def desktop_approvals(base=None):
    """Open approval prompts reported by the plugin, oldest first. A marker from
    a process that has exited is skipped (the plugin sweeps it later)."""
    pending = os.path.join(base or APPROVALS_DIR, "pending")
    try:
        names = os.listdir(pending)
    except OSError:
        return []
    out = []
    for name in names:
        rid = name[:-5] if name.endswith(".json") else ""
        if not REQUEST_ID.match(rid):
            continue
        try:
            with open(os.path.join(pending, name), encoding="utf-8") as handle:
                marker = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(marker, dict) or marker.get("request_id") != rid:
            continue
        if not SESSION_ID.match(str(marker.get("session_key") or "")) or not pid_alive(marker.get("pid")):
            continue
        out.append({
            "request_id": rid,
            "session_key": marker["session_key"],
            "created": marker.get("created"),
            "source": str(marker.get("source") or ""),
            "command": str(marker.get("command") or ""),
            "description": str(marker.get("description") or ""),
            "smart_denied": bool(marker.get("smart_denied")),
            "choices": list(PHONE_CHOICES),
        })
    out.sort(key=lambda item: item.get("created") or 0)
    return out


def record_decision(request_id, choice, base=None):
    """Write the phone's answer for the plugin. Returns an HTTP status:
    202 written, 400 bad input, 404 not pending (answered, timed out, unknown)."""
    if not REQUEST_ID.match(str(request_id or "")) or choice not in PHONE_CHOICES:
        return 400
    root = base or APPROVALS_DIR
    if not any(item["request_id"] == request_id for item in desktop_approvals(root)):
        return 404
    decisions = os.path.join(root, "decisions")
    path = os.path.join(decisions, request_id + ".json")
    tmp = "%s.tmp%d" % (path, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"choice": choice, "at": time.time()}, handle)
        os.replace(tmp, path)
    except OSError:
        return 404
    return 202


def approval_base(request_id):
    """The approvals folder (of whichever profile) where *request_id* is pending."""
    for name in served_profiles():
        base = profile_file(name, "phone-approvals", APPROVALS_DIR)
        if any(item["request_id"] == request_id for item in desktop_approvals(base)):
            return base
    return APPROVALS_DIR


def read_body(client, lines, leftover):
    """A small request body (JSON). Raises ValueError when absent or too big."""
    _fields, _names, length, chunked = header_map(lines)
    if chunked or length is None or length > BODY_LIMIT:
        raise ValueError("body length")
    body = bytearray(leftover or b"")
    client.settimeout(10)
    while len(body) < length:
        chunk = client.recv(length - len(body))
        if not chunk:
            raise ValueError("short body")
        body += chunk
    return bytes(body[:length])


def json_error(client, status, reason, code, message):
    send_response(client, status, reason, json.dumps({"error": {"code": code, "message": message}}),
                  "application/json")


def serve_decide(client, lines, leftover):
    if key_refused(client, lines):
        return
    try:
        data = json.loads(read_body(client, lines, leftover).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        json_error(client, 400, "Bad Request", "bad_request", "Send JSON with request_id and choice")
        return
    if not isinstance(data, dict):
        data = {}
    choice = data.get("choice")
    if choice == "once" and faceid.enrolled():
        try:
            faceid.verify_assertion(data.get("assertion"), "approve",
                                    faceid.binding_for_approval("desktop", "", data.get("request_id")))
        except faceid.AuthError as exc:
            log("desktop approval refused: %s" % exc)
            json_error(client, 403, "Forbidden", "faceid_required", "Face ID check failed. Try again.")
            return
    status = record_decision(data.get("request_id"), choice, approval_base(data.get("request_id")))
    if status == 202:
        log("approval answered from the phone: %s" % choice)
        send_response(client, 202, "Accepted", '{"ok":true}', "application/json")
    elif status == 404:
        json_error(client, 409, "Conflict", "approval_not_pending", "That approval is no longer waiting")
    else:
        json_error(client, 400, "Bad Request", "bad_request", "Only Allow once or Deny can be sent from the phone")


def bearer_ok(lines):
    """The same API key the Hermes API checks, compared in constant time."""
    sent = ""
    for line in lines:
        name, sep, value = line.partition(b":")
        if sep and name.strip().lower() == b"authorization":
            sent = value.strip().decode("iso-8859-1")
    expected = read_env(os.path.join(HERMES_HOME, ".env"), ("API_SERVER_KEY",)).get("API_SERVER_KEY", "")
    if not expected:
        return None
    return hmac.compare_digest(sent.encode("utf-8"), ("Bearer " + expected).encode("utf-8"))


def key_refused(client, lines):
    """Send 503/401 and return True unless the request may proceed: with a
    passkey enrolled that means an unlocked Face ID session (the API key is
    ignored), otherwise the API key."""
    if faceid.enrolled():
        if faceid.session_valid(faceid.cookie_token(lines)):
            return False
        locked(client)
        return True
    auth = bearer_ok(lines)
    if auth is None:
        send_response(client, 503, "Service Unavailable", '{"error":{"message":"No API key configured"}}',
                      "application/json")
        return True
    if not auth:
        send_response(client, 401, "Unauthorized", '{"error":{"message":"Invalid API key"}}', "application/json")
        return True
    return False


def all_active():
    """active_sessions and desktop approvals for every served profile. Each
    entry and approval names its profile. One unreadable profile is skipped."""
    active, approvals, failed = {}, [], False
    for name in served_profiles():
        items = desktop_approvals(profile_file(name, "phone-approvals", APPROVALS_DIR))
        for item in items:
            item["profile"] = name
        try:
            part = active_sessions(profile_file(name, "state.db", STATE_DB),
                                   profile_file(name, "runs_idempotency.db", RUNS_DB), items)
        except sqlite3.Error as exc:
            log("active check (%s): %s" % (name, type(exc).__name__))
            failed = failed or name == "default"
            part = {}
        for sid, entry in part.items():
            active[sid] = dict(entry, profile=name)
        approvals.extend(items)
    approvals.sort(key=lambda item: item.get("created") or 0)
    return (None if failed and not active else active), approvals


def serve_active(client, lines):
    if key_refused(client, lines):
        return
    note_page()
    active, approvals = all_active()
    send_response(client, 200, "OK", json.dumps({"active": active, "approvals": approvals}), "application/json")


def query_of(target):
    return urllib.parse.parse_qs(mounted_target(target).partition("?")[2])


def query_profile(client, query):
    """The ?profile= of a local endpoint, checked. Sends 404 and returns None if unknown."""
    name = (query.get("profile") or ["default"])[0] or "default"
    if name not in served_profiles():
        json_error(client, 404, "Not Found", "unknown_profile", "Unknown profile")
        return None
    return name


def serve_profiles(client, lines):
    if key_refused(client, lines):
        return
    send_response(client, 200, "OK", json.dumps({"profiles": served_profiles()}), "application/json")


def serve_meta(client, target, lines):
    if key_refused(client, lines):
        return
    query = query_of(target)
    profile = query_profile(client, query)
    if profile is None:
        return
    ids = [i for i in ",".join(query.get("ids") or []).split(",") if i]
    try:
        body = json.dumps(session_meta(profile, ids, (query.get("archived") or [""])[0] == "1"))
    except sqlite3.Error as exc:
        log("meta (%s): %s" % (profile, type(exc).__name__))
        body = json.dumps({"meta": {}, "archived": None})
    send_response(client, 200, "OK", body, "application/json")


def serve_busy(client, target, lines):
    if key_refused(client, lines):
        return
    query = query_of(target)
    session_id = (query.get("session") or [""])[0]
    if not SESSION_ID.match(session_id):
        send_response(client, 400, "Bad Request", '{"error":{"message":"Bad session id"}}', "application/json")
        return
    profile = query_profile(client, query)
    if profile is None:
        return
    try:
        body = json.dumps(session_busy(session_id, profile_file(profile, "state.db", STATE_DB)))
    except sqlite3.Error as exc:
        log("busy check: %s" % type(exc).__name__)
        body = json.dumps({"busy": None})
    send_response(client, 200, "OK", body, "application/json")


# --- Cron jobs (read-only) ----------------------------------------------------
# Hermes's API server has no cron routes (only the dashboard does, behind its
# own login), so the proxy reads each served profile's cron files directly:
# cron/jobs.json, the run log cron/executions.db, and the saved output
# cron/output/<job>/<YYYY-mm-dd_HH-MM-SS>.md. Nothing here writes or triggers.
CRON_PATH = "/phone-api/cron"
CRON_RUNS_PATH = "/phone-api/cron/runs"
CRON_OUTPUT_PATH = "/phone-api/cron/output"
JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
OUTPUT_NAME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})\.md$")
RUN_SESSION = re.compile(r"_(\d{8})_(\d{6})$")
OUTPUT_MAX = 400 * 1024
CRON_RUNS_MAX = 30
JOB_FIELDS = ("id", "name", "enabled", "state", "paused_at", "paused_reason", "next_run_at", "last_run_at",
              "last_status", "deliver", "no_agent", "skills", "script", "failure_streak", "created_at", "workdir")


def cron_dir(profile):
    home = profile_home(profile)
    return os.path.join(home, "cron") if home else None


def clip(value, limit):
    return value[:limit] if isinstance(value, str) else None


def cron_jobs(profile):
    """The profile's jobs, without anything the list does not need."""
    try:
        with open(os.path.join(cron_dir(profile), "jobs.json"), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    out = []
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict) or not JOB_ID.match(str(job.get("id") or "")):
            continue
        row = {k: job.get(k) for k in JOB_FIELDS}
        schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        row["schedule"] = clip(job.get("schedule_display") or schedule.get("display") or schedule.get("expr"), 120)
        row["schedule_kind"] = clip(schedule.get("kind"), 20)
        row["last_error"] = clip(job.get("last_error"), 600)
        row["last_delivery_error"] = clip(job.get("last_delivery_error"), 600)
        row["prompt"] = clip(job.get("prompt"), 4000)
        repeat = job.get("repeat") if isinstance(job.get("repeat"), dict) else {}
        row["runs_completed"] = repeat.get("completed")
        row["profile"] = profile
        out.append(row)
    return out


def local_stamp(*parts):
    """Local wall-clock parts (as Hermes names its files) -> epoch seconds."""
    try:
        return time.mktime(tuple(int(p) for p in parts) + (0, 0, -1))
    except (ValueError, OverflowError):
        return None


def iso_stamp(text):
    try:
        return datetime.datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError):
        return None


def cron_runs(profile, job_id, limit=CRON_RUNS_MAX):
    """Newest first. Each run joins its run-log row, its output file and, for
    agent jobs, its transcript session, matched by time; any part may be missing."""
    base = cron_dir(profile)
    execs = []
    db = os.path.join(base, "executions.db")
    if os.path.exists(db):
        conn = ro_connect(db)
        try:
            for row in conn.execute(
                    "SELECT status, claimed_at, started_at, finished_at, error, delivery_outcome FROM executions "
                    "WHERE job_id = ? ORDER BY claimed_at DESC LIMIT ?", (job_id, limit)):
                execs.append({"status": row[0], "started": iso_stamp(row[2] or row[1]),
                              "finished": iso_stamp(row[3]), "error": clip(row[4], 600), "delivery": row[5]})
        finally:
            conn.close()
    outputs = []
    try:
        names = os.listdir(os.path.join(base, "output", job_id))
    except OSError:
        names = []
    for name in names:
        match = OUTPUT_NAME.match(name)
        if match:
            outputs.append((local_stamp(*match.groups()), name))
    sessions = []
    state_db = profile_file(profile, "state.db", STATE_DB)
    if os.path.exists(state_db):
        conn = ro_connect(state_db)
        try:
            prefix = "cron_%s_" % job_id
            for (sid,) in conn.execute("SELECT id FROM sessions WHERE source = 'cron' AND id >= ? AND id < ? "
                                       "ORDER BY id DESC LIMIT ?", (prefix, prefix + "\uffff", limit)):
                match = RUN_SESSION.search(sid)
                if match:
                    d, t = match.groups()
                    sessions.append((local_stamp(d[:4], d[4:6], d[6:], t[:2], t[2:4], t[4:]), sid))
        finally:
            conn.close()

    def take(pool, lo, hi):
        best = None
        for item in pool:
            if item[0] is not None and lo <= item[0] <= hi and (best is None or item[0] < best[0]):
                best = item
        if best:
            pool.remove(best)
        return best

    runs = []
    for ex in execs:
        start = ex["started"] or 0
        end = ex["finished"] or start
        out = take(outputs, start - 5, end + 120)
        ses = take(sessions, start - 60, end + 60)
        ex.update({"output": out[1] if out else None, "session": ses[1] if ses else None, "at": start})
        runs.append(ex)
    # Output kept from before the run log existed, or a run the log missed.
    for stamp, name in outputs:
        ses = take(sessions, stamp - 3600, stamp + 5)
        runs.append({"status": None, "started": None, "finished": stamp, "error": None, "delivery": None,
                     "output": name, "session": ses[1] if ses else None, "at": stamp})
    for stamp, sid in sessions:
        runs.append({"status": None, "started": stamp, "finished": None, "error": None, "delivery": None,
                     "output": None, "session": sid, "at": stamp})
    runs.sort(key=lambda r: r["at"] or 0, reverse=True)
    return runs[:limit]


def cron_output(profile, job_id, name):
    """(text, truncated) or None. Only a file named like Hermes's own output."""
    if not JOB_ID.match(job_id or "") or not OUTPUT_NAME.match(name or ""):
        return None
    folder = os.path.realpath(os.path.join(cron_dir(profile), "output", job_id))
    path = os.path.realpath(os.path.join(folder, name))
    if os.path.dirname(path) != folder:
        return None
    try:
        with open(path, "rb") as handle:
            raw = handle.read(OUTPUT_MAX + 1)
    except OSError:
        return None
    return raw[:OUTPUT_MAX].decode("utf-8", "replace"), len(raw) > OUTPUT_MAX


def cron_profiles(client, query):
    name = (query.get("profile") or ["default"])[0] or "default"
    if name == "all":
        return served_profiles()
    profile = query_profile(client, query)
    return None if profile is None else [profile]


def serve_cron(client, target, lines, local):
    if key_refused(client, lines):
        return
    query = query_of(target)
    try:
        if local == CRON_PATH:
            profiles = cron_profiles(client, query)
            if profiles is None:
                return
            jobs = []
            for profile in profiles:
                jobs.extend(cron_jobs(profile))
            body = {"jobs": jobs}
        else:
            profile = query_profile(client, query)
            if profile is None:
                return
            job_id = (query.get("job") or [""])[0]
            if not JOB_ID.match(job_id):
                json_error(client, 400, "Bad Request", "bad_job", "Bad job id")
                return
            if local == CRON_RUNS_PATH:
                body = {"runs": cron_runs(profile, job_id)}
            else:
                found = cron_output(profile, job_id, (query.get("file") or [""])[0])
                if found is None:
                    json_error(client, 404, "Not Found", "no_output", "No such output")
                    return
                body = {"text": found[0], "truncated": found[1]}
    except sqlite3.Error as exc:
        log("cron read: %s" % type(exc).__name__)
        json_error(client, 503, "Service Unavailable", "cron_unreadable", "Could not read the cron files")
        return
    send_response(client, 200, "OK", json.dumps(body), "application/json")


# --- Face ID (passkey) ---------------------------------------------------------
# faceid.py holds the checks. Before a passkey is enrolled nothing here changes
# how the page works. After, every API request needs the unlock cookie, the
# key is added on this side, and Allow once needs a Face ID signature bound to
# that one approval. Deny never needs Face ID: refusing is always safe.

AUTH_PREFIX = "/phone-api/auth/"
# Answered here, not by Hermes: no key goes out for them.
LOCAL_API = (BUSY_PATH, ACTIVE_PATH, DECIDE_PATH, PROFILES_PATH, META_PATH,
             CRON_PATH, CRON_RUNS_PATH, CRON_OUTPUT_PATH)
# Polls that must not keep a Face ID session awake.
BACKGROUND = (BUSY_PATH, ACTIVE_PATH, PROFILES_PATH, META_PATH)
RUN_APPROVAL = re.compile(r"^/v1/runs/([A-Za-z0-9_.:-]{1,128})/approval$")


def server_key():
    key = read_env(os.path.join(HERMES_HOME, ".env"), ("API_SERVER_KEY",)).get("API_SERVER_KEY", "")
    return key if key and all(32 < ord(ch) < 127 for ch in key) else ""


def origin_ok(lines):
    config = faceid.load_config()
    sent = [line.partition(b":")[2].strip().decode("iso-8859-1") for line in lines
            if line.partition(b":")[0].strip().lower() == b"origin"]
    return bool(config) and len(sent) == 1 and sent[0] == config["origin"]


def locked(client):
    json_error(client, 401, "Unauthorized", "locked", "Locked. Unlock with Face ID.")


def json_ok(client, data, cookies=()):
    send_response(client, 200, "OK", json.dumps(data), "application/json",
                  ["Set-Cookie: " + c for c in cookies])


def serve_auth(client, method, local, lines, leftover):
    name = local[len(AUTH_PREFIX):]
    token = faceid.cookie_token(lines)
    if name == "status" and method == "GET":
        config = faceid.load_config()
        json_ok(client, {"configured": config is not None,
                         "enrolled": bool(config and config["credentials"]),
                         "setup_open": faceid.setup_open(),
                         "unlocked": faceid.session_valid(token)})
        return
    if method != "POST":
        json_error(client, 405, "Method Not Allowed", "bad_method", "Use POST")
        return
    try:
        raw = read_body(client, lines, leftover) if name != "logout" else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        json_error(client, 400, "Bad Request", "bad_request", "Send JSON")
        return
    if not isinstance(data, dict):
        data = {}
    try:
        if name == "register/options":
            if not faceid.check_setup_code(data.get("code")):
                log("face id setup: wrong or expired code")
                json_error(client, 403, "Forbidden", "bad_code", "That setup code is wrong or expired.")
                return
            json_ok(client, faceid.registration_options())
        elif name == "register":
            faceid.finish_registration(data.get("credential"))
            log("face id: passkey enrolled")
            json_ok(client, {"ok": True}, [faceid.session_cookie(faceid.new_session())])
        elif name == "login/options":
            json_ok(client, faceid.assertion_options("login", "login"))
        elif name == "login":
            faceid.verify_assertion(data.get("credential"), "login", "login")
            log("face id: unlocked")
            faceid.drop_session(token)
            json_ok(client, {"ok": True}, [faceid.session_cookie(faceid.new_session())])
        elif name == "approve/options":
            if not faceid.session_valid(token, touch=True):
                locked(client)
                return
            kind = data.get("kind")
            if kind not in ("desktop", "run"):
                raise faceid.AuthError("bad kind")
            json_ok(client, faceid.assertion_options(
                "approve", faceid.binding_for_approval(kind, data.get("run_id"), data.get("request_id"))))
        elif name == "logout":
            faceid.drop_session(token)
            json_ok(client, {"ok": True}, [faceid.clear_cookie()])
        else:
            json_error(client, 404, "Not Found", "not_found", "Unknown")
    except faceid.AuthError as exc:
        log("face id %s refused: %s" % (name, exc))
        json_error(client, 403, "Forbidden", "faceid_failed", "Face ID check failed.")
    except ImportError:
        log("face id: cryptography package missing")
        json_error(client, 503, "Service Unavailable", "faceid_unavailable", "Face ID check is not installed.")


def serve_run_approval(client, head, local, lines, leftover, upstream_addr, api_key):
    """A phone run's approval in Face ID mode: Allow once or Deny only, one
    request, and Allow once must carry a Face ID signature for that request."""
    run_id = RUN_APPROVAL.match(local).group(1)
    try:
        data = json.loads(read_body(client, lines, leftover).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        json_error(client, 400, "Bad Request", "bad_request", "Send JSON with choice")
        return
    if not isinstance(data, dict):
        data = {}
    choice = data.get("choice")
    request_id = data.get("request_id") if isinstance(data.get("request_id"), str) else ""
    if choice not in PHONE_CHOICES:
        json_error(client, 400, "Bad Request", "bad_request", "Only Allow once or Deny can be sent from the phone")
        return
    if choice == "once":
        try:
            faceid.verify_assertion(data.get("assertion"), "approve",
                                    faceid.binding_for_approval("run", run_id, request_id))
        except faceid.AuthError as exc:
            log("run approval refused: %s" % exc)
            json_error(client, 403, "Forbidden", "faceid_required", "Face ID check failed. Try again.")
            return
    body = json.dumps({"choice": choice, "request_id": request_id} if request_id else {"choice": choice}).encode()
    method, target, _lines = split_request(head)
    out = [("%s %s HTTP/1.1" % (method, target)).encode("ascii"), b"Content-Type: application/json",
           b"Content-Length: %d" % len(body)]
    for line in lines:
        name = line.partition(b":")[0].strip().lower()
        if name in (b"accept", b"idempotency-key"):
            out.append(line)
    proxy(client, b"\r\n".join(out) + b"\r\n\r\n", body, upstream_addr, api_key)


def handle_client(client, upstream_addr=UPSTREAM):
    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        head, leftover = read_headers(client)
        if head is None:
            return
        method, target, lines = split_request(head)
        path = origin_target(target).split("?", 1)[0]
        login = request_login(lines)
        if not login or login not in allowed_logins():
            # The login itself is not logged.
            log("%s %s refused, %s" % (method, path, "wrong login" if login else "no login"))
            send_response(client, 403, "Forbidden", "This page is limited to its owner's Tailscale login.\n")
            return
        local = mounted_target(target).split("?", 1)[0]
        if method not in ("GET", "HEAD") and faceid.enrolled() and not origin_ok(lines):
            # Cookies are per host, not per port: the dashboard on :443 is
            # "same-site" with this page. Only this page may change anything.
            log("%s %s refused, wrong origin" % (method, path))
            json_error(client, 403, "Forbidden", "bad_origin", "Wrong origin")
            return
        if local.startswith(AUTH_PREFIX):
            serve_auth(client, method, local, lines, leftover)
            return
        # /p/<profile>/... goes to that profile on the same Hermes listener,
        # with that profile's key. "rest" is the path inside the profile.
        profile, rest = split_profile(local)
        if profile is None:
            log("%s %s refused, unknown profile" % (method, path))
            json_error(client, 404, "Not Found", "unknown_profile", "Unknown profile")
            return
        api_key = None
        local_api = local in LOCAL_API
        if faceid.enrolled() and not page_request(method, target):
            token = faceid.cookie_token(lines)
            # Background polls do not count as use, so an idle page still locks.
            background = local in BACKGROUND or (method == "GET" and rest.startswith("/v1/runs/"))
            if not faceid.session_valid(token, touch=not background):
                locked(client)
                return
            if not local_api:
                api_key = profile_key(profile)
                if not api_key:
                    send_response(client, 503, "Service Unavailable",
                                  '{"error":{"message":"No API key configured"}}', "application/json")
                    return
                if method == "POST" and RUN_APPROVAL.match(rest):
                    serve_run_approval(client, head, rest, lines, leftover, upstream_addr, api_key)
                    return
        elif profile != "default" and not local_api and not page_request(method, target):
            # API-key mode: the page holds the default profile's key. Check it
            # here, then send the request on with this profile's own key.
            if key_refused(client, lines):
                return
            api_key = profile_key(profile)
        if local == BUSY_PATH and method == "GET":
            # Polled every few seconds; not logged, to keep serve.log readable.
            serve_busy(client, target, lines)
            return
        if local == ACTIVE_PATH and method == "GET":
            serve_active(client, lines)
            return
        if local == PROFILES_PATH and method == "GET":
            serve_profiles(client, lines)
            return
        if local == META_PATH and method == "GET":
            serve_meta(client, target, lines)
            return
        if local in (CRON_PATH, CRON_RUNS_PATH, CRON_OUTPUT_PATH) and method == "GET":
            serve_cron(client, target, lines, local)
            return
        if local == DECIDE_PATH and method == "POST":
            serve_decide(client, lines, leftover)
            return
        log("%s %s" % (method, path))
        note_poll(method, rest)
        if page_request(method, target):
            serve_page(client, method, target)
            return
        proxy(client, head, leftover, upstream_addr, api_key)
    except ValueError:
        with _suppress():
            send_response(client, 400, "Bad Request", "Bad request\n")
    except ConnectionRefusedError:
        with _suppress():
            send_response(client, 502, "Bad Gateway", "Hermes API is not reachable\n")
    except Exception as exc:
        log("proxy error: %s" % type(exc).__name__)
        with _suppress():
            send_response(client, 502, "Bad Gateway", "Hermes API is not reachable\n")


# --- Alerts -------------------------------------------------------------------
# Watches Hermes's durable run store. That store holds exactly the runs started
# with an Idempotency-Key, which is what this page sends, so it is the phone's
# runs. A run waiting for approval, or finishing, sends one alert unless the
# page is polling that run right now (the owner is already looking at it).
#
# Where alerts go is the "alerts" block of config.json, read every tick, so an
# edit takes effect without a restart:
#   {"channel": "telegram", "chat_id": "123456789", "url": "https://<host>:8443/"}
#   {"channel": "homeassistant", "notify": "mobile_app_my_iphone", "url": "..."}
# No block, or "channel": "none", turns alerts off. Secrets (TELEGRAM_BOT_TOKEN,
# HASS_URL, HASS_TOKEN) are read from Hermes's .env at send time and never
# logged. The text stays generic because it crosses Apple's or Telegram's
# servers: no run ids, commands or session text.

WATCH_TICK = 3
# The page polls a live run every 2 seconds while it is on screen. iOS freezes
# the page soon after it leaves the screen, so a gap this long means nobody is
# looking.
WATCHING_WINDOW = 12
FAIL_BACKOFF = 30
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
NOTIFY_NAME = re.compile(r"^[a-z0-9_]+$")
# A numeric chat id (negative for groups) or a public @channel name.
CHAT_ID = re.compile(r"^(-?[0-9]{1,20}|@[A-Za-z0-9_]{5,32})$")
TELEGRAM_API = "https://api.telegram.org"  # tests point this at a fake

_polls = {}
_polls_lock = threading.Lock()
# None until a send fails. time.monotonic() can start near 0 in a new process,
# so a 0.0 start value would block every alert for the first FAIL_BACKOFF seconds.
_last_fail: list = [None]


def backing_off():
    return _last_fail[0] is not None and time.monotonic() - _last_fail[0] < FAIL_BACKOFF


def note_poll(method, path):
    if method != "GET" or not path.startswith("/v1/runs/"):
        return
    run_id = path[len("/v1/runs/"):].split("/", 1)[0]
    if run_id:
        with _polls_lock:
            _polls[run_id] = time.monotonic()


def watching(run_id):
    with _polls_lock:
        seen = _polls.get(run_id)
    return seen is not None and time.monotonic() - seen < WATCHING_WINDOW


# The page polls /phone-api/active every 5 seconds while it is on screen.
PAGE_KEY = "\0page"


def note_page():
    with _polls_lock:
        _polls[PAGE_KEY] = time.monotonic()


# A desktop approval alerts only after this long, so answering it at the desk
# does not also buzz the phone.
DESKTOP_ALERT_DELAY = 45


def check_desktop(config, approvals, alerted, now=None):
    now = time.time() if now is None else now
    open_ids = set()
    for item in approvals:
        rid = item["request_id"]
        open_ids.add(rid)
        if rid in alerted or now - (item.get("created") or now) < DESKTOP_ALERT_DELAY:
            continue
        if watching(PAGE_KEY):
            # The page is on screen and already shows it; never alert for this one later.
            alerted.add(rid)
            continue
        if send_alert(config, "%s is waiting for your approval on the desktop." % APP_NAME, True):
            alerted.add(rid)
            log("alert sent: desktop approval")
    alerted.intersection_update(open_ids)


def alert_config():
    """The alerts block of config.json, checked, or None when alerts are off."""
    config = load_settings().get("alerts")
    if not isinstance(config, dict):
        return None
    channel = str(config.get("channel") or ("homeassistant" if config.get("notify") else ""))
    if channel == "homeassistant":
        if not NOTIFY_NAME.match(str(config.get("notify") or "")):
            return None
    elif channel == "telegram":
        chat = str(config.get("chat_id") or "")
        if chat and not CHAT_ID.match(chat):
            return None
    else:
        return None
    return dict(config, channel=channel)


def read_env(path, names):
    found = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name = name.strip()
                if name.startswith("export "):
                    name = name[len("export "):].strip()
                if name in names:
                    found[name] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return found


def env_path(config):
    return config.get("env") or os.path.join(HERMES_HOME, ".env")


def homeassistant_request(config, message, urgent):
    env = read_env(env_path(config), ("HASS_URL", "HASS_TOKEN"))
    base = env.get("HASS_URL", "").rstrip("/")
    token = env.get("HASS_TOKEN", "")
    if not base or not token:
        log("alert skipped: HASS_URL or HASS_TOKEN missing")
        return None
    data = {"tag": "hermes-phone",
            "push": {"interruption-level": "time-sensitive" if urgent else "active"}}
    if config.get("url"):
        data["url"] = str(config["url"])
    body = json.dumps({"title": "Hermes", "message": message, "data": data}).encode("utf-8")
    return urllib.request.Request(
        "%s/api/services/notify/%s" % (base, config["notify"]), data=body, method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})


def telegram_request(config, message, urgent):
    """Through the bot Hermes already runs. Sending does not disturb the
    gateway: a bot's own messages never come back to it as updates."""
    env = read_env(env_path(config), ("TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL"))
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat = str(config.get("chat_id") or env.get("TELEGRAM_HOME_CHANNEL") or "")
    if not token or not CHAT_ID.match(chat):
        log("alert skipped: Telegram bot token or chat id missing")
        return None
    payload = {"chat_id": chat, "text": message}
    if config.get("url"):
        payload["reply_markup"] = {"inline_keyboard": [[{"text": "Open Hermes", "url": str(config["url"])}]]}
    return urllib.request.Request(
        "%s/bot%s/sendMessage" % (TELEGRAM_API, token), data=json.dumps(payload).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})


ALERT_CHANNELS = {"homeassistant": homeassistant_request, "telegram": telegram_request}


def send_alert(config, message, urgent):
    if backing_off():
        return False
    request = ALERT_CHANNELS[config.get("channel") or "homeassistant"](config, message, urgent)
    if request is None:
        _last_fail[0] = time.monotonic()
        return False
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
        return True
    except urllib.error.HTTPError as exc:
        # Only the status: the Telegram request URL carries the bot token.
        log("alert failed: HTTP %d" % exc.code)
    except urllib.error.URLError as exc:
        # The reason (refused, unreachable, DNS) names no secret; the URL and token stay out.
        reason = exc.reason
        log("alert failed: URLError %s %s" % (type(reason).__name__, getattr(reason, "errno", "") or ""))
    except Exception as exc:
        log("alert failed: %s" % type(exc).__name__)
    _last_fail[0] = time.monotonic()
    return False


def read_runs(db_path):
    # Hermes creates the store on the first run sent with an Idempotency-Key;
    # until then there is nothing to watch, which is not an error.
    if not os.path.exists(db_path):
        return {}
    uri = "file:%s?mode=ro" % urllib.parse.quote(db_path)
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        rows = conn.execute("SELECT run_id, status_json FROM run_idempotency").fetchall()
    finally:
        conn.close()
    runs = {}
    for run_id, status_json in rows:
        try:
            status = json.loads(status_json)
        except ValueError:
            continue
        if isinstance(status, dict):
            runs[run_id] = status
    return runs


def check_runs(config, runs, known, alerted):
    for run_id, status in runs.items():
        now = str(status.get("status") or "")
        before = known.get(run_id)
        known[run_id] = now
        if now == "waiting_for_approval":
            approval = status.get("approval") or {}
            key = "%s:%s" % (run_id, approval.get("request_id") or status.get("updated_at"))
            if key in alerted or watching(run_id):
                continue
            if send_alert(config, "%s is waiting for your approval." % APP_NAME, True):
                alerted.add(key)
                log("alert sent: approval")
        elif now in TERMINAL and before is not None and before not in TERMINAL:
            # Stop is almost always the owner's own tap; nothing to report.
            if now == "cancelled" or watching(run_id):
                continue
            text = ("%s finished your reply." if now == "completed" else "%s could not finish your reply.") % APP_NAME
            if send_alert(config, text, False):
                log("alert sent: %s" % now)
    for run_id in [r for r in known if r not in runs]:
        del known[run_id]
        for key in [k for k in alerted if k.startswith(run_id + ":")]:
            alerted.discard(key)


def watch_runs():
    known = None
    alerted = set()
    desktop_alerted = set()
    last_error = ""
    while True:
        try:
            config = alert_config()
            if config is None:
                known = None
            else:
                if config.get("approvals_dir"):
                    approvals = desktop_approvals(config["approvals_dir"])
                else:
                    approvals = all_active()[1]
                check_desktop(config, approvals, desktop_alerted)
                if config.get("db"):
                    runs = read_runs(config["db"])
                else:
                    # Every profile's phone runs. Run ids are unique across profiles.
                    runs = {}
                    for name in served_profiles():
                        runs.update(read_runs(profile_file(name, "runs_idempotency.db", RUNS_DB)))
                if known is None:
                    # Start from what is already there, so a restart does not
                    # replay old finishes. A pending approval still alerts.
                    known = {run_id: str(s.get("status") or "") for run_id, s in runs.items()}
                check_runs(config, runs, known, alerted)
            if last_error:
                log("alert watcher: recovered")
            last_error = ""
        except Exception as exc:
            # Log each new failure once, not every tick.
            error = type(exc).__name__
            if error != last_error:
                log("alert watcher: %s" % error)
                last_error = error
        time.sleep(WATCH_TICK)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return True


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        handle_client(self.request, UPSTREAM)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        log("connection failed")


def daemonize():
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir("/")
    os.umask(0o022)
    devnull = os.open("/dev/null", os.O_RDONLY)
    log_fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(devnull, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(devnull)
    if log_fd > 2:
        os.close(log_fd)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


def write_pid():
    with open(PID_PATH, "w") as handle:
        handle.write(str(os.getpid()))

    def _remove(signum=None, frame=None):
        try:
            if open(PID_PATH).read().strip() == str(os.getpid()):
                os.remove(PID_PATH)
        except Exception:
            pass
        if signum is not None:
            raise SystemExit(0)

    import atexit
    atexit.register(_remove)
    signal.signal(signal.SIGTERM, _remove)


def serve():
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    server = Server(LISTEN, Handler)
    write_pid()
    threading.Thread(target=watch_runs, name="alerts", daemon=True).start()
    log("listening on %s:%d -> %s:%d" % (LISTEN[0], LISTEN[1], UPSTREAM[0], UPSTREAM[1]))
    signal.signal(signal.SIGTERM, lambda *_args: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv):
    if argv[:1] == ["--faceid-config"] and len(argv) == 3:
        faceid.configure(argv[1], argv[2])
        print("Face ID configured for %s (%s)" % (argv[1], argv[2]))
        return
    if argv[:1] == ["--setup-code"]:
        print("Setup code (10 minutes): %s" % faceid.new_setup_code())
        return
    if argv[:1] == ["--notify"] and len(argv) == 2:
        # One alert through the configured channel (install test, update check).
        config = alert_config()
        if config is None:
            print("alerts are off (no valid \"alerts\" block in config.json)")
            sys.exit(2)
        ok = send_alert(config, argv[1], False)
        print("alert sent via %s" % config["channel"] if ok else "alert failed, see the line above")
        sys.exit(0 if ok else 1)
    foreground = "--foreground" in argv
    if not foreground:
        daemonize()
    serve()


if __name__ == "__main__":
    main(sys.argv[1:])
