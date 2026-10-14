"""Offline test of the Face ID (passkey) lock in faceid.py and serve.py.

Usage (needs the `cryptography` package, e.g. the Hermes venv):
  ~/.hermes/hermes-agent/venv/bin/python -I tests/faceid_test.py <repo-dir> <empty-scratch-dir>

A software passkey with a real P-256 key stands in for the iPhone. A copy of
the proxy runs on a spare port against a scratch HERMES_HOME and a fake Hermes
API that records what it receives. Nothing touches ~/.hermes or the phone.
"""
import base64
import hashlib
import http.client
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

repo, work = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
os.makedirs(work, exist_ok=True)
sys.path.insert(0, repo)
import faceid  # noqa: E402
import serve  # noqa: E402
# Never read a live install's config.json (alerts, port, Hermes home).
serve.CONFIG_PATH = os.path.join(work, "config.json")

RP = "phone.example.ts.net"
ORIGIN = "https://phone.example.ts.net:8443"
KEY = "test-key-123"
OWNER = "owner@example.com"

home = os.path.join(work, "home")
base = os.path.join(home, "phone-approvals")
for sub in ("pending", "decisions"):
    os.makedirs(os.path.join(base, sub), mode=0o700, exist_ok=True)
with open(os.path.join(home, ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=%s\n" % KEY)
allow = os.path.join(work, "allowed-logins.txt")
with open(allow, "w") as handle:
    handle.write(OWNER + "\n")

serve.HERMES_HOME = home
serve.APPROVALS_DIR = base
serve.ALLOW_PATH = allow
serve.STATE_DB = os.path.join(home, "state.db")
serve.RUNS_DB = os.path.join(home, "runs.db")
faceid.CONFIG_PATH = os.path.join(work, "faceid.json")
faceid.SETUP_PATH = os.path.join(work, "faceid-setup.json")

# Guard: this test must never read the live Face ID files. With a passkey
# enrolled they lock the API-key path; without one they hide that bug.
for _path in (faceid.CONFIG_PATH, faceid.SETUP_PATH):
    _scratch = os.path.realpath(work)
    if os.path.commonpath([os.path.realpath(_path), _scratch]) != _scratch:
        print("FAIL ISOLATION: would read live Face ID file %s" % _path)
        sys.exit(1)
NOW = [time.time()]
faceid.clock = lambda: NOW[0]

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)))


# --- fake Hermes API -------------------------------------------------------------
seen = []


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                     "cookie": self.headers.get("Cookie"), "body": body})
        out = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = _reply


upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
threading.Thread(target=upstream.serve_forever, daemon=True).start()
UP = ("127.0.0.1", upstream.server_address[1])

listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", 0))
listener.listen(16)
PORT = listener.getsockname()[1]


def accept_loop():
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=lambda c=conn: (serve.handle_client(c, UP), c.close()), daemon=True).start()


threading.Thread(target=accept_loop, daemon=True).start()


def req(method, path, body=None, cookie=None, bearer=None, login=OWNER, origin=ORIGIN):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    headers = {"Origin": origin} if origin and method != "GET" else {}
    if login:
        headers["Tailscale-User-Login"] = login
    if cookie:
        headers["Cookie"] = "%s=%s" % (faceid.COOKIE, cookie)
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    cookie_out = None
    for name, value in res.getheaders():
        if name.lower() == "set-cookie" and value.startswith(faceid.COOKIE + "="):
            cookie_out = value.split(";", 1)[0].split("=", 1)[1]
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw
    return res.status, payload, cookie_out


