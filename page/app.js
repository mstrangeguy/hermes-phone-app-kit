const KEY_NAME = "hermes.phone.apiKey";
const SOURCE = {
  cli: "CLI", tui: "CLI", desktop: "Desktop", api_server: "API",
  telegram: "Telegram", discord: "Discord", slack: "Slack", cron: "Cron", oneshot: "One-off"
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

// Every reply this page started and has not seen finish, so a reload can pick
// them all up again. One per session at most; replies in different sessions
// run side by side.
const RUNS_NAME = "hermes.phone.runs";

function saveRuns() {
  try {
    const list = state.runs.filter((r) => r.runId && !r.done).map((r) => ({
      runId: r.runId, sessionId: r.sessionId, profile: r.profile, userText: r.userText, title: r.title
    }));
    if (list.length) localStorage.setItem(RUNS_NAME, JSON.stringify(list));
    else localStorage.removeItem(RUNS_NAME);
    localStorage.removeItem(RUN_NAME);
  } catch (e) {}
}

function savedRuns() {
  const out = [];
  try {
    const list = JSON.parse(localStorage.getItem(RUNS_NAME) || "[]");
    if (Array.isArray(list)) out.push(...list.filter((r) => r && r.runId));
    // The single run an older version of this page saved.
    const old = JSON.parse(localStorage.getItem(RUN_NAME) || "null");
    if (old && old.runId && !out.some((r) => r.runId === old.runId)) out.push(old);
  } catch (e) {}
  return out;
}

const state = {
  sessions: [],
  pages: {},
  meta: {},
  archived: [],
  sessionId: null,
  profile: "default",
  title: "Hermes",
  messages: [],
  hasEarlier: false,
  loadingEarlier: false,
  tail: null,
  runs: []
};

// The run for a session: the live one, or a finished one still shown there.
function runFor(sessionId) {
  if (!sessionId) return null;
  const list = state.runs.filter((r) => r.sessionId === sessionId);
  return list.find((r) => !r.done) || list[list.length - 1] || null;
}

function currentRun() {
  return runFor(state.sessionId);
}

function liveRun(sessionId) {
  const run = runFor(sessionId);
  return run && !run.done ? run : null;
}

function tracked(run) {
  return state.runs.includes(run);
}

function addRun(run) {
  if (!tracked(run)) state.runs.push(run);
}

function dropRun(run) {
  state.runs = state.runs.filter((r) => r !== run);
}

// Hermes serves every profile on one listener; a profile's API is under /p/<name>.
function inProfile(profile, path) {
  return !profile || profile === "default" ? path : "/p/" + encodeURIComponent(profile) + path;
}

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

// The Face ID sheet may hide the page for a moment; that must not count as
// leaving the app (see lockNow).
async function prompting(work) {
  LOCK.prompting++;
  try { return await work(); }
  finally { LOCK.prompting--; LOCK.lastInput = Date.now(); }
}

async function passkeyGet(opts) {
  const cred = await prompting(() => navigator.credentials.get({ publicKey: {
    challenge: fromB64(opts.challenge),
    rpId: opts.rpId,
    timeout: opts.timeout,
    userVerification: opts.userVerification || "required",
    allowCredentials: credList(opts.allowCredentials)
  } }));
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
  const cred = await prompting(() => navigator.credentials.create({ publicKey: {
    challenge: fromB64(opts.challenge),
    rp: opts.rp,
    user: { id: fromB64(opts.user.id), name: opts.user.name, displayName: opts.user.displayName },
    pubKeyCredParams: opts.pubKeyCredParams,
    authenticatorSelection: opts.authenticatorSelection,
    attestation: opts.attestation,
    timeout: opts.timeout,
    excludeCredentials: credList(opts.excludeCredentials)
  } }));
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
// A second caller waits for the fetch already in flight instead of giving up
// (the no-tap unlock starts while showLock's prefetch is still out).
function prefetchLogin() {
  if (LOGIN.fetching) return LOGIN.fetching;
  if (LOGIN.opts && Date.now() - LOGIN.at < CHALLENGE_MS) return Promise.resolve();
  LOGIN.fetching = (async () => {
    try {
      const res = await fetch(apiBase() + "/phone-api/auth/login/options", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (res.ok) { LOGIN.opts = await res.json(); LOGIN.at = Date.now(); }
    } catch (e) {
    } finally {
      LOGIN.fetching = null;
    }
  })();
  return LOGIN.fetching;
}

function showLock(message) {
  AUTH.unlocked = false;
  state.runs = [];
  show("lock-screen");
  lockError(message);
  prefetchLogin();
  autoUnlock();
}

// --- Auto lock and Face ID without a tap -------------------------------------
// WebKit allows one passkey request per page load without a tap. The page
// spends it on unlocking: open the app and Face ID runs. Leaving the app (app
// switcher, Home, screen off) or 10 minutes without a touch locks it: the
// unlock cookie is dropped on the Mac and the lock screen covers the content.
// Coming back reloads the page, which buys a fresh no-tap request. Replies keep
// running on the Mac; the open session (URL hash), running replies and the
// unsent draft come back after the unlock. The Unlock button stays for when
// iOS refuses or the prompt is cancelled.
const IDLE_LOCK_MS = 10 * 60 * 1000;
const DRAFT_NAME = "hermes.phone.draft";
const LOCK = { autoTried: false, prompting: 0, reloadOnShow: false, lastInput: Date.now() };

function autoUnlock() {
  if (LOCK.autoTried || !AUTH.faceid || document.visibilityState !== "visible") return;
  LOCK.autoTried = true;
  unlock();
}

function stashDraft() {
  const text = $("draft").value;
  try {
    if (state.sessionId && text.trim()) sessionStorage.setItem(DRAFT_NAME, JSON.stringify({ id: state.sessionId, text }));
  } catch (e) {}
}

function takeDraft(id) {
  try {
    const saved = JSON.parse(sessionStorage.getItem(DRAFT_NAME) || "null");
    if (!saved || saved.id !== id) return "";
    sessionStorage.removeItem(DRAFT_NAME);
    return String(saved.text || "");
  } catch (e) { return ""; }
}

function lockNow() {
  if (!AUTH.faceid || LOCK.prompting) return;
  // Already on the lock screen (say, Face ID was cancelled): coming back still
  // reloads, so it asks again without a tap.
  LOCK.reloadOnShow = true;
  if (!AUTH.unlocked) return;
  stashDraft();
  $("draft").blur();
  // keepalive: the page may be frozen right after it is hidden.
  try { fetch(apiBase() + "/phone-api/auth/logout", { method: "POST", keepalive: true }).catch(() => {}); }
  catch (e) {}
  LOCK.autoTried = true;  // this page load's no-tap request is spent or saved for the reload
  showLock("");
}

function resumeLocked() {
  if (!LOCK.reloadOnShow) return false;
  LOCK.reloadOnShow = false;
  location.reload();
  return true;
}

for (const name of ["pointerdown", "keydown", "input"]) {
  document.addEventListener(name, () => { LOCK.lastInput = Date.now(); }, { capture: true, passive: true });
}
setInterval(() => {
  if (document.visibilityState === "visible" && Date.now() - LOCK.lastInput > IDLE_LOCK_MS) lockNow();
}, 30000);
window.addEventListener("pageshow", (event) => {
  if (event.persisted && AUTH.faceid) location.reload();
});

async function unlock() {
  const button = $("unlock");
  if (button.disabled) return;
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
    // A refused no-tap attempt is normal (iOS can say no); the button is the fallback.
    lockError(e && e.name === "NotAllowedError" ? "" : faceIdError(e));
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

const SCREENS = ["key-screen", "lock-screen", "setup-screen", "list-screen", "cron-screen", "job-screen", "chat-screen"];
// The status bar takes the colour of the header under it, so it reads as one band.
const BAR_COLOR = { "list-screen": "#010409", "cron-screen": "#010409" };
function show(id) {
  for (const screen of SCREENS) {
    $(screen).hidden = screen !== id;
  }
  if (id !== "list-screen" && id !== "cron-screen") toggleSheet(false);
  const color = BAR_COLOR[id] || "#0d1117";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", color);
  // iOS 26+ ignores theme-color for an installed app and colours the status
  // bar from the fixed element at the top of the page: the .app frame.
  document.documentElement.style.setProperty("--top-bg", color);
}

// Fit the app to the visible area when the keyboard is up, so the composer
// sits right on the keyboard and the header stays at the top.
//
// The app is position: fixed, so it is placed against the layout viewport,
// which is what visualViewport.offsetTop is measured from. It used to be in
// normal flow: when iOS also scrolled the page to reveal the box, the app
// moved by offsetTop AND by that page scroll, about twice the keyboard, and
// the composer ended up above the screen. Any page scroll is undone too.
//
// Only the keyboard may move it: an iOS rubber-band at the end of the
// transcript also fires visualViewport scroll with a bouncing offsetTop, and
// following that made the whole app shake. Unchanged values are not rewritten.
const viewportLast = { h: "", top: "" };
// Set while the picture viewer is open (see openViewer).
const VIEWER = { open: false, tapTimer: 0 };
function syncViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  // A page pinch-zoom also shrinks the visual viewport; it is not the keyboard.
  if (VIEWER.open || vv.scale > 1.01) return;
  const keyboard = vv.height < window.innerHeight - 80;
  const h = Math.round(vv.height) + "px";
  const top = (keyboard ? Math.max(0, Math.round(vv.offsetTop)) : 0) + "px";
  if (window.scrollY || window.scrollX) window.scrollTo(0, 0);
  document.documentElement.classList.toggle("kb", keyboard);
  if (h === viewportLast.h && top === viewportLast.top) return;
  // Keep the transcript anchored to its bottom edge: what you were reading
  // (usually the newest message) stays just above the composer.
  const t = document.getElementById("transcript");
  const fromBottom = t ? t.scrollHeight - t.scrollTop - t.clientHeight : 0;
  if (h !== viewportLast.h) document.documentElement.style.setProperty("--vvh", (viewportLast.h = h));
  if (top !== viewportLast.top) document.documentElement.style.setProperty("--vvtop", (viewportLast.top = top));
  if (t && t.offsetParent) t.scrollTop = Math.max(0, t.scrollHeight - t.clientHeight - fromBottom);
}
if (window.visualViewport) {
  visualViewport.addEventListener("resize", syncViewport);
  visualViewport.addEventListener("scroll", syncViewport);
}
// iOS settles the keyboard after its animation; catch the last position too.
document.addEventListener("focusin", () => { syncViewport(); setTimeout(syncViewport, 350); });
document.addEventListener("focusout", () => setTimeout(syncViewport, 350));
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

// --- Session list: profiles, grouping, sorting, filters ---------------------
// After the desktop sidebar's filter menu. The choices are kept on the phone.
// Profiles come from the Mac (/phone-api/profiles): every profile with its
// own API key. Projects and archived sessions come from /phone-api/meta,
// because Hermes's session list has neither.
const VIEW_NAME = "hermes.phone.view";
const GROUPS = [["date", "Date"], ["project", "Project"], ["status", "Status"], ["source", "Source"], ["profile", "Profile"]];
const SORTS = [["updated", "Updated"], ["created", "Created"], ["status", "Status"], ["tokens", "Tokens"], ["cost", "Cost"]];
const STATUSES = [["approval", "Needs approval"], ["working", "Working"], ["unread", "Unread"], ["idle", "Idle"]];
const STATUS_RANK = { approval: 0, working: 1, unread: 2, idle: 3 };
const VIEW_DEFAULT = { profile: "default", group: "date", sort: "updated", status: [], sources: [], archived: false, collapsed: {} };
const VIEW = loadView();
const PROFILES = { list: ["default"] };
const LIST = { seq: 0, note: "" };

function loadView() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(VIEW_NAME) || "{}") || {}; } catch (e) {}
  const view = Object.assign({}, VIEW_DEFAULT, saved);
  if (!GROUPS.some(([id]) => id === view.group)) view.group = "date";
  if (!SORTS.some(([id]) => id === view.sort)) view.sort = "updated";
  for (const name of ["status", "sources"]) if (!Array.isArray(view[name])) view[name] = [];
  if (!view.collapsed || typeof view.collapsed !== "object") view.collapsed = {};
  view.archived = Boolean(view.archived);
  if (typeof view.profile !== "string" || !view.profile) view.profile = "default";
  return view;
}

