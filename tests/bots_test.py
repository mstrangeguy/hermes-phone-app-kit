"""Offline test of the main list, Bots and Cron screen rules (page/lists.js) and their wiring.

Usage: python3 -I tests/bots_test.py <repo-dir> <empty-scratch-dir>

Covers: the main list hides cron runs, Bot Chats and messaging chats but keeps
api_server, desktop and cli; pinned rows stay; the setting brings everything
back; paging keeps fetching when a page is mostly hidden rows (and stops at the
cap, and the page offset and has_more survive Hermes's pinned rows); the Home
Screen badge counts only unread rows the list shows; finding each profile's Bot Chat, the no-Bot-Chat case, messaging groups
per platform, cron job state dots; that serve.py serves lists.js, the page loads
it before app.js, the drawer order is Sessions, Bots, Cron jobs, and app.js
uses these rules. The rules run in node, the same code the phone runs.
"""
import json
import os
import re
import shutil
import subprocess
import sys

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

results = []


def check(name, ok):
    results.append(bool(ok))
    print(("ok   " if ok else "FAIL ") + name)


def find_node():
    """node from PATH, or the one Hermes ships next to its checkout."""
    candidates = [os.environ.get("NODE") or "", shutil.which("node") or ""]
    agent = os.environ.get("HERMES_AGENT_DIR") or ""
    if agent:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(agent)), "node", "bin", "node"))
    candidates.append(os.path.expanduser("~/.hermes/node/bin/node"))
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