# --- software passkey ----------------------------------------------------------------
def b64e(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def cbor(value):
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        if n < 256:
            return bytes([major << 5 | 24, n])
        return bytes([major << 5 | 25]) + n.to_bytes(2, "big")
    if isinstance(value, bool):
        return bytes([0xF5 if value else 0xF4])
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode()
        return head(3, len(raw)) + raw
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(value)


class Passkey:
    def __init__(self, rp=RP, origin=ORIGIN):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.rp, self.origin = rp, origin

    def _client(self, kind, challenge, origin=None):
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin,
                           "crossOrigin": False}).encode()

    def create(self, options, flags=0x45, origin=None, kind="webauthn.create"):
        nums = self.key.public_key().public_numbers()
        cose = cbor({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        auth = (hashlib.sha256(self.rp.encode()).digest() + bytes([flags]) + (0).to_bytes(4, "big")
                + bytes(16) + len(self.cred_id).to_bytes(2, "big") + self.cred_id + cose)
        att = cbor({"fmt": "none", "attStmt": {}, "authData": auth})
        return {"id": b64e(self.cred_id), "rawId": b64e(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64e(self._client(kind, options["challenge"], origin)),
                             "attestationObject": b64e(att)}}

    def get(self, options, flags=0x05, origin=None, rp=None, count=0, signer=None, kind="webauthn.get"):
        client = self._client(kind, options["challenge"], origin)
        auth = hashlib.sha256((rp or self.rp).encode()).digest() + bytes([flags]) + count.to_bytes(4, "big")
        sig = (signer or self.key).sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64e(self.cred_id), "rawId": b64e(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64e(client), "authenticatorData": b64e(auth),
                             "signature": b64e(sig)}}


# --- before Face ID is set up ---------------------------------------------------------
status, body, _ = req("GET", "/phone-api/auth/status")
check("status before setup", status == 200 and body == {"configured": False, "enrolled": False,
                                                        "setup_open": False, "unlocked": False}, body)
status, _, _ = req("GET", "/api/sessions", bearer=KEY)
check("API key still works before enrollment", status == 200 and seen[-1]["auth"] == "Bearer " + KEY)
status, _, _ = req("GET", "/phone-api/auth/status", login="stranger@example.com")
check("auth endpoints keep the login lock", status == 403)

faceid.configure(RP, ORIGIN)
code = faceid.new_setup_code()
check("setup code format", len(code) == 9 and code[4] == "-")
check("config and setup files are 600", all(os.stat(p).st_mode & 0o777 == 0o600
                                            for p in (faceid.CONFIG_PATH, faceid.SETUP_PATH)))

# --- enrollment -----------------------------------------------------------------------
phone = Passkey()
status, body, _ = req("POST", "/phone-api/auth/register/options", {"code": "0000-0000"})
check("wrong setup code refused", status == 403 and body["error"]["code"] == "bad_code")
status, opts, _ = req("POST", "/phone-api/auth/register/options", {"code": code})
check("right setup code gives options", status == 200 and opts["authenticatorSelection"]["userVerification"]
      == "required" and opts["rp"]["id"] == RP, opts)

status, _, _ = req("POST", "/phone-api/auth/register", {"credential": phone.create(opts, flags=0x41)})
check("registration without Face ID refused", status == 403)
check("refused registration does not enroll", not faceid.enrolled())
status, opts, _ = req("POST", "/phone-api/auth/register/options", {"code": code})
status, _, _ = req("POST", "/phone-api/auth/register", {"credential": phone.create(opts, origin="https://evil.example")})
check("registration from another origin refused", status == 403)
status, opts, _ = req("POST", "/phone-api/auth/register/options", {"code": code})
status, body, cookie = req("POST", "/phone-api/auth/register", {"credential": phone.create(opts)})
check("good registration enrolls and unlocks", status == 200 and cookie and faceid.enrolled(), body)
check("setup code is used up", not os.path.exists(faceid.SETUP_PATH))
status, _, _ = req("POST", "/phone-api/auth/register/options", {"code": code})
check("used code cannot enroll a second passkey", status == 403)

code2 = faceid.new_setup_code()
for _ in range(faceid.SETUP_TRIES):
    req("POST", "/phone-api/auth/register/options", {"code": "1111-1111"})
status, _, _ = req("POST", "/phone-api/auth/register/options", {"code": code2})
check("five wrong codes kill the setup code", status == 403)
NOW[0] += 1
code3 = faceid.new_setup_code()
NOW[0] += faceid.SETUP_TTL + 1
status, _, _ = req("POST", "/phone-api/auth/register/options", {"code": code3})
check("setup code expires after 10 minutes", status == 403)

# --- the lock -------------------------------------------------------------------------
status, body, _ = req("GET", "/api/sessions", bearer=KEY)
check("API key alone no longer opens it", status == 401 and body["error"]["code"] == "locked", body)
status, _, _ = req("GET", "/")
check("page files load while locked", status == 200)
status, _, _ = req("GET", "/phone-api/active", bearer=KEY)
check("active poll locked too", status == 401)

status, opts, _ = req("POST", "/phone-api/auth/login/options", {})
check("login options name the passkey", status == 200 and opts["allowCredentials"][0]["id"] == b64e(phone.cred_id))
cred = phone.get(opts)
status, _, cookie = req("POST", "/phone-api/auth/login", {"credential": cred})
check("Face ID unlock", status == 200 and cookie)
status, _, again = req("POST", "/phone-api/auth/login", {"credential": cred})
check("replayed signature refused", status == 403 and again is None)

status, opts, _ = req("POST", "/phone-api/auth/login/options", {})
status, _, _ = req("POST", "/phone-api/auth/login", {"credential": phone.get(opts, flags=0x01)})
check("signature without Face ID (UV) refused", status == 403)
status, opts, _ = req("POST", "/phone-api/auth/login/options", {})
status, _, _ = req("POST", "/phone-api/auth/login", {"credential": phone.get(opts, origin="https://evil.example")})
check("signature for another origin refused", status == 403)
status, opts, _ = req("POST", "/phone-api/auth/login/options", {})
status, _, _ = req("POST", "/phone-api/auth/login", {"credential": phone.get(opts, rp="evil.example")})
check("signature for another rp id refused", status == 403)
status, opts, _ = req("POST", "/phone-api/auth/login/options", {})
status, _, _ = req("POST", "/phone-api/auth/login",
                   {"credential": phone.get(opts, signer=ec.generate_private_key(ec.SECP256R1()))})
check("signature by another key refused", status == 403)
status, opts, _ = req("POST", "/phone-api/auth/login/options", {})
fake = dict(opts, challenge=b64e(os.urandom(32)))
status, _, _ = req("POST", "/phone-api/auth/login", {"credential": phone.get(fake)})
check("made-up challenge refused", status == 403)
status, opts, _ = req("POST", "/phone-api/auth/login/options", {})
status, _, _ = req("POST", "/phone-api/auth/login", {"credential": phone.get(opts, kind="webauthn.create")})
check("wrong ceremony type refused", status == 403)
status, _, _ = req("POST", "/phone-api/auth/login", {"credential": "nonsense"})
check("garbage refused", status == 403)

seen.clear()
status, _, _ = req("GET", "/api/sessions", cookie=cookie, bearer="phone-supplied-wrong-key")
check("unlocked request reaches Hermes", status == 200 and len(seen) == 1)
check("proxy adds the API key", seen and seen[-1]["auth"] == "Bearer " + KEY, seen[-1:])
check("phone's own Authorization and cookie are not forwarded",
      seen and seen[-1]["cookie"] is None and "phone-supplied" not in (seen[-1]["auth"] or ""))
status, body, _ = req("GET", "/phone-api/auth/status", cookie=cookie)
check("status reports unlocked", body.get("unlocked") is True and body.get("enrolled") is True, body)

# idle and absolute expiry
NOW[0] += faceid.SESSION_IDLE - 60
status, _, _ = req("GET", "/phone-api/active", cookie=cookie)
check("background poll allowed inside idle window", status == 200)
NOW[0] += 120
status, _, _ = req("GET", "/api/sessions", cookie=cookie)
check("polls do not keep it unlocked: locks after 30 min idle", status == 401)

status, opts, _ = req("POST", "/phone-api/auth/login/options", {})
status, _, cookie = req("POST", "/phone-api/auth/login", {"credential": phone.get(opts)})
for _ in range(37):  # 37 x 20 min = 12 h 20 min, never idle 30 min
    NOW[0] += 20 * 60
    req("GET", "/api/sessions", cookie=cookie)
status, _, _ = req("GET", "/api/sessions", cookie=cookie)
check("locks after 12 hours even when in use", status == 401)

status, opts, _ = req("POST", "/phone-api/auth/login/options", {})
status, _, cookie = req("POST", "/phone-api/auth/login", {"credential": phone.get(opts)})
status, _, cleared = req("POST", "/phone-api/auth/logout", cookie=cookie)
status, _, _ = req("GET", "/api/sessions", cookie=cookie)
check("Lock button ends the session", status == 401)
status, opts, _ = req("POST", "/phone-api/auth/login/options", {})
status, _, cookie = req("POST", "/phone-api/auth/login", {"credential": phone.get(opts)})

# --- origin check (dashboard on :443 is same-site) ---------------------------------------
marker_rid = "o" * 32
with open(os.path.join(base, "pending", marker_rid + ".json"), "w") as handle:
    json.dump({"request_id": marker_rid, "session_key": "sess_1", "pid": os.getpid(), "created": time.time(),
               "command": "x", "description": "x"}, handle)
for label, origin in (("dashboard origin", "https://phone.example.ts.net"), ("no origin", None)):
    status, _, _ = req("POST", "/phone-api/approvals/decide", {"request_id": marker_rid, "choice": "deny"},
                       cookie=cookie, origin=origin)
    check("POST from %s refused" % label, status == 403)
seen.clear()
status, _, _ = req("POST", "/v1/runs", {"input": "x"}, cookie=cookie, origin="https://phone.example.ts.net")
check("new run from another origin refused, not forwarded", status == 403 and not seen)
os.remove(os.path.join(base, "pending", marker_rid + ".json"))

# --- desktop approvals ------------------------------------------------------------------
RID = "r" * 32
OTHER = "s" * 32


def marker(rid):
    with open(os.path.join(base, "pending", rid + ".json"), "w") as handle:
        json.dump({"request_id": rid, "session_key": "sess_1", "pid": os.getpid(), "created": time.time(),
                   "command": "rm -rf build", "description": "recursive delete"}, handle)


def decision(rid):
    path = os.path.join(base, "decisions", rid + ".json")
    if not os.path.exists(path):
        return None
    with open(path) as handle:
        data = json.load(handle)
    os.remove(path)
    return data["choice"]


marker(RID)
marker(OTHER)
status, _, _ = req("POST", "/phone-api/approvals/decide", {"request_id": RID, "choice": "once"}, cookie=cookie)
check("desktop Allow once without Face ID refused", status == 403 and decision(RID) is None)
status, _, _ = req("POST", "/phone-api/approvals/decide", {"request_id": RID, "choice": "deny"}, cookie=cookie)
check("desktop Deny needs no Face ID", status == 202 and decision(RID) == "deny")
status, _, _ = req("POST", "/phone-api/approvals/decide", {"request_id": RID, "choice": "deny"})
check("desktop Deny still needs the unlock", status == 401)

status, _, _ = req("POST", "/phone-api/auth/approve/options", {"kind": "desktop", "request_id": OTHER})
check("approval challenge needs the unlock", status == 401)
status, opts, _ = req("POST", "/phone-api/auth/approve/options", {"kind": "desktop", "request_id": OTHER},
                      cookie=cookie)
status, _, _ = req("POST", "/phone-api/approvals/decide",
                   {"request_id": RID, "choice": "once", "assertion": phone.get(opts)}, cookie=cookie)
check("signature for a different approval refused", status == 403 and decision(RID) is None)
status, opts, _ = req("POST", "/phone-api/auth/approve/options", {"kind": "desktop", "request_id": RID},
                      cookie=cookie)
status, lopts, _ = req("POST", "/phone-api/auth/login/options", {})
status, _, _ = req("POST", "/phone-api/approvals/decide",
                   {"request_id": RID, "choice": "once", "assertion": phone.get(lopts)}, cookie=cookie)
check("unlock signature cannot approve", status == 403 and decision(RID) is None)
good = phone.get(opts)
status, _, _ = req("POST", "/phone-api/approvals/decide",
                   {"request_id": RID, "choice": "once", "assertion": good}, cookie=cookie)
check("desktop Allow once with its own Face ID signature", status == 202 and decision(RID) == "once")
status, _, _ = req("POST", "/phone-api/approvals/decide",
                   {"request_id": RID, "choice": "once", "assertion": good}, cookie=cookie)
check("approval signature is single use", status == 403 and decision(RID) is None)
status, opts, _ = req("POST", "/phone-api/auth/approve/options", {"kind": "desktop", "request_id": RID},
                      cookie=cookie)
NOW[0] += faceid.CHALLENGE_TTL + 1
status, _, _ = req("POST", "/phone-api/approvals/decide",
                   {"request_id": RID, "choice": "once", "assertion": phone.get(opts)}, cookie=cookie)
check("approval challenge expires", status == 403 and decision(RID) is None)
status, opts, _ = req("POST", "/phone-api/auth/approve/options", {"kind": "desktop", "request_id": RID},
                      cookie=cookie)
status, _, _ = req("POST", "/phone-api/approvals/decide",
                   {"request_id": RID, "choice": "always", "assertion": phone.get(opts)}, cookie=cookie)
check("always refused even with Face ID", status == 400 and decision(RID) is None)

# --- phone-run approvals (forwarded to Hermes) ---------------------------------------------
RUN = "run_abc123"
seen.clear()
status, _, _ = req("POST", "/v1/runs/%s/approval" % RUN, {"choice": "once", "request_id": RID}, cookie=cookie)
check("run Allow once without Face ID refused, not forwarded", status == 403 and not seen)
status, _, _ = req("POST", "/v1/runs/%s/approval" % RUN, {"choice": "session"}, cookie=cookie)
check("run 'session' refused", status == 400 and not seen)
status, _, _ = req("POST", "/v1/runs/%s/approval" % RUN, {"choice": "deny", "request_id": RID,
                                                          "resolve_all": True}, cookie=cookie)
sent = json.loads(seen[-1]["body"]) if seen else {}
check("run Deny forwarded without Face ID, extras stripped",
      status == 200 and sent == {"choice": "deny", "request_id": RID} and seen[-1]["auth"] == "Bearer " + KEY, sent)
status, opts, _ = req("POST", "/phone-api/auth/approve/options", {"kind": "run", "run_id": "run_other",
                                                                  "request_id": RID}, cookie=cookie)
seen.clear()
status, _, _ = req("POST", "/v1/runs/%s/approval" % RUN, {"choice": "once", "request_id": RID,
                                                          "assertion": phone.get(opts)}, cookie=cookie)
check("signature for another run refused", status == 403 and not seen)
status, opts, _ = req("POST", "/phone-api/auth/approve/options", {"kind": "run", "run_id": RUN,
                                                                  "request_id": RID}, cookie=cookie)
status, _, _ = req("POST", "/v1/runs/%s/approval" % RUN, {"choice": "once", "request_id": RID,
                                                          "assertion": phone.get(opts)}, cookie=cookie)
sent = json.loads(seen[-1]["body"]) if seen else {}
check("run Allow once with Face ID forwarded, signature not passed on",
      status == 200 and sent == {"choice": "once", "request_id": RID}, sent)
status, _, _ = req("POST", "/v1/runs/%s/approval" % RUN, {"choice": "deny"}, bearer=KEY)
check("run approval with API key only is locked", status == 401)

# --- signature counter (non-iCloud authenticators) ------------------------------------------
status, opts, _ = req("POST", "/phone-api/auth/login/options", {})
status, _, _ = req("POST", "/phone-api/auth/login", {"credential": phone.get(opts, count=5)})
check("moving counter accepted", status == 200)
status, opts, _ = req("POST", "/phone-api/auth/login/options", {})
status, _, _ = req("POST", "/phone-api/auth/login", {"credential": phone.get(opts, count=5)})
check("counter that does not move refused (cloned key)", status == 403)

# --- secrets stay out of the log -------------------------------------------------------------
check("enrolled credential stores only a public key",
      set(json.load(open(faceid.CONFIG_PATH))["credentials"][0]) == {"id", "x", "y", "count", "label", "created"})

passed = sum(results)
print("%d/%d passed" % (passed, len(results)))
sys.exit(0 if passed == len(results) else 1)
