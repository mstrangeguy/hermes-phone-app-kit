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

# 9. Hermes v0.22+: the Desktop attaches to the gateway, which asks through its
# own stores, not tui_gateway (section 8 still passes there, but proves nothing
# for the Desktop). Skipped on v0.21.
try:
    from gateway import session_server_requests as ssr  # noqa: E402
    from gateway.session_clarify_batch import ask_clarify_batch  # noqa: E402
    from gateway.session_events import SessionEvents  # noqa: E402
    from gateway.session_pending_controls import PendingControls  # noqa: E402
    from tools import approval_context, clarify_gateway  # noqa: E402
    V22 = hasattr(clarify_gateway, "get_pending")
except ImportError:
    V22 = False

agent_dir = os.environ.get("HERMES_AGENT_DIR")
if agent_dir:
    check("testing the Hermes checkout in HERMES_AGENT_DIR",
          os.path.realpath(approval.__file__).startswith(os.path.realpath(agent_dir) + os.sep))

if not V22:
    print("SKIP v0.22 gateway clarify cases (this Hermes has no gateway clarify stores)")
else:
    import types  # noqa: E402
    import uuid  # noqa: E402

    class Viewer:
        """A Desktop attached to the session's stream: takes every frame."""
        def __init__(self):
            self.frames = []

        def write(self, obj):
            self.frames.append(obj)
            return True

        def close(self):
            pass

    def stream():
        events = SessionEvents()
        viewer = Viewer()
        events.fanout.attach(viewer)
        published = []
        events.observers.add(published.append)
        return events, viewer, published

    def as_turn(sid, key, platform, fn):
        # What the gateway binds around a turn: the session vars and the
        # approval key (the human-input hook reads both).
        tokens = set_session_vars(platform=platform, session_key=key, session_id=sid)
        key_token = approval_context.set_current_session_key(key)
        try:
            return fn()
        finally:
            approval_context.reset_current_session_key(key_token)
            clear_session_vars(tokens)

    def published_types(published):
        return [(f["params"]["type"], f["params"]["payload"]) for f in published]

    def marker_for(rid):
        try:
            with open(os.path.join(cl_pending, rid + ".json"), encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def answer(rid, answers):
        with open(os.path.join(cl_decisions, rid + ".json"), "w") as handle:
            json.dump({"answers": answers}, handle)

    # 9a. Batch (the Desktop composer's turns): gateway.session_server_requests.
    SID_A, KEY_A = "20261102_080000_aaaa11", "agent:main:local:dm:20261102_080000_aaaa11"
    events_a, viewer_a, published_a = stream()
    turn_a = types.SimpleNamespace(
        _approval_owner=(types.SimpleNamespace(sessions={SID_A: types.SimpleNamespace(event_stream=events_a)}),
                         SID_A, 1),
        _ctx=types.SimpleNamespace(agent_holder=[None]))

    def ask_batch(questions, out):
        out["r"] = json.loads(as_turn(SID_A, KEY_A, "local", lambda: clarify_tool(
            questions, callback=lambda qs: ask_clarify_batch(turn_a, qs))))

    out9 = {}
    t9 = threading.Thread(target=ask_batch, args=([
        {"question": "Which house?", "choices": ["Home", "Cabin"]},
        {"question": "Which checks?", "choices": ["Locks", "Cameras", "Lights"], "multi_select": True},
        {"question": "Anything else?"}], out9), daemon=True)
    t9.start()
    names = wait_for(cl_markers)
    check("v0.22 batch: marker written", len(names) == 1)
    rid = names[0][:-5] if names else ""
    marker = marker_for(rid) or {}
    check("v0.22 batch: marker is the open request", rid in ssr._open and rid.startswith("srq-"))
    check("v0.22 batch: marker names the chat by session id, with the questions",
          marker.get("session_key") == SID_A and marker.get("kind") == "clarify"
          and [q["qid"] for q in marker.get("questions", [])] == ["q0", "q1", "q2"]
          and marker["questions"][1]["multi_select"] is True and len(marker["questions"][0]["choices"]) >= 2)
    check("v0.22 batch: the Desktop got the request", any(f.get("id") == rid for f in viewer_a.frames))
    answer(rid, {"q0": "Cabin", "q1": ["Locks", "Lights"], "q2": None})
    t9.join(10)
    resp = (out9.get("r") or {}).get("responses") or []
    check("v0.22 batch: the phone's answers reach the agent",
          out9.get("r", {}).get("outcome") == "submitted" and len(resp) == 3
          and resp[0]["user_response"] == "Cabin" and resp[1]["user_response"] == ["Locks", "Lights"]
          and resp[2]["status"] == "skipped")
    check("v0.22 batch: the Desktop card is withdrawn",
          any(t == "request.cancel" and p.get("id") == rid and p.get("reason") == "resolved"
              for t, p in published_types(published_a)))
    check("v0.22 batch: marker cleared after", wait_for(lambda: not cl_markers()))

    # The Desktop answers one question first (its clarify.lock), the phone the rest.
    out10 = {}
    t10 = threading.Thread(target=ask_batch, args=([{"question": "A?", "choices": ["x", "y"]},
                                                    {"question": "B?", "choices": ["x", "y"]}], out10), daemon=True)
    t10.start()
    names = wait_for(cl_markers)
    rid = names[0][:-5] if names else ""
    ssr.lock_answer(rid, "q0", "y", session_ok=lambda session_id, events: True)
    check("v0.22 batch: marker shows the Desktop's answer",
          wait_for(lambda: (marker_for(rid) or {}).get("answered") == ["q0"]))
    answer(rid, {"q0": "x", "q1": "x"})
    t10.join(10)
    resp = (out10.get("r") or {}).get("responses") or []
    check("v0.22 batch: Desktop's locked answer kept, the rest from the phone",
          len(resp) == 2 and resp[0]["user_response"] == "y" and resp[1]["user_response"] == "x")
    check("v0.22 batch: marker cleared after the mixed answer", wait_for(lambda: not cl_markers()))

    # The Desktop answers everything first: its answers win, a late phone answer does nothing.
    out11 = {}
    t11 = threading.Thread(target=ask_batch, args=([{"question": "C?", "choices": ["x", "y"]}], out11), daemon=True)
    t11.start()
    names = wait_for(cl_markers)
    rid = names[0][:-5] if names else ""
    ssr.lock_answer(rid, "q0", "y", session_ok=lambda session_id, events: True)
    t11.join(10)
    check("v0.22 batch: Desktop answering first clears the marker", wait_for(lambda: not cl_markers()))
    answer(rid, {"q0": "x"})
    time.sleep(1.5)
    resp = (out11.get("r") or {}).get("responses") or []
    check("v0.22 batch: Desktop answering first wins",
          len(resp) == 1 and resp[0]["user_response"] == "y" and not cl_markers())
    stale = os.path.join(cl_decisions, rid + ".json")
    if os.path.exists(stale):
        os.remove(stale)

    # 9b. One card per question (other local-viewer turns, e.g. a phone message
    # sent into a Desktop chat): tools.clarify_gateway. This is what
    # gateway/run_turn_runner.py _ask_clarify_question does, minus the
    # messaging adapter: register, project to the Desktop, wait, re-read.
    SID_B, KEY_B = "20261102_080500_bbbb22", "agent:main:local:dm:20261102_080500_bbbb22"
    events_b, viewer_b, published_b = stream()
    controls = PendingControls(events_b)

    def one_card_each(questions):
        answers = {}
        reply = {"answers": answers, "outcome": "submitted"}
        for item in questions:
            cid = uuid.uuid4().hex[:10]
            entry = clarify_gateway.register(clarify_id=cid, session_key=KEY_B, question=item["question"],
                                             choices=item["choices"] or None,
                                             multi_select=bool(item["multi_select"]))
            controls.register_clarify(SID_B, 1, entry)
            raw = clarify_gateway.wait_for_response(cid, 10)
            controls.snapshot(SID_B, 1)
            if raw is None or raw == clarify_gateway.CANCELLED:
                reply["outcome"] = "timed_out"
                break
            answers[item["qid"]] = None if raw == clarify_gateway.SKIPPED else raw
        return reply

    def ask_single(questions, out, platform="local"):
        out["r"] = json.loads(as_turn(SID_B, KEY_B, platform, lambda: clarify_tool(
            questions, callback=one_card_each)))

    def settled(cid):
        return any(t == "clarify.settled" and p.get("prompt_id") == cid for t, p in published_types(published_b))

    out12 = {}
    t12 = threading.Thread(target=ask_single, args=([
        {"question": "Which house?", "choices": ["Home", "Cabin"]},
        {"question": "Which checks?", "choices": ["Locks", "Cameras", "Lights"], "multi_select": True}],
        out12), daemon=True)
    t12.start()
    names = wait_for(cl_markers)
    check("v0.22 single: marker written", len(names) == 1)
    cid1 = names[0][:-5] if names else ""
    marker = marker_for(cid1) or {}
    check("v0.22 single: marker is the open card, by session id",
          clarify_gateway.get_pending(cid1) is not None and marker.get("session_key") == SID_B
          and [q["qid"] for q in marker.get("questions", [])] == ["q0"]
          and marker["questions"][0]["choices"][-1] == "Cabin")
    answer(cid1, {"q0": "Cabin"})
    check("v0.22 single: the Desktop card is withdrawn", wait_for(lambda: settled(cid1)))
    names = wait_for(lambda: [n for n in cl_markers() if n != cid1 + ".json"])
    cid2 = names[0][:-5] if names else ""
    marker = marker_for(cid2) or {}
    check("v0.22 single: the next question gets its own marker",
          len(names) == 1 and marker.get("questions", [{}])[0].get("multi_select") is True)
    answer(cid2, {"q0": ["Locks", "Lights"]})
    t12.join(10)
    resp = (out12.get("r") or {}).get("responses") or []
    check("v0.22 single: the phone's answers reach the agent",
          out12.get("r", {}).get("outcome") == "submitted" and len(resp) == 2
          and resp[0]["user_response"] == "Cabin" and resp[1]["user_response"] == ["Locks", "Lights"])
    check("v0.22 single: last Desktop card withdrawn", settled(cid2))
    check("v0.22 single: markers cleared after", wait_for(lambda: not cl_markers()))

    # The Desktop answers first, through its own route (clarify.respond).
    out13 = {}
    t13 = threading.Thread(target=ask_single, args=([{"question": "D?", "choices": ["x", "y"]}], out13),
                           daemon=True)
    t13.start()
    names = wait_for(cl_markers)
    cid = names[0][:-5] if names else ""
    status = controls.respond(SID_B, 1, cid, {"answer": "y"}, kind="clarify")
    t13.join(10)
    check("v0.22 single: Desktop answering first clears the marker",
          status.get("status") == "resolved" and wait_for(lambda: not cl_markers()))
    answer(cid, {"q0": "x"})
    time.sleep(1.5)
    resp = (out13.get("r") or {}).get("responses") or []
    check("v0.22 single: Desktop answering first wins", len(resp) == 1 and resp[0]["user_response"] == "y")
    if os.path.exists(os.path.join(cl_decisions, cid + ".json")):
        os.remove(os.path.join(cl_decisions, cid + ".json"))

    # A phone-started run's question has its own card on the run stream.
    out14 = {}
    t14 = threading.Thread(target=ask_single, args=([{"question": "E?", "choices": ["x", "y"]}], out14,
                                                    "api_server"), daemon=True)
    t14.start()
    wait_for(lambda: clarify_gateway._entries)
    time.sleep(1.5)
    check("v0.22 single: no marker for a phone-started run", not cl_markers())
    for pending_cid in list(clarify_gateway._entries):
        clarify_gateway.resolve_gateway_clarify(pending_cid, "x")
    t14.join(10)

    # 9c. Approvals on v0.22: the gateway key is not a chat id the phone knows,
    # so the marker carries the session id; the phone still answers it.
    SID_C, KEY_C = "20261102_081000_cccc33", "agent:main:local:dm:20261102_081000_cccc33"
    holder = {}

    def gw_approval():
        def run():
            approval.register_gateway_notify(KEY_C, lambda data: None)
            holder["d"] = _await_gateway_decision(KEY_C, lambda data: None, {
                "command": "git push --force", "description": "d", "pattern_key": "k", "pattern_keys": ["k"]})
        as_turn(SID_C, KEY_C, "local", run)

    t15 = threading.Thread(target=gw_approval, daemon=True)
    t15.start()
    names = wait_for(markers)
    with open(os.path.join(pending_dir, names[0])) as handle:
        marker = json.load(handle)
    check("v0.22 approval: marker names the chat by session id", marker["session_key"] == SID_C)
    decide(names[0][:-5], "once")
    t15.join(5)
    check("v0.22 approval: the phone's answer resolves it", holder.get("d", {}).get("choice") == "once")
    check("v0.22 approval: marker removed after answer", wait_for(lambda: not markers()))

print("%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
