"""Offline test of serve.py's desktop-approval endpoints and alert.

Usage: python3 -I tests/approvals_test.py <repo-dir> <empty-scratch-dir>
Starts a copy of the proxy on a spare port against a scratch HERMES_HOME.
Nothing touches ~/.hermes, the real Hermes API, Home Assistant or the phone.
"""
import http.client
import json
import os
import socket
import sqlite3
import sys
import threading
import time

repo, work = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
os.makedirs(work, exist_ok=True)
sys.path.insert(0, repo)
import serve  # noqa: E402
import faceid  # noqa: E402
# Never read a live install's config.json (alerts, port, Hermes home).
serve.CONFIG_PATH = os.path.join(work, "config.json")

# Never read the live Face ID config: with a passkey enrolled it locks the API-key path.
faceid.CONFIG_PATH = os.path.join(work, "faceid.json")
faceid.SETUP_PATH = os.path.join(work, "faceid-setup.json")

# Guard: this test must never read the live Face ID files. With a passkey
# enrolled they lock the API-key path; without one they hide that bug.
for _path in (faceid.CONFIG_PATH, faceid.SETUP_PATH):
    _scratch = os.path.realpath(work)
    if os.path.commonpath([os.path.realpath(_path), _scratch]) != _scratch:
        print("FAIL ISOLATION: would read live Face ID file %s" % _path)
        sys.exit(1)

home = os.path.join(work, "home")
base = os.path.join(home, "phone-approvals")
for sub in ("pending", "decisions"):
    os.makedirs(os.path.join(base, sub), mode=0o700, exist_ok=True)