function saveView() {
  try { localStorage.setItem(VIEW_NAME, JSON.stringify(VIEW)); } catch (e) {}
}

function viewChanged() {
  return VIEW.group !== "date" || VIEW.sort !== "updated" || VIEW.status.length > 0 ||
    VIEW.sources.length > 0 || VIEW.archived;
}

function profileLabel(name) {
  if (!name || name === "default") return "Default";
  if (name === "all") return "All profiles";
  return name.split(/[-_]+/).filter(Boolean)
    .map((w) => w.length <= 2 ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1)).join(" ");
}

function viewProfiles() {
  return VIEW.profile === "all" ? PROFILES.list : [VIEW.profile];
}

async function loadProfiles() {
  try {
    const res = await api("/phone-api/profiles");
    if (res.ok) {
      const data = await res.json();
      if (Array.isArray(data.profiles) && data.profiles.length) PROFILES.list = data.profiles;
    }
  } catch (e) {
    if (e.status === 401) throw e;
  }
  if (VIEW.profile !== "all" && !PROFILES.list.includes(VIEW.profile)) VIEW.profile = "default";
  if (VIEW.group === "profile" && VIEW.profile !== "all") VIEW.group = "date";
}

function sessionKey(session) {
  return (session._profile || "default") + ":" + session.id;
}

async function loadSessions(reset) {
  const profiles = viewProfiles();
  const seq = ++LIST.seq;
  const pages = reset ? {} : state.pages;
  const results = await Promise.all(profiles.map(async (profile) => {
    const page = pages[profile] || { offset: 0, more: true };
    if (!page.more) return { profile, rows: null };
    const res = await api(inProfile(profile, "/api/sessions?limit=50&offset=" + page.offset));
    if (!res.ok) {
      // One profile down still shows the others.
      if (profiles.length === 1) throw new Error(await errorMessage(res));
      return { profile, rows: null, failed: true };
    }
    const data = await res.json();
    return { profile, rows: Array.isArray(data.data) ? data.data : [], more: Boolean(data.has_more) };
  }));
  if (seq !== LIST.seq) return;
  const merged = new Map((reset ? [] : state.sessions).map((s) => [sessionKey(s), s]));
  const failed = [];
  for (const r of results) {
    if (r.failed) failed.push(profileLabel(r.profile));
    if (!r.rows) continue;
    const page = pages[r.profile] || { offset: 0 };
    page.offset += r.rows.length;
    page.more = r.more;
    pages[r.profile] = page;
    for (const row of r.rows) {
      row._profile = r.profile;
      merged.set(sessionKey(row), row);
    }
  }
  state.pages = pages;
  state.sessions = [...merged.values()];
  LIST.note = failed.length ? "Could not load " + failed.join(", ") + "." : "";
  await loadMeta(profiles).catch((e) => { if (e.status === 401) throw e; });
  if (seq !== LIST.seq) return;
  renderSessions();
  // The Source choices come from the loaded rows; refresh an open sheet.
  if (!$("drawer").hidden && DRAWER.mode === "sessions") renderSheet();
}

function listHasMore() {
  return viewProfiles().some((p) => state.pages[p] && state.pages[p].more);
}

// Project names (for "Group by project") and archived sessions, read on the Mac.
async function loadMeta(profiles) {
  const wantProjects = VIEW.group === "project";
  if (!wantProjects && !VIEW.archived) {
    state.archived = [];
    return;
  }
  const archived = [];
  await Promise.all(profiles.map(async (profile) => {
    const ids = wantProjects
      ? state.sessions.filter((s) => s._profile === profile && !(s.id in state.meta)).map((s) => s.id).slice(0, 200)
      : [];
    if (!ids.length && !VIEW.archived) return;
    const res = await api("/phone-api/meta?profile=" + encodeURIComponent(profile) +
      "&ids=" + encodeURIComponent(ids.join(",")) + (VIEW.archived ? "&archived=1" : ""));
    if (!res.ok) return;
    const data = await res.json();
    const meta = data.meta && typeof data.meta === "object" ? data.meta : {};
    for (const id of ids) state.meta[id] = meta[id] || { project: "" };
    for (const id of Object.keys(meta)) state.meta[id] = meta[id];
    for (const row of Array.isArray(data.archived) ? data.archived : []) {
      row._profile = profile;
      archived.push(row);
    }
  }));
  state.archived = archived;
}

function refreshMeta() {
  return loadMeta(viewProfiles()).then(renderSessions).catch((e) => {
    if (e.status === 401) return showKey(e.message);
    renderSessions();
  });
}

function listError(e) {
  if (e && e.status === 401) return showKey(e.message);
  LIST.note = (e && e.message) || "Could not load sessions";
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
  const run = liveRun(id);
  if (run) {
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
// When the view groups, sorts or filters by status, a change of state moves
// rows, so the list is rebuilt instead (scroll position kept).
const DOTS = { sig: "" };
function paintDots() {
  if (VIEW.group === "status" || VIEW.sort === "status" || VIEW.status.length) {
    const sig = state.sessions.map((s) => s.id + ":" + sessionDot(s.id)).join(",");
    if (sig === DOTS.sig) return;
    DOTS.sig = sig;
    const list = $("session-list");
    const top = list.scrollTop;
    renderSessions();
    list.scrollTop = top;
    return;
  }
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
  // In "All profiles", every row from a profile other than Default says which.
  const profile = session._profile || "default";
  if (VIEW.profile === "all" && profile !== "default" && VIEW.group !== "profile" || session.archived) {
    const tags = document.createElement("div");
    tags.className = "tags";
    if (VIEW.profile === "all" && profile !== "default" && VIEW.group !== "profile") {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = profileLabel(profile);
      tags.append(tag);
    }
    if (session.archived) {
      const tag = document.createElement("span");
      tag.className = "tag muted";
      tag.textContent = "Archived";
      tags.append(tag);
    }
    if (preview.textContent) tags.append(preview);
    button.append(tags);
  } else if (preview.textContent) {
    button.append(preview);
  }
  button.addEventListener("click", () => openSession(session.id, session.title || "New session", true, profile));
  return button;
}

function sessionStatus(session) {
  return sessionDot(session.id) || "idle";
}

function sessionCost(session) {
  const n = Number(session.actual_cost_usd != null ? session.actual_cost_usd : session.estimated_cost_usd);
  return isFinite(n) ? n : 0;
}

function sessionTokens(session) {
  return (Number(session.input_tokens) || 0) + (Number(session.output_tokens) || 0);
}

// Sort key, highest first; recency (the order Hermes sends) breaks ties.
function sortValue(session) {
  switch (VIEW.sort) {
    case "created": return stampMs(session.started_at);
    case "tokens": return sessionTokens(session);
    case "cost": return sessionCost(session);
    case "status": return -STATUS_RANK[sessionStatus(session)];
    default: return stampMs(session.last_active || session.started_at);
  }
}

function sortSessions(list) {
  const recent = (s) => stampMs(s.last_active || s.started_at);
  return list.slice().sort((a, b) => (sortValue(b) - sortValue(a)) || (recent(b) - recent(a)));
}

function shownSessions() {
  const archived = VIEW.archived ? state.archived : [];
  const seen = new Set(state.sessions.map(sessionKey));
  let list = state.sessions.concat(archived.filter((s) => !seen.has(sessionKey(s))));
  if (VIEW.status.length) list = list.filter((s) => VIEW.status.includes(sessionStatus(s)));
  if (VIEW.sources.length) list = list.filter((s) => VIEW.sources.includes(sourceGroup(s.source)));
  return list;
}

function sourceGroup(source) {
  return sourceLabel(source) || "Other";
}

// [{key, label, sessions}] in the order the groups are shown.
function groupSessions(list) {
  const sorted = sortSessions(list);
  if (VIEW.group === "date") {
    const groups = [];
    const pinned = sorted.filter((s) => s.pinned);
    if (pinned.length) groups.push({ key: "pinned", label: "Pinned", sessions: pinned });
    // Date headers follow recency even when the rows inside are sorted another way.
    const byDate = new Map();
    const order = list.filter((s) => !s.pinned).slice()
      .sort((a, b) => stampMs(b.last_active || b.started_at) - stampMs(a.last_active || a.started_at));
    for (const s of order) {
      const label = bucketLabel(s.last_active || s.started_at) || "Older";
      if (!byDate.has(label)) byDate.set(label, []);
    }
    for (const s of sorted) if (!s.pinned) byDate.get(bucketLabel(s.last_active || s.started_at) || "Older").push(s);
    for (const [label, sessions] of byDate) groups.push({ key: "date:" + label, label, sessions });
    return groups;
  }
  const keyOf = {
    project: (s) => (state.meta[s.id] && state.meta[s.id].project) || "",
    status: (s) => sessionStatus(s),
    source: (s) => sourceGroup(s.source),
    profile: (s) => s._profile || "default"
  }[VIEW.group];
  const labelOf = {
    project: (k) => k || "No project",
    status: (k) => (STATUSES.find(([id]) => id === k) || [k, k])[1],
    source: (k) => k,
    profile: (k) => profileLabel(k)
  }[VIEW.group];
  const map = new Map();
  for (const s of sorted) {
    const k = keyOf(s);
    if (!map.has(k)) map.set(k, []);
    map.get(k).push(s);
  }
  let keys = [...map.keys()];
  if (VIEW.group === "status") keys.sort((a, b) => STATUS_RANK[a] - STATUS_RANK[b]);
  else if (VIEW.group === "profile") keys.sort((a, b) => PROFILES.list.indexOf(a) - PROFILES.list.indexOf(b));
  else {
    // Busiest group first (by its newest session); the catch-all group last.
    const newest = (k) => Math.max(...map.get(k).map((s) => stampMs(s.last_active || s.started_at)));
    keys.sort((a, b) => (a === "" || a === "Other") - (b === "" || b === "Other") || newest(b) - newest(a));
  }
  return keys.map((k) => ({ key: VIEW.group + ":" + k, label: labelOf(k), sessions: map.get(k) }));
}

function groupHeader(group) {
  const head = document.createElement("button");
  head.type = "button";
  head.className = "bucket group";
  const closed = Boolean(VIEW.collapsed[group.key]);
  head.setAttribute("aria-expanded", closed ? "false" : "true");
  const chev = document.createElement("span");
  chev.className = "chev";
  chev.setAttribute("aria-hidden", "true");
  chev.textContent = "›";
  const label = document.createElement("span");
  label.className = "glabel";
  label.textContent = group.label;
  const count = document.createElement("span");
  count.className = "gcount";
  count.textContent = String(group.sessions.length);
  head.append(chev, label, count);
  head.addEventListener("click", () => {
    if (VIEW.collapsed[group.key]) delete VIEW.collapsed[group.key];
    else VIEW.collapsed[group.key] = true;
    saveView();
    renderSessions();
  });
  return head;
}

function paintListHead() {
  const chip = $("profile-chip");
  if (chip) {
    chip.textContent = profileLabel(VIEW.profile);
    chip.hidden = PROFILES.list.length < 2 && VIEW.profile === "default";
  }
  for (const btn of document.querySelectorAll("#list-screen button.nav-btn")) btn.classList.toggle("on", viewChanged());
}

function renderSessions() {
  const root = $("session-list");
  const scroll = root.scrollTop;
  root.textContent = "";
  paintListHead();
  if (LIST.note) {
    const note = document.createElement("div");
    note.className = "empty note-line";
    note.textContent = LIST.note;
    root.appendChild(note);
  }
  const list = shownSessions();
  if (!list.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = state.sessions.length || state.archived.length ? "Nothing matches these filters." : "No sessions yet.";
    root.appendChild(empty);
  }
  const groups = groupSessions(list);
  // The date view keeps today's look: no header over the first group unless something sits above it.
  const plainFirst = VIEW.group === "date" && groups.length && groups[0].key !== "pinned";
  groups.forEach((group, i) => {
    if (VIEW.group === "date" && i === 0 && plainFirst) {
      for (const session of group.sessions) root.appendChild(sessionButton(session));
      return;
    }
    root.appendChild(groupHeader(group));
    if (VIEW.collapsed[group.key]) return;
    for (const session of group.sessions) root.appendChild(sessionButton(session));
  });
  if (listHasMore()) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "more";
    more.textContent = "More";
    more.addEventListener("click", async () => {
      more.disabled = true;
      more.textContent = "Loading…";
      try { await loadSessions(false); }
      catch (e) { listError(e); }
    });
    root.appendChild(more);
  }
  root.scrollTop = scroll;
}

// --- View sheet -------------------------------------------------------------
function chipRow(options, isOn, onPick) {
  const row = document.createElement("div");
  row.className = "chips";
  for (const [id, label] of options) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip" + (isOn(id) ? " on" : "");
    chip.setAttribute("aria-pressed", isOn(id) ? "true" : "false");
    chip.textContent = label;
    chip.addEventListener("click", () => onPick(id));
    row.append(chip);
  }
  return row;
}

