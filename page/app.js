const KEY_NAME = "hermes.phone.apiKey";
const SOURCE = {
  cli: "CLI", tui: "CLI", desktop: "Desktop", api_server: "API",
  telegram: "Telegram", discord: "Discord", slack: "Slack", cron: "Cron"
};
const CHOICE = {
  once: "Allow once",
  session: "Allow for session",
  always: "Always allow",
  deny: "Deny"
};

const RUN_NAME = "hermes.phone.run";
// Messages per page. The server caps a page at 500; 200 keeps a phone render
// quick. The quiet refresh during a run (U6) reads only the newest 50. Each
// request asks for one extra row, so "Load earlier" shows only when older
// rows exist. The server serves a session's active history only: rows archived
// by context compaction are not returned.
const paging = { size: 200, quiet: 50 };
// Hermes stores a mid-run steer as its own user row, wrapped in this marker.
const STEER_KIND = "steer";
const STEER_OPEN = /^\s*\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*/;
const STEER_CLOSE = /\s*\[\/OUT-OF-BAND USER MESSAGE\]\s*$/;

// Tools whose result carries a diff. Same set the desktop app uses for its
// "files changed" card (apps/desktop/src/lib/tool-render-class.ts).
const EDIT_TOOLS = new Set(["edit_file", "patch", "write_file"]);
// Argument fields that best say what a tool call acted on, in order.
const ARG_KEYS = ["command", "path", "file_path", "query", "pattern", "url", "urls", "name", "goal", "action", "code", "prompt"];
const PATH_KEYS = new Set(["path", "file_path"]);

function oneLine(text, max) {
  const line = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
  const n = max || 90;
  return line.length > n ? line.slice(0, n - 1) + "…" : line;
}

function baseName(path) {
  const parts = String(path || "").replace(/\\/g, "/").split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path || "");
}

function callId(call) {
  return (call && (call.id || call.call_id)) || "";
}

function callArgs(call) {
  const raw = call && call.function ? call.function.arguments : call && call.arguments;
  const parsed = parseJson(raw);
  return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
}

function argSummary(args) {
  for (const k of ARG_KEYS) {
    let v = args[k];
    if (Array.isArray(v)) v = v.join(", ");
    if (typeof v !== "string" || !v.trim()) continue;
    return oneLine(PATH_KEYS.has(k) ? baseName(v) : v);
  }
  return "";
}

// The diff inside a file-edit result. History holds the full JSON; the live
// stream sends at most 500 characters of it, cut mid-string, so fall back to
// pulling the string out by hand and dropping a half-written escape.
function diffOf(text) {
  const obj = parseJson(text);
  if (obj && typeof obj === "object") {
    for (const k of ["inline_diff", "diff"]) {
      if (typeof obj[k] === "string" && obj[k].trim()) return obj[k];
    }
    return "";
  }
  if (typeof text !== "string") return "";
  const m = text.match(/"(?:inline_diff|diff)":\s*"((?:[^"\\]|\\.)*)/);
  if (!m) return "";
  let body = m[1].replace(/\\u[0-9a-fA-F]{0,3}$/, "");
  const slashes = body.match(/\\+$/);
  if (slashes && slashes[0].length % 2) body = body.slice(0, -1);
  try { return JSON.parse("\"" + body + "\""); } catch (e) { return ""; }
}

function diffStats(diff) {
  let added = 0;
  let removed = 0;
  for (const line of String(diff || "").split("\n")) {
    if (line.startsWith("+") && !line.startsWith("+++")) added += 1;
    else if (line.startsWith("-") && !line.startsWith("---")) removed += 1;
  }
  return { added, removed };
}

const DIFF_MAX_LINES = 400;

function diffView(diff) {
  const pre = document.createElement("pre");
  pre.className = "diff";
  const lines = String(diff || "").replace(/\n$/, "").split("\n");
  for (const line of lines.slice(0, DIFF_MAX_LINES)) {
    const span = document.createElement("span");
    span.className = line.startsWith("+++") || line.startsWith("---") ? "d-file"
      : line.startsWith("@@") ? "d-hunk"
      : line.startsWith("+") ? "d-add"
      : line.startsWith("-") ? "d-del" : "d-ctx";
    span.textContent = line || " ";
    pre.append(span);
  }
  if (lines.length > DIFF_MAX_LINES) {
    const more = document.createElement("span");
    more.className = "d-file";
    more.textContent = "… " + (lines.length - DIFF_MAX_LINES) + " more lines";
    pre.append(more);
  }
  return pre;
}

function countsNode(stats) {
  const wrap = document.createElement("span");
  wrap.className = "counts";
  const plus = document.createElement("span");
  plus.className = "plus";
  plus.textContent = "+" + stats.added;
  const minus = document.createElement("span");
  minus.className = "minus";
  minus.textContent = "−" + stats.removed;
  wrap.append(plus, " ", minus);
  return wrap;
}

// Readable result text: a terminal's output, a file's content, or the raw text.
function resultText(text) {
  const obj = parseJson(text);
  if (obj && typeof obj === "object" && !Array.isArray(obj)) {
    if (typeof obj.output === "string") {
      const code = typeof obj.exit_code === "number" && obj.exit_code !== 0 ? "\n[exit " + obj.exit_code + "]" : "";
      return (obj.output || "(no output)") + code;
    }
    if (typeof obj.content === "string") return obj.content;
    if (typeof obj.error === "string" && obj.error) return obj.error;
  }
  return String(text || "");
}

function toolFailed(text) {
  const obj = parseJson(text);
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return false;
  return obj.success === false ||
    (typeof obj.error === "string" && obj.error !== "") ||
    (typeof obj.exit_code === "number" && obj.exit_code !== 0);
}

function clock(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? h + ":" + String(m).padStart(2, "0") + ":" + ss : m + ":" + ss;
}

function fmtDur(seconds) {
  const n = Number(seconds);
  if (!isFinite(n) || n < 0.1) return "";
  return n < 60 ? n.toFixed(n < 10 ? 1 : 0) + "s" : clock(n * 1000);
}

function saveRun(run) {
  try {
    if (run && run.runId) {
      localStorage.setItem(RUN_NAME, JSON.stringify({
        runId: run.runId, sessionId: run.sessionId, userText: run.userText, title: run.title
      }));
    } else {
      localStorage.removeItem(RUN_NAME);
    }
  } catch (e) {}
}

function savedRun() {
  try { return JSON.parse(localStorage.getItem(RUN_NAME) || "null"); }
  catch (e) { return null; }
}

const state = {
  sessions: [],
  offset: 0,
  hasMore: false,
  sessionId: null,
  title: "Hermes",
  messages: [],
  hasEarlier: false,
  loadingEarlier: false,
  tail: null,
  run: null
};

const $ = (id) => document.getElementById(id);

function key() {
  try { return localStorage.getItem(KEY_NAME) || ""; }
  catch (e) { return ""; }
}

// Face ID (passkey) lock. Once a passkey is enrolled the Mac holds the API key
// and this page holds only an unlock cookie it cannot read. AUTH.faceid is
// learned from /phone-api/auth/status; if that fails the page falls back to
// the API key, and the Mac still refuses it when Face ID is on.
const AUTH = { faceid: false, unlocked: false, setupOpen: false };
const LOGIN = { opts: null, at: 0, fetching: false };
const SETUP = { opts: null, skipped: false };
const CHALLENGE_MS = 120000;

function authed() {
  return AUTH.faceid ? AUTH.unlocked : Boolean(key());
}

async function loadAuth() {
  try {
    const res = await fetch(apiBase() + "/phone-api/auth/status", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    AUTH.faceid = Boolean(data.enrolled);
    AUTH.unlocked = Boolean(data.unlocked);
    AUTH.setupOpen = Boolean(data.setup_open);
  } catch (e) {}
}

function fromB64(text) {
  const s = String(text || "").replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(s + "===".slice((s.length + 3) % 4));
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out.buffer;
}

function toB64(buffer) {
  const bytes = new Uint8Array(buffer);
  let raw = "";
  for (let i = 0; i < bytes.length; i++) raw += String.fromCharCode(bytes[i]);
  return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function credList(list) {
  return (list || []).map((c) => ({ type: c.type || "public-key", id: fromB64(c.id) }));
}

async function passkeyGet(opts) {
  const cred = await navigator.credentials.get({ publicKey: {
    challenge: fromB64(opts.challenge),
    rpId: opts.rpId,
    timeout: opts.timeout,
    userVerification: opts.userVerification || "required",
    allowCredentials: credList(opts.allowCredentials)
  } });
  return {
    id: cred.id, rawId: toB64(cred.rawId), type: cred.type,
    response: {
      clientDataJSON: toB64(cred.response.clientDataJSON),
      authenticatorData: toB64(cred.response.authenticatorData),
      signature: toB64(cred.response.signature)
    }
  };
}

async function passkeyCreate(opts) {
  const cred = await navigator.credentials.create({ publicKey: {
    challenge: fromB64(opts.challenge),
    rp: opts.rp,
    user: { id: fromB64(opts.user.id), name: opts.user.name, displayName: opts.user.displayName },
    pubKeyCredParams: opts.pubKeyCredParams,
    authenticatorSelection: opts.authenticatorSelection,
    attestation: opts.attestation,
    timeout: opts.timeout,
    excludeCredentials: credList(opts.excludeCredentials)
  } });
  return {
    id: cred.id, rawId: toB64(cred.rawId), type: cred.type,
    response: {
      clientDataJSON: toB64(cred.response.clientDataJSON),
      attestationObject: toB64(cred.response.attestationObject)
    }
  };
}

function faceIdError(e) {
  if (e && e.name === "NotAllowedError") return "Face ID was cancelled or timed out. Tap to try again.";
  return (e && e.message) || "Face ID check failed.";
}

function lockError(text) {
  $("lock-err").textContent = text || "";
  $("lock-err").hidden = !text;
}

// The challenge is fetched before the tap, so Safari runs Face ID straight
// from the tap instead of after a network wait.
async function prefetchLogin() {
  if (LOGIN.fetching || (LOGIN.opts && Date.now() - LOGIN.at < CHALLENGE_MS)) return;
  LOGIN.fetching = true;
  try {
    const res = await fetch(apiBase() + "/phone-api/auth/login/options", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (res.ok) { LOGIN.opts = await res.json(); LOGIN.at = Date.now(); }
  } catch (e) {
  } finally {
    LOGIN.fetching = false;
  }
}

function showLock(message) {
  AUTH.unlocked = false;
  if (state.run) state.run = null;
  show("lock-screen");
  lockError(message);
  prefetchLogin();
}

async function unlock() {
  const button = $("unlock");
  button.disabled = true;
  lockError("");
  try {
    let opts = LOGIN.opts && Date.now() - LOGIN.at < CHALLENGE_MS ? LOGIN.opts : null;
    LOGIN.opts = null;
    if (!opts) {
      await prefetchLogin();
      opts = LOGIN.opts;
      LOGIN.opts = null;
      if (!opts) throw new Error("Can't reach Hermes from this page.");
    }
    const credential = await passkeyGet(opts);
    const res = await fetch(apiBase() + "/phone-api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ credential }) });
    if (!res.ok) throw new Error(await errorMessage(res));
    AUTH.unlocked = true;
    await boot();
  } catch (e) {
    lockError(faceIdError(e));
    prefetchLogin();
  } finally {
    button.disabled = false;
  }
}

function setupError(text) {
  $("setup-err").textContent = text || "";
  $("setup-err").hidden = !text;
}

function showSetup() {
  SETUP.opts = null;
  show("setup-screen");
  $("setup-code").value = "";
  $("setup-code").disabled = false;
  $("setup-btn").textContent = "Check code";
  $("setup-text").textContent = "Enter the setup code shown on the Mac.";
  setupError("");
}

// Two taps: the first checks the code and fetches the challenge, the second
// runs Face ID directly from the tap.
async function setupStep() {
  const button = $("setup-btn");
  button.disabled = true;
  setupError("");
  try {
    if (!SETUP.opts) {
      const res = await fetch(apiBase() + "/phone-api/auth/register/options", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: $("setup-code").value }) });
      if (!res.ok) throw new Error(await errorMessage(res));
      SETUP.opts = await res.json();
      $("setup-code").disabled = true;
      $("setup-text").textContent = "Code accepted. Tap Set up Face ID and confirm with Face ID.";
      button.textContent = "Set up Face ID";
      return;
    }
    const opts = SETUP.opts;
    SETUP.opts = null;
    const credential = await passkeyCreate(opts);
    const res = await fetch(apiBase() + "/phone-api/auth/register", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ credential }) });
    if (!res.ok) throw new Error(await errorMessage(res));
    setKey("");
    AUTH.faceid = true;
    AUTH.unlocked = true;
    AUTH.setupOpen = false;
    await boot();
  } catch (e) {
    if (!SETUP.opts) {
      $("setup-code").disabled = false;
      button.textContent = "Check code";
      $("setup-text").textContent = "Enter the setup code shown on the Mac.";
    }
    setupError(faceIdError(e));
  } finally {
    button.disabled = false;
  }
}
function setKey(value) {
  try {
    if (value) localStorage.setItem(KEY_NAME, value);
    else localStorage.removeItem(KEY_NAME);
  } catch (e) {}
}

