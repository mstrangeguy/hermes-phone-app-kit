"""Web Push for the phone page (Home Screen app notifications).

The Mac signs and encrypts each alert and posts it to the phone's push service
(Apple's web.push.apple.com for an iPhone). Apple delivers it; nothing on the
internet has to reach the Mac, so the page stays tailnet-only.

- VAPID (RFC 8292): an ES256 key pair made once on this Mac. The phone
  subscribes with the public half; only this Mac can send to it.
- aes128gcm (RFC 8291): the alert is end-to-end encrypted to the phone, so
  Apple carries it but cannot read it.
- Declarative Web Push (iOS 18.4+): the alert is the standard JSON a browser
  shows by itself, with no service worker. No script runs on the phone, so a
  failed script can never cost the subscription.

State, next to serve.py, gitignored and mode 600:
  webpush.json  {"private_key", "public_key", "subscriptions": [...]}

Needs the `cryptography` package (the Hermes Python has it). Imported only
when a key is made or an alert is sent.
"""

import base64
import json
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "webpush.json")

# Push services a subscription may point at. The Mac posts to the endpoint, so
# anything else (a LAN address, this Mac) is refused.
PUSH_HOSTS = ("push.apple.com", "fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com")
MAX_SUBSCRIPTIONS = 10
RECORD_SIZE = 4096
MAX_PLAINTEXT = 3800
# Apple asks for a VAPID token at most 24 h ahead and not re-signed more than
# once an hour.
JWT_LIFETIME = 12 * 60 * 60
JWT_REUSE = 60 * 60
SEND_TIMEOUT = 10

clock = time.time  # tests replace this
urlopen = urllib.request.urlopen  # tests replace this (no real push service)

_lock = threading.Lock()
_jwt_cache = {}  # (audience, subject, public key) -> (token, signed_at)


def b64e(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text):
    text = str(text or "").strip().replace("+", "-").replace("/", "_").rstrip("=")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- state -----------------------------------------------------------------------

def load_state(path=None):
    try:
        with open(path or STATE_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data, path=None):
    path = path or STATE_PATH
    tmp = "%s.tmp%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


def _private_key(data):
    from cryptography.hazmat.primitives.asymmetric import ec
    value = int.from_bytes(b64d(data["private_key"]), "big")
    return ec.derive_private_key(value, ec.SECP256R1())


def _public_bytes(key):
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


def ensure_keys(path=None):
    """The VAPID key pair, made on first use. Returns the state dict."""
    with _lock:
        data = load_state(path)
        if data.get("private_key") and data.get("public_key"):
            return data
        from cryptography.hazmat.primitives.asymmetric import ec
        key = ec.generate_private_key(ec.SECP256R1())
        data["private_key"] = b64e(key.private_numbers().private_value.to_bytes(32, "big"))
        data["public_key"] = b64e(_public_bytes(key))
        data.setdefault("subscriptions", [])
        save_state(data, path)
        return data


def public_key(path=None):
    return ensure_keys(path)["public_key"]


# --- subscriptions ---------------------------------------------------------------

def endpoint_ok(endpoint):
    try:
        url = urllib.parse.urlsplit(str(endpoint or ""))
    except ValueError:
        return False
    host = (url.hostname or "").lower()
    if url.scheme != "https" or url.username or url.password or len(str(endpoint)) > 1024:
        return False
    try:
        if url.port not in (None, 443):
            return False
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


def clean_subscription(raw):
    """A browser PushSubscription (toJSON form) checked, or None."""
    if not isinstance(raw, dict):
        return None
    endpoint = raw.get("endpoint")
    keys = raw.get("keys") if isinstance(raw.get("keys"), dict) else {}
    if not endpoint_ok(endpoint):
        return None
    try:
        p256dh = b64d(keys.get("p256dh"))
        auth = b64d(keys.get("auth"))
    except (ValueError, TypeError):
        return None
    if len(p256dh) != 65 or p256dh[0] != 4 or len(auth) != 16:
        return None
    return {"endpoint": str(endpoint), "p256dh": b64e(p256dh), "auth": b64e(auth)}


def add_subscription(raw, path=None):
    sub = clean_subscription(raw)
    if sub is None:
        return False
    ensure_keys(path)
    with _lock:
        data = load_state(path)
        subs = [s for s in data.get("subscriptions") or [] if s.get("endpoint") != sub["endpoint"]]
        sub["created"] = int(clock())
        subs.append(sub)
        data["subscriptions"] = subs[-MAX_SUBSCRIPTIONS:]
        save_state(data, path)
    return True


def remove_subscription(endpoint, path=None):
    with _lock:
        data = load_state(path)
        subs = data.get("subscriptions") or []
        kept = [s for s in subs if s.get("endpoint") != endpoint]
        if len(kept) == len(subs):
            return False
        data["subscriptions"] = kept
        save_state(data, path)
    return True


def subscriptions(path=None):
    return [s for s in load_state(path).get("subscriptions") or [] if isinstance(s, dict)]


def is_subscribed(endpoint, path=None):
    return any(s.get("endpoint") == endpoint for s in subscriptions(path))


