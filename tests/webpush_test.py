"""Offline test of Web Push: encryption, VAPID, subscriptions, the serve.py
endpoints and the "webpush" alert channel.

Usage: python -I tests/webpush_test.py <repo-dir> <empty-scratch-dir>
Needs the cryptography package (run on the Hermes Python). Nothing reaches a
real push service or the phone: webpush.urlopen is replaced by a fake.
"""
import http.client
import io
import json
import os
import socket
import sqlite3
import stat
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse

repo, work = sys.argv[1], sys.argv[2]
os.makedirs(work, exist_ok=True)
sys.path.insert(0, repo)
import serve  # noqa: E402
import faceid  # noqa: E402
import webpush  # noqa: E402

serve.CONFIG_PATH = os.path.join(work, "config.json")
serve.LOG_PATH = os.path.join(work, "serve.log")
serve.APP_NAME = "Test Name"
logged = []
serve.log = logged.append
faceid.CONFIG_PATH = os.path.join(work, "faceid.json")
faceid.SETUP_PATH = os.path.join(work, "faceid-setup.json")
webpush.STATE_PATH = os.path.join(work, "webpush.json")
for _path in (faceid.CONFIG_PATH, faceid.SETUP_PATH, serve.CONFIG_PATH, webpush.STATE_PATH):
    _scratch = os.path.realpath(work)
    if os.path.commonpath([os.path.realpath(_path), _scratch]) != _scratch:
        print("FAIL ISOLATION: would touch live file %s" % _path)
        sys.exit(1)
for _path in (webpush.STATE_PATH, serve.CONFIG_PATH, serve.LOG_PATH):
    if os.path.exists(_path):
        os.remove(_path)

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

results = []


def check(name, ok):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name)


b64e, b64d = webpush.b64e, webpush.b64d