function show(id) {
  for (const screen of ["key-screen", "lock-screen", "setup-screen", "list-screen", "chat-screen"]) {
    $(screen).hidden = screen !== id;
  }
}

// Fit the app to the visible area when the keyboard is up. Only the keyboard
// may move it: an iOS rubber-band at the end of the transcript also fires
// visualViewport scroll with a bouncing offsetTop, and following that made
// the whole app shake. Unchanged values are not rewritten.
const viewportLast = { h: "", top: "" };
function syncViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  const keyboard = vv.height < window.innerHeight - 80;
  const h = Math.round(vv.height) + "px";
  const top = (keyboard ? Math.max(0, Math.round(vv.offsetTop)) : 0) + "px";
  if (h !== viewportLast.h) document.documentElement.style.setProperty("--vvh", (viewportLast.h = h));
  if (top !== viewportLast.top) document.documentElement.style.setProperty("--vvtop", (viewportLast.top = top));
}
if (window.visualViewport) {
  visualViewport.addEventListener("resize", syncViewport);
  visualViewport.addEventListener("scroll", syncViewport);
}
syncViewport();

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));
}

// Markdown is rendered by marked and then sanitized by DOMPurify, both served
// from vendor/ on this origin. Raw HTML in a message is shown as text, not
// parsed: models write <placeholders> far more often than they mean markup.
marked.use({
  gfm: true,
  breaks: true,
  async: false,
  renderer: {
    html(token) { return escapeHtml(token.text); },
    // Task lists as text, so no form element is ever needed.
    checkbox(token) { return token.checked ? "☑ " : "☐ "; }
  }
});

const PURIFY = {
  ALLOWED_TAGS: [
    "a", "b", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4",
    "h5", "h6", "hr", "i", "li", "ol", "p", "pre", "s", "strong", "table",
    "tbody", "td", "th", "thead", "tr", "ul"
  ],
  ALLOWED_ATTR: ["href", "title", "align", "start"],
  ALLOWED_URI_REGEXP: /^(?:https?:|mailto:)/i
};

DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A" && node.getAttribute("href")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

function renderMarkdown(src) {
  const text = String(src ?? "").replace(/\u0000/g, "");
  return DOMPurify.sanitize(marked.parse(text), PURIFY);
}

function textOf(content) {
  if (content == null) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.map((part) => {
      if (typeof part === "string") return part;
      if (!part || typeof part !== "object") return "";
      if (typeof part.text === "string") return part.text;
      if (part.type === "image_url" || part.type === "image") return "[image]";
      return "";
    }).filter(Boolean).join("\n");
  }
  if (typeof content === "object" && typeof content.text === "string") return content.text;
  return "";
}

function parseJson(value) {
  if (typeof value !== "string") return value;
  try { return JSON.parse(value); } catch (e) { return value; }
}

function toolCallsOf(message) {
  const calls = parseJson(message && message.tool_calls);
  return Array.isArray(calls) ? calls : [];
}

function toolName(call) {
  if (!call) return "tool";
  if (call.function && call.function.name) return call.function.name;
  return call.name || call.tool || "tool";
}

