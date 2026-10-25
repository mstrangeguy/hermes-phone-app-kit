"""Offline test of reactions, task lists, search, and pin/archive wiring.

Usage: python3 -I tests/chat_test.py <repo-dir> <empty-scratch-dir>

Covers, against a throwaway Hermes home: /phone-api/reactions read and set
(Tapback rules, other authors and metadata kept, lineage scope, the settings
gate, bad input), the once-only "[The user reacted ...]" notes, and
/phone-api/search (titles, message text, compression tips, hidden chats,
wildcards, read-only). In node: page/chatparts.js (task-list parsing, tree,
progress, when the card shows; reaction toggling; double tap; search filter).
And that the page loads chatparts.js and wires the new pieces.
"""
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
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

results = []


def check(name, ok):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name)


DEFAULT_KEY = "default-key-0123456789"
OFF_KEY = "offprofile-key-abcdefgh"
home = os.path.join(work, "home")
off = os.path.join(home, "profiles", "quiet")
os.makedirs(off, exist_ok=True)
with open(os.path.join(home, ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=%s\n" % DEFAULT_KEY)
with open(os.path.join(off, ".env"), "w") as handle:
    handle.write("API_SERVER_KEY=%s\n" % OFF_KEY)
with open(os.path.join(home, "config.yaml"), "w") as handle:
    handle.write("model:\n  default: x\ndisplay:\n  compact: false\n  message_reactions: true\n")
with open(os.path.join(off, "config.yaml"), "w") as handle:
    handle.write("display:\n  message_reactions: false\n")
allow = os.path.join(work, "allowed-logins.txt")
with open(allow, "w") as handle:
    handle.write("owner@example.com\n")

SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, parent_session_id TEXT, end_reason TEXT,
  started_at REAL, last_activity_at REAL, ended_at REAL, archived INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0,
  hidden INTEGER DEFAULT 0);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
  display_metadata TEXT, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0, tool_name TEXT);
