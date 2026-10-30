"""Offline test of sending into Desktop chats through the gateway (Hermes v0.22+).

Usage: python3 -I tests/desktopsend_test.py <repo-dir> <empty-scratch-dir>

No real Hermes gateway is used. A fake gateway client is injected into
desktop_send.py two ways: directly (its send() with a fake connect), and as a
fake hermes_cli/gateway_client.py in a scratch "agent" folder, so the real
subprocess path (serve.py -> scripts/py -I desktop_send.py) runs end to end.

Covers: prompt.submit carries attachments for every picture (regression: the
gateway silently drops a picture without them); each refusal maps to its text;
no prompt.submit retry after a send error or a deadline; capability false when
the import fails, true when it works; /phone-api/desktop-send refuses pictures
that did not come from /phone-api/upload (other files, other profiles); no
message text, paths, session id or key in serve.log; and, in node, the page
rules in chatparts.js (route choice, picture-line matching, follow steps), plus
the app.js wiring (Stop and guidance hidden, /v1/runs path kept).
"""
import asyncio
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading

repo, work = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
os.makedirs(work, exist_ok=True)
for name in [k for k in os.environ if k.startswith("HERMES_SESSION_")]:
    del os.environ[name]
sys.path.insert(0, repo)
import serve  # noqa: E402
import faceid  # noqa: E402
import desktop_send  # noqa: E402

serve.CONFIG_PATH = os.path.join(work, "config.json")
faceid.CONFIG_PATH = os.path.join(work, "faceid.json")
faceid.SETUP_PATH = os.path.join(work, "faceid-setup.json")
for _path in (faceid.CONFIG_PATH, faceid.SETUP_PATH, serve.CONFIG_PATH):
    _scratch = os.path.realpath(work)
    if os.path.commonpath([os.path.realpath(_path), _scratch]) != _scratch:
        print("FAIL ISOLATION: would read live file %s" % _path)
        sys.exit(1)

results = []


def check(name, ok):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
SECRET_TEXT = "the alarm code is 4417 zebra"


# --- 1. The helper with a fake client --------------------------------------------
class FakeRPCError(ValueError):
    def __init__(self, reason, data=None):
        super().__init__(reason)
        self.data = data or {}


class FakeClient:
    def __init__(self, script):
        self.script = script
        self.calls = []

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        action = self.script.get(method)
        if callable(action):
            return await action(params) if asyncio.iscoroutinefunction(action) else action(params)
        if isinstance(action, BaseException):
            raise action
        if action is not None:
            return action
        if method == "session.resume":
            return {"session_id": "tip-" + params["session_id"]}
        if method == "image.attach_bytes":
            n = sum(1 for m, _p in self.calls if m == "image.attach_bytes")
            return {"attached": True, "path": "/gw/native-inputs/pic%d.%s" % (n, params["filename"].rsplit(".", 1)[-1])}
        if method == "prompt.submit":
            return {"status": "queued", "admission_id": "adm-1"}
        return {}


def fake_connect(client, fail=None):
    class Ctx:
        async def __aenter__(self):
            if fail:
                raise fail
            return client

        async def __aexit__(self, *exc):
            return False
    return lambda: Ctx()


def run(request, script=None, fail=None, timeout=None):
    client = FakeClient(script or {})
    connect = fake_connect(client, fail)
    if timeout is None:
        out = asyncio.run(desktop_send.send(request, connect, FakeRPCError))
    else:
        out = asyncio.run(desktop_send.send_bounded(request, connect, FakeRPCError, timeout=timeout))
    return out, client


def submits(client):
    return [p for m, p in client.calls if m == "prompt.submit"]


pics = os.path.join(work, "pics")
os.makedirs(pics, exist_ok=True)
p1, p2 = os.path.join(pics, "phone_a.png"), os.path.join(pics, "phone b.jpg")
with open(p1, "wb") as handle:
    handle.write(PNG)
with open(p2, "wb") as handle:
    handle.write(JPEG)