function sheetSection(title, body) {
  const box = document.createElement("section");
  box.className = "sheet-sec";
  const h = document.createElement("h3");
  h.textContent = title;
  box.append(h, body);
  return box;
}

function toggleIn(list, id) {
  const i = list.indexOf(id);
  if (i >= 0) list.splice(i, 1);
  else list.push(id);
}

function renderSheet() {
  const body = $("sheet-body");
  body.textContent = "";
  if (PROFILES.list.length > 1) {
    const options = [["all", "All"]].concat(PROFILES.list.map((p) => [p, profileLabel(p)]));
    body.append(sheetSection("Profile", chipRow(options, (id) => VIEW.profile === id, (id) => {
      if (VIEW.profile === id) return;
      VIEW.profile = id;
      if (id !== "all" && VIEW.group === "profile") VIEW.group = "date";
      saveView();
      renderSheet();
      state.sessions = [];
      state.archived = [];
      LIST.note = "";
      renderSessions();
      loadSessions(true).catch(listError);
    })));
  }
  const groups = GROUPS.filter(([id]) => id !== "profile" || VIEW.profile === "all");
  body.append(sheetSection("Group by", chipRow(groups, (id) => VIEW.group === id, (id) => {
    VIEW.group = id;
    saveView();
    renderSheet();
    if (id === "project") refreshMeta();
    else renderSessions();
  })));
  body.append(sheetSection("Sort by", chipRow(SORTS, (id) => VIEW.sort === id, (id) => {
    VIEW.sort = id;
    saveView();
    renderSheet();
    renderSessions();
  })));
  body.append(sheetSection("Status", chipRow(STATUSES, (id) => VIEW.status.includes(id), (id) => {
    toggleIn(VIEW.status, id);
    saveView();
    renderSheet();
    renderSessions();
  })));
  const sources = [...new Set(state.sessions.concat(state.archived).map((s) => sourceGroup(s.source)).concat(VIEW.sources))]
    .sort().map((s) => [s, s]);
  if (sources.length > 1 || VIEW.sources.length) {
    body.append(sheetSection("Source", chipRow(sources, (id) => VIEW.sources.includes(id), (id) => {
      toggleIn(VIEW.sources, id);
      saveView();
      renderSheet();
      renderSessions();
    })));
  }
  body.append(sheetSection("Show", chipRow([["archived", "Archived"]], () => VIEW.archived, () => {
    VIEW.archived = !VIEW.archived;
    saveView();
    renderSheet();
    refreshMeta();
  })));
  const reset = document.createElement("button");
  reset.type = "button";
  reset.className = "textbtn sheet-reset";
  reset.textContent = "Reset view";
  reset.disabled = !viewChanged() && !Object.keys(VIEW.collapsed).length;
  reset.addEventListener("click", () => {
    Object.assign(VIEW, VIEW_DEFAULT, { profile: VIEW.profile, status: [], sources: [], collapsed: {} });
    saveView();
    renderSheet();
    state.archived = [];
    renderSessions();
  });
  body.append(reset);
}

// --- Side drawer --------------------------------------------------------------
// Opened by the menu button or the word Hermes. It switches between Sessions
// and Cron jobs and holds that view's choices (the old View sheet), plus Lock.
const DRAWER = { mode: "sessions", touchX: null };

function toggleSheet(open) {
  const wrap = $("drawer");
  const showIt = open == null ? wrap.hidden : open;
  wrap.hidden = !showIt;
  for (const button of document.querySelectorAll("button.nav-btn")) {
    button.setAttribute("aria-expanded", showIt ? "true" : "false");
  }
  if (showIt) renderDrawer();
}

function renderDrawer() {
  for (const button of document.querySelectorAll(".drawer-mode")) {
    const on = button.dataset.mode === DRAWER.mode;
    button.classList.toggle("on", on);
    button.setAttribute("aria-current", on ? "page" : "false");
  }
  if (DRAWER.mode === "cron") renderCronSheet();
  else renderSheet();
}

function pickMode(mode) {
  toggleSheet(false);
  if (mode === "cron") {
    history.replaceState({ cron: true }, "", "#cron");
    showCron();
  } else {
    history.replaceState({}, "", location.pathname);
    showList();
  }
}

// --- Cron jobs ------------------------------------------------------------------
// Read-only: the Mac reads each profile's cron files (/phone-api/cron*).
const CRON = { jobs: [], profile: "all", seq: 0, note: "", loaded: false, job: null, runs: null, open: {} };
const CRON_PROFILE_NAME = "hermes.phone.cronProfile";
try { CRON.profile = localStorage.getItem(CRON_PROFILE_NAME) || "all"; } catch (e) {}

function stampOf(value) {
  if (value == null || value === "") return 0;
  if (typeof value === "number") return value > 1e12 ? value : value * 1000;
  const n = Date.parse(value);
  return Number.isFinite(n) ? n : 0;
}

function relTime(ms) {
  if (!ms) return "";
  const delta = ms - Date.now();
  const abs = Math.abs(delta);
  const min = Math.round(abs / 60000);
  let text;
  if (min < 1) text = "now";
  else if (min < 60) text = min + "m";
  else if (min < 60 * 36) text = Math.round(min / 60) + "h";
  else text = Math.round(min / 1440) + "d";
  if (text === "now") return text;
  return delta > 0 ? "in " + text : text + " ago";
}

function clockTime(ms) {
  if (!ms) return "";
  const d = new Date(ms);
  const sameDay = dayStart(ms) === dayStart(Date.now());
  const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return sameDay ? time : d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }) + ", " + time;
}

// "15 3 * * *" -> "Daily 3:15 AM". Only the shapes people actually write;
// anything else is shown as written.
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
function hm(h, m) {
  const d = new Date(2000, 0, 1, h, m);
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
function plainSchedule(text) {
  const parts = String(text || "").trim().split(/\s+/);
  if (parts.length !== 5) return text || "";
  const [mi, ho, dom, mon, dow] = parts;
  const num = /^\d+$/;
  if (mon !== "*" || !num.test(mi)) return text;
  if (ho === "*" && dom === "*" && dow === "*") return "Hourly at :" + mi.padStart(2, "0");
  if (!/^\d+(,\d+)*$/.test(ho)) return text;
  const times = ho.split(",").map((h) => hm(Number(h), Number(mi))).join(", ");
  if (dom === "*" && dow === "*") return "Daily " + times;
  if (dom === "*" && /^\d(,\d)*$/.test(dow)) return dow.split(",").map((d) => DOW[Number(d)] || d).join(", ") + " " + times;
  if (dom === "*" && dow === "1-5") return "Weekdays " + times;
  return text;
}

function jobHealth(job) {
  const status = String(job.last_status || "").toLowerCase();
  if (job.last_error || /error|fail/.test(status)) return "bad";
  if (job.state === "paused" || job.enabled === false) return "paused";
  return "";
}

function jobStateLabel(job) {
  if (job.state === "completed") return "Finished";
  if (job.state === "paused" || job.enabled === false) return "Paused";
  return "";
}

function statusWord(status) {
  return String(status || "").replace(/_/g, " ");
}

async function loadCron() {
  const seq = ++CRON.seq;
  try {
    const res = await api("/phone-api/cron?profile=" + encodeURIComponent(CRON.profile));
    if (!res.ok) throw new Error(await errorMessage(res));
    const data = await res.json();
    if (seq !== CRON.seq) return;
    CRON.jobs = Array.isArray(data.jobs) ? data.jobs : [];
    CRON.note = "";
  } catch (e) {
    if (e.status === 401) return showKey(e.message);
    if (seq !== CRON.seq) return;
    CRON.note = e.message || "Could not load cron jobs";
  }
  CRON.loaded = true;
  renderCron();
}

function sortedJobs() {
  // Scheduled jobs by next run; paused and finished ones after.
  const rank = (job) => (jobStateLabel(job) ? 1 : 0);
  return CRON.jobs.slice().sort((a, b) =>
    rank(a) - rank(b) || (stampOf(a.next_run_at) || Infinity) - (stampOf(b.next_run_at) || Infinity) ||
    String(a.name).localeCompare(String(b.name)));
}

function cronRow(job) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "session job";
  const row = document.createElement("div");
  row.className = "row";
  const dot = document.createElement("span");
  const health = jobHealth(job);
  dot.className = "sdot" + (health ? " dot-" + health : "");
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = job.name || job.id;
  const when = document.createElement("span");
  when.className = "when";
  const label = jobStateLabel(job);
  when.textContent = label || relTime(stampOf(job.next_run_at));
  row.append(dot, name, when);
  const tags = document.createElement("div");
  tags.className = "tags";
  const preview = document.createElement("span");
  preview.className = "preview";
  const last = stampOf(job.last_run_at);
  preview.textContent = [plainSchedule(job.schedule), last ? "last " + relTime(last) + (job.last_status ? " · " + statusWord(job.last_status) : "") : "never run"]
    .filter(Boolean).join(" · ");
  tags.append(preview);
  if (job.profile && job.profile !== "default") {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = profileLabel(job.profile);
    tags.append(tag);
  }
  button.append(row, tags);
  button.addEventListener("click", () => openJob(job, true));
  return button;
}