"""


def make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    rows = [
        # A compressed chat: root -> tip.
        ("root1", "desktop", "Garage door plan", None, "compression", 100, 150, 150, 0, 0, 0),
        ("tip1", "desktop", "Garage door plan", "root1", None, 160, 900, None, 0, 0, 0),
        ("other", "api_server", "Kids pickup times", None, None, 200, 800, None, 0, 1, 0),
        ("cronrun", "cron", "Nightly check", None, None, 300, 700, None, 0, 0, 0),
        ("hiddenx", "desktop", "Secret helper", None, None, 400, 600, None, 0, 0, 1),
        ("botchat", "desktop", "Bot Chat", None, None, 410, 950, None, 0, 0, 1),
        ("old", "desktop", "Old 100% sure", None, None, 50, 60, None, 1, 0, 0),
    ]
    conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    msgs = [
        (1, "root1", "user", "the garage door sticks in winter", None, 0, 1),  # compacted, still shown
        (2, "root1", "assistant", "Try the torsion spring first.", json.dumps(
            {"delivery": "x", "reactions": [{"emoji": "\U0001f44d", "author": "agent", "at": 1}]}), 0, 1),
        (3, "tip1", "user", "What about the opener?", None, 1, 0),
        (4, "tip1", "assistant", "Check the opener's travel limits.",
         json.dumps(json.dumps({"reactions": [{"emoji": "\u2764\ufe0f", "author": "user", "at": 2, "seen": True}]})), 1, 0),
        (5, "other", "user", "pickup is at 11:20 for the garage crew", None, 1, 0),
        (6, "other", "assistant", "Noted.", None, 1, 0),
        (7, "cronrun", "assistant", "garage sensor ok", None, 1, 0),
        (8, "hiddenx", "assistant", "garage hidden text", None, 1, 0),
        (9, "botchat", "assistant", "garage from the bot", None, 1, 0),
        (10, "tip1", "assistant", "rewound garage row", None, 0, 0),  # neither active nor compacted
        (11, "other", "user", "the taxbill is due", None, 1, 0),  # an unescaped a_b would match "axb"
    ]
    conn.executemany("INSERT INTO messages (id, session_id, role, content, display_metadata, active, compacted) "
                     "VALUES (?,?,?,?,?,?,?)", msgs)
    conn.commit()
    conn.close()


make_db(os.path.join(home, "state.db"))
make_db(os.path.join(off, "state.db"))

serve.HERMES_HOME = home
serve.STATE_DB = os.path.join(home, "state.db")
serve.ALLOW_PATH = allow


def db_digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
listener.close()
server = serve.Server(("127.0.0.1", port), serve.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def call(path, key=DEFAULT_KEY, method="GET", body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Tailscale-User-Login": "owner@example.com"}
    if key:
        h["Authorization"] = "Bearer " + key
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
        h["Content-Length"] = str(len(data))
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw


def meta_of(row_id, db=None):
    conn = sqlite3.connect(db or serve.STATE_DB)
    raw = conn.execute("SELECT display_metadata FROM messages WHERE id = ?", (row_id,)).fetchone()[0]
    conn.close()
    return serve.display_meta(raw)


HEART, THUMB = "\u2764\ufe0f", "\U0001f44d"
R = "/phone-api/reactions"

# --- Reactions: read ----------------------------------------------------------
before = db_digest(serve.STATE_DB)
status, body = call(R + "?session=tip1&ids=1,2,3,4,5,6")
check("read: enabled from config.yaml", status == 200 and body.get("enabled") is True)
got = body.get("reactions", {}) if isinstance(body, dict) else {}
check("read: compacted parent row of the lineage", got.get("2") == [{"emoji": THUMB, "author": "agent"}])
check("read: double-encoded metadata decoded", got.get("4") == [{"emoji": HEART, "author": "user"}])
check("read: seen flag and time not sent", "seen" not in json.dumps(got) and '"at"' not in json.dumps(got))
check("read: rows of another chat not returned", "5" not in got and "6" not in got)
check("read: nothing written", db_digest(serve.STATE_DB) == before)
check("read: bad session 400", call(R + "?session=../x&ids=1")[0] == 400)
check("read: needs the key", call(R + "?session=tip1&ids=1", key="wrong")[0] == 401)
status, body = call(R + "?profile=quiet&session=tip1&ids=2,4")
check("read: profile with reactions off says so, sends none", status == 200 and body == {"enabled": False, "reactions": {}})
check("read: unknown profile 404", call(R + "?profile=nope&session=tip1&ids=2")[0] == 404)

# --- Reactions: set -----------------------------------------------------------
status, body = call(R, method="POST", body={"session": "tip1", "row_id": 2, "emoji": HEART})
check("set: heart on a lineage row", status == 200 and body.get("reactions") ==
      [{"emoji": THUMB, "author": "agent"}, {"emoji": HEART, "author": "user", "new": True}])
meta = meta_of(2)
check("set: other metadata kept", meta.get("delivery") == "x")
check("set: agent's reaction kept", any(r.get("author") == "agent" for r in meta["reactions"]))
check("set: stored as Hermes does (emoji, author, at)",
      any(r.get("author") == "user" and r.get("emoji") == HEART and isinstance(r.get("at"), float) for r in meta["reactions"]))
status, body = call(R, method="POST", body={"session": "tip1", "row_id": 2, "emoji": THUMB})
check("set: a different emoji replaces yours", [r["emoji"] for r in body["reactions"] if r["author"] == "user"] == [THUMB])
status, body = call(R, method="POST", body={"session": "tip1", "row_id": 2, "emoji": THUMB})
check("set: the same emoji again takes it back", all(r["author"] != "user" for r in body["reactions"]))
call(R, method="POST", body={"session": "tip1", "row_id": 3, "emoji": HEART})
status, body = call(R, method="POST", body={"session": "tip1", "row_id": 3, "emoji": None})
check("set: null clears", status == 200 and body["reactions"] == [])
check("set: empty list leaves no reactions key", "reactions" not in (meta_of(3) or {}))
check("set: row of another chat 404", call(R, method="POST", body={"session": "tip1", "row_id": 5, "emoji": HEART})[0] == 404)
check("set: rewound row 404", call(R, method="POST", body={"session": "tip1", "row_id": 10, "emoji": HEART})[0] == 404)
check("set: only the six Tapbacks", call(R, method="POST", body={"session": "tip1", "row_id": 2, "emoji": "x"})[0] == 400)
check("set: row id must be a number", call(R, method="POST", body={"session": "tip1", "row_id": True, "emoji": HEART})[0] == 400)
check("set: not JSON 400", call(R, method="POST", body="nope")[0] == 400)
check("set: needs the key", call(R, method="POST", key="", body={"session": "tip1", "row_id": 2, "emoji": HEART})[0] == 401)
off_before = db_digest(os.path.join(off, "state.db"))
check("set: refused where reactions are off",
      call(R + "?profile=quiet", method="POST", body={"session": "tip1", "row_id": 2, "emoji": HEART})[0] == 409)
check("set: off profile untouched", db_digest(os.path.join(off, "state.db")) == off_before)

# --- Reactions: told to the agent once -----------------------------------------
call(R, method="POST", body={"session": "tip1", "row_id": 2, "emoji": HEART})
call(R, method="POST", body={"session": "tip1", "row_id": 3, "emoji": "\U0001f602"})
status, body = call(R + "/unseen", method="POST", body={"session": "tip1"})
notes = body.get("notes", "") if isinstance(body, dict) else ""
check("notes: reply reaction, Hermes's wording",
      '[The user reacted %s to your message: "Try the torsion spring first."]' % HEART in notes)
check("notes: own message", '[The user reacted \U0001f602 to their own message: "What about the opener?"]' in notes)
check("notes: an already-told reaction is not repeated", "travel limits" not in notes)
check("notes: agent reactions are not notes", notes.count("[The user reacted") == 2)
check("notes: marked seen in the store", all(r.get("seen") for r in meta_of(2)["reactions"] if r["author"] == "user"))
check("notes: second call empty", call(R + "/unseen", method="POST", body={"session": "tip1"})[1] == {"notes": ""})
status, body = call(R + "?session=tip1&ids=2")
check("read: told reactions are no longer new", "new" not in json.dumps(body))

# --- Search ---------------------------------------------------------------------
S = "/phone-api/search"
before = db_digest(serve.STATE_DB)
status, body = call(S + "?q=garage")
hits = body.get("results", []) if isinstance(body, dict) else []
ids = [h["id"] for h in hits]
check("search: 200", status == 200)
check("search: compressed chat listed once, as its tip", ids.count("tip1") == 1 and "root1" not in ids)
check("search: message text found, with a snippet", any(h["id"] == "other" and "garage" in h["preview"] for h in hits))
check("search: newest first", ids == sorted(ids, key=lambda i: -next(h["last_active"] for h in hits if h["id"] == i)))
check("search: hidden chats left out, Bot Chat kept", "hiddenx" not in ids and "botchat" in ids)
check("search: cron runs returned (the page decides)", "cronrun" in ids)
check("search: pinned flag sent", next(h for h in hits if h["id"] == "other")["pinned"] is True)
check("search: rewound rows not searched", all("rewound" not in h["preview"] for h in hits))
status, body = call(S + "?q=" + "Garage%20door")
check("search: title match, any case", [h["id"] for h in body["results"]] == ["tip1"] and body["results"][0]["match"] == "title")
status, body = call(S + "?q=100%25")
check("search: % is literal, archived chat found", [h["id"] for h in body["results"]] == ["old"] and body["results"][0]["archived"])
check("search: _ is literal", call(S + "?q=a_b")[1] == {"results": []})
check("search: too short 400", call(S + "?q=g")[0] == 400)
check("search: needs the key", call(S + "?q=garage", key="nope")[0] == 401)
check("search: per profile", call(S + "?profile=quiet&q=garage")[0] == 200)
check("search: nothing written", db_digest(serve.STATE_DB) == before)

# --- Face ID mode ----------------------------------------------------------------
real = (faceid.enrolled, faceid.session_valid)
faceid.enrolled = lambda: True
faceid.session_valid = lambda token, touch=False: False
check("locked without unlock cookie", call(S + "?q=garage", key="")[0] == 401)
faceid.session_valid = lambda token, touch=False: True
check("open with unlock cookie", call(R + "?session=tip1&ids=2", key="")[0] == 200)
faceid.enrolled, faceid.session_valid = real
server.shutdown()


# --- The page rules, run in node ------------------------------------------------
def find_node():
    candidates = [os.environ.get("NODE") or "", shutil.which("node") or ""]
    agent = os.environ.get("HERMES_AGENT_DIR") or ""
    if agent:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(agent)), "node", "bin", "node"))
    candidates.append(os.path.expanduser("~/.hermes/node/bin/node"))
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


JS = r"""
const fs = require("fs");
const vm = require("vm");
const box = {};
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), box);
const P = box.PhoneParts;
const out = [];
const ok = (name, cond) => out.push([name, Boolean(cond)]);
const J = JSON.stringify;

