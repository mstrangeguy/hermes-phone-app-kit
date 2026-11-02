"""Offline test of plugin/phone-approvals against the real Hermes approval code.

Usage (Hermes venv python, from any directory):
  scripts/py -I tests/plugin_test.py <repo-dir> <empty-scratch-dir>

Builds a throwaway HERMES_HOME with the plugin enabled, raises real gateway
approvals on threads, and answers them through decision files the way serve.py
does. Nothing touches ~/.hermes. Re-run after every Hermes update.
"""
import json
import os
import sys
import threading
import time

repo, work = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
REAL_HOME = os.path.expanduser("~")
home = os.path.join(work, "hermes-home")
os.makedirs(os.path.join(home, "plugins"), exist_ok=True)
link = os.path.join(home, "plugins", "phone-approvals")
if not os.path.islink(link):
    os.symlink(os.path.join(repo, "plugin", "phone-approvals"), link)
with open(os.path.join(home, "config.yaml"), "w") as handle:
    handle.write("plugins:\n  enabled: [phone-approvals]\napprovals:\n  timeout: 6\n")
os.environ["HOME"] = work
os.environ["HERMES_HOME"] = home
# A throwaway home is a new "data root" to Hermes: importing its desktop
# gateway (section 8) would otherwise set up a fresh runtime for it and re-run
# this test inside that, without Hermes's dependencies.
os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
# Run as a desktop-style process even when launched from inside a Hermes turn:
# an inherited HERMES_SESSION_PLATFORM=api_server makes the plugin (correctly)
# skip every approval, and the test would fail for the wrong reason.
for _name in [n for n in os.environ if n.startswith("HERMES_SESSION_")]:
    del os.environ[_name]
# The Hermes checkout whose approval code is under test. HOME is replaced above,
# so resolve it from the real account, not "~".
sys.path.insert(0, os.environ.get("HERMES_AGENT_DIR") or os.path.join(REAL_HOME, ".hermes", "hermes-agent"))
os.chdir(work)

from hermes_cli.plugins import discover_plugins  # noqa: E402
from tools import approval  # noqa: E402
from tools.approval_gateway_wait import _await_gateway_decision  # noqa: E402

discover_plugins()
base = os.path.join(home, "phone-approvals")
pending_dir = os.path.join(base, "pending")
decisions_dir = os.path.join(base, "decisions")
results = []


def check(name, ok):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name)


def wait_for(fn, limit=5.0):
    end = time.time() + limit
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.1)
    return fn()


def raise_approval(key, command, settles):
    out = {}

    def run():
        approval.register_gateway_notify(key, lambda data: None)
        out["d"] = _await_gateway_decision(key, lambda data: settle_on(key, data, settles), {
            "command": command, "description": "test approval",
            "pattern_key": "k", "pattern_keys": ["k"]})

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


def settle_on(key, data, settles):
    # What tui_gateway does: attach a settle hook that withdraws the desktop prompt.
    rid = data.get("request_id")
    threading.Timer(0.05, lambda: approval.register_gateway_settle(
        key, rid, lambda reason: settles.append((rid, reason)))).start()


def markers():
    try:
        return sorted(os.listdir(pending_dir))
    except FileNotFoundError:
        return []


def decide(rid, choice):
    with open(os.path.join(decisions_dir, rid + ".json"), "w") as handle:
        json.dump({"choice": choice}, handle)


check("plugin loaded", "hermes_plugins.phone_approvals" in sys.modules)

# 1. Marker written, secret redacted, modes tight.
settles = []
secret = "sk-ant-api03-" + "A" * 40
t1, out1 = raise_approval("sess-a", "curl -H 'x-api-key: %s' https://example.com && rm -rf build" % secret, settles)
names = wait_for(markers)
check("marker written", len(names) == 1)
rid = names[0][:-5] if names else ""
check("marker id is the live request id", rid in {e["request_id"] for e in approval.list_gateway_approvals("sess-a")})
with open(os.path.join(pending_dir, names[0])) as handle:
    marker = json.load(handle)
check("secret not in marker", secret not in json.dumps(marker))
check("command kept", "rm -rf build" in marker["command"])
check("marker carries session and pid", marker["session_key"] == "sess-a" and marker["pid"] == os.getpid())
check("dir mode 700", oct(os.stat(base).st_mode & 0o777) == "0o700")
check("file mode 600", oct(os.stat(os.path.join(pending_dir, names[0])).st_mode & 0o777) == "0o600")

# 2. Bad choice and foreign id are ignored; the prompt stays open.
decide(rid, "always")
decide("ffffffffffffffffffffffffffffffff", "once")
time.sleep(2.5)
check("'always' from the phone ignored", t1.is_alive() and not os.path.exists(os.path.join(decisions_dir, rid + ".json")))
check("unknown request id not consumed", os.path.exists(os.path.join(decisions_dir, "ffffffffffffffffffffffffffffffff.json")))

# 3. "once" resolves, the desktop's settle hook fires, marker goes.
decide(rid, "once")
t1.join(5)
check("once resolved the wait", out1.get("d", {}).get("choice") == "once" and out1["d"].get("resolved"))
check("desktop prompt withdrawn (settle=resolved)", (rid, "resolved") in settles)
check("marker removed after answer", wait_for(lambda: not markers()))

# 4. "deny" works too.
settles = []
t2, out2 = raise_approval("sess-b", "git push --force", settles)
names = wait_for(markers)
decide(names[0][:-5], "deny")
t2.join(5)
check("deny resolved the wait", out2.get("d", {}).get("choice") == "deny")