# --- RFC 8291 section 5 / appendix A ----------------------------------------
as_private = ec.derive_private_key(int.from_bytes(b64d("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
                                   ec.SECP256R1())
out = webpush.encrypt(b64d("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
                      b64d("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
                      b64d("BTBZMqHH6r4Tts7J_aSIgg"), as_private=as_private, salt=b64d("DGv6ra1nlYgDCS1FRnbzlw"))
check("RFC 8291 test vector, byte for byte", b64e(out) == (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_"
    "yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"))


# A fake phone: its own key pair and auth secret, and the receiver side of RFC 8291.
class Phone:
    def __init__(self, endpoint):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                         serialization.PublicFormat.UncompressedPoint)
        self.auth = os.urandom(16)
        self.endpoint = endpoint

    def subscription(self):
        return {"endpoint": self.endpoint, "keys": {"p256dh": b64e(self.public), "auth": b64e(self.auth)}}

    def decrypt(self, body):
        salt, rs, idlen = body[:16], struct.unpack("!I", body[16:20])[0], body[20]
        as_public = body[21:21 + idlen]
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
        secret = self.key.exchange(ec.ECDH(), peer)
        prk_key = webpush._hkdf_extract(self.auth, secret)
        ikm = webpush._hkdf_expand(prk_key, b"WebPush: info\x00" + self.public + as_public, 32)
        prk = webpush._hkdf_extract(salt, ikm)
        cek = webpush._hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = webpush._hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)
        plain = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
        assert rs == 4096 and plain.endswith(b"\x02")
        return json.loads(plain[:-1].decode("utf-8"))


phone = Phone("https://web.push.apple.com/QGuQyavXutnMnBvR1nTqyO8_test")
message = webpush.declarative("Fix the deck", "Reply finished.", "https://host.example.ts.net:8443/#s=abc", 3)
check("round trip: the phone decrypts the declarative JSON",
      phone.decrypt(webpush.encrypt(json.dumps(message).encode(), phone.public, phone.auth)) == message)
check("declarative format: web_push 8030, navigate, badge as text",
      message["web_push"] == 8030 and message["notification"]["navigate"].endswith("#s=abc")
      and message["notification"]["app_badge"] == "3" and message["notification"]["title"] == "Fix the deck")
try:
    webpush.encrypt(b"x" * (webpush.MAX_PLAINTEXT + 1), phone.public, phone.auth)
    check("oversized payload refused", False)
except ValueError:
    check("oversized payload refused", True)

# --- keys and state file -------------------------------------------------------
pub = webpush.public_key()
mode = stat.S_IMODE(os.stat(webpush.STATE_PATH).st_mode)
check("key pair made on first use, file mode 600", len(b64d(pub)) == 65 and b64d(pub)[0] == 4 and mode == 0o600)
check("key pair is stable", webpush.public_key() == pub)

# --- subjects and endpoints ------------------------------------------------------
check("subject: https tailnet origin ok", webpush.subject_ok("https://mac.tail1234.ts.net:8443"))
check("subject: mailto ok", webpush.subject_ok("mailto:owner@example.com"))
check("subject: localhost, .invalid, bare address refused",
      not any(webpush.subject_ok(s) for s in ("mailto:me@localhost", "https://localhost", "mailto:a@b.invalid",
                                              "owner@example.com", "http://example.com", "")))
check("endpoint: Apple, FCM, Mozilla ok", all(webpush.endpoint_ok(e) for e in (
    "https://web.push.apple.com/abc", "https://fcm.googleapis.com/fcm/send/x",
    "https://updates.push.services.mozilla.com/wpush/v2/x")))
check("endpoint: anything else refused (SSRF guard)", not any(webpush.endpoint_ok(e) for e in (
    "http://web.push.apple.com/abc", "https://127.0.0.1/x", "https://push.apple.com.evil.example/x",
    "https://evilpush.apple.com.example/x", "https://web.push.apple.com:8443/x", "https://u:p@web.push.apple.com/x",
    "https://localhost/x", "ftp://web.push.apple.com/x", "https://web.push.apple.com/" + "a" * 1100)))

# --- subscriptions ---------------------------------------------------------------
check("bad subscription refused (short key)",
      not webpush.add_subscription({"endpoint": phone.endpoint, "keys": {"p256dh": "AAAA", "auth": b64e(phone.auth)}}))
check("subscription for a non-push host refused",
      not webpush.add_subscription(dict(phone.subscription(), endpoint="https://10.0.0.5/x")))
check("subscription stored", webpush.add_subscription(phone.subscription()) and webpush.is_subscribed(phone.endpoint))
webpush.add_subscription(phone.subscription())
check("same endpoint replaced, not doubled", len(webpush.subscriptions()) == 1)
for i in range(12):
    webpush.add_subscription(Phone("https://web.push.apple.com/extra%d" % i).subscription())
check("at most 10 kept, newest win", len(webpush.subscriptions()) == 10 and not webpush.is_subscribed(phone.endpoint))
for sub in webpush.subscriptions():
    webpush.remove_subscription(sub["endpoint"])
check("remove", webpush.subscriptions() == [] and not webpush.remove_subscription("https://web.push.apple.com/none"))

# --- VAPID -----------------------------------------------------------------------
data = webpush.ensure_keys()
token = webpush.vapid_token("https://web.push.apple.com", "https://mac.example.ts.net:8443", data, now=1000000)
head, claims, sig = token.split(".")
claims = json.loads(b64d(claims))
check("JWT header ES256", json.loads(b64d(head)) == {"typ": "JWT", "alg": "ES256"})
check("JWT claims: aud origin only, exp <= 24h, sub",
      claims["aud"] == "https://web.push.apple.com" and 0 < claims["exp"] - 1000000 <= 24 * 3600
      and claims["sub"] == "https://mac.example.ts.net:8443")
raw = b64d(sig)
public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64d(data["public_key"]))
try:
    public.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                  ("%s.%s" % (head, token.split(".")[1])).encode(), ec.ECDSA(hashes.SHA256()))
    check("JWT signature verifies with the public key", len(raw) == 64)
except Exception:
    check("JWT signature verifies with the public key", False)
