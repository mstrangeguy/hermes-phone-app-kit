"""Offline test of the serve.py busy check against a throwaway state.db.

Usage: python3 -I tests/busy_test.py <repo-dir> <empty-scratch-dir>
"""
import os
import sqlite3
import sys
import time

repo, work = sys.argv[1], sys.argv[2]
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

db = os.path.join(work, "state.db")
if os.path.exists(db):
    os.remove(db)
conn = sqlite3.connect(db)
conn.executescript("""
CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT);
CREATE TABLE session_turn_leases (conversation_id TEXT PRIMARY KEY, holder TEXT NOT NULL,
  acquired_at REAL NOT NULL, expires_at REAL NOT NULL);
""")
now = time.time()
live = "pid=%d:turn=x:platform=desktop" % os.getpid()
conn.executemany("INSERT INTO sessions VALUES (?,?,?)", [
    ("root", None, "compression"), ("child", "root", None),
    ("forked", "other", None), ("other", None, "branched"),
    ("idle", None, None), ("stale", None, None), ("dead", None, None)])
conn.executemany("INSERT INTO session_turn_leases VALUES (?,?,?,?)", [
    ("root", live, now - 30, now + 300),
    ("other", live, now - 5, now + 300),
    ("stale", live, now - 900, now - 1),
    ("dead", "pid=999999:turn=y:platform=cli", now - 5, now + 300)])
conn.commit()
conn.close()

results = []


def check(name, ok):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name)


r = serve.session_busy("root", db)
check("busy session", r["busy"] is True and r["platform"] == "desktop")
check("holder string not returned", "holder" not in r and str(os.getpid()) not in str(r))
check("compression child maps to root lease", serve.session_busy("child", db)["busy"] is True)
check("fork of a busy session is not busy", serve.session_busy("forked", db)["busy"] is False)
check("no lease is idle", serve.session_busy("idle", db)["busy"] is False)
check("expired lease is idle", serve.session_busy("stale", db)["busy"] is False)
check("dead holder pid is idle", serve.session_busy("dead", db)["busy"] is False)
check("unknown session is idle", serve.session_busy("nope", db)["busy"] is False)
check("id pattern rejects paths", not serve.SESSION_ID.match("../x") and not serve.SESSION_ID.match(""))
check("id pattern takes real ids", bool(serve.SESSION_ID.match("20261011_203846_b84588")))

# --- /phone-api/active: every live session, approval from the run store ---
runs = os.path.join(work, "runs.db")
if os.path.exists(runs):
    os.remove(runs)
rc = sqlite3.connect(runs)
rc.execute("CREATE TABLE run_idempotency (run_id TEXT PRIMARY KEY, status_json TEXT, updated_at REAL)")
rc.executemany("INSERT INTO run_idempotency VALUES (?,?,?)", [
    ("r1", '{"status": "waiting_for_approval", "session_id": "child"}', now),
    ("r2", '{"status": "completed", "session_id": "other"}', now),
    ("r3", "not json", now)])
rc.commit()
rc.close()

act = serve.active_sessions(db, runs)
check("active lists live leases only", set(act) == {"root", "child", "other"})
check("approval found through a lineage tip", act["root"]["state"] == "approval" and act["child"]["state"] == "approval")
check("completed run does not mark approval", act["other"]["state"] == "working")
check("active has no holder string", all(set(v) == {"state", "since", "platform"} for v in act.values()))
check("missing run store is fine", serve.active_sessions(db, os.path.join(work, "none.db"))["other"]["state"] == "working")

# --- Hermes read state (last_read_at watermark), shared with the desktop ---
# An older Hermes without last_read_at: an sqlite error, which all_active logs and skips.
try:
    serve.read_states(db)
    check("no last_read_at column is an sqlite error", False)
except sqlite3.Error:
    check("no last_read_at column is an sqlite error", True)

rdb = os.path.join(work, "read.db")
if os.path.exists(rdb):
    os.remove(rdb)
conn = sqlite3.connect(rdb)
conn.executescript("""
CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT,
  started_at REAL, last_activity_at REAL, last_read_at REAL);
CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL);
""")
day = 86400
conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?)", [
    ("fresh", None, None, now - 600, now - 60, now - 300),        # activity after read -> unread
    ("seen", None, None, now - 600, now - 400, now - 300),        # read after activity -> read
    ("msgnewer", None, None, now - 600, now - 400, now - 300),    # heartbeat lags, a message is newer -> unread
    ("never", None, None, now - 600, now - 60, None),             # NULL = never tracked -> absent
    ("zero", None, None, now - 600, None, 0.0),                   # 0 = marked unread -> unread
    ("old", None, None, now - 90 * day, now - 89 * day, now - 95 * day),  # outside the window -> absent
    ("croot", None, "compression", now - 900, now - 800, now - 100),
    ("ctip", "croot", None, now - 800, now - 20, now - 100)])     # lineage: tip newer than watermark
conn.executemany("INSERT INTO messages (session_id, timestamp) VALUES (?,?)", [
    ("msgnewer", now - 10), ("seen", now - 450)])
conn.commit()
conn.close()
rs = serve.read_states(rdb, now)
check("activity after the watermark is unread", rs.get("fresh") is True)
check("watermark after activity is read", rs.get("seen") is False)
check("a newer message beats a lagging heartbeat", rs.get("msgnewer") is True)
check("never-tracked session is left to the page", "never" not in rs)
check("0 watermark (marked unread) is unread", rs.get("zero") is True)
check("sessions outside the window are left out", "old" not in rs)
check("compression lineage shares one answer", rs.get("croot") is True and rs.get("ctip") is True)
check("read state is plain booleans", all(isinstance(v, bool) for v in rs.values()))

print("%d passed, %d failed" % (results.count(True), results.count(False)))
sys.exit(0 if all(results) else 1)