with open(os.path.join(home, ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=test-key-123\n")
allow = os.path.join(work, "allowed-logins.txt")
with open(allow, "w") as handle:
    handle.write("# owner\nowner@example.com\n")

db = os.path.join(home, "state.db")
conn = sqlite3.connect(db)
conn.executescript("""
CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT);
CREATE TABLE session_turn_leases (conversation_id TEXT PRIMARY KEY, holder TEXT NOT NULL,
  acquired_at REAL NOT NULL, expires_at REAL NOT NULL);
""")
now = time.time()
conn.executemany("INSERT INTO sessions VALUES (?,?,?)", [("root", None, "compression"), ("tip", "root", None)])
conn.execute("INSERT INTO session_turn_leases VALUES (?,?,?,?)",
             ("root", "pid=%d:turn=x:platform=desktop" % os.getpid(), now - 30, now + 300))
conn.commit()
conn.close()

serve.HERMES_HOME = home
serve.STATE_DB = db
serve.RUNS_DB = os.path.join(home, "runs_idempotency.db")
serve.APPROVALS_DIR = base
serve.ALLOW_PATH = allow

RID = "a" * 32
DEAD = "b" * 32


def marker(rid, key, pid, created):
    with open(os.path.join(base, "pending", rid + ".json"), "w") as handle:
        json.dump({"request_id": rid, "session_key": key, "pid": pid, "created": created,
                   "command": "rm -rf build", "description": "recursive delete", "source": "desktop"}, handle)


marker(RID, "root", os.getpid(), now - 5)
marker(DEAD, "root", 999999, now - 5)
with open(os.path.join(base, "pending", "../evil.json".replace("/", "_")), "w") as handle:
    handle.write("{}")

results = []


def check(name, ok):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name)


listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
listener.close()
serve.LISTEN = ("127.0.0.1", port)
server = serve.Server(serve.LISTEN, serve.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def call(method, path, body=None, login="owner@example.com", key="test-key-123", raw=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if login:
        headers["Tailscale-User-Login"] = login
    if key:
        headers["Authorization"] = "Bearer " + key
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        headers["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    out = r.status, r.read()
    c.close()
    return out


status, body = call("GET", "/phone-api/active")
data = json.loads(body)
check("active 200", status == 200)
check("one live approval listed (dead pid and bad name skipped)", [a["request_id"] for a in data["approvals"]] == [RID])
check("approval carries the lineage", set(data["approvals"][0]["sessions"]) == {"root", "tip"})
check("phone choices only once/deny", data["approvals"][0]["choices"] == ["once", "deny"])
check("session marked approval", data["active"]["tip"]["state"] == "approval")

check("wrong login refused", call("POST", "/phone-api/approvals/decide", {"request_id": RID, "choice": "once"},
                                  login="someone@example.com")[0] == 403)
check("no API key refused", call("POST", "/phone-api/approvals/decide", {"request_id": RID, "choice": "once"},
                                 key="")[0] == 401)
check("'always' refused", call("POST", "/phone-api/approvals/decide", {"request_id": RID, "choice": "always"})[0] == 400)
check("'session' refused", call("POST", "/phone-api/approvals/decide", {"request_id": RID, "choice": "session"})[0] == 400)
check("path-like id refused", call("POST", "/phone-api/approvals/decide",
                                   {"request_id": "../../x" + "a" * 10, "choice": "once"})[0] == 400)
check("dead process id is not pending", call("POST", "/phone-api/approvals/decide",
                                             {"request_id": DEAD, "choice": "once"})[0] == 409)
check("unknown id is not pending", call("POST", "/phone-api/approvals/decide",
                                        {"request_id": "c" * 32, "choice": "once"})[0] == 409)
check("bad JSON 400", call("POST", "/phone-api/approvals/decide", raw=b"{nope")[0] == 400)
check("oversize body 400", call("POST", "/phone-api/approvals/decide", raw=b"x" * 5000)[0] == 400)
check("no decision file yet", os.listdir(os.path.join(base, "decisions")) == [])

status, _ = call("POST", "/phone-api/approvals/decide", {"request_id": RID, "choice": "once"})
check("once accepted 202", status == 202)
path = os.path.join(base, "decisions", RID + ".json")
check("decision file written", os.path.exists(path) and json.load(open(path))["choice"] == "once")
check("decision file mode 600", oct(os.stat(path).st_mode & 0o777) == "0o600")

# Alert: one push after the delay, none while the page is on screen.
sent = []
serve.send_alert = lambda config, message, urgent, push=None: sent.append((message, urgent)) or True
with serve._polls_lock:
    serve._polls.pop(serve.PAGE_KEY, None)
alerted = set()
items = [{"request_id": "d" * 32, "created": 1000.0}]
serve.check_desktop({}, items, alerted, now=1010.0)
check("no alert inside the delay", sent == [])
serve.check_desktop({}, items, alerted, now=1050.0)
check("one alert after the delay", len(sent) == 1 and sent[0][1] is True and "approval" in sent[0][0])
serve.check_desktop({}, items, alerted, now=1100.0)
check("not repeated", len(sent) == 1)
check("generic text, no command", "rm -rf" not in sent[0][0] and "build" not in sent[0][0])
serve.check_desktop({}, [], alerted, now=1100.0)
check("forgotten when answered", not alerted)
serve.note_page()
serve.check_desktop({}, [{"request_id": "e" * 32, "created": 1000.0}], alerted, now=1050.0)
check("no alert while the page is on screen", len(sent) == 1)

# --- Desktop questions (clarify) -----------------------------------------------
QRID = "srq-" + "d" * 12
cl_pending = os.path.join(base, "clarify", "pending")
cl_decisions = os.path.join(base, "clarify", "decisions")
for path in (cl_pending, cl_decisions):
    os.makedirs(path, mode=0o700, exist_ok=True)


def question_marker(rid, pid, answered=()):
    with open(os.path.join(cl_pending, rid + ".json"), "w") as handle:
        json.dump({"request_id": rid, "kind": "clarify", "session_key": "root", "pid": pid, "created": now,
                   "answered": list(answered), "questions": [
                       {"qid": "q0", "question": "Which house?", "choices": ["Home (Recommended)", "Cabin"],
                        "multi_select": False},
                       {"qid": "q1", "question": "Which checks?", "choices": ["Locks", "Lights"], "multi_select": True},
                       {"qid": "q2", "question": "Anything else?", "choices": None, "multi_select": False}]}, handle)


question_marker(QRID, os.getpid(), answered=["q2"])
question_marker("srq-" + "e" * 12, 999999)
status, body = call("GET", "/phone-api/active")
qs = [a for a in json.loads(body)["approvals"] if a.get("kind") == "clarify"]
check("question listed with /active (dead pid skipped)", [q["request_id"] for q in qs] == [QRID])
check("question carries the lineage", set(qs[0]["sessions"]) == {"root", "tip"} if qs else False)
q0 = qs[0]["questions"][0] if qs else {}
check("Recommended label moved to an index", q0.get("choices") == ["Home", "Cabin"] and q0.get("recommended") == 0)
check("desktop's answered question flagged", qs and qs[0]["questions"][2]["answered"] is True)

A = "/phone-api/clarify/answer"
check("question: wrong login refused", call("POST", A, {"request_id": QRID, "answers": {"q0": "Home"}},
                                            login="x@example.com")[0] == 403)
check("question: no API key refused", call("POST", A, {"request_id": QRID, "answers": {"q0": "Home"}}, key="")[0] == 401)
check("question: all blank refused", call("POST", A, {"request_id": QRID, "answers": {"q0": None, "q1": []}})[0] == 400)
check("question: list for a single question refused",
      call("POST", A, {"request_id": QRID, "answers": {"q0": ["Home", "Cabin"]}})[0] == 400)
check("question: unknown id 409", call("POST", A, {"request_id": "srq-" + "f" * 12, "answers": {"q0": "x"}})[0] == 409)
check("question: dead process 409", call("POST", A, {"request_id": "srq-" + "e" * 12, "answers": {"q0": "x"}})[0] == 409)
check("question: bad JSON 400", call("POST", A, raw=b"{nope")[0] == 400)
check("question: no decision written yet", os.listdir(cl_decisions) == [])
status, _ = call("POST", A, {"request_id": QRID, "answers": {"q0": " Cabin ", "q1": ["Locks", "Lights"],
                                                             "q2": "ignored", "q9": "extra"}})
check("question: answer accepted 202", status == 202)
with open(os.path.join(cl_decisions, QRID + ".json")) as handle:
    written = json.load(handle)["answers"]
check("question: only open questions written, trimmed",
      written == {"q0": "Cabin", "q1": ["Locks", "Lights"]})
check("question: decision file mode 600",
      os.stat(os.path.join(cl_decisions, QRID + ".json")).st_mode & 0o777 == 0o600)

# The alert for a waiting question says it is a question.
sent = []
serve.send_alert = lambda config, message, urgent, push=None: sent.append((message, push)) or True
serve.watching = lambda key: False
alerted = set()
serve.check_desktop({"channel": "webpush"}, [dict(qs[0], profile="default", created=now - 120)], alerted, now=now)
check("question alert wording", sent and "question" in sent[-1][0] and sent[-1][1]["body"] == "Has a question for you.")

server.shutdown()
print("%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