check("JWT reused within the hour",
      webpush.vapid_token("https://web.push.apple.com", "https://mac.example.ts.net:8443", data, now=1000000 + 1800) == token)
check("JWT re-signed after an hour",
      webpush.vapid_token("https://web.push.apple.com", "https://mac.example.ts.net:8443", data, now=1000000 + 3700) != token)
check("audience is the endpoint origin", webpush.audience("https://web.push.apple.com/abc/def") == "https://web.push.apple.com")

# --- sending through a fake push service ------------------------------------------
sent_requests = []
behaviour = {}


class FakeResponse:
    def __init__(self):
        self.status = 201

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_urlopen(request, timeout=None):
    sent_requests.append(request)
    code = behaviour.get(request.full_url, 201)
    if code != 201:
        body = b'{"reason":"BadJwtToken"}' if code == 403 else b'{"reason":"Unregistered"}'
        raise urllib.error.HTTPError(request.full_url, code, "x", {}, io.BytesIO(body))
    return FakeResponse()


webpush.urlopen = fake_urlopen
good = Phone("https://web.push.apple.com/good")
gone = Phone("https://web.push.apple.com/gone")
bad = Phone("https://web.push.apple.com/badjwt")
for p in (good, gone, bad):
    webpush.add_subscription(p.subscription())
behaviour[gone.endpoint] = 410
behaviour[bad.endpoint] = 403
sent, problems = webpush.send_all(message, "https://mac.example.ts.net:8443", urgent=True)
check("send_all: one delivered, two problems", sent == 1 and len(problems) == 2)
check("410 removes the subscription", not webpush.is_subscribed(gone.endpoint) and webpush.is_subscribed(good.endpoint))
check("403 keeps it and reports Apple's reason", webpush.is_subscribed(bad.endpoint)
      and any("403 BadJwtToken" in p for p in problems))
check("problems never name an endpoint", not any("push.apple.com" in p or "good" in p for p in problems))
req = [r for r in sent_requests if r.full_url == good.endpoint][-1]
hdr = {k.lower(): v for k, v in req.header_items()}
check("request headers: vapid auth, aes128gcm, TTL, Urgency high",
      hdr["authorization"].startswith("vapid t=") and hdr["authorization"].endswith(", k=" + data["public_key"])
      and hdr["content-encoding"] == "aes128gcm" and hdr["urgency"] == "high" and int(hdr["ttl"]) > 0)
check("the phone can read what was sent", good.decrypt(req.data) == message)
webpush.remove_subscription(bad.endpoint)

# --- serve.py: alert channel ---------------------------------------------------------
home = os.path.join(work, "home")
os.makedirs(os.path.join(home, "profiles", "work"), exist_ok=True)
with open(os.path.join(home, ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=default-key-0123456789\n")
with open(os.path.join(home, "profiles", "work", ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=work-key-0123456789abc\n")
for db in (os.path.join(home, "state.db"), os.path.join(home, "profiles", "work", "state.db")):
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT, title TEXT)")
    conn.commit()
    conn.close()
conn = sqlite3.connect(os.path.join(home, "state.db"))
conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?)", [
    ("root1", None, "compression", "Plan the  garden\nbeds"), ("tip1", "root1", None, None), ("bare", None, None, "")])
conn.commit()
conn.close()
conn = sqlite3.connect(os.path.join(home, "profiles", "work", "state.db"))
conn.execute("INSERT INTO sessions VALUES ('w1', NULL, NULL, 'Work thing')")
conn.commit()
conn.close()
serve.HERMES_HOME = home
serve.STATE_DB = os.path.join(home, "state.db")
serve.LOG_PATH = os.path.join(work, "serve.log")

check("session title, whitespace folded", serve.session_title("default", "root1") == "Plan the garden beds")
check("session title from an earlier part of a compressed lineage", serve.session_title("default", "tip1")
      == "Plan the garden beds")
check("session title per profile", serve.session_title("work", "w1") == "Work thing")
check("no title: empty, then app name in the alert", serve.session_title("default", "bare") == ""
      and serve.push_alert("default", "bare", "x")["title"] == "Test Name")