function when(stamp) {
  const n = Number(stamp);
  if (!n) return "";
  const then = n > 1e12 ? n : n * 1000;
  const delta = Date.now() - then;
  const min = Math.round(delta / 60000);
  if (min < 1) return "now";
  if (min < 60) return min + "m";
  const hr = Math.round(min / 60);
  if (hr < 24) return hr + "h";
  const day = Math.round(hr / 24);
  if (day < 7) return day + "d";
  return new Date(then).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function sourceLabel(source) {
  if (!source) return "";
  return SOURCE[source] || source;
}

function stampMs(stamp) {
  const n = Number(stamp);
  if (!n) return 0;
  return n > 1e12 ? n : n * 1000;
}

function dayStart(ms) {
  const d = new Date(ms);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

function weekStartsOn() {
  try {
    const locale = new Intl.Locale(Intl.DateTimeFormat().resolvedOptions().locale);
    const info = locale.getWeekInfo ? locale.getWeekInfo() : locale.weekInfo;
    if (info && typeof info.firstDay === "number") return info.firstDay % 7;
  } catch (e) {}
  return 1;
}

function bucketLabel(stamp) {
  const then = stampMs(stamp);
  if (!then) return "";
  const start = dayStart(then);
  const today = dayStart(Date.now());
  const diff = Math.round((today - start) / 86400000);
  if (diff <= 0) return "Earlier today";
  if (diff === 1) return "Yesterday";
  const weekStart = today - ((new Date(today).getDay() - weekStartsOn() + 7) % 7) * 86400000;
  if (start >= weekStart) return "Earlier this week";
  if (start >= weekStart - 7 * 86400000) return "Last week";
  const d = new Date(then);
  const now = new Date();
  if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()) return "Earlier this month";
  return d.toLocaleDateString(undefined, d.getFullYear() === now.getFullYear()
    ? { month: "long" }
    : { month: "long", year: "numeric" });
}

function apiBase() {
  const path = location.pathname || "/";
  if (path === "/phone" || path.indexOf("/phone/") === 0) return "/phone";
  return "";
}

async function api(path, options) {
  const opts = options || {};
  const headers = new Headers(opts.headers || {});
  const saved = AUTH.faceid ? "" : key();
  if (saved) headers.set("Authorization", "Bearer " + saved);
  if (opts.body != null && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const url = path.charAt(0) === "/" ? apiBase() + path : path;
  let res;
  try {
    res = await fetch(url, {
      method: opts.method || "GET",
      headers,
      body: opts.body == null ? undefined : opts.body,
      signal: opts.signal
    });
  } catch (e) {
    if (e && e.name === "AbortError") throw e;
    const err = new Error("Can't reach Hermes from this page.");
    err.network = true;
    throw err;
  }
  if (res.status === 401 && AUTH.faceid) {
    showLock("");
    const err = new Error("Locked. Unlock with Face ID.");
    err.status = 401;
    err.locked = true;
    throw err;
  }
  if (res.status === 401) {
    setKey("");
    const err = new Error("That key was rejected.");
    err.status = 401;
    throw err;
  }
  return res;
}

async function errorMessage(res) {
  try {
    const data = await res.clone().json();
    if (data && data.error && data.error.message) return data.error.message;
  } catch (e) {}
  return "Request failed (" + res.status + ")";
}

async function errorCode(res) {
  try {
    const data = await res.clone().json();
    return data && data.error && data.error.code || "";
  } catch (e) { return ""; }
}

function notice(text) {
  const el = $("notice");
  if (!el) return;
  el.textContent = text || "";
  el.hidden = !text;
}

function showKey(message) {
  if (AUTH.faceid) return showLock(message && message.indexOf("Locked") === 0 ? "" : message);
  show("key-screen");
  $("key-input").value = "";
  const err = $("key-err");
  err.hidden = !message;
  err.textContent = message || "";
}

async function loadSessions(reset) {
  if (reset) state.offset = 0;
  const res = await api("/api/sessions?limit=50&offset=" + state.offset);
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  const rows = Array.isArray(data.data) ? data.data : [];
  state.sessions = reset ? rows : state.sessions.concat(rows);
  state.hasMore = Boolean(data.has_more);
  state.offset += rows.length;
  renderSessions();
}

// Session status dots, after the desktop sidebar: amber = waiting for your
// approval, accent = working, green = finished since you last opened it.
// The proxy's /phone-api/active lists every session with a live turn (any
// window); approvals are known only for replies started on this page.
const ACTIVE = { map: {}, timer: 0, checking: false, every: 5000 };
const UNREAD_NAME = "hermes.phone.unread";

function unreadSet() {
  try { return new Set(JSON.parse(localStorage.getItem(UNREAD_NAME) || "[]")); }
  catch (e) { return new Set(); }
}
function saveUnread(set) {
  try { localStorage.setItem(UNREAD_NAME, JSON.stringify([...set].slice(-100))); } catch (e) {}
}
function markRead(id) {
  const set = unreadSet();
  if (set.delete(id)) saveUnread(set);
}

function sessionDot(id) {
  const run = state.run;
  if (run && run.sessionId === id && !run.done) {
    return run.approvals && run.approvals.length ? "approval" : "working";
  }
  if (DESKTOP.list.some((a) => Array.isArray(a.sessions) && a.sessions.includes(id) && !a.sent)) return "approval";
  const live = ACTIVE.map[id];
  if (live) return live.state === "approval" ? "approval" : "working";
  return unreadSet().has(id) ? "unread" : "";
}

const DOT_LABEL = { approval: "Waiting for your approval", working: "Working", unread: "Finished, not opened yet" };

async function checkActive() {
  if (ACTIVE.checking || document.hidden || !authed()) return;
  ACTIVE.checking = true;
  try {
    const res = await api("/phone-api/active");
    if (!res.ok) return;
    const data = await res.json();
    if (!data.active || typeof data.active !== "object") return;
    const before = ACTIVE.map;
    ACTIVE.map = data.active;
    setDesktopApprovals(Array.isArray(data.approvals) ? data.approvals : []);
    // A turn that ended while you were not looking at it becomes unread.
    const set = unreadSet();
    let ended = false;
    for (const id of Object.keys(before)) {
      if (data.active[id]) continue;
      ended = true;
      const viewing = state.sessionId === id && !$("chat-screen").hidden;
      if (!viewing) set.add(id);
    }
    if (ended) saveUnread(set);
    if (!$("list-screen").hidden) {
      if (ended) await loadSessions(true).catch(() => renderSessions());
      else paintDots();
    }
  } catch (e) {
    // Quiet: the list still works without dots.
  } finally {
    ACTIVE.checking = false;
  }
}

function watchActive() {
  if (ACTIVE.timer) return;
  ACTIVE.timer = setInterval(checkActive, ACTIVE.every);
  checkActive();
}

// Repaint just the dots, without rebuilding the list (keeps scroll and taps).
function paintDots() {
  for (const button of document.querySelectorAll("#session-list .session")) {
    paintDot(button, button.dataset.id);
  }
}

function paintDot(button, id) {
  const kind = sessionDot(id);
  const dot = button.querySelector(".sdot");
  dot.className = "sdot" + (kind ? " dot-" + kind : "");
  dot.title = DOT_LABEL[kind] || "";
  dot.setAttribute("aria-label", DOT_LABEL[kind] || "");
  button.classList.toggle("live", kind === "working" || kind === "approval");
  const time = button.querySelector(".when");
  time.textContent = kind === "approval" ? "Needs approval" : kind === "working" ? "Working" : time.dataset.idle;
}

function sessionButton(session) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "session";
  button.dataset.id = session.id;
  if (session.id === state.sessionId) button.classList.add("on");
  const row = document.createElement("div");
  row.className = "row";
  const dot = document.createElement("span");
  dot.className = "sdot";
  dot.setAttribute("role", "status");
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = session.title || "New session";
  const time = document.createElement("div");
  time.className = "when";
  const label = sourceLabel(session.source);
  const age = when(session.last_active || session.started_at);
  time.dataset.idle = label ? age + " · " + label : age;
  row.append(dot, name, time);
  paintDot(button.appendChild(row).parentNode, session.id);
  const preview = document.createElement("div");
  preview.className = "preview";
  preview.textContent = (session.preview || "").replace(/\s+/g, " ").trim();
  button.append(row);
  if (preview.textContent) button.append(preview);
  button.addEventListener("click", () => openSession(session.id, session.title || "New session", true));
  return button;
}

function renderSessions() {
  const root = $("session-list");
  root.textContent = "";
  const pinned = state.sessions.filter((s) => s.pinned);
  const rest = state.sessions.filter((s) => !s.pinned);
  if (!pinned.length && !rest.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No sessions yet.";
    root.appendChild(empty);
  }
  for (const session of pinned) root.appendChild(sessionButton(session));
  let labelled = pinned.length > 0;
  let last = "";
  for (const session of rest) {
    const label = bucketLabel(session.last_active || session.started_at);
    if (labelled && label && label !== last) {
      const bucket = document.createElement("div");
      bucket.className = "bucket";
      bucket.textContent = label;
      root.appendChild(bucket);
    }
    last = label;
    labelled = true;
    root.appendChild(sessionButton(session));
  }
  if (state.hasMore) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "more";
    more.textContent = "More";
    more.addEventListener("click", async () => {
      more.disabled = true;
      try { await loadSessions(false); }
      catch (e) {
        const note = document.createElement("div");
        note.className = "empty";
        note.textContent = e.message || "Could not load sessions";
        root.appendChild(note);
      }
    });
    root.appendChild(more);
  }
}

function hashId() {
  const hash = location.hash || "";
  if (!hash.startsWith("#s=")) return "";
  try { return decodeURIComponent(hash.slice(3)); }
  catch (e) { return ""; }
}

async function openSession(id, title, push) {
  state.sessionId = id;
  state.title = title || "New session";
  state.messages = [];
  state.hasEarlier = false;
  OTHER.busy = null;
  markRead(id);
  historyNodes.clear();
  if (state.tail && state.tail.sessionId !== id) state.tail = null;
  notice("");
  toggleMenu(false);
  $("chat-title").textContent = state.title;
  if (push) history.pushState({ id }, "", "#s=" + encodeURIComponent(id));
  show("chat-screen");
  $("transcript").textContent = "";
  const status = document.createElement("div");
  status.className = "status-line";
  status.textContent = "Loading…";
  $("transcript").append(status);
  try {
    await reloadMessages();
  } catch (e) {
    if (e.status === 401) return showKey(e.message);
    status.textContent = e.message || "Could not open this session";
  }
  paintRunChrome();
  watchOther(true);
  $("draft").focus();
}

function messagesPath(id, offset, size) {
  return "/api/sessions/" + encodeURIComponent(id) + "/messages?order=latest&limit=" + (size + 1) +
    (offset ? "&offset=" + offset : "");
}

// Rows come back oldest first; a full page plus one means more exist, and the
// extra (oldest) row is dropped so the next page starts on it.
function trimPage(rows, size) {
  const more = rows.length > size;
  return { more, rows: more ? rows.slice(rows.length - size) : rows };
}

// Reload the newest page. Older pages the user already loaded are kept, so a
// reload during or after a run never throws away "Load earlier" results.
async function reloadMessages(opts) {
  const quiet = Boolean(opts && opts.quiet);
  const size = quiet ? paging.quiet : paging.size;
  const id = state.sessionId;
  const res = await api(messagesPath(id, 0, size));
  if (state.sessionId !== id) return;
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  if (state.sessionId !== id) return;
  let moved = false;
  if (data.session_id && data.session_id !== id) {
    moved = true;
    state.sessionId = data.session_id;
    history.replaceState({ id: state.sessionId }, "", "#s=" + encodeURIComponent(state.sessionId));
    if (state.run && state.run.openedId === id) state.run.sessionId = data.session_id;
  }
  const page = trimPage(Array.isArray(data.data) ? data.data : [], size);
  const fresh = page.rows;
  const first = fresh.length ? fresh[0].id : null;
  const older = moved || first == null ? [] : state.messages.filter((m) => m && m.id != null && m.id < first);
  state.messages = older.concat(fresh);
  if (!older.length) state.hasEarlier = page.more;
  renderTranscript();
  if (quiet) return;
  try {
    const info = await api("/api/sessions/" + encodeURIComponent(state.sessionId));
    if (state.sessionId !== id && state.sessionId !== data.session_id) return;
    if (info.ok) {
      const body = await info.json();
      const title = body.session && body.session.title;
      if (title) {
        state.title = title;
        $("chat-title").textContent = title;
      }
    }
  } catch (e) {}
}

function nearBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 90;
}

const REPLYING = "Replying…";
// Live run DOM is kept per block so a streamed token updates one element, not the whole page.
const liveNodes = new WeakMap();

// History nodes are reused across renders, keyed by message id, so a reload
// keeps opened tool results and Thinking blocks open and costs no re-render.
const historyNodes = new Map();

// The signature covers each call's result, so a step drawn while it was still
// running is redrawn once its result is saved.
function historyNode(message, results) {
  const calls = toolCallsOf(message);
  const done = calls.map((call) => {
    const r = results && results.get(callId(call));
    return r ? textOf(r.content).length : -1;
  }).join(",");
  const sig = textOf(message.content).length + ":" + calls.length + ":" + done + ":" +
    (calls.length && runningHere() ? "live" : "") + ":" + (message.display_kind || "");
  const hit = message.id != null ? historyNodes.get(message.id) : null;
  if (hit && hit.sig === sig) return hit.node;
  const node = renderHistoryMessage(message, results || new Map());
  if (message.id != null) historyNodes.set(message.id, { sig, node });
  return node;
}

function runningHere() {
  return Boolean(state.run && !state.run.done && state.run.sessionId === state.sessionId);
}

// A user row with any display_kind came from Hermes, not the person typing:
// guidance (steer), model-switch notices, background-process reports.
function realUser(message) {
  return Boolean(message) && message.role === "user" && !message.display_kind;
}

function steerText(content) {
  return textOf(content).replace(STEER_OPEN, "").replace(STEER_CLOSE, "");
}

function earlierButton() {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "more earlier";
  button.textContent = state.loadingEarlier ? "Loading…" : "Load earlier messages";
  button.disabled = state.loadingEarlier;
  button.addEventListener("click", loadEarlier);
  return button;
}

// U5. Pages back from the newest message. The offset can overlap rows already
// held if new messages arrived since; the id filter drops the overlap.
async function loadEarlier() {
  if (state.loadingEarlier || !state.hasEarlier) return;
  const id = state.sessionId;
  state.loadingEarlier = true;
  renderTranscript();
  try {
    const res = await api(messagesPath(id, state.messages.length, paging.size));
    if (state.sessionId !== id) return;
    if (!res.ok) throw new Error(await errorMessage(res));
    const data = await res.json();
    if (state.sessionId !== id) return;
    const page = trimPage(Array.isArray(data.data) ? data.data : [], paging.size);
    const first = state.messages.length ? state.messages[0].id : Infinity;
    const older = page.rows.filter((m) => m && m.id != null && m.id < first);
    state.hasEarlier = page.more;
    const root = $("transcript");
    const fromBottom = root.scrollHeight - root.scrollTop;
    state.messages = older.concat(state.messages);
    state.loadingEarlier = false;
    renderTranscript({ fromBottom });
  } catch (e) {
    if (e.status === 401) return showKey(e.message);
    notice(e.message || "Could not load earlier messages");
  } finally {
    if (state.loadingEarlier) {
      state.loadingEarlier = false;
      if (state.sessionId === id) renderTranscript();
    }
  }
}