function renderCron() {
  const root = $("cron-list");
  const scroll = root.scrollTop;
  root.textContent = "";
  $("cron-screen").querySelector(".cron-title").textContent = CRON.profile === "all" ? "Cron" : "Cron · " + profileLabel(CRON.profile);
  if (CRON.note) {
    const note = document.createElement("div");
    note.className = "empty note-line";
    note.textContent = CRON.note;
    root.append(note);
  }
  const jobs = sortedJobs();
  if (!jobs.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = CRON.loaded ? "No cron jobs." : "Loading…";
    root.append(empty);
  }
  let split = false;
  jobs.forEach((job) => {
    if (!split && jobStateLabel(job)) {
      split = true;
      const head = document.createElement("div");
      head.className = "bucket";
      head.textContent = "Paused and finished";
      root.append(head);
    }
    root.append(cronRow(job));
  });
  root.scrollTop = scroll;
}

function renderCronSheet() {
  const body = $("sheet-body");
  body.textContent = "";
  if (PROFILES.list.length > 1) {
    const options = [["all", "All"]].concat(PROFILES.list.map((p) => [p, profileLabel(p)]));
    body.append(sheetSection("Profile", chipRow(options, (id) => CRON.profile === id, (id) => {
      if (CRON.profile === id) return;
      CRON.profile = id;
      try { localStorage.setItem(CRON_PROFILE_NAME, id); } catch (e) {}
      CRON.jobs = [];
      CRON.loaded = false;
      renderCronSheet();
      renderCron();
      loadCron();
    })));
  }
  const note = document.createElement("p");
  note.className = "sheet-note";
  note.textContent = "Read-only. Jobs are changed from the desktop app or a chat.";
  body.append(note);
}

function showCron() {
  DRAWER.mode = "cron";
  CRON.job = null;
  show("cron-screen");
  renderCron();
  loadCron();
}

function cronHashFor(job) {
  return "#cron=" + encodeURIComponent(job.id) + (job.profile && job.profile !== "default" ? "&p=" + encodeURIComponent(job.profile) : "");
}

function cronHashTarget() {
  const hash = location.hash || "";
  if (hash === "#cron") return { list: true };
  if (!hash.startsWith("#cron=")) return null;
  const params = new URLSearchParams(hash.slice(1));
  const id = params.get("cron") || "";
  return id ? { id, profile: params.get("p") || "default" } : { list: true };
}

async function openCronTarget(target) {
  if (!target || target.list) return showCron();
  DRAWER.mode = "cron";
  if (!CRON.jobs.length) await loadCron();
  const job = CRON.jobs.find((j) => j.id === target.id && (j.profile || "default") === target.profile);
  if (job) openJob(job, false);
  else showCron();
}

function kvRow(dl, label, value, cls) {
  if (value == null || value === "") return;
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value;
  if (cls) dd.className = cls;
  dl.append(dt, dd);
}

function jobSummary(job) {
  const box = document.createElement("section");
  box.className = "job-sum";
  const dl = document.createElement("dl");
  dl.className = "kv";
  kvRow(dl, "Schedule", plainSchedule(job.schedule) + (plainSchedule(job.schedule) !== job.schedule ? "  (" + job.schedule + ")" : ""));
  kvRow(dl, "State", jobStateLabel(job) || "Scheduled");
  const next = stampOf(job.next_run_at);
  if (!jobStateLabel(job)) kvRow(dl, "Next run", next ? clockTime(next) + " (" + relTime(next) + ")" : "");
  const last = stampOf(job.last_run_at);
  kvRow(dl, "Last run", last ? clockTime(last) + (job.last_status ? " · " + statusWord(job.last_status) : "") : "Never");
  kvRow(dl, "Runs", job.runs_completed != null ? String(job.runs_completed) : "");
  kvRow(dl, "Kind", job.no_agent ? "Script only" : "Agent" + (Array.isArray(job.skills) && job.skills.length ? " · " + job.skills.join(", ") : ""));
  kvRow(dl, "Script", job.script || "");
  kvRow(dl, "Delivers to", job.deliver || "");
  if (job.failure_streak) kvRow(dl, "Failures in a row", String(job.failure_streak), "bad");
  kvRow(dl, "Last error", job.last_error || "", "bad");
  kvRow(dl, "Delivery error", job.last_delivery_error || "", "bad");
  kvRow(dl, "Paused because", job.paused_reason || "");
  box.append(dl);
  if (job.prompt) {
    const details = document.createElement("details");
    details.className = "job-prompt";
    const summary = document.createElement("summary");
    summary.textContent = "Prompt";
    const pre = document.createElement("pre");
    pre.textContent = job.prompt;
    details.append(summary, pre);
    box.append(details);
  }
  return box;
}

async function openJob(job, push) {
  CRON.job = job;
  CRON.runs = null;
  CRON.open = {};
  DRAWER.mode = "cron";
  toggleSheet(false);
  if (push) history.pushState({ job: job.id, profile: job.profile }, "", cronHashFor(job));
  $("job-title").textContent = job.name || job.id;
  const tag = $("job-profile");
  const other = job.profile && job.profile !== "default";
  tag.hidden = !other;
  tag.textContent = other ? profileLabel(job.profile) : "";
  show("job-screen");
  renderJob();
  try {
    const res = await api("/phone-api/cron/runs?profile=" + encodeURIComponent(job.profile || "default") +
      "&job=" + encodeURIComponent(job.id));
    if (!res.ok) throw new Error(await errorMessage(res));
    const data = await res.json();
    if (CRON.job !== job) return;
    CRON.runs = Array.isArray(data.runs) ? data.runs : [];
  } catch (e) {
    if (e.status === 401) return showKey(e.message);
    if (CRON.job !== job) return;
    CRON.runs = { error: e.message || "Could not load runs" };
  }
  renderJob();
}

function runStatus(run) {
  if (run.status === "running" || run.status === "claimed") return ["Running", "working"];
  if (run.status === "failed" || run.error) return ["Failed", "bad"];
  if (run.status === "completed") return ["Done", ""];
  if (run.status === "unknown") return ["Unknown", "paused"];
  return ["", ""];
}

function runRow(job, run, index) {
  const wrap = document.createElement("div");
  wrap.className = "run";
  const head = document.createElement("button");
  head.type = "button";
  head.className = "run-head";
  const [label, tone] = runStatus(run);
  const dot = document.createElement("span");
  dot.className = "sdot" + (tone ? " dot-" + tone : "");
  const when = document.createElement("span");
  when.className = "run-when";
  when.textContent = clockTime(stampOf(run.at || run.started || run.finished));
  const meta = document.createElement("span");
  meta.className = "run-meta";
  const start = stampOf(run.started);
  const end = stampOf(run.finished);
  const parts = [label];
  if (start && end && end >= start) parts.push(fmtDur((end - start) / 1000));
  if (run.delivery) parts.push(statusWord(run.delivery));
  meta.textContent = parts.filter(Boolean).join(" · ");
  const chev = document.createElement("span");
  chev.className = "chev";
  chev.textContent = "›";
  head.append(dot, when, meta, chev);
  wrap.append(head);
  const key = String(index);
  const open = CRON.open[key];
  head.setAttribute("aria-expanded", open ? "true" : "false");
  head.addEventListener("click", () => {
    if (CRON.open[key]) delete CRON.open[key];
    else CRON.open[key] = { full: false };
    renderJob();
    if (CRON.open[key] && run.output && CRON.open[key].text == null) loadRunOutput(job, run, key);
  });
  if (open) wrap.append(runBody(job, run, open));
  return wrap;
}

// Agent runs log the prompt (with whole skills) before "## Response"; script
// runs log their output after a "---" rule. Show the useful part first.
function outputMain(text) {
  const at = text.lastIndexOf("\n## Response");
  if (at >= 0) return text.slice(at + "\n## Response".length).trim();
  const rule = text.indexOf("\n---\n");
  if (rule >= 0) return text.slice(rule + 5).trim();
  return text.trim();
}

function runBody(job, run, open) {
  const body = document.createElement("div");
  body.className = "run-body";
  if (run.error) {
    const err = document.createElement("div");
    err.className = "run-err";
    err.textContent = run.error;
    body.append(err);
  }
  if (run.output) {
    if (open.error) {
      const err = document.createElement("div");
      err.className = "run-err";
      err.textContent = open.error;
      body.append(err);
    } else if (open.text == null) {
      const wait = document.createElement("div");
      wait.className = "meta";
      wait.textContent = "Loading output…";
      body.append(wait);
    } else if (open.full) {
      const pre = document.createElement("pre");
      pre.className = "run-log";
      pre.textContent = open.text + (open.truncated ? "\n\n[cut at 400 KB]" : "");
      body.append(pre);
    } else {
      const main = outputMain(open.text);
      const md = document.createElement("div");
      md.className = "assistant";
      md.innerHTML = renderMarkdown(main || "(No output)");
      body.append(md);
    }
  } else if (!run.error) {
    const none = document.createElement("div");
    none.className = "meta";
    none.textContent = "No saved output for this run.";
    body.append(none);
  }
  const actions = document.createElement("div");
  actions.className = "run-actions";
  if (run.output && open.text != null) {
    const full = document.createElement("button");
    full.type = "button";
    full.className = "textbtn";
    full.textContent = open.full ? "Show result" : "Full log";
    full.addEventListener("click", () => { open.full = !open.full; renderJob(); });
    actions.append(full);
  }
  if (run.session) {
    const go = document.createElement("button");
    go.type = "button";
    go.className = "textbtn";
    go.textContent = "Open transcript";
    go.addEventListener("click", () => openSession(run.session, (job.name || "Cron") + " · " + clockTime(stampOf(run.at)), true, job.profile || "default"));
    actions.append(go);
  }
  if (actions.childNodes.length) body.append(actions);
  return body;
}