# --- The rules, run in node --------------------------------------------------
JS = r"""
const fs = require("fs");
const vm = require("vm");
const box = {};
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), box);
const L = box.PhoneLists;
const out = [];
const ok = (name, cond) => out.push([name, Boolean(cond)]);
const ids = (rows) => rows.map((r) => r.id).join(",");

const rows = [
  { id: "c1", source: "cron", title: "Reconcile · 3:15" },
  { id: "b1", source: "desktop", title: "Bot Chat" },
  { id: "b2", source: "desktop", title: " Bot Chat " },
  { id: "t1", source: "telegram", title: "tg" },
  { id: "t2", source: "Telegram", title: "tg upper" },
  { id: "h1", source: "homeassistant" },
  { id: "a1", source: "api_server", title: "phone chat" },
  { id: "d1", source: "desktop", title: "desk" },
  { id: "l1", source: "cli" },
  { id: "x1", source: "desktop", title: "Bot Chat ideas" },
  { id: "u1", source: null },
  { id: "p1", source: "cron", pinned: true },
  { id: "p2", source: "slack", pinned: true },
  { id: "p3", source: "desktop", title: "Bot Chat", pinned: true }
];
const main = L.mainRows(rows, false);
ok("main list hides cron", !main.some((r) => r.id === "c1"));
ok("main list hides Bot Chat", !main.some((r) => r.id === "b1" || r.id === "b2"));
ok("main list hides messaging (any case)", !main.some((r) => ["t1", "t2", "h1"].includes(r.id)));
ok("main list keeps api_server, desktop, cli", ["a1", "d1", "l1"].every((id) => main.some((r) => r.id === id)));
ok("main list keeps a title that only starts with Bot Chat", main.some((r) => r.id === "x1"));
ok("main list keeps unknown source", main.some((r) => r.id === "u1"));
ok("pinned rows stay whatever their source", ["p1", "p2", "p3"].every((id) => main.some((r) => r.id === id)));
ok("main list exact", ids(main) === "a1,d1,l1,x1,u1,p1,p2,p3");
ok("setting on shows everything", ids(L.mainRows(rows, true)) === ids(rows));
ok("mainRows tolerates junk", L.mainRows(null, false).length === 0);
ok("api_server is not messaging on the phone", !L.isMessaging("api_server"));
ok("desktop messaging list kept", ["telegram", "slack", "discord", "email", "sms", "webhook", "homeassistant", "feishu"].every(L.isMessaging));
ok("local sources are not messaging", !["desktop", "cli", "tui", "cron", "oneshot", ""].some(L.isMessaging));

// Home Screen badge: only unread ids the main list shows.
const bc = L.badgeCount;
ok("badge: own chat unread counts 1", bc(new Set(["d1"]), rows, false) === 1);
ok("badge: cron row counts 0 with the setting off", bc(new Set(["c1"]), rows, false) === 0);
ok("badge: cron row counts 1 with the setting on", bc(new Set(["c1"]), rows, true) === 1);
ok("badge: pinned cron row counts 1 either way", bc(new Set(["p1"]), rows, false) === 1 && bc(new Set(["p1"]), rows, true) === 1);
ok("badge: bot chat counts 0 with the setting off", bc(new Set(["b1"]), rows, false) === 0);
ok("badge: id with no row (compression ancestor) counts 0", bc(new Set(["gone"]), rows, false) === 0 && bc(new Set(["gone"]), rows, true) === 0);
ok("badge: empty set counts 0", bc(new Set(), rows, false) === 0 && bc(new Set(), rows, true) === 0);
ok("badge: mixed set counts only shown rows", bc(new Set(["d1", "c1", "b1", "t1", "gone"]), rows, false) === 1);
ok("badge: takes an array and tolerates junk", bc(["d1", "a1"], rows, false) === 2 && bc(null, rows, false) === 0 && bc(new Set(["d1"]), null, false) === 0);

// Shared read state: the Mac's answer wins for chats Hermes tracks.
const eu = L.effectiveUnread;
const has = (s, id) => Array.from(s).includes(id);
ok("read: Mac-unread chat is unread with no local mark", has(eu([], { d1: { unread: true, profile: "default" } }), "d1"));
ok("read: read on the Mac clears a local mark", !has(eu(["d1"], { d1: { unread: false, profile: "default" } }), "d1"));
ok("read: untracked chat keeps its local mark", has(eu(["a1"], { d1: { unread: false } }), "a1"));
ok("read: no read state = local marks only", Array.from(eu(["a1", "d1"], undefined)).length === 2 && Array.from(eu(["a1"], null)).length === 1);
ok("read: junk entries ignored", Array.from(eu([], { x: null, y: { unread: "yes" } })).length === 0);
ok("read: badge counts Mac-unread rows", bc(eu([], { d1: { unread: true } }), rows, false) === 1);

async function paging() {
  // A page that is mostly cron: 48 cron + 2 own, then a page of own chats.
  const pages = [
    Array.from({ length: 50 }, (_, i) => ({ id: "r" + i, source: i < 48 ? "cron" : "desktop" })),
    Array.from({ length: 50 }, (_, i) => ({ id: "s" + i, source: "desktop" })),
    Array.from({ length: 50 }, (_, i) => ({ id: "t" + i, source: "desktop" }))
  ];
  let fetched = 0;
  const loaded = [];
  const round = async () => {
    loaded.push(...pages[fetched++]);
    return { visible: L.mainRows(loaded, false).length, more: fetched < pages.length };
  };
  const n = await L.fillPages(round, 50, 5);
  ok("mostly-cron page fetches the next page", n === 2 && fetched === 2);
  ok("then the screen has a full page", L.mainRows(loaded, false).length >= 50);

  let calls = 0;
  const allCron = async () => { calls++; return { visible: 0, more: true }; };
  ok("all-cron history stops at the cap", (await L.fillPages(allCron, 50, 5)) === 5 && calls === 5);

  calls = 0;
  const short = async () => { calls++; return { visible: 3, more: false }; };
  ok("stops when the server has no more", (await L.fillPages(short, 50, 5)) === 1 && calls === 1);

  calls = 0;
  const full = async () => { calls++; return { visible: 50, more: true }; };
  ok("a full first page fetches once", (await L.fillPages(full, 50, 5)) === 1 && calls === 1);

  calls = 0;
  const stale = async () => { calls++; return { stale: true }; };
  ok("a newer load stops the old one", (await L.fillPages(stale, 50, 5)) === 1 && calls === 1);

  // With the setting on, the same mostly-cron page is already full.
  fetched = 0; loaded.length = 0;
  const roundAll = async () => {
    loaded.push(...pages[fetched++]);
    return { visible: L.mainRows(loaded, true).length, more: fetched < pages.length };
  };
  ok("setting on: no extra page", (await L.fillPages(roundAll, 50, 5)) === 1);
}

function steps() {
  const page = (n, pins) => Array.from({ length: n }, (_, i) => ({ id: "s" + i, pinned: i < (pins || 0) }));
  let st = L.pageStep(page(50, 1), 50, false);
  ok("full page with a pin in it: ask again (Hermes says no more)", st.more && st.advance === 50);
  st = L.pageStep(page(53, 0), 50, false);
  ok("pins added past the limit do not move the offset", st.advance === 50 && st.more);
  st = L.pageStep(page(20, 0), 50, false);
  ok("short page ends it", !st.more && st.advance === 20);
  st = L.pageStep([], 50, false);
  ok("empty page ends it", !st.more && st.advance === 0);
  ok("has_more true is kept", L.pageStep(page(10, 0), 50, true).more);
}

function bots() {
  const lookup = [
    { id: "old", title: "Bot Chat", archived: true, last_active: 200 },
    { id: "live", title: "Bot Chat", last_active: 100 },
    { id: "near", title: "Bot Chat notes", last_active: 300 }
  ];
  ok("Bot Chat found by exact title", (L.pickBotChat(lookup) || {}).id === "live");
  ok("archived Bot Chat is not offered", (L.pickBotChat([lookup[0]])) === null);
  ok("no Bot Chat: null (row shows No Bot Chat yet)", L.pickBotChat([]) === null && L.pickBotChat(undefined) === null);
  ok("near-miss title is not the Bot Chat", L.pickBotChat([lookup[2]]) === null);

  const msg = [
    { id: "tg-old", source: "telegram", last_active: 100 },
    { id: "tg-new", source: "telegram", last_active: 300 },
    { id: "sl", source: "slack", last_active: 400 },
    { id: "api", source: "api_server", last_active: 900 },
    { id: "desk", source: "desktop", last_active: 900 },
    { id: "cr", source: "cron", last_active: 900 },
    { id: "tgbot", source: "telegram", title: "Bot Chat", last_active: 999 }
  ];
  const groups = L.messagingGroups(msg);
  ok("one group per platform", groups.map((g) => g.source).join(",") === "slack,telegram");
  ok("platform labels", groups.map((g) => g.label).join(",") === "Slack,Telegram");
  ok("rows newest first in a group", ids(groups[1].sessions) === "tg-new,tg-old");
  ok("api_server, local and cron rows get no heading", !groups.some((g) => ["api_server", "desktop", "cron"].includes(g.source)));
  ok("Bot Chat is not listed under a platform", !groups.some((g) => g.sessions.some((s) => s.id === "tgbot")));
  ok("no messaging chats: no headings", L.messagingGroups([{ id: "d", source: "desktop" }]).length === 0);
}

function cron() {
  const d = L.jobDot;
  ok("scheduled job: accent", d({ state: "scheduled" }) === "on" && d({}) === "on");
  ok("running job: pulsing accent", d({ state: "running" }) === "running");
  ok("paused job: amber", d({ state: "paused" }) === "held");
  ok("finished or disabled job: grey", d({ state: "completed" }) === "off" && d({ enabled: false }) === "off");
  ok("error job: red", d({ state: "error" }) === "bad");
  ok("last run failed: red", d({ state: "scheduled", last_status: "failed" }) === "bad" && d({ last_error: "x" }) === "bad");
}

(async () => {
  await paging();
  steps();
  bots();
  cron();
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stdout.write(JSON.stringify([["node harness: " + e, false]])); });
"""