// Tool results keyed by the call they answer, so a step row shows its call
// and its result together, the way the desktop app draws them.
function resultsByCall(messages) {
  const map = new Map();
  for (const m of messages) {
    if (m && m.role === "tool" && m.tool_call_id) map.set(m.tool_call_id, m);
  }
  return map;
}

function callsShown(messages) {
  const ids = new Set();
  for (const m of messages) {
    if (m && m.role === "assistant") for (const call of toolCallsOf(m)) if (callId(call)) ids.add(callId(call));
  }
  return ids;
}

// "N files changed" for one turn: every landed edit with a diff, one row per
// file, +/- summed. Mirrors the desktop card (changed-files.ts).
const turnCards = new Map();

function editsOf(message, results) {
  const out = [];
  for (const call of toolCallsOf(message)) {
    const name = toolName(call);
    if (!EDIT_TOOLS.has(name)) continue;
    const result = results.get(callId(call));
    if (!result || toolFailed(textOf(result.content))) continue;
    const diff = diffOf(textOf(result.content));
    if (!diff) continue;
    const args = callArgs(call);
    const obj = parseJson(textOf(result.content)) || {};
    const path = args.path || args.file_path || obj.resolved_path || obj.path || "";
    if (path) out.push({ path, diff });
  }
  return out;
}

function changedFilesCard(turnKey, edits) {
  const byPath = new Map();
  for (const edit of edits) {
    const file = byPath.get(edit.path) || { path: edit.path, diffs: [], added: 0, removed: 0 };
    const stats = diffStats(edit.diff);
    file.added += stats.added;
    file.removed += stats.removed;
    file.diffs.push(edit.diff);
    byPath.set(edit.path, file);
  }
  const files = [...byPath.values()];
  const sig = turnKey + "|" + files.map((f) => f.path + ":" + f.added + ":" + f.removed + ":" + f.diffs.length).join("|");
  const hit = turnCards.get(turnKey);
  if (hit && hit.sig === sig) return hit.node;
  const card = document.createElement("div");
  card.className = "changed";
  const head = document.createElement("div");
  head.className = "changed-head";
  const label = document.createElement("span");
  label.textContent = files.length === 1 ? "1 file changed" : files.length + " files changed";
  const review = document.createElement("button");
  review.type = "button";
  review.className = "changed-review";
  review.textContent = "Review";
  head.append(label, review);
  card.append(head);
  const rows = [];
  for (const file of files) {
    const row = document.createElement("div");
    row.className = "changed-file";
    const button = document.createElement("button");
    button.type = "button";
    const icon = document.createElement("span");
    icon.className = "file-ico";
    icon.textContent = /\.md$/i.test(file.path) ? "M↓" : "◇";
    const name = document.createElement("span");
    name.className = "file-name";
    name.textContent = baseName(file.path);
    button.title = file.path;
    button.append(icon, name, countsNode(file));
    const body = document.createElement("div");
    body.className = "changed-diff";
    body.hidden = true;
    const where = document.createElement("div");
    where.className = "file-path";
    where.textContent = file.path;
    body.append(where);
    for (const diff of file.diffs) body.append(diffView(diff));
    button.addEventListener("click", () => { body.hidden = !body.hidden; row.classList.toggle("open", !body.hidden); });
    row.append(button, body);
    rows.push({ row, body });
    card.append(row);
  }
  // Review opens every file's diff, or closes them all if they are all open.
  review.addEventListener("click", () => {
    const open = rows.some((r) => r.body.hidden);
    for (const r of rows) { r.body.hidden = !open; r.row.classList.toggle("open", open); }
  });
  turnCards.set(turnKey, { sig, node: card });
  return card;
}

function renderTranscript(opts) {
  const root = $("transcript");
  const stick = nearBottom(root);
  const top = root.scrollTop;
  root.textContent = "";
  if (state.hasEarlier) root.append(earlierButton());
  let shown = 0;
  const results = resultsByCall(state.messages);
  const called = callsShown(state.messages);
  let turnKey = "start";
  let edits = [];
  const flush = () => {
    if (edits.length) root.append(changedFilesCard(turnKey, edits));
    edits = [];
  };
  for (const message of state.messages) {
    if (!message || message.display_kind === "hidden") continue;
    if (message.role === "system") continue;
    if (realUser(message)) {
      flush();
      turnKey = "u" + message.id;
    }
    // A result whose call is on screen is drawn inside that call's row.
    if (message.role === "tool" && message.tool_call_id && called.has(message.tool_call_id)) continue;
    if (message.role === "assistant") edits = edits.concat(editsOf(message, results));
    const node = historyNode(message, results);
    if (!node) continue;
    root.append(node);
    shown += 1;
  }
  flush();
  if (!shown && !(state.run && state.run.sessionId === state.sessionId)) {
    const empty = document.createElement("div");
    empty.className = "intro";
    const mark = document.createElement("div");
    mark.className = "wordmark";
    mark.textContent = "Hermes Agent";
    const hint = document.createElement("p");
    hint.textContent = "Send the task, failing path, or half-formed plan. I'll help turn it into action.";
    empty.append(mark, hint);
    root.append(empty);
  }
  renderLive(true);
  if (opts && opts.fromBottom != null) root.scrollTop = root.scrollHeight - opts.fromBottom;
  else root.scrollTop = stick ? root.scrollHeight : top;
  paintDraftHint();
}

function paintDraftHint() {
  const run = state.run;
  const here = run && run.sessionId === state.sessionId;
  const hasUser = state.messages.some(realUser);
  $("draft").placeholder = here ? "Add guidance while it works"
    : OTHER.busy ? "Busy in another window"
    : hasUser ? "Send a follow-up" : "Give Hermes a task";
}

function userBubble(text, steer) {
  const wrap = document.createElement("div");
  wrap.className = "msg user" + (steer ? " steer" : "");
  if (steer) {
    const label = document.createElement("div");
    label.className = "steer-label";
    label.textContent = "Guidance";
    wrap.append(label);
  }
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  wrap.append(bubble);
  return wrap;
}

