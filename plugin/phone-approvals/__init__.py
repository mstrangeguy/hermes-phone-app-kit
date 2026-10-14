"""Answer approval prompts from the phone chat page.

Hermes keeps a pending approval only in the memory of the process that raised
it (for the desktop app, its `hermes serve` backend). This plugin runs inside
that process. When a prompt opens it writes one marker file the phone proxy can
read; a background thread picks up the phone's answer from a decision file and
resolves that exact request. The desktop withdraws its own prompt when that
happens (Hermes's settle hook).

Files, under <hermes home>/phone-approvals/ (directories 700, files 600):
  pending/<request_id>.json    written here, removed when the prompt ends
  decisions/<request_id>.json  written by the phone proxy, consumed here

Only "once" and "deny" are accepted from the phone. A decision is applied only
when its request id is still pending in THIS process, so a stale, replayed or
invented file does nothing. If anything here fails, the prompt still works on
the desktop and times out to deny as usual.

Relies on tools.approval internals (list_gateway_approvals,
resolve_gateway_approval). Re-run tests/plugin_test.py after a Hermes update.
"""

import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger(__name__)

CHOICES = ("once", "deny")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TICK = 1.0
DECISION_TTL = 15 * 60
SWEEP_EVERY = 60
COMMAND_LIMIT = 4000

_lock = threading.Lock()
# request_id -> (session_key, pending_dir, decisions_dir). Directories are
# captured in the hook, inside the turn's profile scope; the worker thread
# runs outside it.
_mine = {}
_worker: list = [None]
_last_sweep = [0.0]


def _dirs():
    from hermes_constants import get_hermes_home

    base = os.path.join(str(get_hermes_home()), "phone-approvals")
    pending = os.path.join(base, "pending")
    decisions = os.path.join(base, "decisions")
    for path in (base, pending, decisions):
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    return pending, decisions


def _write_json(path, data):
    tmp = "%s.tmp%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _session_env(name):
    try:
        from gateway.session_context import get_session_env

        return get_session_env(name, "") or ""
    except Exception:
        return ""


def _redact(command):
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(str(command or ""), force=True)
    except Exception:
        # Never show a command we could not redact.
        return ""
    if len(text) > COMMAND_LIMIT:
        text = text[:COMMAND_LIMIT] + "\n… (cut)"
    return text


def _live_ids(session_key):
    from tools.approval import list_gateway_approvals

    return {entry.get("request_id") for entry in list_gateway_approvals(session_key)}


def _on_request(session_key="", coalesced=False, **kwargs):
    # A coalesced follower adds no prompt of its own; its leader already has a marker.
    if coalesced or not session_key:
        return
    # Replies the phone started have their own card on the run stream.
    if _session_env("HERMES_SESSION_PLATFORM") == "api_server":
        return
    try:
        from tools.approval import list_gateway_approvals

        entries = list_gateway_approvals(session_key)
        pending_dir, decisions_dir = _dirs()
    except Exception:
        logger.debug("phone-approvals: could not read the approval queue", exc_info=True)
        return
    with _lock:
        new = [e for e in entries if REQUEST_ID.match(str(e.get("request_id") or ""))
               and e["request_id"] not in _mine]
        for entry in new:
            _mine[entry["request_id"]] = (session_key, pending_dir, decisions_dir)
    source = _session_env("HERMES_SESSION_SOURCE") or _session_env("HERMES_SESSION_PLATFORM")
    for entry in new:
        rid = entry["request_id"]
        marker = {
            "request_id": rid,
            "session_key": session_key,
            "pid": os.getpid(),
            "created": time.time(),
            "source": source,
            "command": _redact(entry.get("command")),
            "description": str(entry.get("description") or "")[:500],
            "smart_denied": bool(entry.get("smart_denied")),
        }
        try:
            _write_json(os.path.join(pending_dir, rid + ".json"), marker)
        except OSError:
            logger.debug("phone-approvals: could not write marker", exc_info=True)
    if new:
        _ensure_worker()


def _on_response(session_key="", **kwargs):
    # The prompt ended (answered anywhere, timed out, withdrawn): drop its marker now.
    if session_key:
        try:
            _reconcile(only_key=session_key)
        except Exception:
            logger.debug("phone-approvals: reconcile failed", exc_info=True)


def _read_choice(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return ""
    choice = data.get("choice") if isinstance(data, dict) else ""
    return choice if choice in CHOICES else ""


def _reconcile(only_key=None):
    """Apply phone decisions to our own pending prompts and clear finished markers.
    Returns True while this process still has prompts to watch."""
    from tools.approval import resolve_gateway_approval

    with _lock:
        mine = dict(_mine)
    for rid, (key, pending_dir, decisions_dir) in mine.items():
        if only_key is not None and key != only_key:
            continue
        decision = os.path.join(decisions_dir, rid + ".json")
        live = rid in _live_ids(key)
        if live and only_key is None and os.path.exists(decision):
            choice = _read_choice(decision)
            _remove(decision)
            if choice:
                resolved = resolve_gateway_approval(key, choice, request_id=rid)
                logger.info("phone-approvals: %s from the phone (%d resolved)", choice, resolved)
                live = rid in _live_ids(key)
        if not live:
            _remove(os.path.join(pending_dir, rid + ".json"))
            _remove(decision)
            with _lock:
                _mine.pop(rid, None)
    with _lock:
        return bool(_mine)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (OSError, ValueError, TypeError):
        return True
    return True


def _sweep(pending_dir, decisions_dir):
    """Remove markers left by a process that died, and decisions nobody consumed."""
    now = time.time()
    for name in os.listdir(pending_dir):
        path = os.path.join(pending_dir, name)
        try:
            with open(path, encoding="utf-8") as handle:
                pid = json.load(handle).get("pid")
        except (OSError, ValueError, AttributeError):
            pid = None
        if pid is not None and not _pid_alive(pid):
            _remove(path)
    for name in os.listdir(decisions_dir):
        path = os.path.join(decisions_dir, name)
        try:
            if now - os.path.getmtime(path) > DECISION_TTL:
                _remove(path)
        except OSError:
            pass


def _run():
    while True:
        try:
            _reconcile()
            if time.monotonic() - _last_sweep[0] > SWEEP_EVERY:
                _last_sweep[0] = time.monotonic()
                with _lock:
                    homes = {(p, d) for _, p, d in _mine.values()}
                for pending_dir, decisions_dir in homes:
                    _sweep(pending_dir, decisions_dir)
        except Exception:
            logger.debug("phone-approvals: worker tick failed", exc_info=True)
        with _lock:
            if not _mine:
                _worker[0] = None
                return
        time.sleep(TICK)


def _ensure_worker():
    with _lock:
        if _worker[0] is not None and _worker[0].is_alive():
            return
        thread = threading.Thread(target=_run, name="phone-approvals", daemon=True)
        _worker[0] = thread
    thread.start()


def register(ctx):
    ctx.register_hook("pre_approval_request", _on_request)
    ctx.register_hook("post_approval_response", _on_response)