node = find_node()
check("node found (PATH or Hermes's own)", node)
if node:
    harness = os.path.join(work, "lists_harness.js")
    with open(harness, "w") as handle:
        handle.write(JS)
    proc = subprocess.run([node, harness, os.path.join(repo, "page", "lists.js")],
                          capture_output=True, text=True, timeout=60, cwd=work)
    try:
        js_results = json.loads(proc.stdout or "[]")
    except ValueError:
        js_results = [["node output parsed: " + (proc.stderr or proc.stdout)[:300], False]]
    check("node ran the rules", proc.returncode == 0 and len(js_results) >= 30)
    for name, ok in js_results:
        check(name, ok)

# --- Wiring --------------------------------------------------------------------
def read(rel):
    with open(os.path.join(repo, rel), encoding="utf-8") as handle:
        return handle.read()


html = read("page/index.html")
app = read("page/app.js")

check("serve.py serves lists.js", serve.STATIC.get("/lists.js", (None,))[0] == "page/lists.js")
check("lists.js is a page request", serve.page_request("GET", "/lists.js"))
scripts = re.findall(r'<script src="([^"]+)"></script>', html)
check("lists.js loads before app.js", "lists.js" in scripts and scripts.index("lists.js") < scripts.index("app.js"))
modes = re.findall(r'data-mode="([a-z]+)"', html)
check("drawer order Sessions, Bots, Cron jobs", modes == ["sessions", "bots", "cron"])
check("bots screen in the page", 'id="bots-screen"' in html and 'id="bots-list"' in html)
check("no inline script or style, no on* handlers",
      not re.search(r"<script(?![^>]*\bsrc=)", html) and "<style" not in html and
      not re.search(r"\son[a-z]+\s*=", html) and not re.search(r'\sstyle="', html))
