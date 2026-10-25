// Service worker for browsers without Declarative Web Push (iOS before 18.4,
// desktop browsers). The Mac always sends the declarative JSON
// ({"web_push": 8030, "notification": {...}}); a browser that understands it
// shows it with no script at all, and the page registers this worker only
// when window.pushManager is missing.
"use strict";

self.addEventListener("push", (event) => {
  // A declarative-capable browser already has the notification it will show.
  if (event.notification) return;
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  const note = (data && data.notification) || {};
  const work = [self.registration.showNotification(String(note.title || "Hermes"), {
    body: String(note.body || ""),
    data: { url: String(note.navigate || self.registration.scope) },
  })];
  if (note.app_badge != null && self.navigator.setAppBadge) {
    work.push(self.navigator.setAppBadge(Number(note.app_badge) || 0).catch(() => {}));
  }
  event.waitUntil(Promise.all(work));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || self.registration.scope;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const win of wins) {
      if (new URL(win.url).origin !== self.location.origin) continue;
      await win.focus();
      win.postMessage({ type: "open", url });
      return;
    }
    await self.clients.openWindow(url);
  })());
});