async function loadRunOutput(job, run, key) {
  const open = CRON.open[key];
  try {
    const res = await api("/phone-api/cron/output?profile=" + encodeURIComponent(job.profile || "default") +
      "&job=" + encodeURIComponent(job.id) + "&file=" + encodeURIComponent(run.output));
    if (!res.ok) throw new Error(await errorMessage(res));
    const data = await res.json();
    open.text = String(data.text || "");
    open.truncated = Boolean(data.truncated);
  } catch (e) {
    if (e.status === 401) return showKey(e.message);
    open.error = e.message || "Could not load the output";
  }
  if (CRON.job === job && CRON.open[key] === open) renderJob();
}

function renderJob() {
  const job = CRON.job;
  if (!job) return;
  const root = $("job-body");
  const scroll = root.scrollTop;
  root.textContent = "";
  root.append(jobSummary(job));
  const head = document.createElement("div");
  head.className = "bucket";
  head.textContent = "Runs";
  root.append(head);
  if (CRON.runs == null || CRON.runs.error) {
    const note = document.createElement("div");
    note.className = CRON.runs ? "empty note-line" : "meta run-note";
    note.textContent = CRON.runs ? CRON.runs.error : "Loading runs…";
    root.append(note);
  } else if (!CRON.runs.length) {
    const none = document.createElement("div");
    none.className = "meta run-note";
    none.textContent = "No runs yet.";
    root.append(none);
  } else {
    CRON.runs.forEach((run, i) => root.append(runRow(job, run, i)));
  }
  root.scrollTop = scroll;
}

// "#s=<id>" for the default profile, "#s=<id>&p=<profile>" for another.
function hashFor(id, profile) {
  return "#s=" + encodeURIComponent(id) + (profile && profile !== "default" ? "&p=" + encodeURIComponent(profile) : "");
}

function hashTarget() {
  const hash = location.hash || "";
  if (!hash.startsWith("#s=")) return null;
  const params = new URLSearchParams(hash.slice(1));
  const id = params.get("s") || "";
  return id ? { id, profile: params.get("p") || "default" } : null;
}

// API paths for the open session's profile.
function here(path) {
  return inProfile(state.profile, path);
}

async function openSession(id, title, push, profile) {
  dropDoneRuns(id);
  state.sessionId = id;
  state.profile = profile || "default";
  state.title = title || "New session";
  state.messages = [];
  state.hasEarlier = false;
  OTHER.busy = null;
  // Pictures waiting in the box belong to the session they were picked in.
  clearTray(true);
  paintSendState();
  markRead(id);
  historyNodes.clear();
  if (state.tail && state.tail.sessionId !== id) state.tail = null;
  notice("");
  toggleMenu(false);
  toggleSheet(false);
  $("chat-title").textContent = state.title;
  paintChatProfile();
  if (push) history.pushState({ id, profile: state.profile }, "", hashFor(id, state.profile));
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
  const draft = takeDraft(id);
  if (draft && !$("draft").value) {
    $("draft").value = draft;
    $("draft").dispatchEvent(new Event("input"));
  }
  // No auto-focus: opening a session should not raise the keyboard (and its
  // viewport jump) before you ask for it. Tap the box to type.
}

function paintChatProfile() {
  const tag = $("chat-profile");
  const other = state.profile && state.profile !== "default";
  tag.hidden = !other;
  tag.textContent = other ? profileLabel(state.profile) : "";
}

function messagesPath(id, offset, size) {
  return here("/api/sessions/" + encodeURIComponent(id) + "/messages?order=latest&limit=" + (size + 1) +
    (offset ? "&offset=" + offset : ""));
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
    history.replaceState({ id: state.sessionId, profile: state.profile }, "", hashFor(state.sessionId, state.profile));
    for (const run of state.runs) if (run.openedId === id || run.sessionId === id) run.sessionId = data.session_id;
    saveRuns();
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
    const info = await api(here("/api/sessions/" + encodeURIComponent(state.sessionId)));
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
  return Boolean(liveRun(state.sessionId));
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
  // Build the wanted list, then move only what changed. Clearing the
  // transcript and re-appending detached every node, and a detached box loses
  // its scroll: an opened Thought or tool result jumped back to its top on
  // every 4-second refresh while a reply ran.
  const wanted = [];
  if (state.hasEarlier) wanted.push(earlierButton());
  let shown = 0;
  const results = resultsByCall(state.messages);
  const called = callsShown(state.messages);
  let turnKey = "start";
  let edits = [];
  const flush = () => {
    if (edits.length) wanted.push(changedFilesCard(turnKey, edits));
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
    wanted.push(node);
    shown += 1;
  }
  flush();
  if (!shown && !currentRun()) {
    const empty = document.createElement("div");
    empty.className = "intro";
    const mark = document.createElement("div");
    mark.className = "wordmark";
    mark.textContent = "Hermes Agent";
    const hint = document.createElement("p");
    hint.textContent = "Send the task, failing path, or half-formed plan. I'll help turn it into action.";
    empty.append(mark, hint);
    wanted.push(empty);
  }
  // The live reply's container stays last and stays attached.
  let live = $("live");
  if (!live) {
    live = document.createElement("div");
    live.id = "live";
  }
  wanted.push(live);
  // Drop stale nodes first, so a redrawn message is a single insert and the
  // nodes after it are not moved (a move detaches them too).
  const keep = new Set(wanted);
  for (const child of Array.from(root.children)) if (!keep.has(child)) child.remove();
  wanted.forEach((node, i) => {
    if (root.children[i] !== node) root.insertBefore(node, root.children[i] || null);
  });
  while (root.children.length > wanted.length) root.lastElementChild.remove();
  renderLive(true);
  if (opts && opts.fromBottom != null) root.scrollTop = root.scrollHeight - opts.fromBottom;
  else root.scrollTop = stick ? root.scrollHeight : top;
  paintDraftHint();
}

function paintDraftHint() {
  const live = liveRun(state.sessionId);
  const hasUser = state.messages.some(realUser);
  $("draft").placeholder = live ? "Add guidance while it works"
    : OTHER.busy ? "Busy in another window"
    : hasUser ? "Send a follow-up" : "Give Hermes a task";
}

// sources: pictures to show in the bubble, each {url} (a local preview) or
// {path} (a file on the Mac). Without them, picture lines in the saved text
// are turned back into pictures.
function userBubble(text, steer, sources) {
  const wrap = document.createElement("div");
  wrap.className = "msg user" + (steer ? " steer" : "");
  if (steer) {
    const label = document.createElement("div");
    label.className = "steer-label";
    label.textContent = "Guidance";
    wrap.append(label);
  }
  let shown = text;
  let attached = sources || [];
  if (!steer && !sources) {
    const parsed = picText(text);
    if (parsed.paths.length || parsed.files.length) {
      shown = parsed.text;
      attached = parsed.paths.map((path) => ({ kind: "image", path }))
        .concat(parsed.files.map((path) => ({ kind: "file", name: baseName(path) })));
    }
  }
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (shown) {
    const words = document.createElement("div");
    words.className = "bubble-text";
    words.textContent = shown;
    bubble.append(words);
  }
  const pics = attached.filter((s) => s.kind !== "file");
  const files = attached.filter((s) => s.kind === "file");
  if (pics.length) {
    const row = document.createElement("div");
    row.className = "pics";
    for (const source of pics) row.append(picFigure(source));
    bubble.append(row);
  }
  if (files.length) {
    const list = document.createElement("div");
    list.className = "files";
    for (const source of files) list.append(fileChip(source));
    bubble.append(list);
  }
  wrap.append(bubble);
  return wrap;
}

function baseName(path) {
  return String(path || "").split("/").filter(Boolean).pop() || "file";
}

function sizeText(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

function fileChip(source) {
  const chip = document.createElement("div");
  chip.className = "file-chip";
  const icon = document.createElement("span");
  icon.className = "file-icon";
  icon.setAttribute("aria-hidden", "true");
  const name = document.createElement("span");
  name.className = "file-name";
  name.textContent = source.name || "file";
  chip.append(icon, name);
  if (source.size != null) {
    const size = document.createElement("span");
    size.className = "file-size";
    size.textContent = sizeText(source.size);
    chip.append(size);
  }
  return chip;
}

// --- Pictures and files -------------------------------------------------------
// The + opens iOS's picker (Photo Library, Take Photo, Choose File). Saved where the desktop saves its own
// (<profile>/images, <profile>/attachments) and listed in the message as the
// desktop's @image:/@file: lines, so either app shows them from history.
// A picture is shrunk on the phone (longest side PIC_SIDE, JPEG, which also
// drops its location data) and also sent as an image part. Hermes stores the
// text with "[screenshot]" in place of each image part; the @image: line is
// what brings the picture back. A file is sent unchanged (up to FILE_LIMIT).
// Phone messages from before this used "[Image attached at: <path>]"; those
// still show.
// Receiving: a MEDIA:/path tag or a ![](/path) image in a reply is shown as a
// picture, fetched through /phone-api/media (images only, never logged).

const PIC_SIDE = 2560;
const PIC_LIMIT = 4;
const PIC_MESSAGE_MAX = 9 * 1024 * 1024;
const PIC_DEFAULT = "What do you see in this picture?";
const FILE_LIMIT = 25 * 1024 * 1024;
const FILE_DEFAULT = "Please look at the attached file.";

function attachWord(items) {
  const pics = items.filter((i) => i.kind !== "file").length;
  const files = items.length - pics;
  if (!files) return pics > 1 ? "the pictures" : "the picture";
  if (!pics) return files > 1 ? "the files" : "the file";
  return "the attachments";
}
// This page's earlier format (kept so old phone messages still show), and the
// plain hint lines it adds for the model next to the desktop-style directives.
const PIC_LINE = /^\[Image attached at: (.+)\]$/;
const FILE_LINE = /^\[File attached at: (.+)\]$/;
const REF_LINE = /^@(image|file):(?:`([^`]+)`|"([^"]+)"|'([^']+)'|(\S+))$/;
const PIC_EXT = /\.(png|jpe?g|gif|webp|heic|heif)$/i;
const PICS = { items: [], pickerOpen: false };
const MEDIA = new Map();
// Read by Hermes as part of its instructions for every turn sent from here.
const PHONE_INSTRUCTIONS =
  "The user is on the Hermes phone page, which shows pictures. To show an image file from this Mac, " +
  "put MEDIA:/absolute/path/to/file.png on its own line (png, jpg, gif, webp or heic). This replaces the " +
  "API-server note that MEDIA tags show as plain text on the runs endpoint. Pictures and files the user " +
  "sends are saved on this Mac and listed in the message, one per line, as @file:<path> (before the text) " +
  "or @image:<path> (after it); a path with spaces is wrapped in backticks. Pictures also arrive as images. A file's " +
  "contents are NOT included: open each @file: path with your file tools (read_file, or the pdf skill " +
  "for PDFs) before answering about it.";

// Same quoting as Hermes's format_reference_value, so @image:/@file: lines read back whole.
function refValue(path) {
  if (!/[\s()[\]{}<>"'`]/.test(path)) return path;
  for (const q of ["`", "\"", "'"]) if (path.indexOf(q) < 0) return q + path + q;
  return path;
}