check("main list uses mainRows", re.search(r"function ownSessions\(\)[\s\S]{0,400}PhoneLists\.mainRows", app))
check("badge uses badgeCount with the View setting",
      "PhoneLists.badgeCount(shownUnreadIds(), state.sessions, VIEW.allSources)" in app)
check("unread = local marks merged with the Mac's read state",
      "PhoneLists.effectiveUnread(unreadSet(), ACTIVE.read)" in app)
check("dots use the merged unread set", 'return shownUnreadIds().has(id) ? "unread" : "";' in app)
check("opening a chat marks it read on the Mac",
      re.search(r"function markRead\(id\) \{[\s\S]{0,200}markReadOnMac\(id\);", app)
      and re.search(r"function markReadOnMac\(id\) \{[\s\S]{0,400}method: \"PATCH\",\s*body: JSON\.stringify\(\{ unread: false \}\)", app))
check("only Mac-unread chats are patched", "if (!known || known.unread !== true || READS.pending.has(id)) return;" in app)
check("an open chat that turns unread on the Mac is read there",
      re.search(r"ACTIVE\.read = data\.read[\s\S]{0,300}markReadOnMac\(state\.sessionId\)", app))
check("no badge from the raw unread set", not re.search(r"setBadge\(\s*set\.size", app) and "unread=\" + unreadSet().size" not in app)
check("badge waits for the first list load", re.search(r"function shownUnread\(\)\s*\{\s*if \(!LIST\.loaded\) return null;", app))
check("unread= left out before the first list load", 'unread === null ? "" : "?unread=" + unread' in app)
check("badge refreshed after the list loads",
      re.search(r"async function loadSessions[\s\S]{0,4000}LIST\.loaded = true;[\s\S]{0,600}refreshBadge\(\);", app))
check("badge refreshed when the View setting changes",
      re.search(r"VIEW\.allSources = !VIEW\.allSources;[\s\S]{0,200}refreshBadge\(\);", app))
check("list paging uses fillPages and pageStep",
      re.search(r"async function loadSessions[\s\S]{0,3000}PhoneLists\.pageStep[\s\S]{0,1500}PhoneLists\.fillPages", app))
check("setting stored with the view, off by default", "allSources: false" in app and "view.allSources = Boolean" in app)
check("Bot Chat found by exact title, hidden included",
      "\"/api/sessions?title=\" + encodeURIComponent(PhoneLists.BOT_CHAT_TITLE) + \"&include_hidden=1" in app)
bots_code = app[app.index("// --- Bots ---"):app.index("// --- Cron jobs ---")]
check("Bots screen never creates a session", "method" not in bots_code and "POST" not in bots_code)
check("cron rows use the job state dot", "PhoneLists.jobDot(job)" in app)
check("a run with its chat opens it", re.search(r"run\.session && !run\.error\)[\s\S]{0,200}openSession\(run\.session", app))

passed = sum(results)
print("%d/%d passed" % (passed, len(results)))
sys.exit(0 if passed == len(results) else 1)