check("bad session id never queried", serve.session_title("default", "x' OR 1=1 --") == "")
check("link for default and other profiles", serve.hash_for("tip1", "default") == "#s=tip1"
      and serve.hash_for("w1", "work") == "#s=w1&p=work")

with open(serve.CONFIG_PATH, "w") as handle:
    json.dump({"alerts": {"channel": "webpush", "url": "http://insecure.example/"}}, handle)
check("webpush channel needs an https url", serve.alert_config() is None)
with open(serve.CONFIG_PATH, "w") as handle:
    json.dump({"alerts": {"channel": "webpush", "url": "https://localhost:8443/"}}, handle)
check("webpush channel refuses a subject Apple rejects", serve.alert_config() is None)
with open(serve.CONFIG_PATH, "w") as handle:
    json.dump({"alerts": {"channel": "webpush", "url": "https://mac.example.ts.net:8443/"}}, handle)
config = serve.alert_config()
check("webpush channel accepted, subject = page origin", config is not None
      and serve.push_subject(config) == "https://mac.example.ts.net:8443")

serve._last_fail[0] = None
serve.note_page(2)  # the page says 2 unread
with serve._polls_lock:
    serve._polls.clear()
known, alerted = {}, set()
runs = {"r1": {"status": "running", "session_id": "tip1", "_profile": "default"},
        "r2": {"status": "running", "session_id": "w1", "_profile": "work"}}
serve.check_runs(config, runs, known, alerted)
del sent_requests[:]
runs["r1"] = dict(runs["r1"], status="completed")
runs["r2"] = dict(runs["r2"], status="waiting_for_approval", approval={"request_id": "req1"})
serve.check_runs(config, runs, known, alerted)
got = [good.decrypt(r.data)["notification"] for r in sent_requests]
done = [n for n in got if n["body"] == "Reply finished."]
appr = [n for n in got if n["body"] == "Waiting for your approval."]
check("finish alert: session title, opens that session", len(done) == 1 and done[0]["title"] == "Plan the garden beds"
      and done[0]["navigate"] == "https://mac.example.ts.net:8443/#s=tip1")
check("approval alert: other profile's title and link", len(appr) == 1 and appr[0]["title"] == "Work thing"
      and appr[0]["navigate"] == "https://mac.example.ts.net:8443/#s=w1&p=work")
check("badge = page's unread + sessions alerted since", sorted(n["app_badge"] for n in got) == ["3", "4"])
urg = {good.decrypt(r.data)["notification"]["body"]: dict(r.header_items()).get("Urgency") for r in sent_requests}
check("approval high urgency, finish normal",
      urg.get("Waiting for your approval.") == "high" and urg.get("Reply finished.") == "normal")
serve.note_page(0)
check("opening the page resets the badge", serve.badge_for("default/x") == 1)

del sent_requests[:]
serve.check_desktop(config, [{"request_id": "d1", "session_key": "w1", "profile": "work", "created": 1}],
                    set(), now=1000)
with serve._polls_lock:
    serve._polls.clear()
serve.check_desktop(config, [{"request_id": "d2", "session_key": "w1", "profile": "work", "created": 1}],
                    set(), now=1000)
got = [good.decrypt(r.data)["notification"] for r in sent_requests]
check("desktop approval alert: title, body, link", len(got) >= 1 and got[-1]["title"] == "Work thing"
      and got[-1]["body"] == "Waiting for your approval on the desktop." and got[-1]["navigate"].endswith("#s=w1&p=work"))

del sent_requests[:]
check("--notify style alert: app name and message, opens the page",
      serve.send_alert(config, "Update check failed.", False) and
      good.decrypt(sent_requests[-1].data)["notification"] == {
          "title": "Test Name", "body": "Update check failed.", "navigate": "https://mac.example.ts.net:8443/",
          "silent": False})

webpush.remove_subscription(good.endpoint)
serve._last_fail[0] = None
check("no subscriptions: skipped, no backoff", not serve.send_alert(config, "x", False) and serve._last_fail[0] is None)
webpush.add_subscription(good.subscription())