function renderHistoryMessage(message, results) {
  const text = textOf(message.content).trim();
  const calls = toolCallsOf(message);
  const reasoning = textOf(message.reasoning || message.reasoning_content).trim();
  if (message.role === "user") {
    if (message.display_kind === STEER_KIND) {
      const steer = steerText(message.content);
      return steer.trim() ? userBubble(steer, true) : null;
    }
    if (!text) return null;
    if (message.display_kind) {
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = text.replace(/^\[System:\s*/, "").replace(/\]$/, "");
      return note;
    }
    return userBubble(textOf(message.content), false);
  }
  if (message.role === "tool") {
    // Its call is on an earlier page, so draw the result on its own.
    const raw = textOf(message.content);
    return stepRow({ name: message.tool_name || "tool", summary: "", state: toolFailed(raw) ? "failed" : "done", result: raw });
  }
  if (message.role !== "assistant" && !text && !calls.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  if (reasoning) wrap.append(thinkBlock(reasoning));
  if (text && calls.length) {
    // Text written alongside tool calls is the running commentary the
    // desktop shows between steps.
    const body = document.createElement("div");
    body.className = "assistant thought";
    body.innerHTML = renderMarkdown(textOf(message.content));
    wrap.append(body);
  }
  for (const call of calls) wrap.append(stepRow(stepFromCall(call, results.get(callId(call)))));
  if (text && !calls.length) {
    const body = document.createElement("div");
    body.className = "assistant";
    body.innerHTML = renderMarkdown(textOf(message.content));
    wrap.append(body);
  }
  if (!wrap.childNodes.length) return null;
  return wrap;
}

// Saved model reasoning: collapsed, but with its first line showing so it
// reads as part of the turn rather than a bare label.
function thinkBlock(text) {
  const box = document.createElement("div");
  box.className = "think";
  const button = document.createElement("button");
  button.type = "button";
  const label = document.createElement("span");
  label.className = "think-label";
  label.textContent = "Thought";
  const first = document.createElement("span");
  first.className = "think-first";
  first.textContent = oneLine(text, 120);
  button.append(label, first);
  const pre = document.createElement("pre");
  pre.textContent = text;
  pre.hidden = true;
  button.addEventListener("click", () => {
    pre.hidden = !pre.hidden;
    box.classList.toggle("open", !pre.hidden);
  });
  box.append(button, pre);
  return box;
}

// What the model wrote between steps (the stream's reasoning.available, or
// assistant text saved next to tool calls). Shown inline, like the desktop.
function thoughtLine(text) {
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  const body = document.createElement("div");
  body.className = "assistant thought";
  body.innerHTML = renderMarkdown(text);
  wrap.append(body);
  return wrap;
}

function stepFromCall(call, result) {
  const raw = result ? textOf(result.content) : "";
  return {
    name: toolName(call),
    summary: argSummary(callArgs(call)),
    state: result ? (toolFailed(raw) ? "failed" : "done") : (runningHere() ? "running" : "called"),
    result: raw
  };
}

// One tool step, drawn like the desktop's step rows: an icon that spins while
// it runs and turns into a tick or a cross, the tool, what it acted on, and
// the time or the +/- of an edit. Tap to open the result or the diff.
function stepRow(step) {
  const box = document.createElement("div");
  box.className = "step";
  const head = document.createElement("button");
  head.type = "button";
  head.className = "step-head";
  const icon = document.createElement("span");
  icon.className = "step-ico";
  icon.setAttribute("aria-hidden", "true");
  const name = document.createElement("span");
  name.className = "step-name";
  const sum = document.createElement("span");
  sum.className = "step-sum";
  const meta = document.createElement("span");
  meta.className = "step-meta";
  head.append(icon, name, sum, meta);
  const body = document.createElement("div");
  body.className = "step-body";
  body.hidden = true;
  box.append(head, body);
  head.addEventListener("click", () => {
    const s = box._step;
    if (!s || !s.result) return;
    if (body.hidden && !body.childNodes.length) fillStepBody(body, s);
    body.hidden = !body.hidden;
    box.classList.toggle("open", !body.hidden);
  });
  paintStep(box, step);
  return box;
}

function paintStep(box, step) {
  box._step = step;
  box.className = "step " + (step.state || "done") + (box.classList.contains("open") ? " open" : "");
  const [icon, name, sum, meta] = box.firstChild.children;
  icon.textContent = step.state === "running" ? "" : step.state === "failed" ? "✕" : step.state === "called" ? "·" : "✓";
  name.textContent = step.name || "tool";
  sum.textContent = step.summary || "";
  meta.textContent = "";
  const diff = EDIT_TOOLS.has(step.name) && step.result ? diffOf(step.result) : "";
  if (diff && step.state !== "failed") meta.append(countsNode(diffStats(diff)));
  const time = step.state === "running" && step.started ? clock(Date.now() - step.started)
    : step.dur ? fmtDur(step.dur) : step.state === "failed" ? "failed" : "";
  if (time) meta.append((diff ? " " : "") + time);
  box.firstChild.disabled = !step.result;
  // A changed result invalidates an opened body.
  const body = box.lastChild;
  if (body.childNodes.length && body._for !== step.result) {
    body.textContent = "";
    if (!body.hidden) fillStepBody(body, step);
  }
}

function fillStepBody(body, step) {
  body._for = step.result;
  const diff = EDIT_TOOLS.has(step.name) ? diffOf(step.result) : "";
  if (diff) body.append(diffView(diff));
  else {
    const pre = document.createElement("pre");
    const text = resultText(step.result);
    pre.textContent = text.length > 4000 ? text.slice(0, 4000) + "…" : text;
    body.append(pre);
  }
  if (step.partial) {
    const note = document.createElement("div");
    note.className = "step-note";
    note.textContent = "Preview. The full result shows when the reply finishes.";
    body.append(note);
  }
}

function ensureRun(sessionId) {
  if (state.run && state.run.sessionId === sessionId) return state.run;
  return null;
}

function liveContainer() {
  let live = $("live");
  if (!live) {
    live = document.createElement("div");
    live.id = "live";
    $("transcript").append(live);
  }
  return live;
}

function userNode(run) {
  if (!run.userText || historyHasUser(run)) return null;
  if (!run._userNode) {
    const wrap = document.createElement("div");
    wrap.className = "msg user";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = run.userText;
    wrap.append(bubble);
    run._userNode = wrap;
  }
  return run._userNode;
}

// What the run is doing right now, in words. The stream says which tool runs;
// with only polling, the run status's last_event is all there is.
function runPhase(run) {
  if (run.approvals.length) return { label: "Waiting for your approval", since: run.phaseSince, wait: true };
  const open = [...run.blocks].reverse().find((b) => b.kind === "tool" && b.state === "running");
  if (open) {
    return { label: "Running " + open.name + (open.summary ? " · " + oneLine(open.summary, 60) : ""), since: open.started };
  }
  if (run.stopping) return { label: "Stopping…", since: run.phaseSince };
  if (!run.runId) return { label: "Starting…", since: run.startedAt };
  const last = run.blocks[run.blocks.length - 1];
  if (last && last.kind === "text" && run.lastEvent === "message.delta") return { label: "Writing the reply", since: run.phaseSince };
  if (!run.streamOpen && run.lastEvent === "tool.started") return { label: "Running a step", since: run.phaseSince };
  return { label: run.sawEvent ? "Thinking" : "Working", since: run.phaseSince || run.startedAt };
}

// The live status line: a pulsing dot, what is happening, how long this part
// has taken, and the whole reply's elapsed time. Repainted every second.
function statusNode(run) {
  if (!run._statusNode) {
    const line = document.createElement("div");
    const dot = document.createElement("span");
    dot.className = "dot";
    const label = document.createElement("span");
    label.className = "lbl";
    const phase = document.createElement("span");
    phase.className = "phase-t";
    const total = document.createElement("span");
    total.className = "total-t";
    line.append(dot, label, phase, total);
    run._statusNode = line;
  }
  const line = run._statusNode;
  if (run.done) {
    if (!run.statusText) return null;
    line.className = "status-line";
    line.children[1].textContent = run.statusText;
    line.children[2].textContent = "";
    line.children[3].textContent = "";
    return line;
  }
  const phase = runPhase(run);
  line.className = "status-line live" + (phase.wait ? " wait" : "");
  line.children[1].textContent = phase.label;
  const now = Date.now();
  line.children[2].textContent = phase.since && now - phase.since >= 3000 ? clock(now - phase.since) : "";
  line.children[3].textContent = run.startedAt ? clock(now - run.startedAt) : "";
  return line;
}

function stepOfBlock(block) {
  return {
    name: block.name,
    summary: block.summary || "",
    state: block.state,
    started: block.started,
    dur: block.dur,
    result: block.result || "",
    partial: Boolean(block.result)
  };
}

function blockSig(block) {
  return block.kind === "tool"
    ? [block.state, block.summary, block.result, block.dur].join("\u0001")
    : (block.text || "");
}

function updateToolBlock(box, block) {
  paintStep(box, stepOfBlock(block));
}

// A reply this page did not start (desktop, CLI, a cron job) holds a turn
// lease in Hermes. The proxy reports it; while it is held, the open session's
// saved history is refreshed every few seconds and a status line shows it.
const OTHER = { every: 4000, timer: 0, busy: null, checking: false };
const OTHER_PLATFORM = { desktop: "the desktop app", cli: "the CLI", tui: "the CLI", cron: "a cron job", api_server: "another client" };

function otherLine() {
  const busy = OTHER.busy;
  if (!busy || state.run && state.run.sessionId === state.sessionId) return null;
  if (!OTHER.node) {
    const line = document.createElement("div");
    for (const cls of ["dot", "lbl", "phase-t", "total-t"]) {
      const span = document.createElement("span");
      span.className = cls;
      line.append(span);
    }
    OTHER.node = line;
  }
  const line = OTHER.node;
  const waiting = desktopApprovalsHere().length > 0;
  line.className = "status-line live" + (waiting ? " wait" : "");
  line.children[1].textContent = waiting
    ? "Waiting for your approval"
    : "Working in " + (OTHER_PLATFORM[busy.platform] || "another Hermes window");
  line.children[2].textContent = "";
  line.children[3].textContent = busy.since ? clock(Date.now() - busy.since * 1000) : "";
  return line;
}

async function checkOther() {
  const id = state.sessionId;
  if (!id || OTHER.checking || document.hidden || $("chat-screen").hidden) return;
  if (state.run && state.run.sessionId === id) { OTHER.busy = null; return; }
  OTHER.checking = true;
  try {
    const res = await api("/phone-api/busy?session=" + encodeURIComponent(id));
    if (!res.ok || state.sessionId !== id) return;
    const data = await res.json();
    if (data.busy == null) return;
    const was = Boolean(OTHER.busy);
    OTHER.busy = data.busy ? data : null;
    if (OTHER.busy || was) {
      // Busy: the newest rows. Just finished: one full reload for the final answer.
      await reloadMessages({ quiet: Boolean(OTHER.busy) });
    }
    if (state.sessionId === id) renderLive();
  } catch (e) {
    if (e.status === 401) showKey(e.message);
  } finally {
    OTHER.checking = false;
  }
}

function watchOther(on) {
  clearInterval(OTHER.timer);
  OTHER.timer = 0;
  OTHER.busy = null;
  if (!on) return;
  OTHER.timer = setInterval(checkOther, OTHER.every);
  checkOther();
}

// One shared one-second tick while a reply runs in the open session: the
// status line's timers and the running step's clock.
setInterval(() => {
  if (OTHER.busy && OTHER.node && OTHER.node.isConnected && !document.hidden) otherLine();
  const run = state.run;
  if (!run || run.done || run.sessionId !== state.sessionId || document.hidden) return;
  if (run._statusNode && run._statusNode.isConnected) statusNode(run);
  for (const block of run.blocks) {
    if (block.kind !== "tool" || block.state !== "running") continue;
    const entry = liveNodes.get(block);
    if (entry) paintStep(entry.node, stepOfBlock(block));
  }
}, 1000);

function liveNode(block) {
  const sig = blockSig(block);
  const entry = liveNodes.get(block);
  if (!entry) {
    const node = renderLiveBlock(block);
    liveNodes.set(block, { node, sig });
    return node;
  }
  if (entry.sig === sig) return entry.node;
  entry.sig = sig;
  if (block.kind === "text") {
    entry.node.querySelector(".assistant").innerHTML = renderMarkdown(block.text || "");
  } else if (block.kind === "tool") {
    updateToolBlock(entry.node, block);
  } else {
    entry.node = renderLiveBlock(block);
  }
  return entry.node;
}

// Reconcile the live run into #live without clearing it, so scroll position, open
// Thinking/result blocks and text selection survive each streamed event.
function renderLive(rebuilding) {
  const root = $("transcript");
  const live = liveContainer();
  const stick = !rebuilding && nearBottom(root);
  const run = state.run && state.run.sessionId === state.sessionId ? state.run : null;
  const nodes = [];
  if (run) {
    nodes.push(userNode(run));
    for (const block of run.blocks) nodes.push(liveNode(block));
    nodes.push(statusNode(run));
  } else {
    if (state.tail && state.tail.sessionId === state.sessionId) {
      for (const block of state.tail.blocks) nodes.push(liveNode(block));
    }
    nodes.push(otherLine());
  }
  const wanted = nodes.filter(Boolean);
  wanted.forEach((node, i) => {
    if (live.children[i] !== node) live.insertBefore(node, live.children[i] || null);
  });
  while (live.children.length > wanted.length) live.lastElementChild.remove();
  if (stick) root.scrollTop = root.scrollHeight;
}

function historyHasUser(run) {
  const needle = (run.userText || "").trim();
  let count = 0;
  let last = "";
  for (const message of state.messages) {
    if (realUser(message)) {
      count += 1;
      last = textOf(message.content).trim();
    }
  }
  return count > (run.userCountAtSend || 0) && last === needle;
}

// U6. With the stream gone, saved history carries the progress. Once the
// user's message is in history, drop live blocks that history now repeats.
// Local notes stay, and so does guidance Hermes has not taken in yet.
function pruneSavedBlocks(run) {
  if (!historyHasUser(run)) return;
  const taken = state.messages
    .filter((m) => m && m.display_kind === STEER_KIND)
    .map((m) => steerText(m.content).trim());
  run.blocks = run.blocks.filter((block) =>
    block.kind === "note" ||
    (block.kind === "steer" && !taken.some((text) => text.includes(block.text.trim()))));
}

function renderLiveBlock(block) {
  if (block.kind === "steer") return userBubble(block.text || "", true);
  if (block.kind === "text") {
    const wrap = document.createElement("div");
    wrap.className = "msg assistant";
    const body = document.createElement("div");
    body.className = "assistant";
    body.innerHTML = renderMarkdown(block.text || "");
    wrap.append(body);
    return wrap;
  }
  if (block.kind === "think") return thoughtLine(block.text || "");
  if (block.kind === "note") {
    const note = document.createElement("div");
    note.className = "note";
    note.textContent = block.text || "";
    return note;
  }
  return stepRow(stepOfBlock(block));
}

function paintRunChrome() {
  const run = state.run;
  const here = run && run.sessionId === state.sessionId;
  const other = $("other-run");
  // While this session's reply runs, Send becomes "add guidance" (U3) and Stop sits beside it.
  $("stop").hidden = !here;
  $("send").setAttribute("aria-label", here ? "Send guidance" : "Send");
  $("draft").disabled = Boolean(run) && !here;
  $("send").disabled = !$("draft").value.trim() || (Boolean(run) && !here) || Boolean(here && run.steering);
  paintDraftHint();
  if (run && !here && $("chat-screen").hidden === false) {
    other.hidden = false;
    other.textContent = "";
    other.append("A reply is still running. ");
    const jump = document.createElement("button");
    jump.type = "button";
    jump.textContent = "Open it";
    jump.addEventListener("click", () => openSession(run.sessionId, run.title || "Hermes", true));
    other.append(jump);
  } else {
    other.hidden = true;
    other.textContent = "";
  }
  renderApprovals();
}

// Approvals raised in the desktop app (or any Hermes process running the
// phone-approvals plugin) arrive with /phone-api/active. They are answered
// through the proxy, not a run. The phone offers Allow once and Deny only;
// Allow takes a second tap.
const DESKTOP = { list: [], sig: "" };
const PHONE_CHOICES = ["once", "deny"];
const ARM_MS = 4000;

function desktopApprovalsHere() {
  const id = state.sessionId;
  if (!id) return [];
  const own = state.run && state.run.sessionId === id ? state.run.approvals : [];
  return DESKTOP.list.filter((a) =>
    Array.isArray(a.sessions) && a.sessions.includes(id) &&
    !own.some((o) => o.request_id && o.request_id === a.request_id));
}

function setDesktopApprovals(list) {
  const keep = new Map(DESKTOP.list.map((a) => [a.request_id, a]));
  // Reuse the object a card was built from, so its button handlers and the
  // page's own state (armed, sent, error) survive each poll.
  DESKTOP.list = list.map((a) => {
    const old = keep.get(a.request_id);
    return old ? Object.assign(old, a, pick(old, ["armed", "sent", "error"])) : a;
  });
  const sig = JSON.stringify(desktopApprovalsHere().map((a) => a.request_id));
  if (sig === DESKTOP.sig) return;
  DESKTOP.sig = sig;
  if (!$("chat-screen").hidden) {
    renderApprovals();
    renderLive();
  }
}

function pick(obj, names) {
  const out = {};
  if (!obj) return out;
  for (const name of names) if (obj[name] !== undefined) out[name] = obj[name];
  return out;
}

function renderApprovals() {
  const root = $("approvals");
  root.textContent = "";
  const run = state.run;
  if (run && run.sessionId === state.sessionId) {
    for (const approval of run.approvals) root.append(approvalCard(run, approval));
  }
  for (const approval of desktopApprovalsHere()) root.append(approvalCard(null, approval));
}

function approvalCard(run, approval) {
  const card = document.createElement("div");
  card.className = "approval";
  const title = document.createElement("h3");
  title.textContent = run ? "Approval needed" : "Approval needed on the desktop";
  card.append(title);
  const desc = approval.description || approval.command || "Hermes wants to use a tool.";
  const p = document.createElement("div");
  p.className = "desc";
  p.textContent = desc;
  card.append(p);
  if (approval.command && approval.command !== desc) {
    const cmd = document.createElement("pre");
    cmd.className = "cmd";
    cmd.textContent = approval.command;
    card.append(cmd);
  }
  if (approval.smart_denied) {
    const note = document.createElement("div");
    note.className = "meta";
    note.textContent = "This one can only be allowed once.";
    card.append(note);
  }
  if (approval.sent) {
    const note = document.createElement("div");
    note.className = "meta";
    note.textContent = "Sent. Waiting for Hermes to pick it up…";
    card.append(note);
    return card;
  }
  const choices = document.createElement("div");
  choices.className = "choices";
  // Session-wide and permanent allows are left to the desktop.
  const offered = PHONE_CHOICES.filter((c) =>
    !Array.isArray(approval.choices) || !approval.choices.length || approval.choices.includes(c));
  for (const choice of offered) {
    const button = document.createElement("button");
    button.type = "button";
    const armed = choice === "once" && approval.armed && approval.armed > Date.now();
    button.className = "textbtn" + (choice === "deny" ? " deny" : armed ? " primary armed" : "");
    button.textContent = armed ? "Tap again to allow" : CHOICE[choice] || choice;
    if (choice === "once" && AUTH.faceid) {
      button.textContent = "Allow once with Face ID";
      approvalChallenge(run, approval).catch(() => {});
    }
    button.addEventListener("click", () => {
      if (choice === "once" && AUTH.faceid) {
        approveWithFaceId(run, approval, button);
        return;
      }
      if (choice === "once" && !(approval.armed && approval.armed > Date.now())) {
        approval.armed = Date.now() + ARM_MS;
        renderApprovals();
        setTimeout(renderApprovals, ARM_MS + 50);
        return;
      }
      approval.armed = 0;
      if (run) decide(run, approval, choice, button);
      else decideDesktop(approval, choice, button);
    });
    choices.append(button);
  }
  card.append(choices);
  if (approval.error) {
    const err = document.createElement("div");
    err.className = "err";
    err.textContent = approval.error;
    card.append(err);
  }
  return card;
}

// A Face ID signature good only for this approval and only for Allow once.
async function approvalChallenge(run, approval) {
  const c = approval.challenge;
  if (c && Date.now() - c.at < CHALLENGE_MS) return;
  if (approval.fetching) return;
  approval.fetching = true;
  try {
    const body = run
      ? { kind: "run", run_id: run.runId, request_id: approval.request_id || "" }
      : { kind: "desktop", request_id: approval.request_id };
    const res = await api("/phone-api/auth/approve/options", { method: "POST", body: JSON.stringify(body) });
    if (res.ok) approval.challenge = { opts: await res.json(), at: Date.now() };
  } finally {
    approval.fetching = false;
  }
}

async function approveWithFaceId(run, approval, button) {
  approval.error = "";
  const take = () => {
    const c = approval.challenge;
    approval.challenge = null;
    return c && Date.now() - c.at < CHALLENGE_MS ? c.opts : null;
  };
  let opts = take();
  let assertion;
  try {
    if (!opts) {
      button.disabled = true;
      await approvalChallenge(run, approval);
      opts = take();
      if (!opts) throw new Error("Could not start Face ID. Try again.");
    }
    assertion = await passkeyGet(opts);
  } catch (e) {
    if (e && e.locked) return;
    approval.error = faceIdError(e);
    renderApprovals();
    return;
  }
  if (run) decide(run, approval, "once", button, assertion);
  else decideDesktop(approval, "once", button, assertion);
}

async function decide(run, approval, choice, button, assertion) {
  button.disabled = true;
  approval.error = "";
  const body = { choice };
  if (approval.request_id) body.request_id = approval.request_id;
  if (assertion) body.assertion = assertion;
  try {
    const res = await api("/v1/runs/" + encodeURIComponent(run.runId) + "/approval", {
      method: "POST",
      body: JSON.stringify(body)
    });
    if (!res.ok) {
      if (res.status === 409 && await errorCode(res) === "approval_not_pending") {
        run.approvals = run.approvals.filter((item) => item !== approval);
        renderLive();
        paintRunChrome();
        return;
      }
      approval.error = await errorMessage(res);
      renderApprovals();
      return;
    }
    run.approvals = run.approvals.filter((item) => item !== approval);
    run.statusText = REPLYING;
    renderLive();
    paintRunChrome();
  } catch (e) {
    if (e.status === 401) return showKey(e.message);
    approval.error = e.message || "Could not send that decision";
    renderApprovals();
  }
}

async function decideDesktop(approval, choice, button, assertion) {
  button.disabled = true;
  approval.error = "";
  const body = { request_id: approval.request_id, choice };
  if (assertion) body.assertion = assertion;
  try {
    const res = await api("/phone-api/approvals/decide", {
      method: "POST",
      body: JSON.stringify(body)
    });
    if (res.status === 409) {
      DESKTOP.list = DESKTOP.list.filter((item) => item !== approval);
      DESKTOP.sig = "";
      renderApprovals();
      return;
    }
    if (!res.ok) {
      approval.error = await errorMessage(res);
      renderApprovals();
      return;
    }
    // The card stays as "Sent" until the plugin applies it and the marker clears.
    approval.sent = true;
    renderApprovals();
    setTimeout(checkActive, 1500);
  } catch (e) {
    if (e.status === 401) return showKey(e.message);
    approval.error = e.message || "Could not send that decision";
    renderApprovals();
  }
}

function addApproval(run, event) {
  const id = event.request_id || "";
  if (id && run.approvals.some((item) => item.request_id === id)) return;
  run.approvals.push({
    request_id: id,
    command: event.command || "",
    description: event.description || "",
    choices: event.choices || ["once", "deny"],
    smart_denied: Boolean(event.smart_denied),
    error: ""
  });
  run.statusText = "Waiting for approval";
}

function liveTextBlock(run) {
  let block = run.blocks[run.blocks.length - 1];
  if (!block || block.kind !== "text") {
    block = { kind: "text", text: "" };
    run.blocks.push(block);
  }
  return block;
}

function onRunEvent(run, event) {
  const name = event.event || "";
  run.sawEvent = true;
  if (name && name !== run.lastEvent) run.phaseSince = Date.now();
  if (name) run.lastEvent = name;
  if (name === "message.delta") {
    liveTextBlock(run).text += event.delta || "";
  } else if ((name === "message.interim" && !event.already_streamed && event.text) ||
             (name === "reasoning.available" && event.text)) {
    // What the model wrote with this step. Hermes sends it twice, as an interim
    // message and as reasoning.available (cut to 500 characters); keep one.
    const text = String(event.text).trim();
    const prev = [...run.blocks].reverse().find((b) => b.kind !== "tool" || b.state !== "running");
    const same = prev && prev.kind === "think" &&
      (prev.text.trim().startsWith(text.slice(0, 480)) || text.startsWith(prev.text.trim().slice(0, 480)));
    if (same) {
      if (text.length > prev.text.trim().length) prev.text = text;
    } else {
      run.blocks.push({ kind: "think", text });
    }
  } else if (name === "tool.started") {
    run.blocks.push({
      kind: "tool",
      name: event.tool || "tool",
      state: "running",
      summary: oneLine(event.preview || ""),
      started: Date.now()
    });
  } else if (name === "tool.completed") {
    const open = run.blocks.find((block) => block.kind === "tool" && block.state === "running" && (!event.tool || block.name === event.tool));
    const dur = event.duration != null ? Number(event.duration) : (open && open.started ? (Date.now() - open.started) / 1000 : 0);
    const failed = Boolean(event.error) || toolFailed(event.preview || "");
    if (open) {
      open.state = failed ? "failed" : "done";
      open.dur = dur;
      open.result = event.preview || "";
    } else {
      run.blocks.push({
        kind: "tool",
        name: event.tool || "tool",
        state: failed ? "failed" : "done",
        dur,
        result: event.preview || ""
      });
    }
  } else if (name === "subagent.start") {
    run.blocks.push({
      kind: "tool",
      name: "subagent",
      state: "running",
      summary: oneLine(event.goal || event.preview || ""),
      started: Date.now(),
      subagent: event.subagent_id || event.task_id || ""
    });
  } else if (name === "subagent.complete") {
    const id = event.subagent_id || event.task_id || "";
    const open = run.blocks.find((block) => block.kind === "tool" && block.name === "subagent" && block.state === "running" && (!id || !block.subagent || block.subagent === id));
    const failed = event.status && !["completed", "done", "success"].includes(event.status);
    if (open) {
      open.state = failed ? "failed" : "done";
      open.dur = open.started ? (Date.now() - open.started) / 1000 : 0;
      open.result = event.summary || event.preview || "";
    } else {
      run.blocks.push({ kind: "tool", name: "subagent", state: failed ? "failed" : "done", result: event.summary || event.preview || "" });
    }
  } else if (name === "run.stopping") {
    run.stopping = true;
  } else if (name === "approval.request") {
    addApproval(run, event);
  } else if (name === "approval.responded") {
    run.approvals = run.approvals.filter((item) => !event.request_id || item.request_id !== event.request_id);
    run.statusText = REPLYING;
  } else if (name === "run.completed" || name === "run.failed" || name === "run.cancelled" || name === "run.interrupted") {
    finishRun(run, event);
    return;
  }
  if (state.sessionId === run.sessionId) {
    renderLive();
    paintRunChrome();
  } else if ($("list-screen").hidden === false) {
    renderSessions();
  }
}

function finishRun(run, event) {
  if (run.done) return;
  run.done = true;
  saveRun(null);
  const name = event.event || event.status || "";
  // The final answer is also relayed as the last step's text; drop that copy.
  const output = String(event.output || "").trim();
  const last = run.blocks[run.blocks.length - 1];
  if (output && last && last.kind === "think" && output.startsWith(String(last.text || "").trim().replace(/…$/, ""))) {
    run.blocks.pop();
  }
  if ((name === "run.completed" || event.status === "completed") && event.output && !liveTextBlock(run).text) {
    liveTextBlock(run).text = event.output;
  }
  for (const block of run.blocks) {
    if (block.kind === "tool" && block.state === "running") block.state = "called";
  }
  if (name === "run.failed" || event.status === "failed") {
    run.blocks.push({ kind: "note", text: event.error || "The run failed." });
  }
  // Guidance that arrived after the last step was never read. Hermes returns
  // it; put it back in the box so it can go as the next message.
  if (event.pending_steer) {
    const draft = $("draft");
    if (!draft.value.trim()) {
      draft.value = event.pending_steer;
      draft.dispatchEvent(new Event("input"));
    }
    notice("The reply finished before Hermes read your guidance. It is back in the box to send as a message.");
  }
  if (name === "run.cancelled" || name === "run.interrupted" || event.status === "cancelled" || event.status === "interrupted") {
    run.statusText = "Stopped";
  } else {
    run.statusText = "";
  }
  run.approvals = [];
  const sessionId = run.sessionId;
  state.run = null;
  paintRunChrome();
  if (state.sessionId === sessionId) {
    reloadMessages().then(() => {
      if (state.sessionId !== sessionId) return;
      if (historyHasUser(run)) {
        // History now holds the reply; local notes stay under it.
        keepNotes(run);
        renderLive();
        return;
      }
      if (run.userText && !historyHasUser(run)) {
        state.run = run;
        renderTranscript();
        paintRunChrome();
      }
    }).catch(() => {
      state.run = run;
      renderTranscript();
      paintRunChrome();
    });
  }
  if ($("list-screen").hidden === false) loadSessions(true).catch(() => renderSessions());
}

async function readStream(run, response) {
  const reader = response.body && response.body.getReader ? response.body.getReader() : null;
  if (!reader) throw new Error("This browser cannot stream the reply.");
  const decoder = new TextDecoder();
  let buf = "";
  while (!run.done) {
    const step = await reader.read();
    if (step.done) break;
    run.sawBytes = true;
    buf += decoder.decode(step.value, { stream: true });
    buf = buf.replace(/\r\n/g, "\n");
    let split;
    while ((split = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, split);
      buf = buf.slice(split + 2);
      const data = frame.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).replace(/^ /, "")).join("\n");
      if (!data) continue;
      let event;
      try { event = JSON.parse(data); } catch (e) { continue; }
      if (!run.done) onRunEvent(run, event);
    }
  }
}