// Attachment lines in a saved user message: the desktop's @image:/@file:
// directives and this page's hint lines. Each path counts once.
function picText(text) {
  const paths = [];
  const files = [];
  const keep = [];
  const add = (list, path) => { if (path && !paths.includes(path) && !files.includes(path)) list.push(path); };
  for (const line of String(text || "").split("\n")) {
    const clean = line.trim();
    const pic = clean.match(PIC_LINE);
    const file = pic ? null : clean.match(FILE_LINE);
    const ref = pic || file ? null : clean.match(REF_LINE);
    if (pic) add(paths, pic[1].trim());
    else if (file) add(files, file[1].trim());
    else if (ref) add(ref[1] === "image" ? paths : files, ref[2] || ref[3] || ref[4] || ref[5]);
    else keep.push(line);
  }
  if (!paths.length && !files.length) return { text: String(text || ""), paths, files };
  return { text: keep.filter((line) => line.trim() !== "[screenshot]").join("\n").trim(), paths, files };
}

function mediaUrl(path) {
  let found = MEDIA.get(path);
  if (!found) {
    found = api("/phone-api/media?path=" + encodeURIComponent(path)).then(async (res) => {
      if (!res.ok) throw new Error("missing");
      return URL.createObjectURL(await res.blob());
    });
    found.catch(() => MEDIA.delete(path));
    MEDIA.set(path, found);
  }
  return found;
}

function picFigure(source) {
  const box = document.createElement("button");
  box.type = "button";
  box.className = "pic";
  const img = document.createElement("img");
  img.alt = "Picture";
  img.decoding = "async";
  box.append(img);
  if (source.url) {
    img.src = source.url;
  } else {
    box.classList.add("loading");
    mediaUrl(source.path).then((url) => {
      img.src = url;
      box.classList.remove("loading");
    }).catch(() => {
      box.classList.remove("loading");
      box.classList.add("missing");
      box.textContent = "Picture not available";
    });
  }
  box.addEventListener("click", () => { if (img.src) openViewer(img.src); });
  return box;
}

// Full screen. Tap to close; press and hold the picture to save or share it.
// The viewer zooms the picture itself. Letting iOS zoom the whole page broke
// the layout: the page reads a pinch as the keyboard (both shrink the visual
// viewport) and moves the app around. Pinch or double-tap to zoom, drag to
// pan, tap (unzoomed) or × to close.
function openViewer(src) {
  const viewer = document.createElement("div");
  viewer.className = "viewer";
  const img = document.createElement("img");
  img.src = src;
  img.alt = "Picture";
  img.draggable = false;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "viewer-close";
  close.setAttribute("aria-label", "Close");
  close.textContent = "×";
  viewer.append(img, close);
  document.body.append(viewer);
  VIEWER.open = true;

  const view = { scale: 1, x: 0, y: 0 };
  const pointers = new Map();
  let pinch = null;
  let moved = false;
  let lastTap = 0;
  // Zoom by resizing the picture, not by a CSS scale: Safari draws a scaled
  // layer from its 1x bitmap, so a zoomed screenshot came out blurry. A real
  // size makes it redraw from the full-resolution file.
  let base = null;
  const paint = () => {
    if (!base) base = { w: img.offsetWidth, h: img.offsetHeight };
    if (view.scale === 1) {
      img.style.width = img.style.height = img.style.maxWidth = img.style.maxHeight = "";
    } else {
      img.style.maxWidth = img.style.maxHeight = "none";
      img.style.width = base.w * view.scale + "px";
      img.style.height = base.h * view.scale + "px";
    }
    img.style.transform = "translate(" + view.x + "px, " + view.y + "px)";
  };
  // Keep the picture from being dragged entirely off screen.
  const clamp = () => {
    if (!base) base = { w: img.offsetWidth, h: img.offsetHeight };
    const w = base.w * view.scale;
    const h = base.h * view.scale;
    const maxX = Math.max(0, (w - viewer.clientWidth) / 2);
    const maxY = Math.max(0, (h - viewer.clientHeight) / 2);
    view.x = Math.min(maxX, Math.max(-maxX, view.x));
    view.y = Math.min(maxY, Math.max(-maxY, view.y));
  };
  const zoomAt = (scale, cx, cy) => {
    const next = Math.min(6, Math.max(1, scale));
    const rect = viewer.getBoundingClientRect();
    const px = cx - rect.left - rect.width / 2;
    const py = cy - rect.top - rect.height / 2;
    // Keep the point under the fingers where it is.
    view.x = px - (px - view.x) * (next / view.scale);
    view.y = py - (py - view.y) * (next / view.scale);
    view.scale = next;
    if (next === 1) { view.x = 0; view.y = 0; }
    clamp();
    paint();
  };
  const shut = () => {
    viewer.remove();
    VIEWER.open = false;
    syncViewport();
  };

  viewer.addEventListener("pointerdown", (event) => {
    if (event.target === close) return;
    viewer.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    moved = false;
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { dist: Math.hypot(a.x - b.x, a.y - b.y), scale: view.scale };
    }
  });
  viewer.addEventListener("pointermove", (event) => {
    const prev = pointers.get(event.pointerId);
    if (!prev) return;
    const now = { x: event.clientX, y: event.clientY };
    pointers.set(event.pointerId, now);
    if (pointers.size === 2 && pinch) {
      const [a, b] = [...pointers.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      zoomAt(pinch.scale * dist / (pinch.dist || 1), (a.x + b.x) / 2, (a.y + b.y) / 2);
      moved = true;
    } else if (pointers.size === 1 && view.scale > 1) {
      view.x += now.x - prev.x;
      view.y += now.y - prev.y;
      clamp();
      paint();
      if (Math.abs(now.x - prev.x) + Math.abs(now.y - prev.y) > 2) moved = true;
    } else if (Math.abs(now.x - prev.x) + Math.abs(now.y - prev.y) > 6) {
      moved = true;
    }
  });
  const lift = (event) => {
    if (!pointers.has(event.pointerId)) return;
    pointers.delete(event.pointerId);
    if (pointers.size < 2) pinch = null;
    if (pointers.size || moved) return;
    const at = Date.now();
    if (at - lastTap < 300) {
      // Double tap: zoom in to 2.5x there, or back out.
      lastTap = 0;
      clearTimeout(VIEWER.tapTimer);
      zoomAt(view.scale > 1 ? 1 : 2.5, event.clientX, event.clientY);
      return;
    }
    lastTap = at;
    // A single tap closes, but only unzoomed, and only once it is not a double tap.
    clearTimeout(VIEWER.tapTimer);
    VIEWER.tapTimer = setTimeout(() => { if (view.scale === 1) shut(); }, 300);
  };
  viewer.addEventListener("pointerup", lift);
  viewer.addEventListener("pointercancel", (event) => { pointers.delete(event.pointerId); pinch = null; });
  // iOS's own pinch gesture would zoom the page behind the picture.
  for (const name of ["gesturestart", "gesturechange", "gestureend"]) {
    viewer.addEventListener(name, (event) => event.preventDefault());
  }
  close.addEventListener("click", (event) => { event.stopPropagation(); shut(); });
}

// Reply text with its picture tags turned into pictures. Tags inside code stay text.
const PIC_MARK = "HermesPhonePic";
const MEDIA_TAG = /MEDIA:\s*(?:`([^`\n]+)`|"([^"\n]+)"|((?:~\/|\/)[^\s`"'<>]+))|!\[[^\]\n]*\]\(((?:~\/|\/)[^)\s]+)\)/g;

function setReplyHtml(el, text) {
  const found = [];
  const marked = String(text ?? "").replace(MEDIA_TAG, (raw, a, b, c, d) => {
    const path = (a || b || c || d || "").trim().replace(/[.,;:)]+$/, "");
    if (!PIC_EXT.test(path)) return raw;
    found.push({ raw, path });
    return PIC_MARK + (found.length - 1) + "X";
  });
  el.innerHTML = renderMarkdown(marked);
  if (!found.length) return;
  const mark = new RegExp(PIC_MARK + "(\\d+)X", "g");
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const value = node.nodeValue;
    if (value.indexOf(PIC_MARK) < 0) continue;
    const inCode = node.parentElement && node.parentElement.closest("code, pre");
    const frag = document.createDocumentFragment();
    let last = 0;
    let m;
    mark.lastIndex = 0;
    while ((m = mark.exec(value))) {
      frag.append(value.slice(last, m.index));
      const item = found[Number(m[1])];
      if (!item) frag.append(m[0]);
      else if (inCode) frag.append(item.raw);
      else frag.append(picFigure({ path: item.path }));
      last = m.index + m[0].length;
    }
    frag.append(value.slice(last));
    node.replaceWith(frag);
  }
}

// Shrink on the phone: a camera photo is several MB, and Hermes takes at most 10 MB per message.
// 2560 px keeps a phone screenshot sharp when zoomed (the model gets its own smaller copy anyway).
async function shrinkPicture(file) {
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      el.onerror = () => reject(new Error("That picture could not be opened."));
      el.src = url;
    });
    const scale = Math.min(1, PIC_SIDE / Math.max(img.naturalWidth, img.naturalHeight, 1));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(img.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(img.naturalHeight * scale));
    canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
    if (!blob) throw new Error("That picture could not be prepared.");
    return blob;
  } finally {
    URL.revokeObjectURL(url);
  }
}

function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("Could not read the picture."));
    reader.readAsDataURL(blob);
  });
}

