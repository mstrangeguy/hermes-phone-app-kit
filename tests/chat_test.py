"""Offline test of reactions, task lists, search, and pin/archive wiring.

Usage: python3 -I tests/chat_test.py <repo-dir> <empty-scratch-dir>

Covers, against a throwaway Hermes home: /phone-api/reactions read and set
(Tapback rules, other authors and metadata kept, lineage scope, the settings
gate, bad input), the once-only "[The user reacted ...]" notes, and
/phone-api/search (titles, message text, compression tips, hidden chats,
wildcards, read-only). In node: page/chatparts.js (task-list parsing, tree,
progress, when the card shows; reaction toggling; double tap; search filter;
when a long message you sent folds; what Copy copies).
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

# --- Reactions: the settings gate without PyYAML ------------------------------
# Hermes's own Python may lack PyYAML; then serve.py reads config.yaml itself.
# A real config.yaml has blank lines and comments all through `display:`.
# sys.modules["yaml"] = None makes `import yaml` fail here even where it is
# installed, so these cases mean the same on every Mac.
GATE_CASES = [
    ("compact block", "display:\n  compact: false\n  message_reactions: true\n", True),
    ("blank line before the key", "display:\n  compact: false\n\n  message_reactions: true\n", True),
    ("column-0 comment before the key", "display:\n  compact: false\n# note\n  message_reactions: true\n", True),
    ("indented comment before the key", "display:\n  compact: false\n  # note\n  message_reactions: true\n", True),
    ("inline comment after true", "display:\n  message_reactions: true  # tapbacks\n", True),
    ("yes/On in any case", "display:\n  message_reactions: On\n", True),
    ("long section, key far down",
     "model:\n  default: x\n\ndisplay:\n  compact: false\n\n" + "  # filler\n  other: 1\n\n" * 50 +
     "  message_reactions: true\n\nterminal:\n  backend: local\n", True),
    ("false", "display:\n  compact: false\n\n  message_reactions: false\n", False),
    ("key under the next top-level section", "display:\n  compact: false\n\nother:\n  message_reactions: true\n", False),
    ("nested key of the same name", "display:\n  skin:\n    message_reactions: true\n  compact: false\n", False),
    ("key missing", "display:\n  compact: false\n", False),
    ("no display section", "model:\n  default: x\n", False),
]
real_home, real_yaml = serve.HERMES_HOME, sys.modules.get("yaml", "absent")
sys.modules["yaml"] = None
try:
    for i, (label, text, want) in enumerate(GATE_CASES):
        serve.HERMES_HOME = os.path.join(work, "gate%d" % i)
        os.makedirs(serve.HERMES_HOME, exist_ok=True)
        with open(os.path.join(serve.HERMES_HOME, "config.yaml"), "w") as handle:
            handle.write(text)
        check("gate without PyYAML: %s -> %s" % (label, want), serve.reactions_enabled("default") is want)
finally:
    serve.HERMES_HOME = real_home
    if real_yaml == "absent":
        sys.modules.pop("yaml", None)
    else:
        sys.modules["yaml"] = real_yaml

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

// Weighted progress: weight 1-100 (unset = 1), waiting only on the in-progress step.
const job = (done, cur, w) => (w || [2, 20, 1, 1, 1, 1, 1, 1]).map((weight, i) => ({ id: "s" + i, content: "step " + i,
  status: i < done ? "completed" : i === cur ? "in_progress" : "pending", weight }));
const wparsed = P.parseTodos([{ id: "a", content: "A", status: "in_progress", weight: 20, waiting: true },
  { id: "b", content: "B", status: "pending", weight: "3", waiting: true }, { id: "c", content: "C", status: "pending", weight: 500 },
  { id: "d", content: "D", status: "pending", weight: "x" }]);
ok("weights: parsed, clamped, waiting only in progress", J(wparsed) === J([
  { id: "a", content: "A", status: "in_progress", weight: 20, waiting: true }, { id: "b", content: "B", status: "pending", weight: 3 },
  { id: "c", content: "C", status: "pending", weight: 100 }, { id: "d", content: "D", status: "pending" }]));
ok("weights: the mockup's percentages", [P.todoProgress(job(1, 1)).percent, P.todoProgress(job(2, 2)).percent,
  P.todoProgress(job(3, 3)).percent, P.todoProgress(job(3, 3, [2, 20, 1, 6, 1, 1, 1, 1])).percent,
  P.todoProgress(job(8, -1)).percent].join() === "7,79,82,70,100");
ok("weights: none set counts tasks", P.todoProgress(list).percent === 25);
const wjob = job(2, 2); wjob[2].waiting = true;
ok("tone: running, red, yellow, green", P.todoTone(job(2, 2), null, true) === "running" &&
  P.todoTone(job(2, 2), "stopped", false) === "stopped" &&
  P.todoTone(job(2, 2), "failed", false) === "failed" && P.todoTone(wjob, null, true) === "waiting" &&
  P.todoTone(job(8, -1), "stopped", false) === "done");
ok("fill: nothing animates on a list's first paint", P.freshlyDone(null, job(3, 3)).length === 0);
ok("fill: only steps that just turned completed", P.freshlyDone(["s0", "s1"], job(3, 3)).join() === "s2" &&
  P.freshlyDone(["s0", "s1", "s2"], job(3, 3)).length === 0 && P.freshlyDone([], job(2, 2)).join() === "s0,s1");
ok("tone: run endings", P.runOutcome("run.failed") === "failed" && P.runOutcome("run.cancelled") === "stopped" &&
  P.runOutcome("run.interrupted") === "stopped" && P.runOutcome("run.completed") === null);

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

// Sticky bar (S53): planning / live / previous / hidden.
const st = (p, o) => P.planState(p, o);
ok("state: planning while working with no list this turn", st(plan, { lastUserRowId: 20, working: true }) === "planning" &&
  st({ todos: null, rowId: 0 }, { working: true }) === "planning" &&
  st(plan, { lastUserRowId: 9, floorRowId: 12, working: true }) === "planning");
ok("state: live once this turn's list arrives", st({ todos: list, rowId: 12.5 }, { lastUserRowId: 9, floorRowId: 12, working: true }) === "live" &&
  st(plan, { lastUserRowId: 9, working: true, dismissed: true }) === "live" && st(plan, { lastUserRowId: 9 }) === "live");
ok("state: previous list after a list-free turn", st(plan, { lastUserRowId: 20 }) === "previous" &&
  st(plan, { lastUserRowId: 9, floorRowId: 12 }) === "previous");
ok("state: hidden when dismissed", st(plan, { lastUserRowId: 9, dismissed: true }) === "hidden" &&
  st(plan, { lastUserRowId: 20, dismissed: true }) === "hidden" &&
  st(plan, { lastUserRowId: 20, working: true, planningDismissed: true }) === "hidden");
ok("state: hidden with no list and nothing running", st({ todos: [], rowId: 0 }, {}) === "hidden" && st(null, {}) === "hidden");

ok("tone: open steps at turn end are yellow", P.todoTone(job(2, 2), null, false) === "waiting" &&
  P.todoTone(job(2, -1), null, false) === "waiting" && P.todoTone(job(8, -1), null, false) === "done");
ok("tone: red kept for stopped/failed at turn end", P.todoTone(job(2, 2), "stopped", false) === "stopped" &&
  P.todoTone(job(2, 2), "failed", false) === "failed");
ok("tone: running again on the next send", P.todoTone(job(2, 2), null, true) === "running");
ok("waited on: the in-progress step, else the first pending", P.waitedOnId(job(2, 2), "waiting") === "s2" &&
  P.waitedOnId(job(2, -1), "waiting") === "s2" && P.waitedOnId(job(2, 2), "running") === null);
const named = (todos, tone) => P.stepName(todos, tone);
const jn = job(2, 2);
ok("step name: running names the in-progress step", named(jn, "running") === jn[2].content);
ok("step name: none between steps while running", named(job(2, -1), "running") === null);
ok("step name: waiting / stopped / failed name the in-progress step, else the first pending",
  named(jn, "waiting") === jn[2].content && named(jn, "stopped") === jn[2].content &&
  named(job(2, -1), "waiting") === job(2, -1)[2].content && named(job(2, -1), "failed") === job(2, -1)[2].content);
ok("step name: finished list names nothing", named(job(8, -1), "done") === null && named([], "running") === null);
const looks = (todos, tone) => { const w = P.waitedOnId(todos, tone); return todos.map((t) => P.segLook(t, tone, w)); };
const lw = looks(job(2, 2), "waiting");
ok("waiting: done steps keep the accent colour, only the waited-on step is yellow",
  lw[0].halt === null && !lw[0].error && lw[1].halt === null && lw[2].halt === "waiting" &&
  lw.slice(3).every((l) => l.halt === null && !l.error));
const lp = looks(job(2, -1), "waiting");
ok("waiting with none in progress: the first pending step is yellow", lp[2].halt === "waiting" && lp[3].halt === null);
const lr = looks(job(2, 2), "stopped");
ok("stopped/failed: the halted step red, no per-step error marks", lr[2].halt === "stopped" &&
  looks(job(2, 2), "failed")[2].halt === "failed" && lr.every((l) => !l.error));
const ej = job(3, 3); ej[0].error = true; ej[3].error = true; ej[4].error = true;
const le = looks(ej, "running");
ok("error: that step red alone", le[0].error && le[0].halt === null && le[1].halt === null && !le[1].error &&
  le[3].halt === "failed" && le[4].halt === "failed" && le[5].halt === null && !le[5].error);
const ed = job(8, -1); ed[1].error = true;
ok("error: a finished list stays green, the errored step red", P.todoTone(ed, null, false) === "done" &&
  P.segLook(ed[1], "done", null).error && !P.segLook(ed[0], "done", null).error);
ok("error: the list's red covers it when stopped", !P.segLook(ej[0], "stopped", null).error);
ok("error: row shows the red cross, waited-on row the yellow bar", P.rowHalt(ej[0], "running", null) === "failed" &&
  P.rowHalt(job(2, 2)[2], "waiting", "s2") === "waiting" && P.rowHalt(job(2, -1)[2], "waiting", "s2") === "waiting" &&
  P.rowHalt(job(2, 2)[1], "waiting", "s2") === null && P.rowHalt(job(2, 2)[2], "stopped", null) === "stopped" &&
  P.rowHalt(job(2, 2)[2], "running", null) === null);
ok("error: parsed on any status but cancelled", J(P.parseTodos([{ id: "a", content: "A", status: "completed", error: true },
  { id: "b", content: "B", status: "cancelled", error: true }, { id: "c", content: "C", status: "pending", error: "yes" }])) ===
  J([{ id: "a", content: "A", status: "completed", error: true }, { id: "b", content: "B", status: "cancelled" },
    { id: "c", content: "C", status: "pending" }]));
// Merges happen in Hermes core; the card reads each write's full list, so a
// set, a clear and a cancel show up as the newest row's flags.
const write = (id, items) => ({ id, role: "tool", tool_name: "todo_list", content: J({ todos: items }) });
const base = [{ id: "a", content: "A", status: "in_progress" }];
const mset = P.latestTodos([write(1, base), write(2, [Object.assign({}, base[0], { error: true })])]);
const mclear = P.latestTodos([write(1, base), write(2, [Object.assign({}, base[0], { error: true })]), write(3, base)]);
const mcancel = P.latestTodos([write(2, [Object.assign({}, base[0], { error: true })]),
  write(3, [{ id: "a", content: "A", status: "cancelled", error: true }])]);
ok("merge: sets and clears error, cancelling drops it", mset.todos[0].error === true && !("error" in mclear.todos[0]) &&
  !("error" in mcancel.todos[0]));

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

const ask = (text, extra) => P.quickReplies([{ id: 1, role: "user", content: "hi" },
  { id: 2, role: "assistant", content: text }].concat(extra || []));
ok("quick: yes/no question offers the two answers", J((ask("Done the check.\n\nShould I make it?") || {}).answers) === J(P.QUICK_ANSWERS));
ok("quick: 'Say go' offers them", ask("Here's the plan.\n\nSay go and I'll start with item 1.") !== null);
ok("quick: an open question does not", ask("Which ones do you want?") === null);
ok("quick: an either/or question does not", ask("Should I reload it, or leave it?") === null);
ok("quick: no question, no buttons", ask("All done. Nothing needs you.") === null);
ok("quick: lock mentioned anywhere, no buttons", ask("The front lock is jammed.\n\nShould I retry?") === null);
ok("quick: alarm, thermostat, garage, siren, camera, disarm: none",
  ["alarm", "thermostat", "garage door", "siren", "camera", "disarm"].every((w) => ask("This touches the " + w + ". Should I go ahead?") === null));
ok("quick: 'block' and 'clock' are not a lock", ask("The clock and block list are fine. Should I continue?") !== null);
ok("quick: a later user message hides them", P.quickReplies([{ id: 2, role: "assistant", content: "Should I?" }, { id: 3, role: "user", content: "yes" }]) === null);
ok("quick: tool rows after the question are skipped", ask("Should I make it?", [{ id: 3, role: "tool", content: "x" }]) !== null);
ok("quick: a tool-call-only step does not count as the question", ask("Want me to fix it?", [{ id: 3, role: "assistant", content: "" }]) !== null);
ok("quick: question inside code ignored", ask("Run this:\n```\nshould I?\n```") === null);

const LH = 23.2;  // 16px text at line height 1.45
const fold = (full, line, open) => J(P.clampState(full, line, P.CLAMP_LINES, open));
const flat = J({ over: false, clamped: false, button: "" });
ok("fold: four lines, like the desktop", P.CLAMP_LINES === 4);
ok("fold: short text not folded, no button", fold(2 * LH, LH, false) === flat);
ok("fold: exactly 4 lines not folded", fold(4 * LH, LH, false) === flat && fold(Math.round(4 * LH), LH, false) === flat);
ok("fold: 4 lines + 1 px folded, Show all", fold(4 * LH + 1, LH, false) === J({ over: true, clamped: true, button: "Show all" }));
ok("fold: opened shows Show less", fold(300 * LH, LH, true) === J({ over: true, clamped: false, button: "Show less" }));
ok("fold: zero, negative or missing line height never folds",
  [0, -5, NaN, undefined, null, "normal", Infinity].every((bad) => fold(300 * LH, bad, false) === flat));
ok("fold: zero or missing height never folds", [0, -1, NaN, undefined].every((bad) => fold(bad, LH, false) === flat));
const longMine = "Line one\n" + "made-up line\n".repeat(300) + "last line";
ok("copy: your own message copies whole, as shown (even folded)",
  P.copyText({ role: "user", content: longMine + "\n@image:/x/made-up.png" }, longMine) === longMine);
ok("copy: without the shown text, the saved text", P.copyText({ role: "user", content: [{ type: "text", text: " hi " }] }) === "hi");
ok("copy: a reply copies as written, Markdown kept", P.copyText({ role: "assistant", content: "**Done.**\n\n- one\n- two" }) === "**Done.**\n\n- one\n- two");
ok("copy: tool rows, empty rows and junk give nothing",
  P.copyText({ role: "tool", content: "x" }) === "" && P.copyText({ role: "assistant", content: "  " }) === "" &&
  P.copyText(null) === "" && P.copyText("text") === "");
ok("fold: bad line count falls back to 4", J(P.clampState(5 * LH, LH, 0, false)) === J(P.clampState(5 * LH, LH, 4, false)) &&
  J(P.clampState(3 * LH, LH, NaN, false)) === flat);

// Date dividers (S56). Times built from local dates, so any time zone works.
const at = (y, mo, d, h, mi) => new Date(y, mo - 1, d, h, mi || 0).getTime() / 1000;
const u = (ts) => ({ role: "user", content: "x", timestamp: ts });
const a = (ts) => ({ role: "assistant", content: "y", timestamp: ts });
const NOW = new Date(2026, 10, 4, 15, 0).getTime();
ok("day: send time from Unix seconds, numeric strings and ms; junk is null",
  P.messageMs({ timestamp: 1793827630.5 }) === 1793827630500 && P.messageMs({ timestamp: "1793827630" }) === 1793827630000 &&
  P.messageMs({ timestamp: 1793827630500 }) === 1793827630500 &&
  [undefined, null, "", "abc", 0, -5, NaN].every((t) => P.messageMs({ timestamp: t }) === null) && P.messageMs(null) === null);
ok("day: key is the local calendar day", P.dayKey(at(2026, 11, 2, 0, 5) * 1000) === "2026-11-02" &&
  P.dayKey(at(2026, 11, 2, 23, 59) * 1000) === "2026-11-02" && P.dayKey(null) === null);
ok("divider: first message gets one, same-day messages none",
  J(P.dayBreaks([u(at(2026, 11, 2, 9)), a(at(2026, 11, 2, 9, 1)), u(at(2026, 11, 2, 14)), a(at(2026, 11, 2, 14, 2))])) ===
  J(["2026-11-02", null, null, null]));
ok("divider: a new day's first message you sent opens it",
  J(P.dayBreaks([u(at(2026, 11, 2, 9)), a(at(2026, 11, 2, 9, 1)), u(at(2026, 11, 3, 8)), a(at(2026, 11, 3, 8, 1))])) ===
  J(["2026-11-02", null, "2026-11-03", null]));
ok("divider: a turn past midnight stays under the day it started",
  J(P.dayBreaks([u(at(2026, 11, 2, 23, 50)), a(at(2026, 11, 3, 0, 10)), a(at(2026, 11, 3, 0, 20)), u(at(2026, 11, 3, 7))])) ===
  J(["2026-11-02", null, null, "2026-11-03"]));
ok("divider: a page that starts on a reply gets one above it",
  J(P.dayBreaks([a(at(2026, 11, 2, 10)), u(at(2026, 11, 3, 9))])) === J(["2026-11-02", "2026-11-03"]));
ok("divider: no time, no divider, and the running day is kept",
  J(P.dayBreaks([u(undefined), u(at(2026, 11, 2, 9)), u(null), u(at(2026, 11, 2, 10)), u(at(2026, 11, 3, 9))])) ===
  J([null, "2026-11-02", null, null, "2026-11-03"]));
ok("divider: Hermes notices (display_kind) don't open a day",
  J(P.dayBreaks([u(at(2026, 11, 2, 9)), { role: "user", display_kind: "steer", timestamp: at(2026, 11, 3, 1) }])) ===
  J(["2026-11-02", null]));
ok("divider: a day opens once even if times go backwards",
  J(P.dayBreaks([u(at(2026, 11, 2, 9)), u(at(2026, 11, 3, 9)), u(at(2026, 11, 2, 12)), u(at(2026, 11, 3, 10))])) ===
  J(["2026-11-02", "2026-11-03", null, null]));
const lab = (key, now) => P.dividerLabel(key, now);
const short = (y, mo, d) => new Date(y, mo - 1, d).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
ok("label: Today and Yesterday prefix, older plain", lab("2026-11-04", NOW).text === "Today \u00b7 " + short(2026, 11, 4) &&
  lab("2026-11-04", NOW).prefix === "Today" && lab("2026-11-03", NOW).text === "Yesterday \u00b7 " + short(2026, 11, 3) &&
  lab("2026-11-02", NOW).text === short(2026, 11, 2) && lab("2026-11-02", NOW).prefix === "");
ok("label: short date like Mon, Nov 2 in en-US", !/en/i.test(Intl.DateTimeFormat().resolvedOptions().locale) ||
  lab("2026-11-02", NOW).date === "Mon, Nov 2");
ok("label: year only when it isn't this year", lab("2025-12-31", NOW).date === short(2025, 12, 31) + ", 2025" &&
  !/2026/.test(lab("2026-01-05", NOW).date));
ok("label: Yesterday across a month end and a year end",
  lab("2026-10-31", new Date(2026, 10, 1, 0, 1).getTime()).prefix === "Yesterday" &&
  lab("2026-11-01", new Date(2026, 10, 1, 0, 1).getTime()).prefix === "Today" &&
  lab("2025-12-31", new Date(2026, 0, 1, 9).getTime()).prefix === "Yesterday");
ok("label: worked out against the time it is painted (midnight turns Today into Yesterday)",
  lab("2026-11-04", new Date(2026, 10, 4, 23, 59).getTime()).prefix === "Today" &&
  lab("2026-11-04", new Date(2026, 10, 5, 0, 0).getTime()).prefix === "Yesterday");
ok("label: bad key gives nothing", lab("nope", NOW).text === "" && lab(null, NOW).text === "");

// Pinned date line (S57). Tops from the transcript's visible top; line 4, band 26.
const pin = (openers) => J(P.resolveDatePin(openers, 4, 26));
const D = (day, top) => ({ day, top });
ok("pin: nothing pinned at the very top of a chat", pin([D("2026-11-02", 26), D("2026-11-03", 600)]) === J({ day: "", shift: 0 }));
ok("pin: the day you scrolled into is pinned", pin([D("2026-11-02", -120), D("2026-11-03", 600)]) === J({ day: "2026-11-02", shift: 0 }) &&
  pin([D("2026-11-02", 4), D("2026-11-03", 600)]) === J({ day: "2026-11-02", shift: 0 }));
ok("pin: the latest passed day wins", pin([D("2026-11-01", -900), D("2026-11-02", -300), D("2026-11-03", 500)]) ===
  J({ day: "2026-11-02", shift: 0 }));
ok("pin: the next line pushes the pinned one up", pin([D("2026-11-02", -200), D("2026-11-03", 20)]) === J({ day: "2026-11-02", shift: -10 }) &&
  pin([D("2026-11-02", -200), D("2026-11-03", 30)]) === J({ day: "2026-11-02", shift: 0 }));
ok("pin: hand-over when the next line reaches the top, never two",
  pin([D("2026-11-02", -200), D("2026-11-03", 4.5)]) === J({ day: "2026-11-02", shift: -25.5 }) &&
  pin([D("2026-11-02", -200), D("2026-11-03", 4)]) === J({ day: "2026-11-03", shift: 0 }));
ok("pin: no lines (or junk) pins nothing", pin([]) === J({ day: "", shift: 0 }) && J(P.resolveDatePin(null, 4, 26)) === J({ day: "", shift: 0 }) &&
  pin([{ day: "2026-11-02" }, null, D("2026-11-03", "x")]) === J({ day: "", shift: 0 }));
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
    check("node ran the rules", proc.returncode == 0 and len(js_results) >= 47)
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
css = read("page/app.css")
check("card follows planState, Planning card wired",
      "PhoneParts.planState(PLAN, {" in app and 'view === "planning"' in app and "paintPlanning(box, turn)" in app and
      'now.textContent = "Planning\\u2026"' in app)
check("Planning time ticks every second", re.search(r"setInterval\(\(\) => \{[^}]*\n  if \(!document.hidden\) tickPlanTime\(\);", app))
check("previous list comes back folded, labelled",
      'if (view === "previous" && PLAN.view !== "previous") PLAN.open = false;' in app and '"Previous tasks "' in app)
check("segments and rows use segLook / rowHalt",
      "PhoneParts.segLook(todo, tone, waitingId)" in app and "PhoneParts.rowHalt(todo, tone, waitingId)" in app)
check("planning stripes grey, still with Reduce Motion",
      ".plan-seg.planning {" in css and "var(--muted-fg)" in css.split(".plan-seg.planning {")[1].split("}")[0] and
      re.search(r"prefers-reduced-motion[^\n]*\.plan-seg\.planning", css))
check("waiting no longer tints the done segments", ".plan.tone-waiting { --seg" not in css and ".plan-seg.halt-waiting" in css)
check("errored done step red", ".plan-seg.completed.error { --seg: #f85149; }" in css)
check("step name on its own line under the bar, header keeps count, tag, %",
      "PhoneParts.stepName(PLAN.todos, tone)" in app and 'stepLine.className = "plan-step"' in app and
      "box.append(top, bar);\n  if (stepText)" in app and ".plan-step {" in css and
      "text-overflow: ellipsis" in css.split(".plan-step {")[1].split("}")[0])
check("pin and archive use Hermes's PATCH", re.search(r'method: "PATCH",\s*body: JSON.stringify\(\{ \[flag\]: value \}\)', app))
check("phone turns told reactions cannot attach yet", "react_to_message) cannot attach" in app)
check("search results replace the list", re.search(r"if \(searching\(\)\) \{\s*renderSearch\(root\)", app))
check("chat title opens the session menu", '$("chat-title").addEventListener("click", () => toggleMenu())' in app and
      'id="chat-title" role="button"' in html)
check("header keeps clear of side cutouts", "env(safe-area-inset-right)" in read("page/app.css"))
fold_css = css.split(".bubble-text.clamped {")[1].split("}")[0] if ".bubble-text.clamped {" in css else ""
check("fold: CSS folds to 4 lines with the desktop's fade (and the -webkit- form)",
      "max-height: calc(4 * var(--bubble-lh) * 1em" in fold_css and "overflow: hidden" in fold_css and
      "-webkit-mask-image: linear-gradient(to bottom, #000 55%, transparent)" in fold_css and
      "\n  mask-image: linear-gradient(to bottom, #000 55%, transparent)" in fold_css)
toggle_css = css.split(".clamp-toggle {")[1].split("}")[0] if ".clamp-toggle {" in css else ""
check("fold: Show all is a muted 44 px button, hidden when not needed",
      "min-height: 44px" in toggle_css and "var(--muted-fg)" in toggle_css and ".clamp-toggle[hidden] { display: none; }" in css)
check("fold: user bubbles fold, keyed by row, live run and guidance",
      "function userBubble(text, steer, sources, key)" in app and "watchClamp(words);" in app and
      app.count("rowClampKey(message))") == 2 and
      "userBubble(sources ? run.shownText || \"\" : run.userText, false, sources, run._clampKey)" in app and
      "userBubble(block.text || \"\", true, undefined, block._clampKey)" in app)
check("fold: measured from layout, reads before writes, re-measured on width change",
      "new ResizeObserver(" in app and "words.scrollHeight" in app and "getComputedStyle(words).lineHeight" in app and
      re.search(r"const sizes = texts\.map\([^\n]*\);\n  for \(const size of sizes\)", app) and
      "CLAMP.widths.get(entry.target) === width" in app)
check("fold: rule and button text from PhoneParts.clampState, aria-expanded set",
      "PhoneParts.clampState(size.full, size.line, PhoneParts.CLAMP_LINES, CLAMP.open.has(key))" in app and
      "button.textContent = look.button" in app and 'button.setAttribute("aria-expanded"' in app)
check("fold: opened state kept until Show less or another chat; live -> saved carries over",
      "historyNodes.clear();\n  resetClamps();" in app and 'CLAMP.open.add("m" + saved.id)' in app)
check("fold: a search hit inside a folded message opens it",
      "CLAMP.query = searching() ? SEARCH.q.toLowerCase() : \"\"" in app and
      "words.textContent.toLowerCase().includes(CLAMP.query)" in app)
check("fold: the button is not part of the double tap (buttons skipped)",
      re.search(r'target\.closest\("a, button,[^"]*"\)\) return;\n  if \(!TAP\(', app))
check("fold: only the text folds, the whole text stays in the page",
      "words.textContent = shown;" in app and "words.after(button)" in app and ".bubble-text.clamped" in css and
      ".bubble.clamped" not in css)
tap_src = app.split("function openTapback(node, tapY) {")[1].split("\n}\n")[0] if "function openTapback(node, tapY) {" in app else ""
check("copy: the double-tap bar has Copy, after the reactions",
      "bar.append(copyButton(node, rowId));" in tap_src and
      tap_src.index("PhoneParts.QUICK_REACTIONS") < tap_src.index("copyButton(node, rowId)"))
check("copy: the bar opens with reactions off too (Copy only)",
      "if (!REACT.enabled) return;" not in tap_src and "REACT.enabled ? PhoneParts.QUICK_REACTIONS : []" in tap_src and
      not re.search(r"if \(!TAP\([^\n]*\n  if \(!REACT\.enabled\) return;", app))
copy_src = app.split("function copyButton(node, rowId) {")[1].split("\n}\n")[0] if "function copyButton(node, rowId) {" in app else ""
check("copy: copies the whole message (bubble text, not what is folded) via PhoneParts.copyText",
      'node.querySelector(".bubble-text")' in copy_src and "words.textContent" in copy_src and
      "PhoneParts.copyText(message, words ? words.textContent : undefined)" in copy_src)
check("copy: clipboard write starts inside the tap (no await first), with a fallback and Copied feedback",
      "await" not in copy_src and "copyToClipboard(text)" in copy_src and "navigator.clipboard.writeText(text)" in app and
      'document.execCommand("copy")' in app and 'button.textContent = "Copied"' in copy_src)
check("copy: styled, scratch box off screen", ".tap-copy {" in css and ".copy-scratch {" in css and "left: -9999px" in css)
render_src = app.split("function renderTranscript(opts) {")[1].split("\n}\n")[0] if "function renderTranscript(opts) {" in app else ""
check("dividers: rule from PhoneParts.dayBreaks, label worked out at paint time",
      "PhoneParts.dayBreaks(shownRows.map((r) => r.message))" in render_src and "const nowMs = Date.now();" in render_src and
      "PhoneParts.dividerLabel(key, nowMs)" in app)
check("dividers: spliced in before the day's first message, Load earlier first and #live last",
      "shownRows.push({ message, at: wanted.length });" in render_src and
      "wanted.splice(shownRows[i].at, 0, dayDivider(breaks[i], nowMs))" in render_src and
      render_src.index("dayBreaks") < render_src.index("wanted.push(live)") and
      "if (state.hasEarlier) wanted.push(earlierButton());" in render_src)
check("dividers: one cached node per day, text updated only when the label changes",
      "const dividerNodes = new Map();" in app and "dividerNodes.get(key)" in app and "if (hit.text !== label.text)" in app)
check("dividers: separator role and full-text aria-label, no inline styles",
      'node.setAttribute("role", "separator")' in app and 'setAttribute("aria-label", label.text)' in app and
      ".style." not in app.split("function dayDivider(key, nowMs) {")[1].split("\n}\n")[0])
div_css = css.split(".day-divider {")[1].split("}")[0] if ".day-divider {" in css else ""
check("dividers: centred hairline look, Today/Yesterday in the accent colour",
      "display: flex" in div_css and "gap: 8px" in div_css and "margin: 14px 0 12px" in div_css and
      ".day-line { flex: 1; height: 1px; background: var(--border); }" in css and
      ".day-cap { font-size: 12px; font-weight: 600; color: var(--muted-fg); white-space: nowrap; }" in css and
      ".day-pre { color: var(--primary); }" in css)
pin_src = app.split("function updateDatePin() {")[1].split("\n}\n")[0] if "function updateDatePin() {" in app else ""
check("pin: one overlay outside the scroller, before it, no taps, hidden at first",
      re.search(r'<div class="date-pin-host">\s*<div class="date-pin" id="date-pin" aria-hidden="true" hidden>'
                r'<div class="date-pin-band"><div class="day-divider">[\s\S]*?</div>\s*<div class="transcript" id="transcript">', html) and
      ".date-pin-host { position: relative; height: 0; z-index: 5; pointer-events: none; }" in css and
      "pointer-events: none" in css.split(".date-pin {")[1].split("}")[0] and "overflow: hidden" in css.split(".date-pin {")[1].split("}")[0])
check("pin: solid transcript background, same look as the in-flow line",
      "background: var(--bg)" in css.split(".date-pin-band {")[1].split("}")[0] and ".date-pin-band .day-divider { margin: 0; }" in css)
check("pin: rule from PhoneParts.resolveDatePin, label from dividerLabel, shared cap painter",
      "PhoneParts.resolveDatePin(openers, line, height)" in pin_src and "PhoneParts.dividerLabel(look.day, Date.now())" in pin_src and
      'paintDayCap(pin.querySelector(".day-cap"), label)' in pin_src and "paintDayCap(hit.cap, label);" in app)
check("pin: reads only the day lines' positions; offset set through the CSSOM",
      ':scope > .day-divider[data-day]' in pin_src and "node.dataset.day = key;" in app and
      'band.style.transform = look.shift ? "translateY(" + look.shift + "px)" : ""' in pin_src)
check("pin: re-measured on scroll (one frame at a time), after each render and on a chat switch",
      "scheduleDatePin();\n  const bar = $(\"tapback\");" in app and "if (DATE_PIN.frame) return;" in app and
      "DATE_PIN.frame = requestAnimationFrame(updateDatePin);" in app and
      render_src.rstrip().endswith("updateDatePin();") and
      '$("transcript").textContent = "";\n  updateDatePin();' in app)
# Classic scripts share one global scope: a second top-level function with the
# same name silently replaces the first everywhere (the clockTime bug: the
# Status screen lost the seconds on its "Refreshed" time).
tops = {}
for rel in ("page/lists.js", "page/app.js"):
    for name in re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", read(rel), re.M):
        tops.setdefault(name, []).append(rel)
dupes = sorted(n for n, where in tops.items() if len(where) > 1)
check("no top-level function is defined twice across the page's classic scripts" + (" (" + ", ".join(dupes) + ")" if dupes else ""),
      not dupes)
check("refresh times keep their seconds (refreshClock), cron keeps clockTime",
      "function refreshClock(ms)" in app and 'second: "2-digit"' in app.split("function refreshClock(ms)")[1].split("\n}\n")[0] and
      '"Refreshed " + refreshClock(STATUS.refreshedAt)' in app and '"Updated " + refreshClock(Date.now())' in app)
check("new endpoints are local, not forwarded",
      all(p in serve.LOCAL_API for p in (serve.REACTIONS_PATH, serve.REACTIONS_UNSEEN_PATH, serve.SEARCH_PATH)))

passed = sum(results)
print("%d/%d passed" % (passed, len(results)))
sys.exit(0 if passed == len(results) else 1)