const list = [
  { id: "a", content: "Read", status: "completed" },
  { id: "b", content: "Build", status: "in_progress" },
  { id: "b1", content: "Sub step", status: "pending", parent: "b" },
  { id: "c", content: "Ship", status: "pending" },
  { id: "x", content: "Dropped", status: "cancelled" }
];
ok("parse: tool result JSON", J(P.parseTodos(J({ todos: list, summary: {} }))) === J(list));
ok("parse: todos given as a JSON string", (P.parseTodos({ todos: J(list) }) || []).length === 5);
ok("parse: nesting past the desktop's depth limit refused", P.parseTodos(J(J({ todos: list }))) === null);
ok("parse: bad items skipped", J(P.parseTodos([{ id: "", content: "x", status: "pending" },
  { id: "q", content: "ok", status: "nope" }, { id: "z", content: " fine ", status: "pending", parent: "z" }])) ===
  J([{ id: "z", content: "fine", status: "pending" }]));
ok("parse: not a task list", P.parseTodos("hello") === null && P.parseTodos({ a: 1 }) === null && P.parseTodos("") === null);
ok("legacy tool name", P.isTodoTool("todo") && P.isTodoTool("todo_list") && !P.isTodoTool("terminal"));
const tree = P.todoTree(list).map(([t, d]) => t.id + d).join(",");
ok("tree: child under its parent", tree === "a0,b0,b11,c0,x0");
ok("tree: dangling parent at top", P.todoTree([{ id: "k", content: "k", status: "pending", parent: "gone" }])[0][1] === 0);
const cyc = P.todoTree([{ id: "m", content: "m", status: "pending", parent: "n" }, { id: "n", content: "n", status: "pending", parent: "m" }]);
ok("tree: a cycle still lists every item once", cyc.length === 2);
const prog = P.todoProgress(list);
ok("progress: cancelled leaves the total", prog.done === 1 && prog.total === 4 && prog.open === 3);
ok("progress: current is the in-progress item", prog.current && prog.current.id === "b");
ok("progress: empty", P.todoProgress([]).total === 0 && P.todoProgress(null).current === null);