async function pollRun(run) {
  let failures = 0;
  while (state.run === run && !run.done) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    if (state.run !== run || run.done || !run.runId) return;
    let res;
    try {
      res = await api("/v1/runs/" + encodeURIComponent(run.runId));
    } catch (e) {
      if (e.status === 401) { showKey(e.message); return; }
      failures += 1;
      if (failures >= 30) {
        run.blocks.push({ kind: "note", text: "Lost contact with Hermes. The reply may still be running." });
        run.statusText = "";
        run.done = true;
        state.run = null;
        keepNotes(run);
        if (state.sessionId === run.sessionId) renderTranscript();
        paintRunChrome();
      }
      continue;
    }
    failures = 0;
    if (res.status === 404) {
      run.blocks.push({ kind: "note", text: "This reply is no longer tracked. Showing what was saved." });
      run.done = true;
      state.run = null;
      keepNotes(run);
      paintRunChrome();
      if (state.sessionId === run.sessionId) reloadMessages().catch(() => renderTranscript());
      return;
    }
    if (!res.ok) continue;
    const status = await res.json();
    if (status.created_at && !run.startedAt) run.startedAt = status.created_at * 1000;
    if (status.status === "stopping") run.stopping = true;
    // With the stream closed, the run's last event is the only live signal.
    if (!run.streamOpen && status.last_event && status.last_event !== run.lastEvent) {
      run.lastEvent = status.last_event;
      run.phaseSince = Date.now();
      run.sawEvent = true;
    }
    if (status.status === "waiting_for_approval" && status.approval) {
      addApproval(run, status.approval);
    } else if (run.approvals.length) {
      run.approvals = [];
      if (status.status === "running") run.statusText = REPLYING;
    }
    if (["completed", "failed", "cancelled", "interrupted"].includes(status.status)) {
      finishRun(run, status);
      return;
    }
    // U6. Hermes saves messages after every tool round, so with the stream
    // closed (a reload, a resumed run, a dropped connection) the saved history
    // shows progress. Every other poll, about every 4 seconds.
    run.polls = (run.polls || 0) + 1;
    if (!run.streamOpen && run.polls % 2 === 0 && state.sessionId === run.sessionId && !state.loadingEarlier) {
      try {
        await reloadMessages({ quiet: true });
        if (state.run !== run || run.done) return;
        pruneSavedBlocks(run);
      } catch (e) {
        if (e.status === 401) { showKey(e.message); return; }
      }
    }
    if (state.sessionId === run.sessionId) {
      renderLive();
      paintRunChrome();
    }
  }
}

