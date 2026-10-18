"""Offline test of serve.py's picture endpoints against a throwaway Hermes home.

Usage: python3 -I tests/pictures_test.py <repo-dir> <empty-scratch-dir>

Covers: upload saves a real image under <profile home>/images (mode 600,
named by its bytes, not the request), refuses non-images, oversize, chunked and
empty bodies and unknown profiles; a file upload goes to <profile home>/attachments
under a cleaned own name, never overwriting; media serves images only, under the home or
temp folders only, never through a symlink out, and both sit behind the key /
Face ID gate. File paths are never written to the log.
"""
import http.client
import json
import os
import socket
import stat
import struct
import sys
import threading
import zlib

repo, work = sys.argv[1], sys.argv[2]
os.makedirs(work, exist_ok=True)
sys.path.insert(0, repo)
import serve  # noqa: E402
import faceid  # noqa: E402

serve.CONFIG_PATH = os.path.join(work, "config.json")
faceid.CONFIG_PATH = os.path.join(work, "faceid.json")
faceid.SETUP_PATH = os.path.join(work, "faceid-setup.json")
for _path in (faceid.CONFIG_PATH, faceid.SETUP_PATH, serve.CONFIG_PATH):
    _scratch = os.path.realpath(work)
    if os.path.commonpath([os.path.realpath(_path), _scratch]) != _scratch:
        print("FAIL ISOLATION: would read live file %s" % _path)
        sys.exit(1)

