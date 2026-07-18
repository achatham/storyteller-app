// Service worker. Two strategies:
//  - /api/*  : NETWORK-FIRST (data must be fresh; fall back to cache when offline).
//  - shell   : STALE-WHILE-REVALIDATE (HTML/JS/CSS/icons) -- serve the cached copy
//              instantly so reloads are fast even on a slow/flaky connection, and
//              refresh the cache in the background for next time. (Use the in-app
//              "Check for updates" to jump straight to a new build.)
const CACHE = "storyteller-v10";

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

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  if (url.pathname.startsWith("/api/")) {           // network-first
    e.respondWith(
      fetch(req).then((res) => maybeCache(req, res)).catch(() => caches.match(req))
    );
    return;
  }

  e.respondWith(                                     // stale-while-revalidate
    caches.match(req).then((cached) => {
      const net = fetch(req).then((res) => maybeCache(req, res)).catch(() => cached);
      return cached || net;
    })
  );
});