out, client = run({"session_id": "s1", "text": "what is this", "pictures": [p1, p2]})
methods = [m for m, _p in client.calls]
check("helper: ok with admission id", out.get("ok") is True and out.get("admission_id") == "adm-1"
      and out.get("status") == "queued")
check("helper: resume, attach each picture, then one submit",
      methods == ["session.resume", "image.attach_bytes", "image.attach_bytes", "prompt.submit"])
check("helper: attaches and submits on the session resume returned",
      all(p.get("session_id") == "tip-s1" for m, p in client.calls if m != "session.resume")
      and out.get("session_id") == "tip-s1")
sub = submits(client)[0]
check("helper: prompt.submit carries attachments for every picture (no silent drop)",
      sub.get("attachments") == [{"path": "/gw/native-inputs/pic1.png", "mime": "image/png"},
                                 {"path": "/gw/native-inputs/pic2.jpg", "mime": "image/jpeg"}])
check("helper: picture bytes sent as base64 with a filename",
      [p.get("content_base64") for m, p in client.calls if m == "image.attach_bytes"] ==
      [__import__("base64").b64encode(PNG).decode(), __import__("base64").b64encode(JPEG).decode()])
check("helper: fresh submission id", str(sub.get("submission_id", "")).startswith("phone-")
      and len(sub["submission_id"]) > 20)
check("helper: text unchanged with the @image: flag off", sub.get("text") == "what is this")
_out2, client2 = run({"session_id": "s1", "text": "what is this", "pictures": [p1, p2]})
check("helper: a new submission id per send", submits(client2)[0]["submission_id"] != sub["submission_id"])

out, client = run({"session_id": "s1", "text": "just words"})
check("helper: text only has no attachments", "attachments" not in submits(client)[0] and out["ok"])

out, client = run({"session_id": "s1", "text": "look", "pictures": [p1, p2], "image_refs": True})
check("helper: @image: flag on adds quoted lines",
      submits(client)[0]["text"] == "look\n@image:%s\n@image:`%s`" % (p1, p2))

# Refusals, each to its text. The owner names the reason in data.reason (message may be display text).
coord = FakeRPCError("Session is open elsewhere", {"reason": "runtime_coordination_required"})
out, client = run({"session_id": "s1", "text": SECRET_TEXT}, {"session.resume": coord})
check("refusal: open on another Desktop -> its text",
      out.get("ok") is False and out.get("message") == desktop_send.TEXT_COORDINATION
      and "another Desktop" in out["message"])
check("refusal: nothing submitted when resume is refused", not submits(client))
out, _c = run({"session_id": "s1", "text": "x"}, {"session.resume": FakeRPCError("not_found")})
check("refusal: not_found -> can't open from the phone", out.get("message") == "Hermes can't open this chat from the phone.")
out, client = run({"session_id": "s1", "text": "x"}, {"prompt.submit": FakeRPCError("denied", {"reason": "permission_denied"})})
check("refusal: other reason -> 'Hermes refused the message (<reason>).'",
      out.get("message") == "Hermes refused the message (permission_denied)." and len(submits(client)) == 1)
out, _c = run({"session_id": "s1", "text": "x"}, {"session.resume": FakeRPCError("weird text with spaces!")})
check("refusal: free text reason is not echoed", out.get("message") == "Hermes refused the message (request_failed).")
out, client = run({"session_id": "s1", "text": "x", "pictures": [p1]},
                  {"image.attach_bytes": FakeRPCError("unsupported", {"reason": "invalid_params"})})
check("refusal: attach refused -> nothing submitted", out.get("ok") is False and not submits(client))
for reason in ("not_found", "runtime_coordination_required", "queue_full"):
    check("refusal map: %s" % reason, desktop_send.refusal(reason)["reason"] == reason)

# Never a second submit.
out, client = run({"session_id": "s1", "text": "x"}, {"prompt.submit": ConnectionError("lost")})
check("no retry: lost answer after submit -> unconfirmed, one submit",
      out.get("reason") == "unconfirmed" and len(submits(client)) == 1 and "may still arrive" in out["message"])
