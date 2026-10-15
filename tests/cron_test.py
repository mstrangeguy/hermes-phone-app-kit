"""Offline test of serve.py's read-only cron endpoints against a throwaway Hermes home.

Usage: python3 -I tests/cron_test.py <repo-dir> <empty-scratch-dir>

Covers: job list per profile and for all, the fields sent, runs joined from the
run log, output files and run sessions, output reads (and refusals for any
other path), the key / Face ID gate, and that nothing is written.
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
ha = os.path.join(home, "profiles", "ha-updates")
os.makedirs(ha, exist_ok=True)
with open(os.path.join(home, ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=%s\n" % DEFAULT_KEY)
with open(os.path.join(ha, ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=%s\n" % HA_KEY)
allow = os.path.join(work, "allowed-logins.txt")
with open(allow, "w") as handle:
    handle.write("owner@example.com\n")


def job(jid, name, **extra):
    row = {"id": jid, "name": name, "enabled": True, "state": "scheduled",
           "schedule": {"kind": "cron", "expr": "15 3 * * *", "display": "15 3 * * *"},
           "schedule_display": "15 3 * * *", "prompt": "do the thing", "deliver": "local",
           "repeat": {"times": None, "completed": 2}, "last_status": "ok",
           "next_run_at": "2026-10-15T03:15:00-07:00", "last_run_at": "2026-10-14T03:15:24-07:00",
           "model": "secret-model", "base_url": "http://internal", "origin": {"chat_id": "123"}}
    row.update(extra)
    return row


def write_jobs(base, jobs):
    os.makedirs(os.path.join(base, "cron"), exist_ok=True)
    with open(os.path.join(base, "cron", "jobs.json"), "w") as handle:
        json.dump({"jobs": jobs, "updated_at": "x"}, handle)


write_jobs(home, [job("aaa111", "Default job", no_agent=True, script="x.sh"),
                  {"id": "../bad", "name": "bad id"}, "not a dict"])
write_jobs(ha, [job("bbb222", "HA job", prompt="p" * 9000, last_error="e" * 2000)])

EXEC_SCHEMA = """CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
 process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER, status TEXT NOT NULL,
 handoff_pending INTEGER NOT NULL DEFAULT 0, handoff_started_at REAL, claimed_at TEXT NOT NULL,
 started_at TEXT, finished_at TEXT, error TEXT, delivery_outcome TEXT, scheduled_instant TEXT);"""


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) + time.strftime("%z")[:3] + ":" + time.strftime("%z")[3:]


def fname(ts):
    return time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(ts)) + ".md"


def sid(jid, ts):
    return "cron_%s_%s" % (jid, time.strftime("%Y%m%d_%H%M%S", time.localtime(ts)))


now = int(time.time()) - 7200
t1, t2, t0 = now - 86400, now, now - 5 * 86400  # t0: output only, before the run log
conn = sqlite3.connect(os.path.join(ha, "cron", "executions.db"))
conn.executescript(EXEC_SCHEMA)
conn.execute("INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at, started_at, "
             "finished_at, error, delivery_outcome) VALUES ('e1','bbb222','builtin','p',1,'completed',?,?,?,NULL,'delivered')",
             (iso(t1), iso(t1 + 1), iso(t1 + 15)))
conn.execute("INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at, started_at, "
             "finished_at, error, delivery_outcome) VALUES ('e2','bbb222','builtin','p',1,'failed',?,?,?,'boom','failed')",
             (iso(t2), iso(t2 + 1), iso(t2 + 20)))
conn.execute("INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at) "
             "VALUES ('e3','other','builtin','p',1,'completed',?)", (iso(t2),))
conn.commit()
conn.close()
out_dir = os.path.join(ha, "cron", "output", "bbb222")
os.makedirs(out_dir)
for ts, text in ((t1 + 15, "# run 1\n\n## Response\n\nall good\n"), (t2 + 20, "# run 2\n"),
                 (t0, "# old run\n")):
    with open(os.path.join(out_dir, fname(ts)), "w") as handle:
        handle.write(text)
with open(os.path.join(out_dir, "notes.txt"), "w") as handle:
    handle.write("not output")
with open(os.path.join(ha, "cron", "jobs.secret.md"), "w") as handle:
    handle.write("x")
big = os.path.join(home, "cron", "output", "aaa111")
os.makedirs(big)
with open(os.path.join(big, fname(t2)), "w") as handle:
    handle.write("z" * (serve.OUTPUT_MAX + 10))
st = sqlite3.connect(os.path.join(ha, "state.db"))
st.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
st.executemany("INSERT INTO sessions VALUES (?, 'cron')", [(sid("bbb222", t1),), (sid("bbb222", t2),),
                                                           (sid("bbb2220", t2),)])
st.execute("INSERT INTO sessions VALUES (?, 'desktop')", (sid("bbb222", t0),))
st.commit()
st.close()

serve.HERMES_HOME = home
serve.STATE_DB = os.path.join(home, "state.db")
serve.ALLOW_PATH = allow

results = []


def check(name, ok):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name)


def tree_state():
    out = {}
    for root, _, files in os.walk(home):
        for name in files:
            path = os.path.join(root, name)
            out[path] = os.stat(path).st_mtime_ns
    return out


before = tree_state()

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
listener.close()
server = serve.Server(("127.0.0.1", port), serve.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def call(path, key=DEFAULT_KEY, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Tailscale-User-Login": "owner@example.com"}
    if key:
        h["Authorization"] = "Bearer " + key
    c.request(method, path, headers=h)
    r = c.getresponse()
    body = r.read()
    c.close()
    try:
        return r.status, json.loads(body)
    except ValueError:
        return r.status, body


# --- Jobs -------------------------------------------------------------------
status, body = call("/phone-api/cron")
jobs = body.get("jobs", []) if isinstance(body, dict) else []
check("default jobs only, bad rows skipped", status == 200 and [j["id"] for j in jobs] == ["aaa111"])
status, body = call("/phone-api/cron?profile=all")
check("all profiles", status == 200 and sorted(j["id"] for j in body["jobs"]) == ["aaa111", "bbb222"])
hj = [j for j in body["jobs"] if j["id"] == "bbb222"][0]
check("profile tagged", hj["profile"] == "ha-updates")
check("schedule flattened", hj["schedule"] == "15 3 * * *" and hj["schedule_kind"] == "cron")
check("runs completed", hj["runs_completed"] == 2)
check("long fields clipped", len(hj["prompt"]) == 4000 and len(hj["last_error"]) == 600)
check("model, base_url, origin not sent", not any(k in hj for k in ("model", "base_url", "origin")))
check("profile=ha-updates", [j["id"] for j in call("/phone-api/cron?profile=ha-updates")[1]["jobs"]] == ["bbb222"])
check("unknown profile 404", call("/phone-api/cron?profile=nope")[0] == 404)
check("needs the key", call("/phone-api/cron", key="wrong")[0] == 401)

# --- Runs -------------------------------------------------------------------
status, body = call("/phone-api/cron/runs?profile=ha-updates&job=bbb222")
runs = body.get("runs", []) if isinstance(body, dict) else []
check("runs listed newest first", status == 200 and len(runs) == 3 and runs[0]["status"] == "failed")
check("failed run keeps error and delivery", runs and runs[0]["error"] == "boom" and runs[0]["delivery"] == "failed")
check("run joins its output", runs and runs[0]["output"] == fname(t2 + 20) and runs[1]["output"] == fname(t1 + 15))
check("run joins its session", runs and runs[0]["session"] == sid("bbb222", t2) and runs[1]["session"] == sid("bbb222", t1))
check("other job's session not taken", all(r["session"] != sid("bbb2220", t2) for r in runs))
check("output before the run log still listed", runs and runs[2]["output"] == fname(t0) and runs[2]["status"] is None)
check("non-cron session ignored", runs and runs[2]["session"] is None)
check("other job's run not listed", len(runs) == 3)
check("job with no run log", call("/phone-api/cron/runs?job=aaa111")[1]["runs"][0]["output"] == fname(t2))
check("bad job id 400", call("/phone-api/cron/runs?profile=ha-updates&job=../x")[0] == 400)
check("missing job id 400", call("/phone-api/cron/runs?profile=ha-updates")[0] == 400)
check("unknown job: empty", call("/phone-api/cron/runs?profile=ha-updates&job=zzz")[1] == {"runs": []})

# --- Output -----------------------------------------------------------------
status, body = call("/phone-api/cron/output?profile=ha-updates&job=bbb222&file=" + fname(t1 + 15))
check("output read", status == 200 and "all good" in body["text"] and body["truncated"] is False)
status, body = call("/phone-api/cron/output?job=aaa111&file=" + fname(t2))
check("big output cut", status == 200 and len(body["text"]) == serve.OUTPUT_MAX and body["truncated"] is True)
for bad in ("notes.txt", "../jobs.json", "..%2Fjobs.json", "%2E%2E%2F..%2Fjobs.secret.md", ""):
    check("refused file %r" % bad, call("/phone-api/cron/output?profile=ha-updates&job=bbb222&file=" + bad)[0] == 404)
check("refused job ..", call("/phone-api/cron/output?profile=ha-updates&job=..&file=" + fname(t0))[0] == 400)
check("output from other profile not found",
      call("/phone-api/cron/output?job=bbb222&file=" + fname(t0))[0] == 404)
os.symlink(os.path.join(ha, "cron", "jobs.json"), os.path.join(out_dir, "2020-01-01_00-00-00.md"))
check("symlink out of the folder refused",
      call("/phone-api/cron/output?profile=ha-updates&job=bbb222&file=2020-01-01_00-00-00.md")[0] == 404)
os.remove(os.path.join(out_dir, "2020-01-01_00-00-00.md"))
check("output needs the key", call("/phone-api/cron/output?job=aaa111&file=" + fname(t2), key="")[0] == 401)

# --- Face ID mode -----------------------------------------------------------
real = (faceid.enrolled, faceid.session_valid)
faceid.enrolled = lambda: True
faceid.session_valid = lambda token, touch=False: False
check("locked without unlock cookie", call("/phone-api/cron")[0] == 401)
faceid.session_valid = lambda token, touch=False: True
check("open with unlock cookie", call("/phone-api/cron", key="")[0] == 200)
faceid.enrolled, faceid.session_valid = real

# --- Read-only --------------------------------------------------------------
check("nothing written", tree_state() == before)

server.shutdown()
passed = sum(results)
print("%d/%d passed" % (passed, len(results)))
sys.exit(0 if passed == len(results) else 1)
