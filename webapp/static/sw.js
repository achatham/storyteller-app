// Service worker. Two strategies:
//  - /api/*  : NETWORK-FIRST (data must be fresh; fall back to the offline store
//              / cache when the network is gone).
//  - shell   : STALE-WHILE-REVALIDATE (HTML/JS/CSS/icons) -- serve the cached copy
//              instantly so reloads are fast even on a slow/flaky connection, and
//              refresh the cache in the background for next time. (Use the in-app
//              "Check for updates" to jump straight to a new build.)
//
// Offline books: the reader's "Save for offline" downloads a book's meta, page
// text, chapter flows and every image into IndexedDB (see offline.js). When the
// network fails, we rebuild those exact /api/* responses from IndexedDB here, so a
// saved book reads with zero reader-side offline logic.
importScripts("/static/offline-idb.js");

const CACHE = "storyteller-v13";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (k !== CACHE) await caches.delete(k);
  await self.clients.claim();
})()));

// Cache only successful, same-origin, non-redirected responses (never a 202
// "generating", never a login page the auth proxy redirected us to -- caching
// that under an /api/* key is what makes a re-login show a stale library).
function maybeCache(req, res) {
  try {
    if (res && res.ok && !res.redirected && new URL(res.url).origin === location.origin) {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
    }
  } catch (_) {}
  return res;
}

// Rebuild a Response from the offline store, or null if this URL wasn't saved.
async function fromOffline(url) {
  try {
    const rec = await self.OfflineDB.getResponse(url.pathname);
    if (!rec) return null;
    return new Response(rec.body, {
      status: 200,
      headers: { "Content-Type": rec.type || "application/octet-stream", "X-Offline": "1" },
    });
  } catch (_) { return null; }
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  if (url.pathname.startsWith("/api/")) {           // network-first
    e.respondWith((async () => {
      try {
        const res = await fetch(req);
        // A 202 means "not drawn yet". If we have the page saved offline, prefer
        // the saved image over the placeholder so a bake-in-progress book still
        // shows its saved pictures.
        if (res.status === 202) return (await fromOffline(url)) || res;
        return maybeCache(req, res);
      } catch (_) {
        return (await fromOffline(url)) || (await caches.match(req)) || Response.error();
      }
    })());
    return;
  }

  e.respondWith(                                     // stale-while-revalidate
    caches.match(req).then((cached) => {
      const net = fetch(req).then((res) => maybeCache(req, res)).catch(() => cached);
      return cached || net;
    })
  );
});