// Local notes (failure text, lost contact) outlive the run: they stay under the
// transcript until the next send or a session switch.
function keepNotes(run) {
  const notes = run.blocks.filter((block) => block.kind === "note");
  state.tail = notes.length ? { sessionId: run.sessionId, blocks: notes } : null;
}

function failStart(run, message) {
  run.done = true;
  saveRun(null);
  run.blocks.push({ kind: "note", text: message });
  run.statusText = "";
  run.approvals = [];
  state.run = null;
  keepNotes(run);
  // Nothing was saved, so put the message back in the box to retry.
  const draft = $("draft");
  if (run.userText && !draft.value.trim()) {
    draft.value = run.userText;
    draft.dispatchEvent(new Event("input"));
  }
  if (state.sessionId === run.sessionId) renderTranscript();
  paintRunChrome();
}

async function sendMessage(text) {
  const sessionId = state.sessionId;
  if (!sessionId || state.run) return;
  const run = {
    sessionId,
    openedId: sessionId,
    title: state.title,
    runId: "",
    userText: text,
    blocks: [],
    approvals: [],
    statusText: REPLYING,
    startedAt: Date.now(),
    phaseSince: Date.now(),
    userCountAtSend: state.messages.filter(realUser).length,
    done: false,
    streamOpen: false,
    sawBytes: false
  };
  state.run = run;
  state.tail = null;
  $("draft").value = "";
  $("draft").style.height = "auto";
  renderTranscript();
  paintRunChrome();
  let res;
  try {
    res = await api("/v1/runs", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ input: text, session_id: sessionId })
    });
  } catch (e) {
    if (e.status === 401) {
      state.run = null;
      paintRunChrome();
      return showKey(e.message);
    }
    failStart(run, e.message || "Could not start the reply");
    return;
  }
  if (!res.ok) {
    failStart(run, await errorMessage(res));
    return;
  }
  const started = await res.json();
  run.runId = started.run_id;
  saveRun(run);
  if (!run.runId) {
    finishRun(run, { event: "run.failed", error: "Hermes did not return a run." });
    return;
  }
  pollRun(run);
  try {
    run.streamOpen = true;
    const stream = await api("/v1/runs/" + encodeURIComponent(run.runId) + "/events");
    if (!stream.ok) throw new Error(await errorMessage(stream));
    await readStream(run, stream);
  } catch (e) {
    if (e && e.name === "AbortError") return;
    if (e && e.status === 401) return showKey(e.message);
  } finally {
    run.streamOpen = false;
  }
  if (!run.done) run.statusText = REPLYING;
  if (state.sessionId === run.sessionId) renderLive();
}