const rows = [
  { id: 10, role: "tool", tool_name: "todo_list", content: J({ todos: [list[0]] }) },
  { id: 11, role: "assistant", content: "x" },
  { id: 12, role: "tool", tool_name: "todo_list", content: J({ todos: list }) },
  { id: 13, role: "tool", tool_name: "todo_list", content: "Error: bad" },
  { id: 14, role: "tool", tool_name: "terminal", content: J({ todos: [] }) }
];
const latest = P.latestTodos(rows);
ok("latest: newest valid list", latest && latest.rowId === 12 && latest.todos.length === 5);
ok("latest: none", P.latestTodos([{ id: 1, role: "user", content: "hi" }]) === null);
const plan = { todos: list, rowId: 12 };
ok("card: idle, list of the latest turn shows", P.planVisible(plan, { lastUserRowId: 9 }));
ok("card: idle, closed stays closed", !P.planVisible(plan, { lastUserRowId: 9, dismissed: true }));
ok("card: working shows even if closed", P.planVisible(plan, { lastUserRowId: 9, dismissed: true, working: true }));
ok("card: an older turn's list hides", !P.planVisible(plan, { lastUserRowId: 20, working: true }));
ok("card: after sending, the old list hides", !P.planVisible(plan, { lastUserRowId: 9, floorRowId: 12, working: true }));
ok("card: the new turn's list shows", P.planVisible({ todos: list, rowId: 12.5 }, { lastUserRowId: 9, floorRowId: 12, working: true }));
ok("card: empty list never shows", !P.planVisible({ todos: [], rowId: 99 }, { working: true }));

const agent = [{ emoji: "\ud83d\udc4d", author: "agent" }];
let r = P.applyReaction(agent, "\u2764\ufe0f", "user");
ok("react: add keeps the agent's", r.length === 2 && P.mine(r) === "\u2764\ufe0f");
r = P.applyReaction(r, "\ud83d\ude02", "user");
ok("react: replace", r.length === 2 && P.mine(r) === "\ud83d\ude02");
r = P.applyReaction(r, "\ud83d\ude02", "user");
ok("react: same again retracts", r.length === 1 && P.mine(r) === null);
ok("react: null retracts", P.applyReaction([{ emoji: "x", author: "user" }], null, "user").length === 0);
ok("react: six Tapbacks in Apple's order", P.QUICK_REACTIONS.join("") === "\u2764\ufe0f\ud83d\udc4d\ud83d\udc4e\ud83d\ude02\u203c\ufe0f\u2753");

