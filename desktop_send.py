"""Send one phone message into a Desktop chat through the Hermes gateway.

Since Hermes v0.22.5 the HTTP API refuses a turn in any chat whose source is
not "api_server" or "bot_room" (gateway/session_api_turn.py), so a phone send
into a chat started on a Desktop fails with 409 permission_denied. The way in
that v0.22 still takes is the one the Desktop and `hermes chat --resume` use:
the gateway's JSON-RPC WebSocket, through Hermes's own client
(hermes_cli/gateway_client.py). Its ticket is minted over the gateway's
private Unix socket, so this runs as the same macOS user with no password and
no key. tui_gateway/owner_forward.py does the same steps and is the reference.

serve.py runs this as a subprocess, through scripts/py -I, with HERMES_HOME
set to the chat's profile home. That keeps Hermes's own modules out of the
proxy process and makes profiles work.

    scripts/py -I desktop_send.py --check     {"ok": true} when the client imports
    scripts/py -I desktop_send.py < request   one JSON request on stdin

Request: {"session_id", "text", "pictures": [host paths], "image_refs": false}
Result (stdout, one line): {"ok": true, "admission_id", "status", "session_id"}
or {"ok": false, "reason", "message"}. "message" is the text the page shows.

It returns once the gateway has admitted the message; it does not wait for
the reply. It never sends prompt.submit twice: once the request is on the
socket, a lost answer may still have been queued, so that case is reported as
unconfirmed and left to the person (see owner_forward.py, _submit).
Nothing here prints the message text or picture paths anywhere but stdout's
own result, and the result carries neither.
"""
import asyncio
import base64
import json
import os
import re
import sys
import uuid

ADMIT_TIMEOUT = 60       # seconds for connect + resume + attach + the admission receipt
REQUEST_MAX = 512 * 1024  # stdin
PICTURE_MAX = 25 * 1024 * 1024  # the gateway's own attach limit
PICTURES_MAX = 8

# Shown on the phone as is.
TEXT_COORDINATION = ("This chat is open on another Desktop. Send it from there, "
                     "or quit Hermes on that Desktop and try again in a minute.")
TEXT_NOT_FOUND = "Hermes can't open this chat from the phone."
TEXT_REFUSED = "Hermes refused the message (%s)."
TEXT_UNAVAILABLE = "Hermes's gateway on the Mac did not answer. Nothing was sent."
TEXT_UNCONFIRMED = ("Hermes did not confirm the message. It may still arrive, "
                    "so check the chat before sending it again.")
TEXT_PICTURE = "A picture could not be read on the Mac. Nothing was sent."
TEXT_NO_CLIENT = "This Hermes has no gateway client (it needs Hermes v0.22 or newer)."

# The owner's prompt.submit refusals raised before its durable write: only
# these prove nothing was queued (tui_gateway/owner_forward.py,
# _REFUSED_BEFORE_COMMIT). Anything else after the request went out is
# "unconfirmed", never "nothing was sent".
REFUSED_BEFORE_COMMIT = frozenset({
    "invalid_params", "runtime_draining", "runtime_retired", "profile_mismatch", "permission_denied",
    "not_found", "session_superseded", "stale_epoch", "admission_conflict", "queue_full"})
ADMITTED = ("queued", "started", "terminal")
REASON = re.compile(r"^[A-Za-z0-9_]{1,80}$")


def agent_dir():
    return os.environ.get("HERMES_AGENT_DIR") or os.path.join(os.path.expanduser("~"), ".hermes", "hermes-agent")


def load_client():
    """(connect_gateway, GatewayRPCError) from this Mac's Hermes. Raises ImportError on Hermes
    before v0.22, which has no gateway_client."""
    folder = agent_dir()
    if os.path.isdir(folder) and folder not in sys.path:
        sys.path.insert(0, folder)
    from hermes_cli.gateway_client import GatewayRPCError, connect_gateway
    return connect_gateway, GatewayRPCError


def can_send():
    try:
        load_client()
    except Exception:
        return False
    return True


def reason_of(exc):
    """The bounded reason code of a gateway refusal: data.reason first (the owner
    names its refusal there; message may be display text), then the message."""
    data = getattr(exc, "data", None)
    for value in ((data or {}).get("reason") if isinstance(data, dict) else None, str(exc)):
        if isinstance(value, str) and REASON.match(value):
            return value
    return "request_failed"


def refusal(reason):
    if reason == "runtime_coordination_required":
        message = TEXT_COORDINATION
    elif reason == "not_found":
        message = TEXT_NOT_FOUND
    else:
        message = TEXT_REFUSED % (reason if REASON.match(reason or "") else "unknown")
    return {"ok": False, "reason": reason, "message": message}


def image_mime(head):
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        return "image/heic"
    return None


def ref_value(path):
    """A path as the Desktop's @image: directive quotes it (page/app.js refValue)."""
    if not re.search(r"[\s()\[\]{}<>\"'`]", path):
        return path
    for quote in ("`", '"', "'"):
        if quote not in path:
            return quote + path + quote
    return path


