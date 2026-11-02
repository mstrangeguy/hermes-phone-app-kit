// Service worker. Two jobs:
//
// 1. Offline page. When the Mac cannot be reached (Tailscale off on the
//    phone, the Mac asleep, the proxy stopped), opening the app shows
//    offline.html instead of Safari's error. Loading the app always goes to the
//    Mac first; the copy is used only when that fails. Only offline.html, the
//    stylesheets and the icon are kept: never the app's scripts, an API answer
//    or any chat text. Everything else passes straight through untouched.
//
// 2. Push for browsers without Declarative Web Push (iOS before 18.4, desktop
//    browsers). The Mac always sends the declarative JSON
//    ({"web_push": 8030, "notification": {...}}); a browser that understands
//    it shows it with no script at all.
//
// To undo (1) on every phone: replace this file with one that calls
// self.registration.unregister() and deletes its caches; each phone picks it
// up on its next load.
"use strict";

const OFFLINE_CACHE = "hermes-offline-v1";
const OFFLINE_FILES = ["offline.html", "app.css", "theme.css", "icon-180.png"];
const OFFLINE_URLS = OFFLINE_FILES.map((name) => new URL(name, self.registration.scope).href);
const OFFLINE_PAGE = OFFLINE_URLS[0];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(OFFLINE_CACHE);
    // A file that fails is left out; the page still works without the copy.
    await Promise.all(OFFLINE_URLS.map((url) =>
      fetch(url, { cache: "no-store" }).then((res) => (res.ok ? cache.put(url, res) : null)).catch(() => null)));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name.startsWith("hermes-offline-") && name !== OFFLINE_CACHE) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

async function offlinePage() {
  return (await caches.match(OFFLINE_PAGE)) ||
    new Response("Can't reach the Mac. Check that Tailscale is connected, then try again.",
      { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } });
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.mode === "navigate") {
    // Network first, always. A gateway error from Tailscale Serve (the
    // proxy is down) counts as unreachable too.
    event.respondWith(fetch(req).then((res) => (res.status >= 502 && res.status <= 504 ? offlinePage() : res))
      .catch(() => offlinePage()));
    return;
  }
  const bare = url.origin + url.pathname;
  if (OFFLINE_URLS.indexOf(bare) >= 0) {
    // The offline page's own files: network first, the kept copy if that fails.
    event.respondWith(fetch(req).catch(async () => (await caches.match(bare)) || Response.error()));
  }
  // Anything else (the app's scripts, /api, /phone-api): not handled here.
});

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
