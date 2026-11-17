"""Offline test of the alert log and the Status screen's Recent alerts card.

Usage: python -I tests/alertlog_test.py <repo-dir> <empty-scratch-dir>
Nothing reaches a phone: Home Assistant is a fake local server, and web push
runs with no subscriptions.
"""
import io
import json
import os
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

repo, work = sys.argv[1], sys.argv[2]
os.makedirs(work, exist_ok=True)
sys.path.insert(0, repo)
import serve  # noqa: E402
import faceid  # noqa: E402
import webpush  # noqa: E402

serve.CONFIG_PATH = os.path.join(work, "config.json")
serve.LOG_PATH = os.path.join(work, "serve.log")
serve.APP_NAME = "Test Name"
faceid.CONFIG_PATH = os.path.join(work, "faceid.json")
faceid.SETUP_PATH = os.path.join(work, "faceid-setup.json")
webpush.STATE_PATH = os.path.join(work, "webpush.json")

# FAIL ISOLATION: the alert log must land in the scratch folder, never the live one.
_scratch = os.path.realpath(work)
for _path in (faceid.CONFIG_PATH, faceid.SETUP_PATH, serve.CONFIG_PATH, webpush.STATE_PATH, serve.alert_log_path()):
    if os.path.commonpath([os.path.realpath(_path), _scratch]) != _scratch:
        print("FAIL ISOLATION: would touch live file %s" % _path)
        sys.exit(1)

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def entries():
    with open(serve.alert_log_path(), encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# 1. Every serve.log line starts with the local date and time.
saved_err = sys.stderr
sys.stderr = io.StringIO()
serve.log("hello")
line = sys.stderr.getvalue()
sys.stderr = saved_err
check("log line has a timestamp", line.endswith(" hello\n") and time.strftime("%Y-%m-%d ") == line[:11] and line[13] == ":")

# 2. Home Assistant send: logged as sent, with the generic text it sent.
posts = []


class FakeHA(BaseHTTPRequestHandler):
    status = 200

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        posts.append(self.path)
        self.send_response(FakeHA.status)
        self.end_headers()
        self.wfile.write(b"[]")

    def log_message(self, format, *args):
        pass


ha = HTTPServer(("127.0.0.1", 0), FakeHA)
threading.Thread(target=ha.serve_forever, daemon=True).start()
env_path = os.path.join(work, "fake.env")
with open(env_path, "w") as handle:
    handle.write("HASS_URL=http://127.0.0.1:%d\nHASS_TOKEN=fake-token\n" % ha.server_port)
ha_config = {"channel": "homeassistant", "notify": "mobile_app_test", "env": env_path}

push = serve.push_alert("default", "", "Reply finished.", "reply finished")
check("push_alert carries the kind", push.get("kind") == "reply finished")
ok = serve.send_alert(ha_config, "Test Name finished your reply.", False, push)
got = entries()
check("HA send ok and logged once", ok and len(got) == 1 and got[0]["ok"] is True)
check("HA entry: generic text, kind, channel", got[0]["body"] == "Test Name finished your reply." and
      got[0]["kind"] == "reply finished" and got[0]["channel"] == "homeassistant" and got[0]["title"] == "Test Name")
check("entry has local time with zone and epoch", got[0]["at"][:4] == time.strftime("%Y") and
      got[0]["at"][-6] in "+-" and abs(got[0]["ts"] - time.time()) < 60)
check("log file is owner-only (0600)", stat.S_IMODE(os.stat(serve.alert_log_path()).st_mode) == 0o600)
check("no token in the alert log", "fake-token" not in open(serve.alert_log_path()).read())

# 3. HA failure: logged as not delivered with the HTTP status; the backoff skip is logged too.
FakeHA.status = 500
ok = serve.send_alert(ha_config, "Test Name is waiting for your approval.", True,
                      {"title": "x", "body": "y", "link": "", "kind": "approval"})
got = entries()
check("HA failure logged with status", not ok and got[-1]["ok"] is False and got[-1]["detail"] == "failed: HTTP 500"
      and got[-1]["urgent"] is True)
ok = serve.send_alert(ha_config, "again", False)
got = entries()
check("backoff skip logged", not ok and got[-1]["detail"].startswith("skipped: waiting"))
serve._last_fail[0] = None
FakeHA.status = 200

# 4. Web push with no phone subscribed: logged as skipped, with the push's own title and link.
wp_config = {"channel": "webpush", "url": "https://example.test/"}
wp_push = {"title": "Focus chat", "body": "Has a question for you.", "link": "#s=abc_123",
           "session": "default/abc_123", "kind": "question"}
ok = serve.send_alert(wp_config, "Test Name has a question for you.", True, wp_push)
got = entries()
check("webpush skip logged with title, link, session", not ok and got[-1]["title"] == "Focus chat" and
      got[-1]["link"] == "#s=abc_123" and got[-1]["session"] == "default/abc_123" and
      got[-1]["detail"] == "skipped: no phone has notifications on")
check("skip with no subscription sets no backoff", serve._last_fail[0] is None)

# 5. check_runs sends the right kind for a finished phone reply.
sent = []
real_send = serve.send_alert
serve.send_alert = lambda config, message, urgent, push=None: sent.append(push) or True
known = {"r1": "running", "r2": "running"}
serve.check_runs({}, {"r1": {"status": "completed", "session_id": "s1"},
                      "r2": {"status": "failed", "session_id": "s2"}}, known, set())
serve.send_alert = real_send
kinds = sorted(p.get("kind") for p in sent)
check("check_runs kinds", kinds == ["reply failed", "reply finished"])

# 6. Reading: newest first, garbage lines skipped, 30-day window.
with open(serve.alert_log_path(), "a") as handle:
    handle.write("not json\n")
newest = serve.read_alert_log()
check("read: newest first, garbage skipped", len(newest) == 4 and newest[0]["kind"] == "question" and
      newest[0]["ts"] >= newest[-1]["ts"])
check("read: limit", len(serve.read_alert_log(2)) == 2)

# 7. Pruning drops entries older than ALERT_LOG_DAYS and keeps the rest.
old = {"ts": time.time() - 40 * 86400, "at": "old", "kind": "approval", "ok": True}
with open(serve.alert_log_path()) as handle:
    current = handle.read()
with open(serve.alert_log_path(), "w") as handle:
    handle.write(json.dumps(old) + "\n" + current)
check("read hides entries older than 30 days", all(e.get("at") != "old" for e in serve.read_alert_log()))
serve._alert_log_pruned[0] = 0.0
serve.record_alert("notify", "webpush", "Monitor", "Disk full", "", "", False, True, "sent to 1 phone")
raw = open(serve.alert_log_path()).read()
check("prune removed the old entry and the garbage line", '"at": "old"' not in raw and "not json" not in raw)
check("prune kept recent entries", len(entries()) == 5)

# 8. The Recent alerts card.
card = serve.alerts_card()
check("card id/title", card["id"] == "alerts" and card["title"] == "Recent alerts" and card["link"] == "")
check("card lines match line_links", len(card["lines"]) == len(card["line_links"]) == 5)
first = card["lines"][0]
check("newest line: date, zone, title, body", first.startswith(time.strftime("%a %b ")) and
      time.strftime("%Z") in first and "Monitor · Disk full" in first)
q = [l for l in card["lines"] if "Focus chat" in l][0]
check("undelivered line says so", "(not delivered: skipped: no phone has notifications on)" in q)
check("question line links to its chat", card["line_links"][card["lines"].index(q)] == "#s=abc_123")
check("app-name-only title is left out", all("Test Name · " not in l for l in card["lines"]))
check("card state follows the newest alert", card["state"] == "info")
check("summary is the newest time", card["summary"].startswith("Last " + time.strftime("%a ")))
check("bad links are dropped", serve.STATUS_LINK.match("#s=abc_123") and all(
    (not l) or l.startswith("#") for l in card["line_links"]))
check("alert_when has no leading zero on the day", serve.alert_when(time.mktime((2026, 11, 7, 7, 5, 0, 0, 0, -1)))
      .startswith("Sat Nov 7 07:05"))

serve.record_alert("approval", "webpush", "Piko", "Waiting for your approval.", "#s=p1", "default/p1", True, False, "failed: x")
check("card goes red when the newest alert failed", serve.alerts_card()["state"] == "bad")
os.remove(serve.alert_log_path())
empty = serve.alerts_card()
check("empty card", empty["lines"] == [] and empty["state"] == "ok" and empty["summary"] == "None in 30 days")

# 9. Status body puts the card before the cron card when the screen is on.
with open(serve.CONFIG_PATH, "w") as handle:
    json.dump({"status": {"enabled": True, "file": os.path.join(work, "cards.json")}}, handle)
serve.served_profiles = lambda: []
body = serve.status_body()
ids = [c["id"] for c in body["cards"]]
check("status body: alerts then cron", ids[-2:] == ["alerts", "cron"])

# 10. A log that can't be written never stops an alert.
serve.ALERT_LOG_PATH = os.path.join(work, "no-such-dir", "alert-log.jsonl")
saved_err = sys.stderr
sys.stderr = io.StringIO()
ok = serve.send_alert(ha_config, "still sends", False)
msg = sys.stderr.getvalue()
sys.stderr = saved_err
check("unwritable log: alert still sent", ok and "alert log: could not write" in msg)
serve.ALERT_LOG_PATH = None

failed = [name for name, ok in results if not ok]
print("\n%d passed, %d failed" % (len(results) - len(failed), len(failed)))
sys.exit(1 if failed else 0)