out, client = run({"session_id": "s1", "text": "x"}, {"prompt.submit": FakeRPCError("x", {"reason": "storage_unavailable"})})
check("no retry: refusal that may follow the commit -> unconfirmed, one submit",
      out.get("reason") == "unconfirmed" and len(submits(client)) == 1)


async def hang(_params):
    await asyncio.sleep(30)

out, client = run({"session_id": "s1", "text": "x"}, {"prompt.submit": hang}, timeout=0.3)
check("no retry: deadline after submit -> unconfirmed, one submit",
      out.get("reason") == "unconfirmed" and len(submits(client)) == 1)
out, client = run({"session_id": "s1", "text": "x"}, {"session.resume": hang}, timeout=0.3)
check("deadline before submit -> nothing sent", out.get("reason") == "gateway_unavailable" and not submits(client))
out, client = run({"session_id": "s1", "text": "x"}, fail=OSError("refused"))
check("gateway down -> nothing sent", out.get("reason") == "gateway_unavailable" and "Nothing was sent" in out["message"])
out, client = run({"session_id": "s1", "text": "x"}, {"prompt.submit": {"status": "rejected"}})
check("receipt without admission -> not ok", out.get("ok") is False and out.get("reason") == "not_admitted")
out, client = run({"session_id": "s1", "text": "x", "pictures": [os.path.join(pics, "gone.png")]})
check("missing picture -> refused before connecting", out.get("reason") == "picture_unreadable" and not client.calls)
out, client = run({"session_id": "", "text": "x"})
check("no session id -> refused before connecting", out.get("ok") is False and not client.calls)

# --- 2. The capability check and the subprocess path --------------------------
FAKE_CLIENT = r'''
import contextlib, json, os


class GatewayClientError(ValueError):
    pass


class GatewayRPCError(GatewayClientError):
    def __init__(self, reason, code=None, data=None):
        super().__init__(reason)
        self.code = code
        self.data = data or {}


class _Client:
    async def rpc(self, method, **params):
        mode = os.environ.get("FAKE_GATEWAY_MODE", "ok")
        with open(os.environ["FAKE_GATEWAY_LOG"], "a") as handle:
            handle.write(json.dumps({"method": method, "params": {k: v for k, v in params.items()
                                     if k != "content_base64"}, "home": os.environ.get("HERMES_HOME")}) + "\n")
        if method == "session.resume" and mode == "coordination":
            raise GatewayRPCError("busy elsewhere", 4090, {"reason": "runtime_coordination_required"})
        if method == "session.resume":
            return {"session_id": params["session_id"]}
        if method == "image.attach_bytes":
            return {"attached": True, "path": "/gw/native-inputs/" + params["filename"]}
        if method == "prompt.submit":
            return {"status": "queued", "admission_id": "adm-sub"}
        return {}


@contextlib.asynccontextmanager
async def connect_gateway(endpoint=None, subject=None):
    yield _Client()
'''
agent_none = os.path.join(work, "agent-old")
agent_new = os.path.join(work, "agent-new")
os.makedirs(os.path.join(agent_none, "hermes_cli"), exist_ok=True)
with open(os.path.join(agent_none, "hermes_cli", "__init__.py"), "w") as handle:
    handle.write("")
os.makedirs(os.path.join(agent_new, "hermes_cli"), exist_ok=True)
with open(os.path.join(agent_new, "hermes_cli", "__init__.py"), "w") as handle:
    handle.write("")
with open(os.path.join(agent_new, "hermes_cli", "gateway_client.py"), "w") as handle:
    handle.write(FAKE_CLIENT)
gw_log = os.path.join(work, "gateway-calls.jsonl")
os.environ["FAKE_GATEWAY_LOG"] = gw_log
os.environ["PHONE_APP_PYTHON"] = sys.executable


def helper(args, agent, stdin=b""):
    env = dict(os.environ, HERMES_AGENT_DIR=agent)
    proc = subprocess.run(["/bin/bash", os.path.join(repo, "scripts", "py"), "-I",
                           os.path.join(repo, "desktop_send.py")] + args,
                          input=stdin, capture_output=True, timeout=60, env=env, cwd=work)
    try:
        return json.loads(proc.stdout.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"raw": proc.stdout.decode()[:200], "err": proc.stderr.decode()[-300:]}