# --- RFC 8291 encryption ---------------------------------------------------------

def _hkdf_extract(salt, ikm):
    import hmac
    import hashlib
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk, info, length):
    import hmac
    import hashlib
    # length <= 32: one block is enough.
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(plaintext, ua_public, auth_secret, as_private=None, salt=None):
    """aes128gcm body for one record. ua_public/auth_secret are bytes.
    as_private (an EC private key) and salt are for the RFC test vector only."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(plaintext) > MAX_PLAINTEXT:
        raise ValueError("push payload too large")
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    as_public = _public_bytes(as_private)
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh_secret = as_private.exchange(ec.ECDH(), peer)
    prk_key = _hkdf_extract(auth_secret, ecdh_secret)
    ikm = _hkdf_expand(prk_key, b"WebPush: info\x00" + ua_public + as_public, 32)
    prk = _hkdf_extract(salt, ikm)
    cek = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + ciphertext


# --- VAPID -----------------------------------------------------------------------

def subject_ok(subject):
    """Apple refuses a VAPID subject that is not a real contact (403 BadJwtToken)."""
    subject = str(subject or "")
    if subject.startswith("mailto:"):
        domain = subject.rpartition("@")[2].lower()
        return "@" in subject and "." in domain and not domain.endswith((".invalid", ".local", "localhost"))
    if subject.startswith("https://"):
        host = (urllib.parse.urlsplit(subject).hostname or "").lower()
        return "." in host and not host.endswith((".invalid", ".local")) and host != "localhost"
    return False


def audience(endpoint):
    url = urllib.parse.urlsplit(endpoint)
    return "%s://%s" % (url.scheme, url.netloc)


def vapid_token(aud, subject, data, now=None):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    now = int(clock() if now is None else now)
    cache_key = (aud, subject, data["public_key"])
    with _lock:
        cached = _jwt_cache.get(cache_key)
    if cached and now - cached[1] < JWT_REUSE:
        return cached[0]
    header = b64e(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64e(json.dumps({"aud": aud, "exp": now + JWT_LIFETIME, "sub": subject},
                             separators=(",", ":")).encode())
    signing_input = ("%s.%s" % (header, claims)).encode("ascii")
    der = _private_key(data).sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    token = "%s.%s" % (signing_input.decode("ascii"), b64e(r.to_bytes(32, "big") + s.to_bytes(32, "big")))
    with _lock:
        _jwt_cache[cache_key] = (token, now)
    return token


# --- messages --------------------------------------------------------------------

def declarative(title, body, navigate, badge=None):
    """The Declarative Web Push JSON (web_push 8030). A service worker, if one
    is installed, gets the same JSON and shows it itself."""
    note = {"title": str(title or "")[:120] or "Hermes", "body": str(body or "")[:400],
            "navigate": navigate, "silent": False}
    if badge is not None:
        note["app_badge"] = str(max(0, int(badge)))
    return {"web_push": 8030, "notification": note}


def build_request(sub, message, subject, data, urgent=False, ttl=None):
    body = encrypt(json.dumps(message, ensure_ascii=False).encode("utf-8"),
                   b64d(sub["p256dh"]), b64d(sub["auth"]))
    token = vapid_token(audience(sub["endpoint"]), subject, data)
    headers = {
        "Authorization": "vapid t=%s, k=%s" % (token, data["public_key"]),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl if ttl is not None else (3600 if urgent else 4 * 3600)),
        "Urgency": "high" if urgent else "normal",
    }
    return urllib.request.Request(sub["endpoint"], data=body, method="POST", headers=headers)


def reason_of(error):
    """Apple and the others answer refusals with a short JSON reason; no secrets."""
    try:
        raw = error.read(512).decode("utf-8", "replace")
        reason = json.loads(raw).get("reason") if raw.strip().startswith("{") else raw.strip()
    except Exception:
        reason = ""
    reason = "".join(ch for ch in str(reason or "") if ch.isalnum() or ch in " _-.")[:60]
    return reason


def send_all(message, subject, urgent=False, path=None, opener=None, only=None):
    """Send to every subscription (or just `only`, an endpoint). Gone ones
    (404/410) are removed. Returns (sent, problems): problems are short strings
    with the status and reason only, never an endpoint."""
    opener = opener or urlopen
    data = ensure_keys(path)
    sent, problems = 0, []
    for sub in subscriptions(path):
        if only and sub.get("endpoint") != only:
            continue
        try:
            request = build_request(sub, message, subject, data, urgent)
            with opener(request, timeout=SEND_TIMEOUT) as response:
                response.read()
            sent += 1
        except urllib.error.HTTPError as exc:
            reason = reason_of(exc)
            if exc.code in (404, 410):
                remove_subscription(sub["endpoint"], path)
                problems.append("HTTP %d %s (subscription removed)" % (exc.code, reason or "gone"))
            else:
                problems.append(("HTTP %d %s" % (exc.code, reason)).strip())
        except urllib.error.URLError as exc:
            problems.append("URLError %s" % type(exc.reason).__name__)
        except Exception as exc:
            problems.append(type(exc).__name__)
    return sent, problems
