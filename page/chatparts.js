// Chat-screen rules for the phone page, after the Hermes desktop app:
// task lists (apps/desktop/src/lib/todos.ts and the composer status stack),
// tapback reactions (store/reactions.ts), and search filtering. No DOM and no
// fetch here, so tests/chat_test.py runs these exact functions in node.
(function (root) {
  "use strict";

  // The task tool is `todo_list` on the wire; `todo` survives in older transcripts.
  const TODO_TOOLS = ["todo_list", "todo"];
  const TODO_STATUSES = ["pending", "in_progress", "completed", "cancelled"];

  function isTodoTool(name) {
    return TODO_TOOLS.indexOf(String(name || "")) >= 0;
  }

  function isRecord(v) {
    return Boolean(v) && typeof v === "object" && !Array.isArray(v);
  }

  // A tool result (string or object) to [{id, content, status, parent?}], or
  // null when it is not a task list. Bad items are skipped, as on the desktop.
  function parseTodos(value, depth) {
    const d = depth || 0;
    if (d > 2) return null;
    if (Array.isArray(value)) {
      const out = [];
      for (const item of value) {
        if (!isRecord(item) || TODO_STATUSES.indexOf(item.status) < 0) continue;
        const id = String(item.id == null ? "" : item.id).trim();
        const content = String(item.content == null ? "" : item.content).trim();
        const parent = String(item.parent == null ? "" : item.parent).trim();
        if (!id || !content) continue;
        const row = { id, content, status: item.status };
        if (parent && parent !== id) row.parent = parent;
        // Weighted progress (Hermes core patch): relative effort 1-100, and a
        // flag for an in-progress step that waits on you.
        const weight = typeof item.weight === "number" ? item.weight
          : typeof item.weight === "string" && item.weight.trim() ? Number(item.weight) : NaN;
        if (isFinite(weight)) row.weight = Math.max(1, Math.min(100, Math.round(weight)));
        if (item.waiting === true && item.status === "in_progress") row.waiting = true;
        // Per-step error (Hermes core S53): the step hit an error while the job
        // went on. Any status but cancelled (a cancelled step drops it).
        if (item.error === true && item.status !== "cancelled") row.error = true;
        out.push(row);
      }
      return out;
    }
    if (typeof value === "string" && value.trim()) {
      try { return parseTodos(JSON.parse(value), d + 1); } catch (e) { return null; }
    }
    if (isRecord(value) && Object.prototype.hasOwnProperty.call(value, "todos")) return parseTodos(value.todos, d + 1);
    return null;
  }

  // Parents before children: [[item, depth]]. Dangling or cyclic parents sit at depth 0.
  function todoTree(todos) {
    const list = Array.isArray(todos) ? todos : [];
    const ids = new Set(list.map((t) => t.id));
    const kids = new Map();
    const roots = [];
    for (const t of list) {
      if (t.parent && ids.has(t.parent) && t.parent !== t.id) {
        if (!kids.has(t.parent)) kids.set(t.parent, []);
        kids.get(t.parent).push(t);
      } else {
        roots.push(t);
      }
    }
    const out = [];
    const seen = new Set();
    const walk = (t, depth) => {
      if (seen.has(t.id)) return;
      seen.add(t.id);
      out.push([t, depth]);
      for (const k of kids.get(t.id) || []) walk(k, depth + 1);
    };
    for (const t of roots) walk(t, 0);
    for (const t of list) if (!seen.has(t.id)) walk(t, 0);
    return out;
  }

  // An item's share of the job for the bar: its weight, 1 when unset.
  function todoWeight(t) {
    const w = Number(t && t.weight);
    return isFinite(w) && w >= 1 ? Math.min(100, Math.round(w)) : 1;
  }

  // {done, total, open, current, percent}: done counts completed; cancelled
  // items leave the total (they are not left to do); current is the
  // in-progress item; percent is completed weight over all counted weight.
  function todoProgress(todos) {
    const list = Array.isArray(todos) ? todos : [];
    const counted = list.filter((t) => t.status !== "cancelled");
    const done = counted.filter((t) => t.status === "completed").length;
    const current = list.find((t) => t.status === "in_progress") || null;
    let all = 0;
    let finished = 0;
    for (const t of counted) {
      all += todoWeight(t);
      if (t.status === "completed") finished += todoWeight(t);
    }
    const percent = all ? Math.round(100 * finished / all) : 0;
    return { done, total: counted.length, open: counted.length - done, current, percent };
  }

  // The card's colour: "done" (green), "failed"/"stopped" (red, from how the
  // reply ended), "waiting" (yellow: the step waits on you, or the reply ended
  // with steps still open, which almost always means a question to you), else
  // "running" while a reply works. "idle" is left only for an empty list.
  function todoTone(todos, outcome, working) {
    const prog = todoProgress(todos);
    if (prog.total > 0 && prog.open === 0) return "done";
    if (outcome === "failed" || outcome === "stopped") return outcome;
    if (prog.current && prog.current.waiting) return "waiting";
    if (working) return "running";
    return prog.open > 0 ? "waiting" : "idle";
  }

  // The step a "waiting" list waits on: the in-progress one, or the first
  // pending one when the reply ended with none in progress. Null otherwise.
  function waitedOnId(todos, tone) {
    if (tone !== "waiting") return null;
    const list = Array.isArray(todos) ? todos : [];
    const step = list.find((t) => t.status === "in_progress") || list.find((t) => t.status === "pending");
    return step ? step.id : null;
  }

  // The step the card names on its line under the bar (S53, item 3c): running,
  // the in-progress step (none between steps); waiting / stopped / failed, the
  // in-progress step, else the first pending one; finished, nothing. Returns
  // the step's text or null.
  function stepName(todos, tone) {
    const list = (Array.isArray(todos) ? todos : []).filter((t) => t.status !== "cancelled");
    if (!list.some((t) => t.status === "pending" || t.status === "in_progress")) return null;
    const running = list.find((t) => t.status === "in_progress");
    if (running) return running.content || null;
    if (tone === "waiting" || tone === "stopped" || tone === "failed") {
      const next = list.find((t) => t.status === "pending");
      return next ? next.content || null : null;
    }
    return null;
  }

  // One bar segment's look, after the desktop's TodoProgressBar (S53):
  // {halt, error}. halt is "waiting" (yellow outline, only the step waited on),
  // "failed"/"stopped" (red outline: the step a stopped or failed list halted
  // on, or an errored step not yet done), or null. error is true for a step
  // marked error while the list is not already all red; a completed one fills
  // red. Finished steps otherwise keep the accent colour (green when done).
  function segLook(todo, tone, waitingId) {
    const listRed = tone === "failed" || tone === "stopped";
    const error = Boolean(todo && todo.error) && todo.status !== "cancelled" && !listRed;
    let halt = null;
    if (todo && waitingId != null && todo.id === waitingId) halt = "waiting";
    else if (todo && todo.status === "in_progress") halt = listRed ? tone : error ? "failed" : null;
    else if (todo && todo.status === "pending" && error) halt = "failed";
    return { halt, error };
  }

  // A task row's mark: "failed"/"stopped" (red ✕), "waiting" (yellow ‖) or
  // null (its normal mark). An errored step shows the red ✕ whatever its
  // status (the desktop's row rule), the step waited on the yellow ‖.
  function rowHalt(todo, tone, waitingId) {
    if (!todo || todo.status === "cancelled") return null;
    if (todo.error) return "failed";
    if (waitingId != null && todo.id === waitingId) return "waiting";
    if (todo.status === "in_progress" && (tone === "failed" || tone === "stopped" || tone === "waiting")) return tone;
    return null;
  }

  // Steps that turned completed since the last paint (they get the 1 s fill).
  // prevDone is the list of completed ids last painted, or null on the first
  // paint of a list, when nothing animates.
  function freshlyDone(prevDone, todos) {
    const out = [];
    if (!Array.isArray(prevDone)) return out;
    const before = new Set(prevDone);
    for (const t of Array.isArray(todos) ? todos : []) {
      if (t.status === "completed" && !before.has(t.id)) out.push(t.id);
    }
    return out;
  }

  // How a phone-run ending maps to the card: failed is red Failed; a Stop or
  // an interruption is red Stopped; a finished reply clears it.
  function runOutcome(eventName) {
    const name = String(eventName || "");
    if (name.indexOf("failed") >= 0) return "failed";
    if (name.indexOf("cancelled") >= 0 || name.indexOf("interrupted") >= 0) return "stopped";
    return null;
  }

  // The newest task list in a transcript page: {todos, rowId} or null.
  // Rows are Hermes messages (role "tool", tool_name, content, id).
  function latestTodos(messages) {
    const rows = Array.isArray(messages) ? messages : [];
    for (let i = rows.length - 1; i >= 0; i--) {
      const m = rows[i];
      if (!m || m.role !== "tool" || !isTodoTool(m.tool_name)) continue;
      const todos = parseTodos(m.content);
      if (todos) return { todos, rowId: Number(m.id) || 0 };
    }
    return null;
  }

  // Whether the plan card shows. Only the latest turn's list: written after
  // the last message you sent (lastUserRowId), and after floorRowId, the list
  // on screen when you sent from this page (your new message is not loaded
  // yet then). Idle, it stays until you close it; while working it always shows.
  function planVisible(plan, opts) {
    if (!plan || !Array.isArray(plan.todos) || !plan.todos.length) return false;
    const o = opts || {};
    if (plan.rowId <= (Number(o.floorRowId) || 0)) return false;
    if (plan.rowId <= (Number(o.lastUserRowId) || 0)) return false;
    return Boolean(o.working) || !o.dismissed;
  }

  // What the card shows (S53 sticky bar):
  //   "live"     this turn's list (as planVisible);
  //   "planning" a reply runs and this turn has no list yet (grey stripes,
  //              elapsed time), unless closed for this turn;
  //   "previous" the reply is over and wrote no list: the newest earlier
  //              list comes back, folded, unless that list was closed;
  //   "hidden"   nothing to show.
  function planState(plan, opts) {
    const o = opts || {};
    const hasList = Boolean(plan) && Array.isArray(plan.todos) && plan.todos.length > 0;
    if (hasList && plan.rowId > (Number(o.floorRowId) || 0) && plan.rowId > (Number(o.lastUserRowId) || 0)) {
      return o.working || !o.dismissed ? "live" : "hidden";
    }
    if (o.working) return o.planningDismissed ? "hidden" : "planning";
    return hasList && !o.dismissed ? "previous" : "hidden";
  }

  // The six iOS Tapbacks, in Apple's order (the desktop's QUICK_REACTIONS).
  const QUICK_REACTIONS = ["\u2764\ufe0f", "\ud83d\udc4d", "\ud83d\udc4e", "\ud83d\ude02", "\u203c\ufe0f", "\u2753"];

  // One reaction per author; the same emoji again (or null) removes it.
  function applyReaction(reactions, emoji, author) {
    const current = Array.isArray(reactions) ? reactions : [];
    const previous = current.find((r) => r.author === author);
    const without = current.filter((r) => r.author !== author);
    if (!emoji || (previous && previous.emoji === emoji)) return without;
    return without.concat([{ emoji, author }]);
  }

  function mine(reactions) {
    const r = (Array.isArray(reactions) ? reactions : []).find((x) => x.author === "user");
    return r ? r.emoji : null;
  }

  // Double tap: a second tap within `ms` and `px` of the first. Returns a
  // function fed (x, y, timeMs) per tap; true on the second tap of a pair.
  function tapDetector(ms, px) {
    const gap = ms || 320;
    const near = px || 30;
    let last = null;
    return function (x, y, t) {
      if (last && t - last.t <= gap && Math.abs(x - last.x) <= near && Math.abs(y - last.y) <= near) {
        last = null;
        return true;
      }
      last = { x, y, t };
      return false;
    };
  }

  // Search hits the main list may show: cron runs only with the View setting on.
  function searchRows(rows, showAll) {
    const list = Array.isArray(rows) ? rows : [];
    return showAll ? list.slice() : list.filter((r) => r && (r.pinned || String(r.source || "").toLowerCase() !== "cron"));
  }

  // Chats started on a Desktop, on Hermes v0.22+. Hermes refuses a phone turn
  // on /v1/runs there unless the chat's source is api_server or bot_room, so
  // the page sends through the gateway instead (/phone-api/desktop-send). An
  // unknown source keeps the /v1/runs path.
  const RUN_SOURCES = ["api_server", "bot_room"];

  function gatewayRoute(canSend, source) {
    const s = String(source || "").trim().toLowerCase();
    return canSend === true && s !== "" && RUN_SOURCES.indexOf(s) < 0;
  }

  function rowText(content) {
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      return content.map((p) => typeof p === "string" ? p : (p && typeof p.text === "string" ? p.text : "")).join("\n");
    }
    return content && typeof content.text === "string" ? content.text : "";
  }

  // A user row's words without its picture lines: the gateway saves a picture
  // as "[Image attached at: <path>]" plus "[screenshot]", the phone as @image:.
  function coreText(text) {
    return String(text || "").split("\n").filter((line) => {
      const t = line.trim();
      return t !== "[screenshot]" && !/^\[Image attached at: .+\]$/.test(t) && !/^@image:\S/.test(t);
    }).join("\n").trim();
  }

  // Where a gateway send stands in the saved rows (newest rows, oldest first):
  // saved = its own user row is there (newer than afterRowId: the same words,
  // else the first user row since the send, in case Hermes saved the words
  // differently); replied = an assistant row with text follows it. When the
  // reply's steps pushed the user row out of the window, every row shown is
  // newer than the send, and an assistant row among them counts as saved.
  function gatewayProgress(rows, sent) {
    const list = (Array.isArray(rows) ? rows : []).filter((r) => r && typeof r === "object");
    const floor = Number(sent && sent.afterRowId) || 0;
    const want = coreText(sent && sent.text);
    const fresh = list.filter((r) => Number(r.id) > floor);
    const isUser = (r) => r.role === "user" && !r.display_kind;
    const said = (r) => r.role === "assistant" && rowText(r.content).trim() !== "";
    let at = fresh.findIndex((r) => isUser(r) && coreText(rowText(r.content)) === want);
    if (at < 0) at = fresh.findIndex(isUser);
    if (at >= 0) return { saved: true, replied: fresh.slice(at + 1).some(said) };
    const scrolled = list.length > 0 && fresh.length === list.length && !fresh.some(isUser);
    return { saved: scrolled && fresh.some(said), replied: scrolled && fresh.some(said) };
  }

  // One look at a gateway send. busy: the chat's turn lease is held. prev:
  // the last step's {seenBusy, quiet, idleSince} ({} at first). Done once the
  // lease is gone and the message's row is saved, and either a reply follows,
  // the lease was seen while it was saved, or 3 looks in a row found no lease.
  // "stalled": not saved and no lease for stallMs; the gateway still holds it.
  function gatewayStep(progress, busy, prev, now, stallMs) {
    const p = prev || {};
    const seenBusy = Boolean(p.seenBusy || (busy && progress.saved));
    if (busy) return { state: "working", seenBusy, quiet: 0, idleSince: null };
    if (progress.saved) {
      const quiet = (p.quiet || 0) + 1;
      const done = progress.replied || seenBusy || quiet >= 3;
      return { state: done ? "done" : "working", seenBusy, quiet, idleSince: null };
    }
    const idleSince = p.idleSince || now;
    return { state: now - idleSince >= (stallMs || 120000) ? "stalled" : "queued", seenBusy, quiet: 0, idleSince };
  }

  // One-tap answers. When the chat's last visible row is Hermes asking a
  // plain yes/no question ("Should I make it?", "Say go and I'll start"),
  // the page offers two buttons that send exactly this text, as if typed.
  // Never for anything touching a lock, the alarm, the thermostat, a garage
  // door, a siren or a camera: those answers are typed by hand.
  const QUICK_ANSWERS = ["Yes, go ahead", "Not now"];
  const QUICK_SENSITIVE = /\b(?:un)?lock(?:s|ed|ing)?\b|\balarm|\bthermostat|\bhvac\b|\bset ?points?\b|\bgarage|\bsiren|\bcamera|\bdisarm|\barm(?:ed|ing)?\b|\bheat(?:ing)?\b|\bcool(?:ing)?\b/i;
  const QUICK_YESNO = /^(?:so,?\s+|and\s+|ok(?:ay)?,?\s+)?(?:should|shall|do|does|did|can|could|would|will|want|is|are|was|ok(?:ay)?|ready|go ahead|proceed|may)\b/i;
  const QUICK_SAY_GO = /\bsay (?:go|yes)\b/i;

  function quickLastLine(text) {
    const noCode = String(text || "").replace(/```[\s\S]*?(?:```|$)/g, " ");
    const lines = noCode.split("\n").map((l) => l.replace(/[*_`>#]+/g, "").trim()).filter(Boolean);
    return lines.length ? lines[lines.length - 1] : "";
  }

  // rows: the transcript, oldest first. Returns {rowId, answers} or null.
  function quickReplies(rows) {
    const list = Array.isArray(rows) ? rows : [];
    for (let i = list.length - 1; i >= 0; i--) {
      const r = list[i];
      if (!r || typeof r !== "object" || r.display_kind === "hidden" || r.role === "system" || r.role === "tool") continue;
      if (r.role === "user") return null;
      if (r.role !== "assistant") continue;
      const text = rowText(r.content).trim();
      if (!text) continue;  // a tool-call-only step
      if (QUICK_SENSITIVE.test(text)) return null;
      const last = quickLastLine(text);
      const sentences = last.split(/(?<=[.!?])\s+/).filter(Boolean);
      const ask = sentences.length ? sentences[sentences.length - 1] : "";
      const yesNo = (/\?\s*$/.test(ask) && QUICK_YESNO.test(ask) && !/\bor\b/i.test(ask)) || QUICK_SAY_GO.test(last);
      return yesNo ? { rowId: r.id, answers: QUICK_ANSWERS.slice() } : null;
    }
    return null;
  }

  // Long messages you sent fold to a few lines, like the desktop's user
  // bubbles (styles.css .sticky-human-clamp: 4 lines and a bottom fade).
  // fullHeight: the text's whole height (scrollHeight); lineHeight: its line
  // height in px. A message that fits gets no fold and no button. Bad sizes
  // never fold: a broken measure must not hide a message.
  const CLAMP_LINES = 4;
  const CLAMP_SLACK = 0.5;  // scrollHeight is a rounded whole pixel

  function clampState(fullHeight, lineHeight, maxLines, open) {
    const lines = Number.isFinite(maxLines) && maxLines >= 1 ? Math.floor(maxLines) : CLAMP_LINES;
    const ok = Number.isFinite(fullHeight) && fullHeight > 0 && Number.isFinite(lineHeight) && lineHeight > 0;
    const over = ok && fullHeight > lineHeight * lines + CLAMP_SLACK;
    if (!over) return { over: false, clamped: false, button: "" };
    return { over: true, clamped: !open, button: open ? "Show less" : "Show all" };
  }

  // Copy, from the double-tap bar: the whole message, folded or not. Your own
  // message as it shows (picture and file lines are not words you typed:
  // shown is the bubble's text); a reply as Hermes wrote it, in Markdown, as
  // the desktop's copy button does. "" when there is nothing to copy.
  function copyText(message, shown) {
    if (!message || typeof message !== "object") return "";
    if (message.role === "user") return String(typeof shown === "string" ? shown : rowText(message.content)).trim();
    if (message.role === "assistant") return rowText(message.content).trim();
    return "";
  }

  root.PhoneParts = {
    CLAMP_LINES, clampState, copyText,
    TODO_TOOLS, isTodoTool, parseTodos, todoTree, todoProgress, todoWeight, todoTone, runOutcome, freshlyDone,
    latestTodos, planVisible, planState, waitedOnId, stepName, segLook, rowHalt,
    QUICK_REACTIONS, applyReaction, mine, tapDetector, searchRows,
    gatewayRoute, coreText, gatewayProgress, gatewayStep,
    QUICK_ANSWERS, quickReplies
  };
})(typeof window !== "undefined" ? window : globalThis);
