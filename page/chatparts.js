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

  // {done, total, open, current}: done counts completed; cancelled items
  // leave the total (they are not left to do); current is the in-progress item.
  function todoProgress(todos) {
    const list = Array.isArray(todos) ? todos : [];
    const counted = list.filter((t) => t.status !== "cancelled");
    const done = counted.filter((t) => t.status === "completed").length;
    const current = list.find((t) => t.status === "in_progress") || null;
    return { done, total: counted.length, open: counted.length - done, current };
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

  root.PhoneParts = {
    TODO_TOOLS, isTodoTool, parseTodos, todoTree, todoProgress, latestTodos, planVisible,
    QUICK_REACTIONS, applyReaction, mine, tapDetector, searchRows,
    gatewayRoute, coreText, gatewayProgress, gatewayStep
  };
})(typeof window !== "undefined" ? window : globalThis);
