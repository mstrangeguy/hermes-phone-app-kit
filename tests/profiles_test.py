"""Offline test of serve.py's multi-profile routing against a throwaway Hermes home.

Usage: python3 -I tests/profiles_test.py <repo-dir> <empty-scratch-dir>

Covers: which profiles are offered, /p/<profile>/ forwarding with that
profile's own key (API-key mode and Face ID mode), refusals, /phone-api/active
merging every profile, /phone-api/busy and /phone-api/meta per profile.
"""
import http.client
import json
import os
import socket
import sqlite3
import sys
import threading
import time

repo, work = sys.argv[1], sys.argv[2]
os.makedirs(work, exist_ok=True)
sys.path.insert(0, repo)
import serve  # noqa: E402
import faceid  # noqa: E402

serve.CONFIG_PATH = os.path.join(work, "config.json")
faceid.CONFIG_PATH = os.path.join(work, "faceid.json")
faceid.SETUP_PATH = os.path.join(work, "faceid-setup.json")
for _path in (faceid.CONFIG_PATH, faceid.SETUP_PATH, serve.CONFIG_PATH):
    _scratch = os.path.realpath(work)
    if os.path.commonpath([os.path.realpath(_path), _scratch]) != _scratch:
        print("FAIL ISOLATION: would read live file %s" % _path)
        sys.exit(1)

DEFAULT_KEY = "default-key-0123456789"
HA_KEY = "ha-key-abcdefghijklmnop"
home = os.path.join(work, "home")
profiles = os.path.join(home, "profiles")
for name in ("work", "nokey", "short", "Bad.Name"):
    os.makedirs(os.path.join(profiles, name), exist_ok=True)