// U4. Session actions: rename, fork, delete. Fork and delete wait until this
// session's reply finishes: a fork copies the messages and marks the original
// "branched", and neither should happen under a run that is still writing.
function sessionBusy() {
  return Boolean(state.run && state.run.sessionId === state.sessionId);
}

function toggleMenu(open) {
  const menu = $("menu");
  if (!menu) return;
  const showIt = open == null ? menu.hidden : open;
  menu.hidden = !showIt;
  $("menu-btn").setAttribute("aria-expanded", showIt ? "true" : "false");
  if (showIt) paintMenu("main");
}

function paintMenu(mode) {
  $("menu-main").hidden = mode !== "main";
  $("rename-form").hidden = mode !== "rename";
  $("delete-confirm").hidden = mode !== "delete";
  menuError("");
  const busy = sessionBusy();
  $("menu-fork").disabled = busy;
  $("menu-delete").disabled = busy;
  $("menu-hint").textContent = busy
    ? "Fork and Delete are available when the reply finishes."
    : "Fork copies this session into a new one. The original stays, marked as branched.";
  if (mode === "rename") {
    const input = $("rename-input");
    input.value = state.title === "New session" ? "" : state.title;
    input.focus();
    input.select();
  }
}

function menuError(text) {
  $("menu-err").textContent = text || "";
  $("menu-err").hidden = !text;
}

async function renameSession(title) {
  const id = state.sessionId;
  const res = await api("/api/sessions/" + encodeURIComponent(id), {
    method: "PATCH",
    body: JSON.stringify({ title })
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  const saved = (data.session && data.session.title) || "";
  for (const session of state.sessions) if (session.id === id) session.title = saved;
  if (state.sessionId === id) {
    state.title = saved || "New session";
    $("chat-title").textContent = state.title;
  }
}

async function forkSession() {
  const res = await api("/api/sessions/" + encodeURIComponent(state.sessionId) + "/fork", {
    method: "POST",
    body: "{}"
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  const fork = data.session || {};
  if (!fork.id) throw new Error("Hermes did not return the new session.");
  await openSession(fork.id, fork.title || "New session", true);
  notice("Forked. You are in the copy; the original is still in the list.");
  loadSessions(true).catch(() => {});
}

async function deleteSession() {
  const id = state.sessionId;
  const res = await api("/api/sessions/" + encodeURIComponent(id), { method: "DELETE" });
  if (!res.ok) throw new Error(await errorMessage(res));
  const saved = savedRun();
  if (saved && saved.sessionId === id) saveRun(null);
  state.sessions = state.sessions.filter((session) => session.id !== id);
  state.sessionId = null;
  state.messages = [];
  state.tail = null;
  toggleMenu(false);
  history.replaceState({}, "", location.pathname);
  show("list-screen");
  renderSessions();
  loadSessions(true).catch(() => {});
}

async function menuAction(button, work) {
  button.disabled = true;
  menuError("");
  try {
    await work();
  } catch (e) {
    if (e.status === 401) return showKey(e.message);
    menuError(e.message || "That did not work");
  } finally {
    button.disabled = false;
  }
}

// U3. Hermes queues guidance and hands it to the model after the current tool
// step, as its own user row. It answers 409 unless the run is "running".
async function steerRun(text) {
  const run = state.run;
  if (!run || run.sessionId !== state.sessionId || !run.runId || run.steering) return;
  run.steering = true;
  paintRunChrome();
  try {
    const res = await api("/v1/runs/" + encodeURIComponent(run.runId) + "/steer", {
      method: "POST",
      body: JSON.stringify({ input: text })
    });
    if (res.ok) {
      $("draft").value = "";
      $("draft").style.height = "auto";
      run.blocks.push({ kind: "steer", text });
    } else if (res.status === 409) {
      notice(run.approvals.length
        ? "Answer the approval first. Your guidance is still in the box."
        : "Hermes is not taking guidance right now. Your text is still in the box.");
    } else {
      notice(await errorMessage(res));
    }
  } catch (e) {
    if (e.status === 401) return showKey(e.message);
    notice(e.message || "Could not send guidance");
  } finally {
    run.steering = false;
    if (state.sessionId === run.sessionId) renderLive();
    paintRunChrome();
  }
}

async function stopRun() {
  const run = state.run;
  if (!run || !run.runId) return;
  $("stop").disabled = true;
  try {
    const res = await api("/v1/runs/" + encodeURIComponent(run.runId) + "/stop", { method: "POST" });
    if (!res.ok) {
      run.blocks.push({ kind: "note", text: await errorMessage(res) });
      renderLive();
    }
  } catch (e) {
    if (e.status === 401) return showKey(e.message);
    run.blocks.push({ kind: "note", text: e.message || "Could not stop" });
    renderLive();
  } finally {
    $("stop").disabled = false;
  }
}

$("key-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const value = $("key-input").value.trim();
  if (!value) {
    if (key()) show("list-screen");
    return;
  }
  setKey(value);
  $("key-input").value = "";
  try {
    await loadSessions(true);
    show("list-screen");
  } catch (e) {
    if (e.status === 401) setKey("");
    showKey(e.message || "That key was rejected.");
  }
});

$("forget").addEventListener("click", () => {
  setKey("");
  state.run = null;
  showKey("");
});

$("key-btn").addEventListener("click", async () => {
  if (!AUTH.faceid) return showKey("");
  try {
    await fetch(apiBase() + "/phone-api/auth/logout", { method: "POST" });
  } catch (e) {}
  showLock("");
});
$("unlock").addEventListener("click", unlock);
$("setup-form").addEventListener("submit", (event) => {
  event.preventDefault();
  setupStep();
});
$("setup-skip").addEventListener("click", () => {
  SETUP.skipped = true;
  SETUP.opts = null;
  boot();
});

$("new-chat").addEventListener("click", async () => {
  $("new-chat").disabled = true;
  try {
    const res = await api("/api/sessions", { method: "POST", body: "{}" });
    if (!res.ok) throw new Error(await errorMessage(res));
    const data = await res.json();
    const session = data.session || {};
    await openSession(session.id, session.title || "New session", true);
    loadSessions(true).catch(() => {});
  } catch (e) {
    if (e.status === 401) return showKey(e.message);
    const note = document.createElement("div");
    note.className = "empty";
    note.textContent = e.message || "Could not start a chat";
    $("session-list").prepend(note);
  } finally {
    $("new-chat").disabled = false;
  }
});

$("menu-btn").addEventListener("click", () => toggleMenu());
$("menu-rename").addEventListener("click", () => paintMenu("rename"));
$("menu-delete").addEventListener("click", () => paintMenu("delete"));
$("rename-cancel").addEventListener("click", () => paintMenu("main"));
$("delete-no").addEventListener("click", () => paintMenu("main"));
$("menu-fork").addEventListener("click", () => menuAction($("menu-fork"), async () => {
  if (sessionBusy()) return;
  await forkSession();
}));
$("delete-yes").addEventListener("click", () => menuAction($("delete-yes"), async () => {
  if (sessionBusy()) return paintMenu("main");
  await deleteSession();
}));
$("rename-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = $("rename-form").querySelector("button[type=submit]");
  menuAction(button, async () => {
    await renameSession($("rename-input").value.trim());
    toggleMenu(false);
  });
});

$("back").addEventListener("click", () => {
  toggleMenu(false);
  if (history.state && history.state.id) history.back();
  else {
    history.pushState({}, "", location.pathname);
    show("list-screen");
    loadSessions(true).catch(() => {});
  }
});

window.addEventListener("popstate", () => {
  const id = hashId();
  if (id) openSession(id, "Hermes", false);
  else {
    show("list-screen");
    renderSessions();
  }
});

$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = $("draft").value.trim();
  if (!text) return;
  notice("");
  if (state.run) {
    if (state.run.sessionId === state.sessionId) steerRun(text);
    return;
  }
  sendMessage(text);
});

$("stop").addEventListener("click", stopRun);

$("draft").addEventListener("input", () => {
  const draft = $("draft");
  draft.style.height = "auto";
  draft.style.height = Math.min(draft.scrollHeight, 120) + "px";
  const run = state.run;
  $("send").disabled = !draft.value.trim() || Boolean(run && (run.sessionId !== state.sessionId || run.steering));
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (state.run && !state.run.streamOpen && !state.run.done && state.sessionId === state.run.sessionId) {
    renderLive();
  }
  if (!$("list-screen").hidden && authed()) loadSessions(true).catch(() => {});
  checkActive();
});

async function boot() {
  await loadAuth();
  if (AUTH.faceid) {
    setKey("");
    $("key-btn").textContent = "Lock";
    if (!AUTH.unlocked) return showLock("");
  } else if (AUTH.setupOpen && !SETUP.skipped) {
    return showSetup();
  }
  if (!authed()) return showKey("");
  watchActive();
  const id = hashId();
  try {
    await loadSessions(true);
  } catch (e) {
    if (e.status === 401) return showKey(e.message);
    show("list-screen");
    $("session-list").textContent = "";
    const note = document.createElement("div");
    note.className = "empty";
    note.textContent = e.message || "Can't reach Hermes from this page.";
    $("session-list").append(note);
    return;
  }
  const saved = savedRun();
  if (saved && saved.runId) {
    try {
      const res = await api("/v1/runs/" + encodeURIComponent(saved.runId));
      if (res.ok) {
        const status = await res.json();
        if (["queued", "running", "stopping", "waiting_for_approval"].includes(status.status)) {
          const run = {
            sessionId: saved.sessionId, openedId: saved.sessionId, title: saved.title || "Hermes",
            runId: saved.runId, userText: saved.userText || "", blocks: [], approvals: [],
            statusText: REPLYING, done: false, streamOpen: false, sawBytes: false,
            startedAt: status.created_at ? status.created_at * 1000 : Date.now(),
            phaseSince: status.updated_at ? status.updated_at * 1000 : Date.now(),
            lastEvent: status.last_event || ""
          };
          state.run = run;
          if (status.status === "waiting_for_approval" && status.approval) addApproval(run, status.approval);
          pollRun(run);
          openSession(saved.sessionId, saved.title || "Hermes", false);
          return;
        }
      }
    } catch (e) {}
    saveRun(null);
  }
  if (id) openSession(id, "Hermes", false);
  else show("list-screen");
}

boot();
