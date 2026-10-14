"""Offline test of the serve.py alert watcher against a fake Home Assistant
and a fake Telegram Bot API.

Usage: python3 -I tests/alerts_test.py <repo-dir> <empty-scratch-dir>
Nothing reaches the real Home Assistant or the phone.
"""
import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

worktree, work = sys.argv[1], sys.argv[2]
os.makedirs(work, exist_ok=True)
sys.path.insert(0, worktree)
import serve  # noqa: E402
import faceid  # noqa: E402

serve.CONFIG_PATH = os.path.join(work, "config.json")
serve.LOG_PATH = os.path.join(work, "serve.log")
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

posts = []


class FakeHA(BaseHTTPRequestHandler):  # also answers as the Telegram Bot API
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        posts.append({"path": self.path, "auth": self.headers.get("Authorization"),
                      "body": json.loads(body)})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"[]")

    def log_message(self, *args):
        pass


ha = HTTPServer(("127.0.0.1", 0), FakeHA)
threading.Thread(target=ha.serve_forever, daemon=True).start()

env_path = os.path.join(work, "fake.env")
with open(env_path, "w") as handle:
    handle.write("# test\nexport HASS_URL=http://127.0.0.1:%d/\nHASS_TOKEN='fake-token'\nOTHER=x\n" % ha.server_port)
db_path = os.path.join(work, "runs.db")
if os.path.exists(db_path):
    os.remove(db_path)
conn = sqlite3.connect(db_path)
conn.execute("CREATE TABLE run_idempotency (run_id TEXT PRIMARY KEY, status_json TEXT)")
conn.commit()


def put(run_id, status):
    conn.execute("INSERT OR REPLACE INTO run_idempotency VALUES (?, ?)", (run_id, json.dumps(status)))
    conn.commit()


def drop(run_id):
    conn.execute("DELETE FROM run_idempotency WHERE run_id = ?", (run_id,))
    conn.commit()


config = {"notify": "mobile_app_test", "env": env_path, "db": db_path, "url": "https://example.test/"}
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


# Existing state at start: an old finished run and a pending approval.
put("run_old", {"status": "completed"})
put("run_wait0", {"status": "waiting_for_approval", "approval": {"request_id": "a0"}})
runs = serve.read_runs(db_path)
known = {r: str(s.get("status") or "") for r, s in runs.items()}
alerted = set()
serve.check_runs(config, runs, known, alerted)
check("startup: old finish not replayed, pending approval alerts once", len(posts) == 1)
check("approval is time-sensitive", posts and posts[-1]["body"]["data"]["push"]["interruption-level"] == "time-sensitive")
check("service path and token", posts and posts[-1]["path"] == "/api/services/notify/mobile_app_test" and posts[-1]["auth"] == "Bearer fake-token")
check("tap url and tag", posts and posts[-1]["body"]["data"].get("url") == "https://example.test/" and posts[-1]["body"]["data"].get("tag") == "hermes-phone")

serve.check_runs(config, serve.read_runs(db_path), known, alerted)
check("same approval does not repeat", len(posts) == 1)

# A new approval on the same run (new request id) alerts again.
put("run_wait0", {"status": "waiting_for_approval", "approval": {"request_id": "a1"}})
serve.check_runs(config, serve.read_runs(db_path), known, alerted)
check("second approval request alerts", len(posts) == 2)

# Run finishes while nobody is watching.
put("run_wait0", {"status": "completed"})
serve.check_runs(config, serve.read_runs(db_path), known, alerted)
check("unwatched finish alerts", len(posts) == 3 and posts[-1]["body"]["message"] == "Hermes finished your reply.")
check("finish is active, not time-sensitive", posts[-1]["body"]["data"]["push"]["interruption-level"] == "active")

# Run that the page is polling: no alerts.
put("run_seen", {"status": "running"})
serve.check_runs(config, serve.read_runs(db_path), known, alerted)
serve.note_poll("GET", "/v1/runs/run_seen")
put("run_seen", {"status": "waiting_for_approval", "approval": {"request_id": "b0"}})
serve.check_runs(config, serve.read_runs(db_path), known, alerted)
put("run_seen", {"status": "completed"})
serve.check_runs(config, serve.read_runs(db_path), known, alerted)
check("watched run: no approval or finish alert", len(posts) == 3)

# Stop (cancelled) never alerts; failure does.
put("run_stop", {"status": "running"})
put("run_fail", {"status": "running"})
serve.check_runs(config, serve.read_runs(db_path), known, alerted)
put("run_stop", {"status": "cancelled"})
put("run_fail", {"status": "failed"})
serve.check_runs(config, serve.read_runs(db_path), known, alerted)
check("cancelled silent, failed alerts", len(posts) == 4 and posts[-1]["body"]["message"] == "Hermes could not finish your reply.")