with open(os.path.join(home, ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=%s\n" % DEFAULT_KEY)
with open(os.path.join(profiles, "work", ".env"), "w") as handle:
    handle.write("# x\nexport API_SERVER_KEY='%s'\n" % HA_KEY)
with open(os.path.join(profiles, "short", ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=tooshort\n")
with open(os.path.join(profiles, "Bad.Name", ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=%s\n" % HA_KEY)
allow = os.path.join(work, "allowed-logins.txt")
with open(allow, "w") as handle:
    handle.write("owner@example.com\n")

now = time.time()
SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT, source TEXT,
  title TEXT, started_at REAL, ended_at REAL, last_activity_at REAL, message_count INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL,
  cwd TEXT, git_repo_root TEXT, archived INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0);
CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT);
CREATE TABLE session_turn_leases (conversation_id TEXT PRIMARY KEY, holder TEXT NOT NULL,
  acquired_at REAL NOT NULL, expires_at REAL NOT NULL);
"""
proj_dir = os.path.join(work, "proj", "Phone App")
os.makedirs(os.path.join(proj_dir, "sub"), exist_ok=True)


def make_state(path, rows, leases=()):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for row in rows:
        conn.execute("INSERT INTO sessions (id, source, title, started_at, last_activity_at, cwd, git_repo_root, "
                     "archived, parent_session_id, end_reason) VALUES (?,?,?,?,?,?,?,?,?,?)", row)
    conn.execute("INSERT INTO messages (session_id, role, content) VALUES ('arch', 'user', 'old question')")
    for lease in leases:
        conn.execute("INSERT INTO session_turn_leases VALUES (?,?,?,?)", lease)
    conn.commit()
    conn.close()


live = "pid=%d:turn=x:platform=desktop" % os.getpid()
make_state(os.path.join(home, "state.db"), [
    ("d1", "desktop", "in project", now - 50, now - 10, os.path.join(proj_dir, "sub"), None, 0, None, None),
    ("d2", "cli", "no project", now - 40, now - 20, "/tmp/elsewhere", None, 0, None, None),
    ("d3", "desktop", "repo root", now - 30, now - 5, "", proj_dir, 0, None, None),
    ("near", "desktop", "prefix only", now - 30, now - 5, proj_dir + "-other", None, 0, None, None),
    ("arch", "desktop", "archived one", now - 900, now - 800, proj_dir, None, 1, None, None),
    ("archroot", "desktop", "compressed", now - 900, now - 700, "", None, 1, None, "compression"),
    ("archtip", "desktop", "tip", now - 600, now - 600, "", None, 1, "archroot", None),
], [("d1", live, now - 5, now + 300)])
make_state(os.path.join(profiles, "work", "state.db"), [
    ("h1", "cron", "nightly", now - 100, now - 50, "", None, 0, None, None),
], [("h1", live, now - 3, now + 300)])
pconn = sqlite3.connect(os.path.join(home, "projects.db"))
pconn.executescript("""
CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE project_folders (project_id TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY (project_id, path));
""")
pconn.executemany("INSERT INTO projects VALUES (?,?,?)", [("p1", "Phone App", 0), ("p2", "Gone", 1),
                                                         ("p3", "Parent", 0)])
pconn.executemany("INSERT INTO project_folders VALUES (?,?)", [
    ("p1", proj_dir), ("p2", os.path.join(proj_dir, "sub")), ("p3", os.path.dirname(proj_dir))])
pconn.commit()
pconn.close()
for name in ("work",):
    pending = os.path.join(profiles, name, "phone-approvals", "pending")
    os.makedirs(pending, exist_ok=True)
    os.makedirs(os.path.join(profiles, name, "phone-approvals", "decisions"), exist_ok=True)
    with open(os.path.join(pending, "h" * 32 + ".json"), "w") as handle:
        json.dump({"request_id": "h" * 32, "session_key": "h1", "pid": os.getpid(), "created": now - 2,
                   "command": "ls", "description": "list", "source": "desktop"}, handle)

serve.HERMES_HOME = home
serve.STATE_DB = os.path.join(home, "state.db")
serve.RUNS_DB = os.path.join(home, "runs_idempotency.db")
serve.APPROVALS_DIR = os.path.join(home, "phone-approvals")
serve.ALLOW_PATH = allow

results = []


def check(name, ok):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name)


# --- Fake Hermes: records each request, answers 200 {} ----------------------
seen = []
up = socket.socket()
up.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
up.bind(("127.0.0.1", 0))
up.listen(16)
up_addr = up.getsockname()


def fake_hermes():
    while True:
        conn, _ = up.accept()
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        seen.append(data.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1"))
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n"
                     b"Connection: close\r\n\r\n{}")
        conn.close()


threading.Thread(target=fake_hermes, daemon=True).start()


class H(serve.socketserver.BaseRequestHandler):
    def handle(self):
        serve.handle_client(self.request, up_addr)


listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
listener.close()
server = serve.Server(("127.0.0.1", port), H)
threading.Thread(target=server.serve_forever, daemon=True).start()


def call(method, path, key=DEFAULT_KEY, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Tailscale-User-Login": "owner@example.com"}
    if key:
        h["Authorization"] = "Bearer " + key
    h.update(headers or {})
    c.request(method, path, headers=h)
    r = c.getresponse()
    out = r.status, r.read()
    c.close()
    return out


def last_auth():
    for line in seen[-1].split("\r\n"):
        if line.lower().startswith("authorization:"):
            return line.split(":", 1)[1].strip()
    return ""


# --- Which profiles ---------------------------------------------------------
check("served: default + keyed profile only", serve.served_profiles() == ["default", "work"])
check("profile key read (export, quotes)", serve.profile_key("work") == HA_KEY)
check("short key not usable", serve.profile_key("short") == "")
check("bad profile name has no home", serve.profile_home("../x") is None and serve.profile_key("../x") == "")
check("split /p/ path", serve.split_profile("/p/work/api/sessions") == ("work", "/api/sessions"))
check("split plain path", serve.split_profile("/api/sessions") == ("default", "/api/sessions"))
check("split unknown", serve.split_profile("/p/nokey/api/sessions")[0] is None)
check("split /p/default refused", serve.split_profile("/p/default/api/sessions")[0] is None)

status, body = call("GET", "/phone-api/profiles")
check("profiles endpoint", status == 200 and json.loads(body)["profiles"] == ["default", "work"])
check("profiles endpoint needs the key", call("GET", "/phone-api/profiles", key="wrong")[0] == 401)

# --- Forwarding, API-key mode -----------------------------------------------
status, _ = call("GET", "/api/sessions?limit=1")
check("default forwarded as is", status == 200 and seen[-1].startswith("GET /api/sessions?limit=1 ")
      and last_auth() == "Bearer " + DEFAULT_KEY)
status, _ = call("GET", "/p/work/api/sessions?limit=1")
check("profile forwarded with its own key", status == 200 and
      seen[-1].startswith("GET /p/work/api/sessions?limit=1 ") and last_auth() == "Bearer " + HA_KEY)
check("default key never sent to a profile", DEFAULT_KEY not in seen[-1])
count = len(seen)
check("profile refused with wrong key", call("GET", "/p/work/api/sessions", key="wrong")[0] == 401)
check("profile refused with no key", call("GET", "/p/work/api/sessions", key="")[0] == 401)
check("phone cannot send the profile key itself", call("GET", "/p/work/api/sessions", key=HA_KEY)[0] == 401)
check("unknown profile 404", call("GET", "/p/nokey/api/sessions")[0] == 404)
check("path-like profile 404", call("GET", "/p/..%2F..%2Fx/api/sessions")[0] == 404)
check("nothing forwarded on refusals", len(seen) == count)
check("phone mount + profile", call("GET", "/phone/p/work/v1/runs/r1")[0] == 200 and
      seen[-1].startswith("GET /p/work/v1/runs/r1 ") and last_auth() == "Bearer " + HA_KEY)

# --- Active, busy, meta -------------------------------------------------------
status, body = call("GET", "/phone-api/active")
data = json.loads(body)
check("active merges profiles", status == 200 and set(data["active"]) == {"d1", "h1"})
check("active entries name the profile", data["active"]["d1"]["profile"] == "default"
      and data["active"]["h1"]["profile"] == "work")
check("profile approval listed with profile", [(a["request_id"], a["profile"]) for a in data["approvals"]]
      == [("h" * 32, "work")])
check("profile approval marks its session", data["active"]["h1"]["state"] == "approval")
check("approval found in the profile's folder",
      serve.approval_base("h" * 32) == os.path.join(profiles, "work", "phone-approvals"))

status, body = call("GET", "/phone-api/busy?session=h1&profile=work")
check("busy reads the profile's state.db", status == 200 and json.loads(body)["busy"] is True)
status, body = call("GET", "/phone-api/busy?session=h1")
check("busy defaults to default profile", status == 200 and json.loads(body)["busy"] is False)
check("busy unknown profile 404", call("GET", "/phone-api/busy?session=h1&profile=nokey")[0] == 404)

status, body = call("GET", "/phone-api/meta?ids=d1,d2,d3,near,../x")
meta = json.loads(body)["meta"]
check("meta 200", status == 200)
check("project by cwd, archived project ignored", meta["d1"]["project"] == "Phone App")
check("project by repo root", meta["d3"]["project"] == "Phone App")
check("longest folder wins over parent", meta["d1"]["project"] != "Parent")
check("name prefix is not a folder match", meta["near"]["project"] == "Parent")
check("no project is empty", meta["d2"]["project"] == "")
check("bad id dropped", "../x" not in meta)
check("archived off by default", json.loads(body)["archived"] is None)
status, body = call("GET", "/phone-api/meta?archived=1")
arch = json.loads(body)["archived"]
check("archived listed, roots only", [r["id"] for r in arch] == ["archroot", "arch"])
check("archived row shape", arch[1]["archived"] is True and arch[1]["preview"] == "old question"
      and arch[1]["source"] == "desktop")
status, body = call("GET", "/phone-api/meta?profile=work&ids=h1")
check("meta per profile (no projects.db)", status == 200 and json.loads(body)["meta"]["h1"]["project"] == "")
check("meta unknown profile 404", call("GET", "/phone-api/meta?profile=nokey")[0] == 404)
check("meta needs the key", call("GET", "/phone-api/meta", key="")[0] == 401)

# --- Face ID mode: the Mac adds each profile's key ----------------------------
faceid.enrolled = lambda: True
faceid.session_valid = lambda token, touch=True: token == "good"
faceid.cookie_token = lambda lines: "good" if any(b"cookie: s=good" in l.lower() for l in lines) else ""
faceid.load_config = lambda: {"origin": "https://phone.example"}
cookie = {"Cookie": "s=good"}
status, _ = call("GET", "/p/work/api/sessions", key="", headers=cookie)
check("face id: profile gets its key", status == 200 and last_auth() == "Bearer " + HA_KEY)
status, _ = call("GET", "/api/sessions", key="", headers=cookie)
check("face id: default gets its key", status == 200 and last_auth() == "Bearer " + DEFAULT_KEY)
check("face id: locked without cookie", call("GET", "/p/work/api/sessions", key="")[0] == 401)
check("face id: unknown profile 404", call("GET", "/p/nokey/api/sessions", key="", headers=cookie)[0] == 404)
status, body = call("GET", "/phone-api/active", key="", headers=cookie)
check("face id: active works", status == 200 and set(json.loads(body)["active"]) == {"d1", "h1"})

server.shutdown()
print("%d passed, %d failed" % (results.count(True), results.count(False)))
sys.exit(0 if all(results) else 1)
