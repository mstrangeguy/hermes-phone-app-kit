"""Offline page worker (page/sw.js), run in node with a fake fetch and cache.

    scripts/py -I tests/sw_test.py <repo> <empty scratch dir>

Checks: the app always loads from the Mac first; the kept offline page shows
only when the Mac cannot be reached (network error, or a 502/503/504 from
Tailscale Serve); the app's scripts, /api and /phone-api are never answered
from the worker; only the offline page, stylesheets and icon are kept.
No network, no browser.
"""
import json
import os
import shutil
import subprocess
import sys

repo, work = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
results = []


def check(name, ok):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name)


def find_node():
    candidates = [os.environ.get("NODE") or "", shutil.which("node") or "",
                  os.path.expanduser("~/.hermes/node/bin/node")]
    for path in candidates:
        if path and os.access(path, os.X_OK):
            return path
    return None


JS = r"""
const fs = require("fs");
const vm = require("vm");
const out = [];
const ok = (name, cond) => out.push([name, Boolean(cond)]);
const SCOPE = "https://mac.example.ts.net:8443/";

class Resp {
  constructor(body, init) { this.body = body; this.status = (init && init.status) || 200; this.ok = this.status < 300; this.tag = (init && init.tag) || ""; }
  static error() { return new Resp("", { status: 0, tag: "error" }); }
}
const store = new Map();
const cache = { put: async (url, res) => { store.set(String(url), res); } };
let net = "up";  // up | down | 502
const fetched = [];
async function fakeFetch(req) {
  const url = typeof req === "string" ? req : req.url;
  fetched.push(url);
  if (net === "down") throw new TypeError("Load failed");
  if (net === "502") return new Resp("bad gateway", { status: 502, tag: "502" });
  return new Resp("from mac " + url, { tag: "mac" });
}
const handlers = {};
const self = {
  registration: { scope: SCOPE, showNotification: async () => {} },
  location: new URL(SCOPE + "sw.js"),
  addEventListener: (type, fn) => { handlers[type] = fn; },
  skipWaiting: async () => {},
  clients: { claim: async () => {}, matchAll: async () => [] },
  navigator: {}
};
const caches = {
  open: async () => cache,
  keys: async () => ["hermes-offline-v0", "hermes-offline-v1", "other-app"],
  delete: async (n) => { deleted.push(n); return true; },
  match: async (url) => store.get(String(url && url.url ? url.url : url))
};
const deleted = [];
const ctx = { self, caches, fetch: fakeFetch, Response: Resp, URL, Promise, console };
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), ctx);

async function fire(type, extra) {
  let responded = null;
  const waits = [];
  const ev = Object.assign({ waitUntil: (p) => waits.push(p), respondWith: (p) => { responded = p; } }, extra || {});
  handlers[type](ev);
  await Promise.all(waits);
  return responded ? await responded : null;
}
const req = (path, mode, method) => ({ url: SCOPE + path, mode: mode || "cors", method: method || "GET" });

(async () => {
  await fire("install");
  const kept = Array.from(store.keys()).map((u) => u.slice(SCOPE.length)).sort();
  ok("install keeps only offline page, styles and icon", JSON.stringify(kept) === JSON.stringify(["app.css", "icon-180.png", "offline.html", "theme.css"]));
  await fire("activate");
  ok("activate drops older offline caches only", JSON.stringify(deleted) === JSON.stringify(["hermes-offline-v0"]));

  net = "up";
  let r = await fire("fetch", { request: req("", "navigate") });
  ok("Mac up: the app loads from the Mac", r && r.tag === "mac");
  r = await fire("fetch", { request: req("#s=abc", "navigate") });
  ok("Mac up: a deep link loads from the Mac", r && r.tag === "mac");

  net = "down";
  r = await fire("fetch", { request: req("", "navigate") });
  ok("Tailscale off: the kept offline page", r && String(r.body).startsWith("from mac " + SCOPE + "offline.html"));
  r = await fire("fetch", { request: req("app.css") });
  ok("Tailscale off: its stylesheet from the copy", r && r.tag === "mac" && String(r.body).includes("app.css"));

  net = "502";
  r = await fire("fetch", { request: req("", "navigate") });
  ok("proxy down (502 from Serve): the offline page", r && String(r.body).includes("offline.html"));

  net = "down";
  for (const path of ["app.js", "chatparts.js", "api/sessions?limit=50", "phone-api/active", "phone-api/media?x=1"]) {
    r = await fire("fetch", { request: req(path) });
    ok("never answered from the worker: " + path, r === null);
  }
  r = await fire("fetch", { request: req("phone-api/auth/login", "cors", "POST") });
  ok("POST never touched", r === null);
  r = await fire("fetch", { request: { url: "https://web.push.apple.com/x", mode: "cors", method: "GET" } });
  ok("other origins never touched", r === null);

  store.clear();
  r = await fire("fetch", { request: req("", "navigate") });
  ok("no copy yet: a plain 503 message, not Safari's error", r && r.status === 503 && String(r.body).includes("Tailscale"));

  ok("push handlers still there", typeof handlers.push === "function" && typeof handlers.notificationclick === "function");
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stdout.write(JSON.stringify([["harness: " + e, false]])); });
"""

node = find_node()
check("node found", node)
if node:
    harness = os.path.join(work, "sw_harness.js")
    with open(harness, "w") as handle:
        handle.write(JS)
    proc = subprocess.run([node, harness, os.path.join(repo, "page", "sw.js")],
                          capture_output=True, text=True, timeout=60, cwd=work)
    try:
        js_results = json.loads(proc.stdout or "[]")
    except ValueError:
        js_results = [["node output parsed: " + (proc.stderr or proc.stdout)[:300], False]]
    check("node ran the worker", proc.returncode == 0 and len(js_results) >= 15)
    for name, ok in js_results:
        check(name, ok)

with open(os.path.join(repo, "page", "offline.html"), encoding="utf-8") as handle:
    page = handle.read()
check("offline page: no script, no inline style", "<script" not in page and "style=" not in page)
with open(os.path.join(repo, "page", "app.js"), encoding="utf-8") as handle:
    app = handle.read()
check("page registers the worker on every load", 'navigator.serviceWorker.register("sw.js", { scope: "./" }).catch' in app)

print("%d/%d passed" % (results.count(True), len(results)))
sys.exit(0 if all(results) else 1)
