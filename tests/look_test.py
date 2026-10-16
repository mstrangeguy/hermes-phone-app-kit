"""Offline test of the per-install name and accent color (config.json
app_name / accent_color).

Usage: python3 -I tests/look_test.py <repo-dir> <empty-scratch-dir>
"""
import http.client
import json
import os
import socket
import sys
import threading

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
allow = os.path.join(work, "allowed-logins.txt")
with open(allow, "w") as handle:
    handle.write("owner@example.com\n")
serve.ALLOW_PATH = allow

results = []


def check(name, ok):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name)


# --- Settings parsing --------------------------------------------------------
check("defaults", serve.app_look({}) == ("Hermes", ""))
check("name and color", serve.app_look({"app_name": " Hermes Work ", "accent_color": "#E5A50A"}) == ("Hermes Work", "#e5a50a"))
check("short color", serve.app_look({"accent_color": "#0f0"})[1] == "#0f0")
check("markup stripped from name", serve.app_look({"app_name": "<b>Work</b>\"'&"})[0] == "bWork/b")
check("long name cut", len(serve.app_look({"app_name": "x" * 80})[0]) == 30)
check("blank name -> default", serve.app_look({"app_name": "  "})[0] == "Hermes")
check("non-string name -> default", serve.app_look({"app_name": 5})[0] == "Hermes")
for bad in ("red", "#12345", "#gggggg", "#4a84fe; color: red", "url(x)", 7):
    check("bad color refused %r" % (bad,), serve.app_look({"accent_color": bad})[1] == "")
check("light accent gets dark text", b"--primary-fg: #161616" in serve.theme_css("#e5a50a"))
check("dark accent gets white text", b"--primary-fg: #ffffff" in serve.theme_css("#1a237e"))
check("no accent: no override", b"--primary" not in serve.theme_css(""))
check("accent colors the header name", b".wordmark-btn { color: var(--primary); }" in serve.theme_css("#e5a50a"))
check("no accent: header name stays white", b"wordmark" not in serve.theme_css(""))

# --- Served page -------------------------------------------------------------
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
listener.close()
server = serve.Server(("127.0.0.1", port), serve.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def get(path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers={"Tailscale-User-Login": "owner@example.com"})
    r = c.getresponse()
    body = r.read()
    length = r.getheader("Content-Length")
    c.close()
    return r.status, body, length


with open(os.path.join(repo, "page", "index.html"), "rb") as handle:
    original = handle.read()
serve.APP_NAME, serve.ACCENT = "Hermes", ""
status, body, _ = get("/")
check("default page unchanged", status == 200 and body == original)
status, body, _ = get("/theme.css")
check("default theme.css served", status == 200 and b"--primary" not in body)

serve.APP_NAME, serve.ACCENT = "Hermes Work", "#e5a50a"
status, body, length = get("/")
text = body.decode("utf-8")
check("title renamed", "<title>Hermes Work</title>" in text)
check("Home Screen title renamed", 'name="apple-mobile-web-app-title" content="Hermes Work"' in text)
check("wordmarks renamed", text.count(">Hermes Work<") >= 5 and ">Hermes<" not in text)
check("content-length matches", int(length) == len(body))
check("theme.css linked", 'href="theme.css"' in text)
status, body, _ = get("/manifest.webmanifest")
data = json.loads(body)
check("manifest renamed", status == 200 and data["name"] == data["short_name"] == "Hermes Work")
status, body, _ = get("/theme.css")
check("accent served", status == 200 and b"--primary: #e5a50a" in body)
check("theme.css in CSP self", "style-src 'self'" in serve.CSP)

server.shutdown()
passed = sum(results)
print("%d/%d passed" % (passed, len(results)))
sys.exit(0 if passed == len(results) else 1)