def read_pictures(paths):
    """[(name, base64, mime)] or None when any picture is missing or not an image."""
    out = []
    for path in paths:
        try:
            with open(path, "rb") as handle:
                data = handle.read(PICTURE_MAX + 1)
        except OSError:
            return None
        mime = image_mime(data[:32])
        if not data or len(data) > PICTURE_MAX or mime is None:
            return None
        out.append((os.path.basename(path), base64.b64encode(data).decode("ascii"), mime))
    return out


def message_text(text, paths, image_refs):
    """The text sent to prompt.submit. With image_refs (off by default) the
    phone's @image: lines are added too, for Desktops older than v0.22 that
    show only those; the model might then see each picture twice."""
    if not image_refs or not paths:
        return text
    return "\n".join([text] + ["@image:" + ref_value(p) for p in paths])


async def send(request, connect, rpc_error, mark=None):
    """Resume the chat, attach each picture, admit the message once.
    connect: an async context manager factory yielding a client with
    rpc(method, **params). rpc_error: the client's refusal class. mark: a dict
    that gets "sent": True the moment prompt.submit is handed to the client."""
    mark = {} if mark is None else mark
    session_id = str(request.get("session_id") or "")
    text = request.get("text") if isinstance(request.get("text"), str) else ""
    paths = [p for p in (request.get("pictures") or []) if isinstance(p, str)][:PICTURES_MAX]
    if not session_id:
        return refusal("invalid_params")
    pictures = read_pictures(paths)
    if pictures is None:
        return {"ok": False, "reason": "picture_unreadable", "message": TEXT_PICTURE}
    if not text.strip() and not pictures:
        return refusal("invalid_params")
    words = message_text(text, paths, bool(request.get("image_refs")))
    sent = False  # prompt.submit is on the socket: from here on, never "nothing was sent"
    try:
        async with connect() as client:
            try:
                resumed = await client.rpc("session.resume", session_id=session_id)
                target = str((resumed or {}).get("session_id") or session_id)
                attached = []
                for name, data, mime in pictures:
                    answer = await client.rpc("image.attach_bytes", session_id=target,
                                              content_base64=data, filename=name)
                    path = (answer or {}).get("path")
                    if not isinstance(path, str) or not path:
                        return refusal("attach_failed")
                    # Without this list the gateway runs the text and silently
                    # drops the picture (Studio test, Hermes v0.22.5).
                    attached.append({"path": path, "mime": mime})
            except rpc_error as exc:
                return refusal(reason_of(exc))
            params = {"session_id": target, "text": words, "submission_id": "phone-" + uuid.uuid4().hex}
            if attached:
                params["attachments"] = attached
            sent = mark["sent"] = True
            try:
                receipt = await client.rpc("prompt.submit", **params)
            except rpc_error as exc:
                reason = reason_of(exc)
                if reason in REFUSED_BEFORE_COMMIT:
                    return refusal(reason)
                return {"ok": False, "reason": "unconfirmed", "message": TEXT_UNCONFIRMED}
            receipt = receipt if isinstance(receipt, dict) else {}
            admission = receipt.get("admission_id")
            if not isinstance(admission, str) or receipt.get("status") not in ADMITTED:
                return refusal("not_admitted")
            return {"ok": True, "admission_id": admission, "status": receipt.get("status"),
                    "session_id": target}
    except Exception:
        if sent:
            return {"ok": False, "reason": "unconfirmed", "message": TEXT_UNCONFIRMED}
        return {"ok": False, "reason": "gateway_unavailable", "message": TEXT_UNAVAILABLE}


async def send_bounded(request, connect, rpc_error, timeout=ADMIT_TIMEOUT):
    """send() with one deadline. A deadline after prompt.submit went out is
    unconfirmed; before it, nothing was sent."""
    mark = {}
    try:
        return await asyncio.wait_for(send(request, connect, rpc_error, mark), timeout)
    except asyncio.TimeoutError:
        if mark.get("sent"):
            return {"ok": False, "reason": "unconfirmed", "message": TEXT_UNCONFIRMED}
        return {"ok": False, "reason": "gateway_unavailable", "message": TEXT_UNAVAILABLE}


def main(argv):
    if "--check" in argv:
        print(json.dumps({"ok": can_send()}))
        return 0
    try:
        raw = sys.stdin.buffer.read(REQUEST_MAX + 1)
        request = json.loads(raw.decode("utf-8")) if len(raw) <= REQUEST_MAX else None
    except (ValueError, UnicodeDecodeError):
        request = None
    if not isinstance(request, dict):
        print(json.dumps(refusal("invalid_params")))
        return 0
    try:
        connect, rpc_error = load_client()
    except Exception:
        print(json.dumps({"ok": False, "reason": "no_gateway_client", "message": TEXT_NO_CLIENT}))
        return 0
    result = asyncio.run(send_bounded(request, connect, rpc_error))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