# 5. Answered on the desktop: marker goes, a late phone decision does nothing.
settles = []
t3, out3 = raise_approval("sess-c", "rm -rf /tmp/x", settles)
names = wait_for(markers)
rid3 = names[0][:-5]
approval.resolve_gateway_approval("sess-c", "deny", request_id=rid3)
t3.join(5)
check("desktop answer clears marker", wait_for(lambda: not markers()))
decide(rid3, "once")
time.sleep(1.5)
check("late phone answer did not change the result", out3["d"]["choice"] == "deny")

# 6. Timeout: marker goes, result is the normal unresolved deny.
t4, out4 = raise_approval("sess-d", "shutdown -h now", [])
wait_for(markers)
t4.join(10)
check("timeout leaves no marker", wait_for(lambda: not markers()))
check("timeout is unresolved", out4["d"]["resolved"] is False)

# 7. Phone runs (api_server) get no marker: their card comes from the run stream.
from gateway.session_context import clear_session_vars, set_session_vars  # noqa: E402

holder = {}


def api_run():
    tokens = set_session_vars(platform="api_server", session_key="sess-e")
    try:
        approval.register_gateway_notify("sess-e", lambda data: None)
        holder["d"] = _await_gateway_decision("sess-e", lambda d: None, {
            "command": "rm -rf y", "description": "d", "pattern_key": "k", "pattern_keys": ["k"]})
    finally:
        clear_session_vars(tokens)


t5 = threading.Thread(target=api_run, daemon=True)
t5.start()
time.sleep(1.5)
check("no marker for a phone-started run", not markers())
approval.resolve_gateway_approval("sess-e", "deny")
t5.join(5)

# 8. Clarify questions, through Hermes's real clarify tool and desktop bridge
# (tools.clarify_tool -> tui_gateway.server._clarify_block -> server_requests).
from tools.clarify_tool import clarify_tool  # noqa: E402
from tui_gateway import server as tui_server, server_requests  # noqa: E402

cl_pending = os.path.join(base, "clarify", "pending")
cl_decisions = os.path.join(base, "clarify", "decisions")
emitted = []
server_requests._emit = lambda event, sid, payload: emitted.append((event, sid, payload))
server_requests._write = lambda frame: emitted.append(("frame", None, frame))  # no desktop on stdout here
tui_server._sessions["sid-clar"] = {"session_key": "sess-clar"}


def cl_markers():
    try:
        return [n for n in os.listdir(cl_pending) if n.endswith(".json")]
    except OSError:
        return []


def ask(questions, out):
    out["r"] = json.loads(clarify_tool(questions, callback=lambda qs: tui_server._clarify_block("sid-clar", qs)))


out6 = {}
t6 = threading.Thread(target=ask, args=([
    {"question": "Which house?", "choices": ["Home", "Cabin"]},
    {"question": "Which checks?", "choices": ["Locks", "Cameras", "Lights"], "multi_select": True},
    {"question": "Anything else?"}], out6), daemon=True)
t6.start()
names = wait_for(cl_markers)
check("clarify: marker written", len(names) == 1)
with open(os.path.join(cl_pending, names[0]), encoding="utf-8") as handle:
    marker = json.load(handle)
check("clarify: marker names the session and the questions",
      marker["session_key"] == "sess-clar" and marker["kind"] == "clarify"
      and [q["qid"] for q in marker["questions"]] == ["q0", "q1", "q2"]
      and marker["questions"][1]["multi_select"] is True and len(marker["questions"][0]["choices"]) >= 2)
with open(os.path.join(cl_decisions, marker["request_id"] + ".json"), "w") as handle:
    json.dump({"answers": {"q0": "Cabin", "q1": ["Locks", "Lights"], "q2": None}}, handle)
t6.join(10)
resp = (out6.get("r") or {}).get("responses") or []
check("clarify: the phone's answers reach the agent",
      out6.get("r", {}).get("outcome") == "submitted" and resp[0]["user_response"] == "Cabin"
      and resp[1]["user_response"] == ["Locks", "Lights"] and resp[2]["status"] == "skipped")
check("clarify: the desktop card is withdrawn", any(e[0] == "request.cancel" and e[2].get("reason") == "answered"
                                                   for e in emitted))
check("clarify: marker cleared after", wait_for(lambda: not cl_markers()))

# An answer the desktop already locked is kept.
out7 = {}
t7 = threading.Thread(target=ask, args=([{"question": "A?", "choices": ["x", "y"]},
                                         {"question": "B?", "choices": ["x", "y"]}], out7), daemon=True)
t7.start()
names = wait_for(cl_markers)
rid = names[0][:-5] if names else ""
server_requests.lock_answer(rid, "q0", "y")
with open(os.path.join(cl_decisions, rid + ".json"), "w") as handle:
    json.dump({"answers": {"q0": "x", "q1": "x"}}, handle)
t7.join(10)
resp = (out7.get("r") or {}).get("responses") or []
check("clarify: desktop's locked answer kept, the rest from the phone",
      len(resp) == 2 and resp[0]["user_response"] == "y" and resp[1]["user_response"] == "x")

check("clarify: marker cleared after the mixed answer", wait_for(lambda: not cl_markers()))

# A decision for a request that is not open makes no marker and answers nothing.
with open(os.path.join(cl_decisions, "srq-000000000000.json"), "w") as handle:
    json.dump({"answers": {"q0": "x"}}, handle)
time.sleep(1.0)
check("clarify: no marker for a request that is not open", not cl_markers())
os.remove(os.path.join(cl_decisions, "srq-000000000000.json"))

print("%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