# A watched run that the page stops polling becomes unwatched.
serve._polls["run_late"] = time.monotonic() - serve.WATCHING_WINDOW - 1
put("run_late", {"status": "running"})
serve.check_runs(config, serve.read_runs(db_path), known, alerted)
put("run_late", {"status": "waiting_for_approval", "approval": {"request_id": "c0"}})
serve.check_runs(config, serve.read_runs(db_path), known, alerted)
check("stale poll counts as not watching", len(posts) == 5)

# Expired rows are forgotten.
drop("run_late")
serve.check_runs(config, serve.read_runs(db_path), known, alerted)
check("expired run forgotten", "run_late" not in known and not any(k.startswith("run_late:") for k in alerted))

# Generic text only: no run ids, commands or session text in any push.
check("no run ids in pushes", not any("run_" in json.dumps(p["body"]) for p in posts))

# Config gate: the "alerts" block of config.json.
def gate(block):
    with open(serve.CONFIG_PATH, "w") as handle:
        json.dump({} if block is None else {"alerts": block}, handle)
    return serve.alert_config()


check("no alerts block -> off", gate(None) is None)
check("channel none -> off", gate({"channel": "none"}) is None)
check("unknown channel -> off", gate({"channel": "pager"}) is None)
check("unsafe notify name rejected", gate({"channel": "homeassistant", "notify": "mobile_app_x/../../config"}) is None)
check("old Home Assistant shape still accepted", (gate({"notify": "mobile_app_x"}) or {}).get("channel") == "homeassistant")
check("unsafe telegram chat id rejected", gate({"channel": "telegram", "chat_id": "1/../../x"}) is None)
check("telegram with chat id accepted", (gate({"channel": "telegram", "chat_id": "-100123"}) or {}).get("channel") == "telegram")

# Telegram channel: bot token from .env, chat id from config, else TELEGRAM_HOME_CHANNEL.
serve.TELEGRAM_API = "http://127.0.0.1:%d" % ha.server_port
tg_env = os.path.join(work, "tg.env")
with open(tg_env, "w") as handle:
    handle.write("TELEGRAM_BOT_TOKEN=123:fake-bot-token\nTELEGRAM_HOME_CHANNEL=55501\n")
before = len(posts)
tg = {"channel": "telegram", "env": tg_env, "url": "https://example.test/"}
check("telegram send ok", serve.send_alert(tg, "Hermes finished your reply.", False))
last = posts[-1] if len(posts) > before else {"path": "", "body": {}}
check("telegram path carries the bot token, not a header", last["path"] == "/bot123:fake-bot-token/sendMessage" and not last["auth"])
check("telegram falls back to the home channel", last["body"].get("chat_id") == "55501")
check("telegram generic text", last["body"].get("text") == "Hermes finished your reply.")
check("telegram open button", last["body"].get("reply_markup", {}).get("inline_keyboard", [[{}]])[0][0].get("url") == "https://example.test/")
serve.send_alert(dict(tg, chat_id="777"), "x", True)
check("telegram chat id from config wins", posts[-1]["body"].get("chat_id") == "777")
with open(tg_env, "w") as handle:
    handle.write("OTHER=1\n")
serve._last_fail[0] = None
check("telegram without token: skipped, not sent", not serve.send_alert(tg, "x", False) and len(posts) == before + 2)
serve._last_fail[0] = None

# Home Assistant down: one failure, then backoff without hammering.
ha.shutdown()
ha.server_close()
before = len(posts)
put("run_down", {"status": "running"})
serve.check_runs(config, serve.read_runs(db_path), known, alerted)
put("run_down", {"status": "waiting_for_approval", "approval": {"request_id": "d0"}})
serve.check_runs(config, serve.read_runs(db_path), known, alerted)
check("HA down: not marked alerted, so it retries later", "run_down:d0" not in alerted)
check("HA down: backoff set", serve.backing_off())
serve._last_fail[0] = None
check("fresh process: no backoff", not serve.backing_off())

log = open(serve.LOG_PATH).read() if os.path.exists(serve.LOG_PATH) else ""
check("tokens never logged", "fake-token" not in log and "fake-bot-token" not in log)

failed = [name for name, ok in results if not ok]
print("\n%d passed, %d failed" % (len(results) - len(failed), len(failed)))
sys.exit(1 if failed else 0)