// kind "image" -> <profile>/images; kind "file" -> <profile>/attachments under its own name.
async function uploadPicture(profile, blob, kind, name) {
  let url = "/phone-api/upload?profile=" + encodeURIComponent(profile || "default");
  if (kind === "file") url += "&kind=file&name=" + encodeURIComponent(name || "attachment");
  const res = await api(url, {
    method: "POST",
    headers: { "Content-Type": (kind === "file" ? "application/octet-stream" : blob.type) || "image/jpeg" },
    body: blob
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  if (!data.path) throw new Error("The " + (kind === "file" ? "file" : "picture") + " was not saved.");
  return data.path;
}

// A tray item is a picture ({kind: "image", blob, url}) or a file
// ({kind: "file", blob, name, size}). Either is ready once it has a blob.
function itemReady(item) {
  return Boolean(item.blob && (item.kind === "file" || item.url));
}

function paintTray() {
  const tray = $("tray");
  tray.textContent = "";
  tray.hidden = !PICS.items.length;
  for (const item of PICS.items) {
    const cell = document.createElement("div");
    if (item.kind === "file") {
      cell.className = "tray-item tray-file";
      cell.append(fileChip({ name: item.name, size: item.size }));
    } else {
      cell.className = "tray-item" + (item.url ? "" : " busy");
      if (item.url) {
        const img = document.createElement("img");
        img.src = item.url;
        img.alt = "Picture to send";
        cell.append(img);
      }
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "tray-remove";
    remove.setAttribute("aria-label", item.kind === "file" ? "Remove " + item.name : "Remove picture");
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      PICS.items = PICS.items.filter((i) => i !== item);
      if (item.url) URL.revokeObjectURL(item.url);
      paintTray();
      paintSendState();
    });
    cell.append(remove);
    tray.append(cell);
  }
}

function clearTray(revoke) {
  if (revoke) for (const item of PICS.items) if (item.url) URL.revokeObjectURL(item.url);
  PICS.items = [];
  paintTray();
}

function readyPics() {
  return PICS.items.filter(itemReady);
}

function paintSendState() {
  const run = liveRun(state.sessionId);
  const busy = PICS.items.some((item) => !itemReady(item));
  $("send").disabled = busy || !($("draft").value.trim() || readyPics().length) || Boolean(run && run.steering);
  $("attach").disabled = Boolean(run);
}

// The photo picker or camera can hide the page; that must not lock it.
function pickerDone() {
  if (!PICS.pickerOpen) return;
  PICS.pickerOpen = false;
  LOCK.prompting = Math.max(0, LOCK.prompting - 1);
  LOCK.lastInput = Date.now();
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
    setReplyHtml(body, textOf(message.content));
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
    // Pictures being sent show from their local preview; after a reload, from the Mac.
    // Without local previews (after a reload) userBubble reads the paths out of the text.
    const sources = run.picSources && run.picSources.length ? run.picSources : undefined;
    run._userNode = userBubble(sources ? run.shownText || "" : run.userText, false, sources);
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
  if (!busy || liveRun(state.sessionId)) return null;
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
  if (liveRun(id)) { OTHER.busy = null; return; }
  OTHER.checking = true;
  try {
    const res = await api("/phone-api/busy?session=" + encodeURIComponent(id) +
      "&profile=" + encodeURIComponent(state.profile || "default"));
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
  const run = liveRun(state.sessionId);
  if (!run || document.hidden) return;
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
    setReplyHtml(entry.node.querySelector(".assistant"), block.text || "");
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
  const run = currentRun();
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

// Hermes saves a picture message's text with a "[screenshot]" line per image.
function sameUserText(text) {
  return String(text || "").split("\n").filter((line) => line.trim() !== "[screenshot]").join("\n").trim();
}

function historyHasUser(run) {
  const needle = sameUserText(run.userText);
  let count = 0;
  let last = "";
  for (const message of state.messages) {
    if (realUser(message)) {
      count += 1;
      last = sameUserText(textOf(message.content));
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
    setReplyHtml(body, block.text || "");
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

// Replies in other sessions keep running and never lock this one. The banner
// only points at them: one line, a link per reply (newest first, at most 2).
function paintRunChrome() {
  const run = liveRun(state.sessionId);
  const other = $("other-run");
  // While this session's reply runs, Send becomes "add guidance" (U3) and Stop sits beside it.
  $("stop").hidden = !run;
  $("send").setAttribute("aria-label", run ? "Send guidance" : "Send");
  $("draft").disabled = false;
  paintSendState();
  paintDraftHint();
  const elsewhere = state.runs.filter((r) => !r.done && r.sessionId !== state.sessionId);
  if (elsewhere.length && $("chat-screen").hidden === false) {
    other.hidden = false;
    other.textContent = "";
    other.append(elsewhere.length === 1 ? "Also working: " : elsewhere.length + " other replies running: ");
    elsewhere.slice(-2).reverse().forEach((r, i) => {
      if (i) other.append(" · ");
      const jump = document.createElement("button");
      jump.type = "button";
      jump.textContent = oneLine(r.title || "Hermes", 28);
      jump.addEventListener("click", () => openSession(r.sessionId, r.title || "Hermes", true, r.profile));
      other.append(jump);
    });
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
  const mine = liveRun(id);
  const own = mine ? mine.approvals : [];
  return DESKTOP.list.filter((a) =>
    Array.isArray(a.sessions) && a.sessions.includes(id) &&
    (a.profile || "default") === (state.profile || "default") &&
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
  const run = liveRun(state.sessionId);
  if (run) {
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
    const res = await api(inProfile(run.profile, "/v1/runs/" + encodeURIComponent(run.runId) + "/approval"), {
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
    paintDots();
  }
}

function finishRun(run, event) {
  if (run.done) return;
  run.done = true;
  saveRuns();
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
  // it; put it back in the box so it can go as the next message. Only when
  // that session is open: the box belongs to whatever session is on screen.
  if (event.pending_steer && state.sessionId === run.sessionId) {
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
  // Finished: drop it, unless its session is open and history does not have
  // the reply yet; then it stays on screen (done) until history catches up.
  dropRun(run);
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
      if (run.userText) {
        addRun(run);
        renderTranscript();
        paintRunChrome();
      }
    }).catch(() => {
      addRun(run);
      renderTranscript();
      paintRunChrome();
    });
  } else {
    // Finished while you were looking at something else.
    const set = unreadSet();
    set.add(sessionId);
    saveUnread(set);
  }
  if ($("list-screen").hidden === false) loadSessions(true).catch(() => renderSessions());
}

// A finished run kept for display is cleared once you leave its session.
function dropDoneRuns(keepId) {
  state.runs = state.runs.filter((r) => !r.done || r.sessionId === keepId);
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

// A run off screen is polled less often; its dot and the finish are all that show.
async function pollRun(run) {
  let failures = 0;
  while (tracked(run) && !run.done) {
    const onScreen = state.sessionId === run.sessionId && !$("chat-screen").hidden;
    await new Promise((resolve) => setTimeout(resolve, onScreen ? 2000 : 5000));
    if (!tracked(run) || run.done || !run.runId) return;
    if (document.hidden) continue;
    let res;
    try {
      res = await api(inProfile(run.profile, "/v1/runs/" + encodeURIComponent(run.runId)));
    } catch (e) {
      if (e.status === 401) { showKey(e.message); return; }
      failures += 1;
      if (failures >= 30) {
        run.blocks.push({ kind: "note", text: "Lost contact with Hermes. The reply may still be running." });
        run.statusText = "";
        run.done = true;
        dropRun(run);
        saveRuns();
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
      dropRun(run);
      saveRuns();
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
        if (!tracked(run) || run.done) return;
        pruneSavedBlocks(run);
      } catch (e) {
        if (e.status === 401) { showKey(e.message); return; }
      }
    }
    if (state.sessionId === run.sessionId) {
      renderLive();
      paintRunChrome();
    } else if (!$("list-screen").hidden) {
      paintDots();
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
  run.blocks.push({ kind: "note", text: message });
  run.statusText = "";
  run.approvals = [];
  dropRun(run);
  saveRuns();
  keepNotes(run);
  // Nothing was saved, so put the message (and its pictures) back to retry.
  const draft = $("draft");
  const words = run.picItems ? run.shownText || "" : run.userText;
  if (state.sessionId === run.sessionId) {
    if (words && !draft.value.trim()) {
      draft.value = words;
      draft.dispatchEvent(new Event("input"));
    }
    if (run.picItems && run.picItems.length && !PICS.items.length) {
      PICS.items = run.picItems;
      run.picItems = null;
      paintTray();
      paintSendState();
    }
  }
  if (state.sessionId === run.sessionId) renderTranscript();
  paintRunChrome();
}

async function sendMessage(text) {
  const sessionId = state.sessionId;
  if (!sessionId || liveRun(sessionId)) return;
  dropDoneRuns(null);
  const picItems = readyPics();
  // Pictures travel inside the message as base64 (a third bigger), and Hermes
  // refuses a message over 10 MB. Files only travel by path.
  const inline = picItems.filter((i) => i.kind !== "file").reduce((n, i) => n + i.blob.size, 0);
  if (inline * 1.37 > PIC_MESSAGE_MAX) {
    notice("Those pictures are too large to send together. Send them in two messages.");
    return;
  }
  if (picItems.length) PICS.items = PICS.items.filter((item) => !picItems.includes(item));
  const run = {
    sessionId,
    openedId: sessionId,
    profile: state.profile || "default",
    title: state.title,
    runId: "",
    // Never empty: it is what marks the run as having a message on screen.
    userText: text || (!picItems.length ? ""
      : picItems.some((i) => i.kind !== "file") ? PIC_DEFAULT : FILE_DEFAULT),
    shownText: text,
    picItems: picItems.length ? picItems : null,
    picSources: picItems.map((item) => item.kind === "file"
      ? { kind: "file", name: item.name, size: item.size } : { kind: "image", url: item.url }),
    blocks: [],
    approvals: [],
    statusText: picItems.length ? "Sending " + attachWord(picItems) + "…" : REPLYING,
    startedAt: Date.now(),
    phaseSince: Date.now(),
    userCountAtSend: state.messages.filter(realUser).length,
    done: false,
    streamOpen: false,
    sawBytes: false
  };
  addRun(run);
  state.tail = null;
  $("draft").value = "";
  $("draft").style.height = "auto";
  paintTray();
  renderTranscript();
  paintRunChrome();
  let input = text;
  if (picItems.length) {
    // Saved on the Mac first, so the message can carry each path. The message
    // ends with the desktop app's own directive lines (@image:, @file:), so
    // both apps show the attachments from history. Pictures also go to the
    // model as images; files go by path only, and the turn's instructions tell
    // Hermes to open them (this endpoint does not expand @file: itself).
    // Order matters to the desktop: it makes chips only of @file: lines that
    // lead the message (as its composer sends them), while @image: lines count
    // anywhere (its gateway puts them last). Files first, text, then pictures.
    try {
      const fileRefs = [];
      const imageRefs = [];
      const parts = [];
      for (const item of picItems) {
        const path = await uploadPicture(run.profile, item.blob, item.kind, item.name);
        if (item.kind === "file") {
          fileRefs.push("@file:" + refValue(path));
        } else {
          imageRefs.push("@image:" + refValue(path));
          parts.push({ type: "image_url", image_url: { url: await blobToDataUrl(item.blob) } });
        }
      }
      const words = fileRefs.concat([text || run.userText], imageRefs).join("\n");
      run.userText = words;
      input = parts.length ? [{ role: "user", content: [{ type: "text", text: words }].concat(parts) }] : words;
    } catch (e) {
      if (e.status === 401) {
        dropRun(run);
        paintRunChrome();
        return showKey(e.message);
      }
      failStart(run, e.message || "Could not send the picture");
      return;
    }
    run.statusText = REPLYING;
    run.phaseSince = Date.now();
  }
  let res;
  try {
    res = await api(inProfile(run.profile, "/v1/runs"), {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ input, session_id: sessionId, instructions: PHONE_INSTRUCTIONS })
    });
  } catch (e) {
    if (e.status === 401) {
      dropRun(run);
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
  saveRuns();
  if (!run.runId) {
    finishRun(run, { event: "run.failed", error: "Hermes did not return a run." });
    return;
  }
  pollRun(run);
  try {
    run.streamOpen = true;
    const stream = await api(inProfile(run.profile, "/v1/runs/" + encodeURIComponent(run.runId) + "/events"));
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
  return Boolean(liveRun(state.sessionId));
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
  const res = await api(here("/api/sessions/" + encodeURIComponent(id)), {
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
  const res = await api(here("/api/sessions/" + encodeURIComponent(state.sessionId) + "/fork"), {
    method: "POST",
    body: "{}"
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const data = await res.json();
  const fork = data.session || {};
  if (!fork.id) throw new Error("Hermes did not return the new session.");
  await openSession(fork.id, fork.title || "New session", true, state.profile);
  notice("Forked. You are in the copy; the original is still in the list.");
  loadSessions(true).catch(() => {});
}

async function deleteSession() {
  const id = state.sessionId;
  const res = await api(here("/api/sessions/" + encodeURIComponent(id)), { method: "DELETE" });
  if (!res.ok) throw new Error(await errorMessage(res));
  state.runs = state.runs.filter((r) => r.sessionId !== id);
  saveRuns();
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
  const run = liveRun(state.sessionId);
  if (!run || !run.runId || run.steering) return;
  run.steering = true;
  paintRunChrome();
  try {
    const res = await api(inProfile(run.profile, "/v1/runs/" + encodeURIComponent(run.runId) + "/steer"), {
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
  // Stop only ever applies to the reply in the session on screen.
  const run = liveRun(state.sessionId);
  if (!run || !run.runId) return;
  $("stop").disabled = true;
  try {
    const res = await api(inProfile(run.profile, "/v1/runs/" + encodeURIComponent(run.runId) + "/stop"), { method: "POST" });
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
    await loadProfiles();
    await loadSessions(true);
    show("list-screen");
  } catch (e) {
    if (e.status === 401) setKey("");
    showKey(e.message || "That key was rejected.");
  }
});

$("forget").addEventListener("click", () => {
  setKey("");
  state.runs = [];
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

// A new session goes to the profile in view; in "All profiles", to Default.
$("new-chat").addEventListener("click", async () => {
  $("new-chat").disabled = true;
  const profile = VIEW.profile === "all" ? "default" : VIEW.profile;
  try {
    const res = await api(inProfile(profile, "/api/sessions"), { method: "POST", body: "{}" });
    if (!res.ok) throw new Error(await errorMessage(res));
    const data = await res.json();
    const session = data.session || {};
    await openSession(session.id, session.title || "New session", true, profile);
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

function showList() {
  DRAWER.mode = "sessions";
  dropDoneRuns(null);
  show("list-screen");
  renderSessions();
  loadSessions(true).catch(listError);
}

$("back").addEventListener("click", () => {
  toggleMenu(false);
  $("draft").blur();
  if (history.state && history.state.id) history.back();
  else if (DRAWER.mode === "cron") {
    history.pushState({ cron: true }, "", "#cron");
    showCron();
  } else {
    history.pushState({}, "", location.pathname);
    showList();
  }
});

window.addEventListener("popstate", () => {
  const cron = cronHashTarget();
  if (cron) return void openCronTarget(cron);
  const target = hashTarget();
  if (target) openSession(target.id, "Hermes", false, target.profile);
  else showList();
});

for (const button of document.querySelectorAll("[data-drawer]")) {
  button.addEventListener("click", () => toggleSheet());
}
for (const button of document.querySelectorAll(".drawer-mode")) {
  button.addEventListener("click", () => pickMode(button.dataset.mode));
}
$("drawer").addEventListener("click", (event) => {
  if (event.target === $("drawer")) toggleSheet(false);
});
// Swipe the drawer left to close it.
$("drawer").addEventListener("touchstart", (event) => {
  DRAWER.touchX = event.touches.length === 1 ? event.touches[0].clientX : null;
}, { passive: true });
$("drawer").addEventListener("touchend", (event) => {
  const start = DRAWER.touchX;
  DRAWER.touchX = null;
  if (start != null && event.changedTouches[0] && start - event.changedTouches[0].clientX > 60) toggleSheet(false);
}, { passive: true });
$("profile-chip").addEventListener("click", () => toggleSheet(true));
$("cron-refresh").addEventListener("click", () => loadCron());
$("job-back").addEventListener("click", () => {
  if (history.state && history.state.job) history.back();
  else {
    history.replaceState({ cron: true }, "", "#cron");
    showCron();
  }
});

$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = $("draft").value.trim();
  const running = liveRun(state.sessionId);
  if (PICS.items.some((item) => !itemReady(item))) return;
  if (!text && (running || !readyPics().length)) return;
  notice("");
  // A reply running in this session takes the text as guidance. One running
  // anywhere else does not matter: this session sends as normal.
  if (running) steerRun(text);
  else sendMessage(text);
});

// The + opens iOS's own picker straight away. iOS already offers Photo Library,
// Take Photo and Choose File there, so a menu of ours in front of it only
// asked the same question twice. Pictures and other files are told apart by type.
$("attach").addEventListener("click", () => {
  if (liveRun(state.sessionId)) return;
  if (PICS.items.length >= PIC_LIMIT) return notice("Up to " + PIC_LIMIT + " attachments per message.");
  // The picker, camera or Files may hide the page; that is not leaving the app.
  if (!PICS.pickerOpen) {
    PICS.pickerOpen = true;
    LOCK.prompting++;
    const token = (PICS.token = (PICS.token || 0) + 1);
    setTimeout(() => { if (PICS.token === token) pickerDone(); }, 5 * 60 * 1000);
  }
  const input = $("file-input");
  input.value = "";
  input.click();
});

async function addPicked(input, asFile) {
  pickerDone();
  const files = Array.from(input.files || []);
  input.value = "";
  const room = PIC_LIMIT - PICS.items.length;
  if (files.length > room) notice("Up to " + PIC_LIMIT + " attachments per message.");
  const added = [];
  for (const file of files.slice(0, Math.max(0, room))) {
    // From Files, a picture still goes as a picture (shrunk, shown, seen by the model).
    const picture = /^image\//.test(file.type || "") && file.type !== "image/svg+xml";
    if (asFile && !picture) {
      if (file.size > FILE_LIMIT) {
        notice(file.name + " is larger than " + Math.round(FILE_LIMIT / 1024 / 1024) + " MB.");
        continue;
      }
      added.push({ kind: "file", file: null, blob: file, name: file.name || "attachment", size: file.size });
    } else {
      added.push({ kind: "image", file, blob: null, url: "" });
    }
  }
  if (!added.length) return;
  PICS.items = PICS.items.concat(added);
  paintTray();
  paintSendState();
  for (const item of added) {
    if (item.kind === "file") continue;
    try {
      item.blob = await shrinkPicture(item.file);
      item.url = URL.createObjectURL(item.blob);
    } catch (e) {
      PICS.items = PICS.items.filter((i) => i !== item);
      notice(e.message || "That picture could not be added.");
    }
    item.file = null;
    paintTray();
    paintSendState();
  }
}

$("file-input").addEventListener("cancel", pickerDone);
$("file-input").addEventListener("change", () => addPicked($("file-input"), true));
window.addEventListener("focus", pickerDone);

$("stop").addEventListener("click", stopRun);

$("draft").addEventListener("input", () => {
  const draft = $("draft");
  draft.style.height = "auto";
  draft.style.height = Math.min(draft.scrollHeight, 120) + "px";
  paintSendState();
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return lockNow();
  pickerDone();
  if (resumeLocked()) return;
  autoUnlock();
  const run = liveRun(state.sessionId);
  if (run && !run.streamOpen) renderLive();
  if (!$("list-screen").hidden && authed()) loadSessions(true).catch(() => {});
  checkActive();
});

// Pick up every reply this page started that is still running.
async function resumeRuns() {
  const resumed = [];
  await Promise.all(savedRuns().map(async (saved) => {
    try {
      const res = await api(inProfile(saved.profile, "/v1/runs/" + encodeURIComponent(saved.runId)));
      if (!res.ok) return;
      const status = await res.json();
      if (!["queued", "running", "stopping", "waiting_for_approval"].includes(status.status)) return;
      if (liveRun(saved.sessionId)) return;
      const run = {
        sessionId: saved.sessionId, openedId: saved.sessionId, profile: saved.profile || "default",
        title: saved.title || "Hermes", runId: saved.runId, userText: saved.userText || "",
        blocks: [], approvals: [], statusText: REPLYING, done: false, streamOpen: false, sawBytes: false,
        startedAt: status.created_at ? status.created_at * 1000 : Date.now(),
        phaseSince: status.updated_at ? status.updated_at * 1000 : Date.now(),
        lastEvent: status.last_event || ""
      };
      if (status.status === "waiting_for_approval" && status.approval) addApproval(run, status.approval);
      addRun(run);
      resumed.push(run);
    } catch (e) {
      if (e.status === 401) throw e;
    }
  }));
  saveRuns();
  for (const run of resumed) pollRun(run);
  return resumed;
}

async function boot() {
  await loadAuth();
  if (AUTH.faceid) {
    setKey("");
    $("key-btn").textContent = "Lock";
    // Every app open asks for Face ID, even when the Mac still holds a live
    // unlock (a logout sent while iOS froze the page may not have landed).
    if (AUTH.unlocked && !LOCK.opened) {
      // Awaited: its cookie-clearing reply must not land after the new unlock's cookie.
      try { await fetch(apiBase() + "/phone-api/auth/logout", { method: "POST" }); } catch (e) {}
      AUTH.unlocked = false;
    }
    LOCK.opened = true;
    if (!AUTH.unlocked) return showLock("");
  } else if (AUTH.setupOpen && !SETUP.skipped) {
    return showSetup();
  }
  if (!authed()) return showKey("");
  watchActive();
  const target = hashTarget();
  try {
    await loadProfiles();
    await loadSessions(true);
  } catch (e) {
    if (e.status === 401) return showKey(e.message);
    show("list-screen");
    LIST.note = e.message || "Can't reach Hermes from this page.";
    renderSessions();
    return;
  }
  let resumed = [];
  try { resumed = await resumeRuns(); }
  catch (e) { if (e.status === 401) return showKey(e.message); }
  // The link wins; otherwise reopen the newest reply still running.
  const last = resumed[resumed.length - 1];
  const cron = cronHashTarget();
  if (target) openSession(target.id, "Hermes", false, target.profile);
  else if (cron) openCronTarget(cron);
  else if (last) openSession(last.sessionId, last.title, false, last.profile);
  else show("list-screen");
}

boot();
