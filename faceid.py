"""Face ID (passkey) lock for the phone page.

A passkey made on the iPhone signs a one-time challenge only after Face ID (or
the phone passcode). This module issues those challenges, checks the
signatures, and keeps the short-lived unlock sessions. serve.py calls it.

State, next to serve.py, both gitignored and mode 600:
  faceid.json        {"rp_id", "origin", "user_id", "credentials": [...]}
  faceid-setup.json  one-time setup code (hashed), 10 minutes, 5 tries

Before any passkey is enrolled the page keeps working with its API key. Once
one is enrolled serve.py requires an unlocked session for everything except
the page files and /phone-api/auth/*, and adds the API key itself.

The signature check needs the `cryptography` package; it is imported only when
a signature is checked, so the proxy still starts without it. Without it an
unlock simply fails (closed).

Python 3.9 compatible: the proxy may run on /usr/bin/python3.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "faceid.json")
SETUP_PATH = os.path.join(ROOT, "faceid-setup.json")

SETUP_TTL = 10 * 60
SETUP_TRIES = 5
CHALLENGE_TTL = 3 * 60
SESSION_IDLE = 30 * 60
SESSION_MAX = 12 * 60 * 60
COOKIE = "hermes_phone_session"

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_AT = 0x40

clock = time.time  # tests replace this

_lock = threading.Lock()
_challenges = {}  # challenge (b64url) -> {"purpose", "binding", "expires"}
_sessions = {}  # token -> {"created", "last"}


class AuthError(Exception):
    """A check failed. The message names the reason class, never a secret."""


# --- encoding ------------------------------------------------------------------

def b64e(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text):
    if not isinstance(text, str) or len(text) > 8192:
        raise AuthError("bad base64")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise AuthError("bad base64")


def cbor_decode(data, pos=0, depth=0):
    """The small CBOR subset WebAuthn uses. Returns (value, next_position)."""
    if depth > 8:
        raise AuthError("cbor too deep")
    if pos >= len(data):
        raise AuthError("cbor truncated")
    head = data[pos]
    major, info = head >> 5, head & 0x1F
    pos += 1
    if info < 24:
        val = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        if pos + size > len(data):
            raise AuthError("cbor truncated")
        val = int.from_bytes(data[pos:pos + size], "big")
        pos += size
    else:
        raise AuthError("cbor unsupported")
    if major == 0:
        return val, pos
    if major == 1:
        return -1 - val, pos
    if major in (2, 3):
        if pos + val > len(data):
            raise AuthError("cbor truncated")
        raw = bytes(data[pos:pos + val])
        if major == 3:
            try:
                return raw.decode("utf-8"), pos + val
            except UnicodeDecodeError:
                raise AuthError("cbor text")
        return raw, pos + val
    if major == 4:
        if val > 64:
            raise AuthError("cbor too long")
        items = []
        for _ in range(val):
            item, pos = cbor_decode(data, pos, depth + 1)
            items.append(item)
        return items, pos
    if major == 5:
        if val > 64:
            raise AuthError("cbor too long")
        out = {}
        for _ in range(val):
            k, pos = cbor_decode(data, pos, depth + 1)
            v, pos = cbor_decode(data, pos, depth + 1)
            if isinstance(k, (list, dict)):
                raise AuthError("cbor key")
            out[k] = v
        return out, pos
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], pos
    raise AuthError("cbor unsupported")


def parse_auth_data(data):
    if len(data) < 37:
        raise AuthError("authenticator data too short")
    out = {"rp_hash": data[:32], "flags": data[32], "count": int.from_bytes(data[33:37], "big")}
    if out["flags"] & FLAG_AT:
        if len(data) < 55:
            raise AuthError("attested data too short")
        length = int.from_bytes(data[53:55], "big")
        if 55 + length > len(data):
            raise AuthError("credential id truncated")
        out["cred_id"] = data[55:55 + length]
        out["cose"], _ = cbor_decode(data, 55 + length)
    return out


def cose_p256(cose):
    """(x, y) from an ES256 COSE key; refuses anything else."""
    if not isinstance(cose, dict) or cose.get(1) != 2 or cose.get(3) != -7 or cose.get(-1) != 1:
        raise AuthError("unsupported key type")
    x, y = cose.get(-2), cose.get(-3)
    if not isinstance(x, bytes) or not isinstance(y, bytes) or len(x) != 32 or len(y) != 32:
        raise AuthError("bad key")
    return x, y


def _public_key(x, y):
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        return ec.EllipticCurvePublicNumbers(
            int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()
    except ValueError:
        raise AuthError("key not on curve")


def _verify_signature(x, y, signature, signed):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        _public_key(x, y).verify(signature, signed, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise AuthError("bad signature")


# --- stored state ----------------------------------------------------------------

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path, data):
    tmp = "%s.tmp%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)
    os.replace(tmp, path)


def load_config():
    data = _read_json(CONFIG_PATH)
    if not data or not data.get("rp_id") or not data.get("origin"):
        return None
    creds = data.get("credentials")
    data["credentials"] = creds if isinstance(creds, list) else []
    return data


def enrolled():
    config = load_config()
    return bool(config and config["credentials"])


def configure(rp_id, origin):
    config = _read_json(CONFIG_PATH) or {}
    config["rp_id"] = rp_id
    config["origin"] = origin.rstrip("/")
    config.setdefault("user_id", b64e(secrets.token_bytes(16)))
    config.setdefault("credentials", [])
    _write_json(CONFIG_PATH, config)
    return config


# --- setup code --------------------------------------------------------------------

def _code_hash(salt, code):
    return hashlib.sha256((salt + ":" + code).encode("utf-8")).hexdigest()


def new_setup_code():
    """Made at the Mac. Returns the code to type on the phone."""
    if load_config() is None:
        raise AuthError("run --faceid-config first")
    code = "%08d" % secrets.randbelow(10 ** 8)
    salt = secrets.token_hex(16)
    _write_json(SETUP_PATH, {"salt": salt, "hash": _code_hash(salt, code),
                             "expires": clock() + SETUP_TTL, "tries": 0})
    return code[:4] + "-" + code[4:]


def setup_open():
    data = _read_json(SETUP_PATH)
    return bool(data and data.get("expires", 0) > clock() and data.get("tries", 0) < SETUP_TRIES)


def check_setup_code(code):
    """True for the current code. Every wrong try counts; the fifth kills it."""
    with _lock:
        data = _read_json(SETUP_PATH)
        if not data or data.get("expires", 0) <= clock() or data.get("tries", 0) >= SETUP_TRIES:
            return False
        typed = "".join(ch for ch in str(code or "") if ch.isdigit())
        ok = hmac.compare_digest(_code_hash(data["salt"], typed), data["hash"])
        if not ok:
            data["tries"] = data.get("tries", 0) + 1
            _write_json(SETUP_PATH, data)
        return ok


def _end_setup():
    try:
        os.remove(SETUP_PATH)
    except FileNotFoundError:
        pass


# --- challenges --------------------------------------------------------------------

def binding_for_approval(kind, run_id, request_id):
    """What an Allow-once signature is good for: one approval, one choice."""
    return "approve:%s:%s:%s:once" % (kind, run_id or "", request_id or "")


def _issue(purpose, binding):
    challenge = b64e(secrets.token_bytes(32))
    now = clock()
    with _lock:
        for key in [k for k, v in _challenges.items() if v["expires"] <= now]:
            del _challenges[key]
        if len(_challenges) > 200:
            raise AuthError("too many open challenges")
        _challenges[challenge] = {"purpose": purpose, "binding": binding, "expires": now + CHALLENGE_TTL}
    return challenge


def _take(challenge, purpose, binding):
    with _lock:
        entry = _challenges.pop(challenge, None)
    if not entry or entry["expires"] <= clock():
        raise AuthError("unknown or expired challenge")
    if entry["purpose"] != purpose or entry["binding"] != binding:
        raise AuthError("challenge for something else")


def registration_options():
    config = load_config()
    if config is None:
        raise AuthError("not configured")
    return {
        "challenge": _issue("register", "register"),
        "rp": {"id": config["rp_id"], "name": "Hermes phone"},
        "user": {"id": config["user_id"], "name": "Hermes phone", "displayName": "Hermes phone"},
        "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
        "authenticatorSelection": {"authenticatorAttachment": "platform", "residentKey": "preferred",
                                   "userVerification": "required"},
        "attestation": "none",
        "timeout": 60000,
        "excludeCredentials": [{"type": "public-key", "id": c["id"]} for c in config["credentials"]],
    }


def assertion_options(purpose, binding):
    config = load_config()
    if not config or not config["credentials"]:
        raise AuthError("no passkey enrolled")
    return {
        "challenge": _issue(purpose, binding),
        "rpId": config["rp_id"],
        "allowCredentials": [{"type": "public-key", "id": c["id"]} for c in config["credentials"]],
        "userVerification": "required",
        "timeout": 60000,
    }


def _client_data(credential, kind, purpose, binding, config):
    raw = b64d((credential.get("response") or {}).get("clientDataJSON"))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AuthError("bad client data")
    if not isinstance(data, dict) or data.get("type") != kind:
        raise AuthError("wrong ceremony")
    if data.get("origin") != config["origin"]:
        raise AuthError("wrong origin")
    if data.get("crossOrigin") is True:
        raise AuthError("cross-origin")
    challenge = data.get("challenge")
    if not isinstance(challenge, str):
        raise AuthError("no challenge")
    _take(b64e(b64d(challenge)), purpose, binding)
    return raw


def _check_flags(auth, config):
    if not hmac.compare_digest(auth["rp_hash"], hashlib.sha256(config["rp_id"].encode("utf-8")).digest()):
        raise AuthError("wrong rp id")
    if not auth["flags"] & FLAG_UP:
        raise AuthError("user not present")
    if not auth["flags"] & FLAG_UV:
        raise AuthError("Face ID or passcode not used")


def finish_registration(credential, label="iPhone"):
    """Check a new passkey (after a correct setup code) and save it."""
    if not isinstance(credential, dict):
        raise AuthError("bad credential")
    config = load_config()
    if config is None:
        raise AuthError("not configured")
    _client_data(credential, "webauthn.create", "register", "register", config)
    att, _ = cbor_decode(b64d((credential.get("response") or {}).get("attestationObject")))
    if not isinstance(att, dict) or not isinstance(att.get("authData"), bytes):
        raise AuthError("bad attestation")
    auth = parse_auth_data(att["authData"])
    _check_flags(auth, config)
    if "cred_id" not in auth:
        raise AuthError("no credential in attestation")
    x, y = cose_p256(auth["cose"])
    _public_key(x, y)
    cred_id = b64e(auth["cred_id"])
    if b64e(b64d(credential.get("rawId") or credential.get("id") or "")) != cred_id:
        raise AuthError("credential id mismatch")
    with _lock:
        config = load_config()
        if config is None:
            raise AuthError("not configured")
        if any(c["id"] == cred_id for c in config["credentials"]):
            raise AuthError("already enrolled")
        config["credentials"].append({"id": cred_id, "x": b64e(x), "y": b64e(y), "count": auth["count"],
                                      "label": str(label)[:40], "created": clock()})
        _write_json(CONFIG_PATH, config)
    _end_setup()
    return cred_id


def verify_assertion(credential, purpose, binding):
    """Raise AuthError unless *credential* is a fresh Face ID signature by an
    enrolled passkey over a challenge issued for exactly (purpose, binding)."""
    if not isinstance(credential, dict):
        raise AuthError("no signature")
    config = load_config()
    if not config or not config["credentials"]:
        raise AuthError("no passkey enrolled")
    cred_id = b64e(b64d(credential.get("rawId") or credential.get("id") or ""))
    stored = next((c for c in config["credentials"] if c["id"] == cred_id), None)
    if stored is None:
        raise AuthError("unknown passkey")
    raw_client = _client_data(credential, "webauthn.get", purpose, binding, config)
    response = credential.get("response") or {}
    auth_raw = b64d(response.get("authenticatorData"))
    auth = parse_auth_data(auth_raw)
    _check_flags(auth, config)
    _verify_signature(b64d(stored["x"]), b64d(stored["y"]), b64d(response.get("signature")),
                      auth_raw + hashlib.sha256(raw_client).digest())
    # iCloud Keychain passkeys always report 0; enforce the counter only when it moves.
    if auth["count"] or stored.get("count"):
        if auth["count"] <= stored.get("count", 0):
            raise AuthError("signature counter went backwards")
        with _lock:
            config = load_config() or {"credentials": []}
            for c in config["credentials"]:
                if c["id"] == cred_id:
                    c["count"] = auth["count"]
            _write_json(CONFIG_PATH, config)


# --- sessions ----------------------------------------------------------------------

def new_session():
    token = secrets.token_urlsafe(32)
    now = clock()
    with _lock:
        _sessions[token] = {"created": now, "last": now}
    return token


def session_valid(token, touch=False):
    if not token:
        return False
    now = clock()
    with _lock:
        entry = _sessions.get(token)
        if not entry:
            return False
        if now - entry["last"] > SESSION_IDLE or now - entry["created"] > SESSION_MAX:
            del _sessions[token]
            return False
        if touch:
            entry["last"] = now
        return True


def drop_session(token):
    with _lock:
        _sessions.pop(token or "", None)


def session_cookie(token):
    return "%s=%s; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=%d" % (COOKIE, token, SESSION_MAX)


def clear_cookie():
    return "%s=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0" % COOKIE


def cookie_token(lines):
    for line in lines:
        name, sep, value = line.partition(b":")
        if not sep or name.strip().lower() != b"cookie":
            continue
        for part in value.decode("iso-8859-1").split(";"):
            key, eq, val = part.strip().partition("=")
            if eq and key == COOKIE:
                return val.strip()
    return ""
