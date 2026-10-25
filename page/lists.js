// List rules for the phone page, after the Hermes desktop sidebar
// (apps/desktop/src/lib/session-source.ts, app/chat/sidebar/, app/cron/job-state.ts).
// No DOM and no fetch here, so tests/bots_test.py runs these exact functions in node.
(function (root) {
  "use strict";

  // The desktop's MESSAGING_SESSION_SOURCE_IDS without api_server: chats
  // started on the phone come in through the API server, so on the phone
  // api_server is the user's own conversation, not a messaging platform.
  const MESSAGING = [
    "telegram", "discord", "slack", "mattermost", "matrix", "signal", "whatsapp",
    "bluebubbles", "photon", "homeassistant", "email", "sms", "webhook",
    "weixin", "wecom", "qqbot", "yuanbao", "dingtalk", "feishu"
  ];
  const PLATFORM_LABEL = {
    telegram: "Telegram", discord: "Discord", slack: "Slack", mattermost: "Mattermost",
    matrix: "Matrix", signal: "Signal", whatsapp: "WhatsApp", bluebubbles: "iMessage",
    photon: "Photon", homeassistant: "Home Assistant", email: "Email", sms: "SMS",
    webhook: "Webhook", weixin: "WeChat", wecom: "WeCom", qqbot: "QQ", yuanbao: "Yuanbao",
    dingtalk: "DingTalk", feishu: "Feishu"
  };
  // Each profile's canonical Bot Chat is the session titled exactly this
  // (unique per profile; tui_gateway/methods_profiles.py _canonical_session_row).
  const BOT_CHAT_TITLE = "Bot Chat";

  function norm(value) {
    return String(value == null ? "" : value).trim().toLowerCase();
  }

  function stamp(row) {
    const v = row && (row.last_active || row.started_at);
    if (v == null || v === "") return 0;
    if (typeof v === "number") return v > 1e12 ? v : v * 1000;
    const n = Date.parse(v);
    return Number.isFinite(n) ? n : 0;
  }

  function isMessaging(source) {
    return MESSAGING.indexOf(norm(source)) >= 0;
  }

  function isCron(row) {
    return norm(row && row.source) === "cron";
  }

  function isBotChat(row) {
    return Boolean(row) && String(row.title || "").trim() === BOT_CHAT_TITLE;
  }

  // The main list holds the user's own chats. Pinned rows stay in Pinned
  // whatever their source, as on the desktop.
  function isOwnChat(row) {
    if (!row) return false;
    if (row.pinned) return true;
    return !isCron(row) && !isBotChat(row) && !isMessaging(row.source);
  }

  // showAll is the View setting "Cron runs and bot chats": the old list.
  function mainRows(rows, showAll) {
    const list = Array.isArray(rows) ? rows : [];
    return showAll ? list.slice() : list.filter(isOwnChat);
  }

  // Paging with client-side filtering: a page of 50 can come back almost
  // empty (a run of cron sessions), so keep asking for the next page until
  // the screen has a full page, the server has no more, or the cap is hit.
  // round() fetches one more page per profile and returns
  // { visible, more } (or { stale: true } when a newer load took over).
  // Returns how many rounds ran.
  async function fillPages(round, target, cap) {
    let rounds = 0;
    for (;;) {
      const r = await round();
      rounds++;
      if (!r || r.stale) return rounds;
      if (!r.more || r.visible >= target || rounds >= cap) return rounds;
    }
  }

  // One /api/sessions page: how far to move the offset, and whether to ask
  // again. Hermes adds pinned rows past the limit and leaves pinned rows out
  // of its has_more count, so a full page with a pin in it says "no more".
  // The first `limit` rows are the window; a full window means ask again
  // (an empty next page then ends it).
  function pageStep(rows, limit, hasMore) {
    const n = Array.isArray(rows) ? rows.length : 0;
    return { advance: Math.min(n, limit), more: Boolean(hasMore) || n >= limit };
  }

  // From an exact-title lookup: the live (not archived) Bot Chat, or null.
  function pickBotChat(rows) {
    const hits = (Array.isArray(rows) ? rows : []).filter((r) => isBotChat(r) && !r.archived);
    hits.sort((a, b) => stamp(b) - stamp(a));
    return hits[0] || null;
  }

  // Messaging-platform chats, one group per platform: rows newest first,
  // groups by their newest row. [{source, label, sessions}]
  function messagingGroups(rows) {
    const map = new Map();
    for (const row of Array.isArray(rows) ? rows : []) {
      const source = norm(row && row.source);
      if (!isMessaging(source) || isBotChat(row)) continue;
      if (!map.has(source)) map.set(source, []);
      map.get(source).push(row);
    }
    const groups = [];
    for (const [source, sessions] of map) {
      sessions.sort((a, b) => stamp(b) - stamp(a));
      groups.push({ source, label: PLATFORM_LABEL[source] || source, sessions });
    }
    groups.sort((a, b) => stamp(b.sessions[0]) - stamp(a.sessions[0]));
    return groups;
  }

  // Cron job state dot, as the desktop's jobState + STATE_DOT: accent for
  // scheduled, pulsing accent while running, amber paused, grey finished or
  // disabled, red on error. The phone also shows red when the last run failed.
  function jobDot(job) {
    const raw = typeof (job && job.state) === "string" ? job.state.trim() : "";
    const state = raw || (job && job.enabled === false ? "disabled" : "scheduled");
    const last = norm(job && job.last_status);
    if (state === "error" || (job && job.last_error) || /error|fail/.test(last)) return "bad";
    if (state === "running") return "running";
    if (state === "paused") return "held";
    if (state === "completed" || state === "disabled") return "off";
    return "on";
  }

  root.PhoneLists = {
    MESSAGING, PLATFORM_LABEL, BOT_CHAT_TITLE,
    isMessaging, isCron, isBotChat, isOwnChat, mainRows, fillPages, pageStep,
    pickBotChat, messagingGroups, jobDot
  };
})(typeof window !== "undefined" ? window : globalThis);