DEFAULT_KEY = "default-key-0123456789"
OTHER_KEY = "other-key-abcdefghijklmnop"
home = os.path.join(work, "home")
other = os.path.join(home, "profiles", "second")
os.makedirs(other, exist_ok=True)
with open(os.path.join(home, ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=%s\n" % DEFAULT_KEY)
with open(os.path.join(other, ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=%s\n" % OTHER_KEY)
allow = os.path.join(work, "allowed-logins.txt")
with open(allow, "w") as handle:
    handle.write("# test\nowner@example.com\n")

serve.HERMES_HOME = home
serve.STATE_DB = os.path.join(home, "state.db")
serve.ALLOW_PATH = allow
LOGGED = []
serve.log = lambda message: LOGGED.append(message)
# Media may only come from the home or temp folders; for the test that is the scratch home.
outside = os.path.join(work, "outside")
os.makedirs(outside, exist_ok=True)
serve.media_roots = lambda: [os.path.realpath(home)]


def png(w=4, h=4):
    raw = b"".join(b"\x00" + b"\xdc\x1e\x1e" * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = png()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64

results = []


def check(name, ok):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name)


listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
listener.close()
server = serve.Server(("127.0.0.1", port), serve.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def call(path, method="GET", body=None, key=DEFAULT_KEY, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Tailscale-User-Login": "owner@example.com"}
    if key:
        h["Authorization"] = "Bearer " + key
    h.update(headers or {})
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    ctype = r.getheader("Content-Type") or ""
    c.close()
    try:
        return r.status, json.loads(data), ctype
    except ValueError:
        return r.status, data, ctype


# --- Image kind ---------------------------------------------------------------
check("png by bytes", serve.image_kind(PNG[:32]) == ("image/png", "png"))
check("jpeg by bytes", serve.image_kind(JPEG[:32]) == ("image/jpeg", "jpg"))
check("heic by bytes", serve.image_kind(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8) == ("image/heic", "heic"))
check("text is not an image", serve.image_kind(b"API_SERVER_KEY=x") is None)

# --- Upload -------------------------------------------------------------------
status, body, _ = call("/phone-api/upload", "POST", PNG, headers={"Content-Type": "image/jpeg"})
check("upload 201", status == 201)
saved = body.get("path", "") if isinstance(body, dict) else ""
folder = os.path.join(home, "images")
check("saved under profile home", os.path.dirname(saved) == folder)
check("named by its bytes, not the header", saved.endswith(".png") and body.get("type") == "image/png")
check("same bytes on disk", open(saved, "rb").read() == PNG if saved else False)
check("file mode 600", saved and stat.S_IMODE(os.stat(saved).st_mode) == 0o600)
check("folder mode 700", stat.S_IMODE(os.stat(folder).st_mode) == 0o700)

status, body, _ = call("/phone-api/upload?profile=second", "POST", JPEG, headers={"Content-Type": "image/jpeg"})
check("upload to another profile", status == 201 and body["path"].startswith(os.path.join(other, "images")))
check("unknown profile 404", call("/phone-api/upload?profile=nope", "POST", PNG)[0] == 404)
check("non-image 415", call("/phone-api/upload", "POST", b"#!/bin/sh\necho hi\n")[0] == 415)
check("empty 400", call("/phone-api/upload", "POST", b"")[0] == 400)
serve.UPLOAD_MAX, real_max = 1024, serve.UPLOAD_MAX
check("too large 413", call("/phone-api/upload", "POST", PNG + b"\x00" * 2048)[0] == 413)
serve.UPLOAD_MAX = real_max


def chunked_upload():
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"POST /phone-api/upload HTTP/1.1\r\nHost: x\r\nTailscale-User-Login: owner@example.com\r\n"
              b"Authorization: Bearer " + DEFAULT_KEY.encode() + b"\r\nTransfer-Encoding: chunked\r\n\r\n"
              b"4\r\nabcd\r\n0\r\n\r\n")
    data = s.recv(4096)
    s.close()
    return data


check("chunked 411", chunked_upload().startswith(b"HTTP/1.1 411"))
check("wrong key 401", call("/phone-api/upload", "POST", PNG, key="wrong-key-0000000000")[0] == 401)
check("no key 401", call("/phone-api/upload", "POST", PNG, key="")[0] == 401)
check("wrong login 403", call("/phone-api/upload", "POST", PNG,
                              headers={"Tailscale-User-Login": "someone@example.com"})[0] == 403)
count = len(os.listdir(folder))
check("refusals wrote nothing", count == 1)

# --- File uploads -------------------------------------------------------------
attach = os.path.join(home, "attachments")
PDF = b"%PDF-1.4\n" + b"x" * 200
fq = lambda name: "/phone-api/upload?kind=file&name=" + serve.urllib.parse.quote(name)  # noqa: E731
status, body, _ = call(fq("Report Q3.pdf"), "POST", PDF)
check("file 201", status == 201 and body["name"] == "Report Q3.pdf")
check("file saved under attachments, own name", body["path"] == os.path.join(attach, "Report Q3.pdf"))
check("file bytes unchanged", open(body["path"], "rb").read() == PDF)
check("file mode 600", stat.S_IMODE(os.stat(body["path"]).st_mode) == 0o600)
status, body, _ = call(fq("Report Q3.pdf"), "POST", b"%PDF-1.4\nsecond")
check("same name gets -2, first kept", body["name"] == "Report Q3-2.pdf"
      and open(os.path.join(attach, "Report Q3.pdf"), "rb").read() == PDF)
status, body, _ = call(fq("../../.env"), "POST", b"API_SERVER_KEY=evil\n")
check("path in name stripped", status == 201 and os.path.dirname(body["path"]) == attach
      and body["name"] == "env" and open(os.path.join(home, ".env")).read().startswith("API_SERVER_KEY=default"))
status, body, _ = call(fq(".hidden"), "POST", b"x")
check("leading dot dropped", body["name"] == "hidden")
status, body, _ = call(fq("a\x00b\nc.txt"), "POST", b"x")
check("control characters replaced", "\x00" not in body["name"] and "\n" not in body["name"])
status, body, _ = call(fq(""), "POST", b"x")
check("no name -> attachment", body["name"] == "attachment")
status, body, _ = call(fq("x" * 300 + ".pdf"), "POST", b"x")
check("long name cut, extension kept", len(body["name"]) <= 120 and body["name"].endswith(".pdf"))
check("file a picture is fine too", call(fq("photo.png"), "POST", PNG)[0] == 201)
serve.ATTACH_MAX, real_attach = 64, serve.ATTACH_MAX
check("file too large 413", call(fq("big.bin"), "POST", b"x" * 100)[0] == 413)
serve.ATTACH_MAX = real_attach
check("file to another profile", call(fq("n.txt").replace("?", "?profile=second&"), "POST", b"x")[1]["path"]
      .startswith(os.path.join(other, "attachments")))
check("file needs the key", call(fq("k.txt"), "POST", b"x", key="")[0] == 401)
check("file never goes to images", all(n.startswith("phone_") for n in os.listdir(folder)))

# --- Media --------------------------------------------------------------------
q = lambda p: "/phone-api/media?path=" + serve.urllib.parse.quote(p)  # noqa: E731
status, data, ctype = call(q(saved))
check("media serves the upload", status == 200 and data == PNG and ctype == "image/png")
note = os.path.join(home, "notes.png")  # an image name, text inside
with open(note, "w") as handle:
    handle.write("API_SERVER_KEY=secret\n")
check("text named .png refused", call(q(note))[0] == 404)
check(".env refused", call(q(os.path.join(home, ".env")))[0] == 404)
away = os.path.join(outside, "away.png")
with open(away, "wb") as handle:
    handle.write(PNG)
check("image outside the roots refused", call(q(away))[0] == 404)
link = os.path.join(home, "link.png")
os.symlink(away, link)
check("symlink out of the roots refused", call(q(link))[0] == 404)
check("relative path refused", call(q("notes.png"))[0] == 404)
check("dot-dot out refused", call(q(os.path.join(home, "..", "outside", "away.png")))[0] == 404)
check("missing file 404", call(q(os.path.join(home, "none.png")))[0] == 404)
check("directory refused", call(q(home))[0] == 404)
check("media needs the key", call(q(saved), key="")[0] == 401)

# --- Face ID mode -------------------------------------------------------------
real = (faceid.enrolled, faceid.session_valid, faceid.load_config)
faceid.enrolled = lambda: True
faceid.load_config = lambda: {"origin": "https://phone.example", "credentials": [1]}
faceid.session_valid = lambda token, touch=False: False
check("locked media 401", call(q(saved), key="")[0] == 401)
check("locked upload 401", call("/phone-api/upload", "POST", PNG, key="",
                                headers={"Origin": "https://phone.example"})[0] == 401)
faceid.session_valid = lambda token, touch=False: True
check("unlocked media 200", call(q(saved), key="")[0] == 200)
check("upload from another origin 403", call("/phone-api/upload", "POST", PNG, key="",
                                             headers={"Origin": "https://other.example"})[0] == 403)
check("unlocked upload 201", call("/phone-api/upload", "POST", PNG, key="",
                                  headers={"Origin": "https://phone.example"})[0] == 201)
faceid.enrolled, faceid.session_valid, faceid.load_config = real

# --- Log ----------------------------------------------------------------------
logged = "\n".join(LOGGED)
check("upload logged by size only", "upload: photo, %d bytes" % len(PNG) in logged)
check("no file path or name in the log", "images/" not in logged and work not in logged and "Report" not in logged)
check("CSP allows previews", "blob:" in serve.CSP)

server.shutdown()
passed = sum(results)
print("%d passed, %d failed" % (passed, len(results) - passed))
sys.exit(0 if all(results) else 1)