check("capability: false when hermes_cli.gateway_client does not import", helper(["--check"], agent_none) == {"ok": False})
check("capability: true when it imports", helper(["--check"], agent_new) == {"ok": True})
out = helper([], agent_none, json.dumps({"session_id": "s1", "text": "x"}).encode())
check("helper on old Hermes: plain refusal, no traceback", out.get("reason") == "no_gateway_client" and out.get("message"))

# --- 3. The proxy endpoint ---------------------------------------------------------
DEFAULT_KEY = "default-key-0123456789"
OTHER_KEY = "otherprofile-key-abcdefgh"
home = os.path.join(work, "home")
other = os.path.join(home, "profiles", "second")
os.makedirs(other, exist_ok=True)
with open(os.path.join(home, ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=%s\n" % DEFAULT_KEY)
with open(os.path.join(other, ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=%s\n" % OTHER_KEY)
allow = os.path.join(work, "allowed-logins.txt")
with open(allow, "w") as handle:
    handle.write("owner@example.com\n")
serve.HERMES_HOME = home
serve.STATE_DB = os.path.join(home, "state.db")
serve.ALLOW_PATH = allow
LOGGED = []
serve.log = LOGGED.append

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
listener.close()
server = serve.Server(("127.0.0.1", port), serve.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def call(path, method="GET", body=None, raw=None, key=DEFAULT_KEY, ctype="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
    h = {"Tailscale-User-Login": "owner@example.com"}
    if key:
        h["Authorization"] = "Bearer " + key
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        h["Content-Type"] = ctype
        h["Content-Length"] = str(len(data))
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    payload = r.read()
    c.close()
    try:
        return r.status, json.loads(payload.decode() or "{}")
    except ValueError:
        return r.status, {}


def cap(agent):
    os.environ["HERMES_AGENT_DIR"] = agent
    serve._DESKTOP_CAP["at"] = None


SEND = "/phone-api/desktop-send"
cap(agent_none)
status, meta = call("/phone-api/meta")
check("meta: desktop_send false on Hermes without the gateway client", status == 200 and meta.get("desktop_send") is False)
cap(agent_new)
status, meta = call("/phone-api/meta")
check("meta: desktop_send true when it imports", status == 200 and meta.get("desktop_send") is True)
check("meta: needs the key", call("/phone-api/meta", key="wrong-key-0000000000")[0] == 401)

status, up = call("/phone-api/upload", "POST", raw=PNG, ctype="image/png")
check("upload: picture saved", status == 201 and os.path.isfile(up.get("path", "")))
uploaded = up.get("path", "")
status, upf = call("/phone-api/upload?kind=file&name=notes.png", "POST", raw=PNG, ctype="application/octet-stream")
check("upload: file saved", status == 201)

open(gw_log, "w").close()
status, body = call(SEND, "POST", {"session_id": "chat1", "text": SECRET_TEXT, "pictures": [uploaded]})
calls = [json.loads(line) for line in open(gw_log) if line.strip()]
check("send: 202 with admission id", status == 202 and body.get("ok") is True and body.get("admission_id") == "adm-sub")
check("send: went through the subprocess helper to the gateway",
      [c["method"] for c in calls] == ["session.resume", "image.attach_bytes", "prompt.submit"])
check("send: submit carries the picture as an attachment",
      calls and calls[-1]["params"].get("attachments") == [{"path": "/gw/native-inputs/" + os.path.basename(uploaded),
                                                           "mime": "image/png"}])
check("send: helper ran with the profile's HERMES_HOME", calls and calls[0]["home"] == os.path.abspath(home))
check("send: text passed as typed (flag off)", calls and calls[-1]["params"]["text"] == SECRET_TEXT)

open(gw_log, "w").close()
not_uploaded = os.path.join(home, "images", "phone_planted.png")
with open(not_uploaded, "wb") as handle:
    handle.write(PNG)
for label, pic in (("a file in images/ the page did not upload", not_uploaded),
                   ("an attachment (not a picture upload)", upf.get("path", "")),
                   ("a path outside the profile", p1),
                   ("a relative path", "images/" + os.path.basename(uploaded)),
                   ("a traversal path", os.path.join(home, "images", "..", "images", "..", ".env"))):
    status, body = call(SEND, "POST", {"session_id": "chat1", "text": "x", "pictures": [pic]})
    check("refused: %s" % label, status == 400 and body.get("error", {}).get("code") == "not_uploaded")
status, body = call(SEND + "?profile=second", "POST", {"session_id": "chat1", "text": "x", "pictures": [uploaded]})
check("refused: another profile's upload", status == 400 and body.get("error", {}).get("code") == "not_uploaded")
check("refused sends never reached the gateway", os.path.getsize(gw_log) == 0)

status, body = call(SEND, "POST", {"session_id": "../etc", "text": "x"})
check("refused: bad session id", status == 400)
status, body = call(SEND, "POST", {"session_id": "chat1", "text": "  "})
check("refused: empty message", status == 400)
check("refused: wrong key", call(SEND, "POST", {"session_id": "chat1", "text": "x"}, key="wrong-key-0000000000")[0] == 401)
check("refused: unknown profile", call(SEND + "?profile=nobody", "POST", {"session_id": "chat1", "text": "x"})[0] == 404)
status, _b = call(SEND, "POST", raw=b"{" + b" " * (serve.DESKTOP_BODY_MAX + 10) + b"}")
check("refused: body too large", status == 400)

os.environ["FAKE_GATEWAY_MODE"] = "coordination"
status, body = call(SEND, "POST", {"session_id": "chat1", "text": SECRET_TEXT})
check("refusal reaches the page as its text, never a bare permission denied",
      status == 409 and body.get("error", {}).get("message") == desktop_send.TEXT_COORDINATION)
os.environ["FAKE_GATEWAY_MODE"] = "ok"

cap(agent_none)
status, body = call(SEND, "POST", {"session_id": "chat1", "text": "x"})
check("old Hermes: endpoint says it is not supported", status == 501 and body.get("error", {}).get("code") == "no_gateway_client")

with open(serve.CONFIG_PATH, "w") as handle:
    json.dump({"desktop_image_refs": True}, handle)
cap(agent_new)
open(gw_log, "w").close()
status, up2 = call("/phone-api/upload", "POST", raw=PNG, ctype="image/png")
status, body = call(SEND, "POST", {"session_id": "chat1", "text": "look", "pictures": [up2["path"]]})
calls = [json.loads(line) for line in open(gw_log) if line.strip()]
check("config desktop_image_refs: @image: line added", status == 202 and calls and
      calls[-1]["params"]["text"] == "look\n@image:" + up2["path"])
os.remove(serve.CONFIG_PATH)

# The log: one line per send, nothing secret.
sends = [line for line in LOGGED if line.startswith("desktop send:")]
check("log: one line per send", sends.count("desktop send: queued, 1 picture") == 2 and
      "desktop send: queued, 0 pictures" not in sends and
      any(line == "desktop send: refused runtime_coordination_required, 0 pictures" for line in sends))
blob = "\n".join(LOGGED)
check("log: no message text", SECRET_TEXT not in blob and "look" not in blob)
check("log: no paths or file names", home not in blob and work not in blob and
      os.path.basename(uploaded) not in blob and "native-inputs" not in blob)
check("log: no session id or key", "chat1" not in blob and DEFAULT_KEY not in blob and OTHER_KEY not in blob)
server.shutdown()


# --- 4. The page rules, run in node ------------------------------------------------
def find_node():
    candidates = [os.environ.get("NODE") or "", shutil.which("node") or ""]
    agent = os.environ.get("HERMES_AGENT_DIR") or ""
    if agent:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(agent)), "node", "bin", "node"))
    candidates.append(os.path.expanduser("~/.hermes/node/bin/node"))
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


JS = r"""
const fs = require("fs");
const vm = require("vm");
const box = {};
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), box);
const P = box.PhoneParts;
const out = [];
const ok = (name, cond) => out.push([name, Boolean(cond)]);

ok("route: Desktop chat with the gateway client", P.gatewayRoute(true, "desktop"));
ok("route: CLI and Telegram chats too", P.gatewayRoute(true, "cli") && P.gatewayRoute(true, "telegram"));
ok("route: phone chats keep /v1/runs", !P.gatewayRoute(true, "api_server") && !P.gatewayRoute(true, "API_SERVER"));
ok("route: Bot Chats keep /v1/runs", !P.gatewayRoute(true, "bot_room"));
ok("route: older Hermes keeps /v1/runs", !P.gatewayRoute(false, "desktop") && !P.gatewayRoute(undefined, "desktop"));
ok("route: unknown source keeps /v1/runs", !P.gatewayRoute(true, "") && !P.gatewayRoute(true, null));

ok("core: gateway picture lines dropped",
  P.coreText("what is this\n[Image attached at: /x/native-inputs/a.png]\n[screenshot]") === "what is this");
ok("core: phone @image: lines dropped, @file: kept",
  P.coreText("@file:/a/b.txt\nhi\n@image:`/a b.png`") === "@file:/a/b.txt\nhi");

const before = [{ id: 40, role: "user", content: "what is this" }, { id: 41, role: "assistant", content: "A cat." }];
const sent = { text: "what is this", afterRowId: 41 };
let g = P.gatewayProgress(before, sent);
ok("progress: an earlier same message is not this one", !g.saved && !g.replied);
const saved = before.concat([{ id: 50, role: "user", content: "what is this\n[Image attached at: /p.png]\n[screenshot]" }]);
g = P.gatewayProgress(saved, sent);
ok("progress: saved with the gateway's picture lines", g.saved && !g.replied);
const replied = saved.concat([{ id: 51, role: "assistant", content: "", tool_calls: [{}] },
  { id: 52, role: "tool", content: "ok" }, { id: 53, role: "assistant", content: [{ type: "text", text: "It reads 42." }] }]);
g = P.gatewayProgress(replied, sent);
ok("progress: replied after tool steps", g.saved && g.replied);
g = P.gatewayProgress([{ id: 60, role: "user", content: "what is this (edited)" }], sent);
ok("progress: first new user row counts when the words differ", g.saved && !g.replied);
g = P.gatewayProgress([{ id: 60, role: "user", content: "x", display_kind: "steer" }], sent);
ok("progress: a Hermes-made user row is not the message", !g.saved);
g = P.gatewayProgress([{ id: 70, role: "tool", content: "r" }, { id: 71, role: "assistant", content: "done" }], sent);
ok("progress: user row scrolled out of the window", g.saved && g.replied);

const s0 = P.gatewayStep({ saved: false, replied: false }, false, {}, 1000, 120000);
ok("step: not saved, no lease -> queued", s0.state === "queued");
ok("step: queued too long -> stalled", P.gatewayStep({ saved: false }, false, s0, 1000 + 120000, 120000).state === "stalled");
ok("step: a lease resets the stall clock", P.gatewayStep({ saved: false }, true, s0, 999999, 120000).state === "working"
  && P.gatewayStep({ saved: false }, false, P.gatewayStep({ saved: false }, true, s0, 5000, 120000), 6000, 120000).state === "queued");
let s = P.gatewayStep({ saved: true, replied: false }, true, s0, 2000, 120000);
ok("step: lease held -> working", s.state === "working" && s.seenBusy);
ok("step: lease gone after it was seen -> done", P.gatewayStep({ saved: true, replied: false }, false, s, 3000, 120000).state === "done");
ok("step: saved and replied, no lease -> done", P.gatewayStep({ saved: true, replied: true }, false, {}, 1, 1).state === "done");
s = P.gatewayStep({ saved: true }, false, {}, 1, 120000);
ok("step: saved, no reply, lease never seen -> waits", s.state === "working" && s.quiet === 1);
s = P.gatewayStep({ saved: true }, false, P.gatewayStep({ saved: true }, false, s, 2, 120000), 3, 120000);
ok("step: ... done after 3 quiet looks", s.state === "done");
ok("step: replied but lease still held -> not done", P.gatewayStep({ saved: true, replied: true }, true, {}, 1, 1).state === "working");
process.stdout.write(JSON.stringify(out));
"""

node = find_node()
check("node found (PATH or Hermes's own)", node)
if node:
    harness = os.path.join(work, "gateway_harness.js")
    with open(harness, "w") as handle:
        handle.write(JS)
    proc = subprocess.run([node, harness, os.path.join(repo, "page", "chatparts.js")],
                          capture_output=True, text=True, timeout=60, cwd=work)
    try:
        js_results = json.loads(proc.stdout or "[]")
    except ValueError:
        js_results = [["node output parsed: " + (proc.stderr or proc.stdout)[:300], False]]
    check("node ran the rules", proc.returncode == 0 and len(js_results) >= 23)
    for name, ok in js_results:
        check(name, ok)


# --- 5. Wiring ---------------------------------------------------------------------
def read(rel):
    with open(os.path.join(repo, rel), encoding="utf-8") as handle:
        return handle.read()


app = read("page/app.js")
html = read("page/index.html")
send_fn = app[app.index("async function sendMessage("):app.index("// --- Chats started on a Desktop")]
check("page: route picked with the capability and the chat's source",
      "PhoneParts.gatewayRoute(await canDesktopSend(run.profile), await chatSource(run.profile, sessionId))" in send_fn)
check("page: route picked before the uploads", send_fn.index("gatewayRoute(") < send_fn.index("uploadPicture("))
check("page: gateway pictures go by path, not inline", re.search(r'run\.via === "gateway"\) \{\s*//[^\n]*\n\s*gatewayPics\.push\(path\)', send_fn))
check("page: gateway send returns before /v1/runs",
      send_fn.index("sendViaGateway(run, input, gatewayPics)") < send_fn.index('"/v1/runs"'))
check("page: other chats keep /v1/runs with an Idempotency-Key",
      re.search(r'inProfile\(run\.profile, "/v1/runs"\),\s*\{\s*method: "POST",\s*headers: \{ "Idempotency-Key"', send_fn))
check("page: posts to /phone-api/desktop-send", '"/phone-api/desktop-send?profile="' in app)
check("page: capability read from /phone-api/meta", "data.desktop_send === true" in app)
check("page: Stop and guidance hidden for gateway sends",
      '$("stop").hidden = !steerable' in app and 'run.via !== "gateway"' in app and
      'run.steering || run.via === "gateway"' in app)
check("page: refusal shows the server's text", "error.message ||" in app and
      re.search(r'if \(error\.code === "unconfirmed"\) gatewayLost\(run, message\);\s*else failStart\(run, message\);', app))
check("page: an unconfirmed send is never put back in the box",
      re.search(r"function gatewayLost\(run, message\) \{(?:(?!function )[\s\S])*?\}\n", app) and
      "draft" not in re.search(r"function gatewayLost\(run, message\) \{[\s\S]*?\n\}\n", app).group(0))
check("page: follows the reply by lease and saved rows",
      "/phone-api/busy?session=" in app and "PhoneParts.gatewayStep(progress" in app and
      'finishRun(run, { status: "completed" })' in app)
check("page: no inline script or style, no on* handlers",
      not re.search(r"<script(?![^>]*\bsrc=)", html) and "<style" not in html and
      not re.search(r"\son[a-z]+\s*=", html) and not re.search(r'\sstyle="', html))
check("serve.py: endpoint is local, not proxied", serve.DESKTOP_SEND_PATH in serve.LOCAL_API)
check("update-check runs this suite", re.search(r"for t in [^;]*\bdesktopsend\b", read("scripts/update-check.sh")))

passed = sum(results)
print("%d passed, %d failed" % (passed, len(results) - passed))
sys.exit(0 if passed == len(results) else 1)