const tap = P.tapDetector(300, 30);
ok("double tap", !tap(10, 10, 0) && tap(15, 12, 200));
ok("slow second tap is a new first tap", !tap(10, 10, 1000) && !tap(10, 10, 1400) && tap(10, 10, 1600));
ok("far second tap is not a double", !tap(10, 10, 3000) && !tap(80, 10, 3100));
ok("a third tap starts over", !tap(0, 0, 5000) && tap(0, 0, 5100) && !tap(0, 0, 5200));

const hits = [{ id: "a", source: "cron" }, { id: "b", source: "desktop" }, { id: "c", source: "cron", pinned: true }];
ok("search: cron hidden by default", P.searchRows(hits, false).map((h) => h.id).join() === "b,c");
ok("search: setting on shows all", P.searchRows(hits, true).length === 3);
process.stdout.write(JSON.stringify(out));
"""

node = find_node()
check("node found (PATH or Hermes's own)", node)
if node:
    harness = os.path.join(work, "parts_harness.js")
    with open(harness, "w") as handle:
        handle.write(JS)
    proc = subprocess.run([node, harness, os.path.join(repo, "page", "chatparts.js")],
                          capture_output=True, text=True, timeout=60, cwd=work)
    try:
        js_results = json.loads(proc.stdout or "[]")
    except ValueError:
        js_results = [["node output parsed: " + (proc.stderr or proc.stdout)[:300], False]]
    check("node ran the rules", proc.returncode == 0 and len(js_results) >= 29)
    for name, ok in js_results:
        check(name, ok)


# --- Wiring -----------------------------------------------------------------------
def read(rel):
    with open(os.path.join(repo, rel), encoding="utf-8") as handle:
        return handle.read()


html = read("page/index.html")
app = read("page/app.js")
scripts = re.findall(r'<script src="([^"]+)"></script>', html)
check("serve.py serves chatparts.js", serve.STATIC.get("/chatparts.js", (None,))[0] == "page/chatparts.js")
check("chatparts.js loads before app.js", "chatparts.js" in scripts and scripts.index("chatparts.js") < scripts.index("app.js"))
check("page has the plan card, search box, Pin and Archive",
      all('id="%s"' % i in html for i in ("plan", "search", "menu-pin", "menu-archive")))
check("no inline script or style, no on* handlers",
      not re.search(r"<script(?![^>]*\bsrc=)", html) and "<style" not in html and
      not re.search(r"\son[a-z]+\s*=", html) and not re.search(r'\sstyle="', html))
check("reactions read after every history load", re.search(r"renderTranscript\(\);\s*updatePlanFromMessages\(\);\s*loadReactions\(\);", app))
check("unseen reactions go with the next message", re.search(r"reactionNotes\(run\.profile, sessionId\)[\s\S]{0,400}instructions \}\)", app))
check("todo step refreshes the card", "PhoneParts.isTodoTool(event.tool)) refreshPlanSoon(run, event.preview)" in app)
check("pin and archive use Hermes's PATCH", re.search(r'method: "PATCH",\s*body: JSON.stringify\(\{ \[flag\]: value \}\)', app))
check("phone turns told reactions cannot attach yet", "react_to_message) cannot attach" in app)
check("search results replace the list", re.search(r"if \(searching\(\)\) \{\s*renderSearch\(root\)", app))
check("chat title opens the session menu", '$("chat-title").addEventListener("click", () => toggleMenu())' in app and
      'id="chat-title" role="button"' in html)
check("header keeps clear of side cutouts", "env(safe-area-inset-right)" in read("page/app.css"))
check("new endpoints are local, not forwarded",
      all(p in serve.LOCAL_API for p in (serve.REACTIONS_PATH, serve.REACTIONS_UNSEEN_PATH, serve.SEARCH_PATH)))

passed = sum(results)
print("%d/%d passed" % (passed, len(results)))
sys.exit(0 if passed == len(results) else 1)