# Generic channels keep generic text even when a push form is passed.
captured = []
serve.ALERT_CHANNELS["homeassistant"] = lambda cfg, msg, urgent: captured.append(msg)
serve.send_alert({"channel": "homeassistant", "notify": "x"}, "Test Name finished your reply.", False,
                 serve.push_alert("default", "tip1", "Reply finished."))
check("Home Assistant still gets generic text, no title", captured == ["Test Name finished your reply."])

# --- serve.py: endpoints --------------------------------------------------------------
allow = os.path.join(work, "allowed-logins.txt")
with open(allow, "w") as handle:
    handle.write("# owner\nowner@example.com\n")
serve.ALLOW_PATH = allow


class H(serve.socketserver.BaseRequestHandler):
    def handle(self):
        serve.handle_client(self.request, ("127.0.0.1", 9))


s = socket.socket()
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
s.close()
server = serve.Server(("127.0.0.1", port), H)
threading.Thread(target=server.serve_forever, daemon=True).start()


def call(method, path, body=None, key="default-key-0123456789", login="owner@example.com"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Tailscale-User-Login": login} if login else {}
    if key:
        h["Authorization"] = "Bearer " + key
    payload = None if body is None else json.dumps(body).encode()
    if payload is not None:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=payload, headers=h)
    r = c.getresponse()
    out = r.status, r.read(), dict(r.getheaders())
    c.close()
    return out


status, body, _ = call("GET", "/phone-api/push/key", login="stranger@example.com")
check("wrong login refused", status == 403)
status, body, _ = call("GET", "/phone-api/push/key", key="wrong-key-0123456789")
check("wrong key refused", status == 401)
status, body, _ = call("GET", "/phone-api/push/key?endpoint=" + urllib.parse.quote(good.endpoint, safe=""))
info = json.loads(body)
check("key endpoint: public key, subscribed, ready, channel", status == 200 and info["public_key"] == pub
      and info["subscribed"] is True and info["ready"] is True and info["channel"] == "webpush")
other = Phone("https://web.push.apple.com/other")
status, body, _ = call("POST", "/phone-api/push/subscribe", {"subscription": other.subscription()})
check("subscribe", status == 200 and webpush.is_subscribed(other.endpoint))
status, body, _ = call("POST", "/phone-api/push/subscribe",
                       {"subscription": dict(other.subscription(), endpoint="https://10.0.0.5/x")})
check("subscribe to a LAN address refused", status == 400)
del sent_requests[:]
status, body, _ = call("POST", "/phone-api/push/test", {"endpoint": other.endpoint})
check("test goes only to the asking phone", status == 200 and json.loads(body)["sent"] == 1
      and [r.full_url for r in sent_requests] == [other.endpoint]
      and other.decrypt(sent_requests[0].data)["notification"]["body"] == "Test notification. Tap to open.")
status, body, _ = call("POST", "/phone-api/push/test", {"endpoint": "https://web.push.apple.com/unknown"})
check("test for an unknown phone: 409", status == 409)
status, body, _ = call("POST", "/phone-api/push/unsubscribe", {"endpoint": other.endpoint})
check("unsubscribe", status == 200 and not webpush.is_subscribed(other.endpoint))
status, body, _ = call("GET", "/phone-api/active?unread=7")
check("active poll records the page's unread count", status == 200 and serve._badge["base"] == 7)
status, body, headers = call("GET", "/sw.js", key=None)
check("sw.js served as JavaScript", status == 200 and headers.get("Content-Type", "").startswith("text/javascript")
      and b"notificationclick" in body)
status, body, headers = call("GET", "/", key=None)
check("CSP allows only this origin's worker", "worker-src 'self'" in headers.get("Content-Security-Policy", ""))
log = "\n".join(logged)
check("log never names an endpoint", "push.apple.com" not in log and "web push: phone subscribed" in log)
server.shutdown()

print("%d passed, %d failed" % (results.count(True), results.count(False)))
sys.exit(0 if all(results) else 1)
