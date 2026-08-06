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

const CACHE = "storyteller-v17";   // bumped for the cover art on the library page

// The app shell, fetched at install time. Without this the cache only ever held
// what happened to be requested while a previous worker was already in control --
// and since activate() wipes every older cache, a version bump left the app with
// NOTHING cached until the next online visit. Going offline in that window got you
// the browser's "site can't be reached" page even with books saved in IndexedDB.
// Precaching makes the hub survive an update on its own; a saved book's own reader
// HTML is stored in IndexedDB by the download (offline.js), which is never wiped.
const SHELL = [
  "/",
  "/static/auth.js",
  "/static/offline-idb.js",
  "/static/offline.js",
  "/static/manifest.webmanifest",
  "/static/icon-192.png",
  "/static/icon-512.png",
];

// Last-resort page for a navigation we have neither cached nor saved, so an
// offline tap lands somewhere with a way back to the (precached) library.
const OFFLINE_PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Storyteller — offline</title><style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
 background:#f4efe6;color:#2b2622;font-family:Georgia,'Times New Roman',serif;padding:24px}
div{max-width:340px;text-align:center}h1{font-size:1.4rem;margin:0 0 10px}
p{color:#9a8a70;font-family:system-ui,sans-serif;font-size:.95rem;line-height:1.45;margin:0 0 20px}
a{display:inline-block;background:#7a5c3e;color:#fff;text-decoration:none;font-family:system-ui,sans-serif;
 padding:12px 22px;border-radius:10px;font-weight:600}</style></head>
<body><div><h1>📴 You're offline</h1>
<p>This page isn't saved on this device. Books you saved for offline reading are still available.</p>
<a href="/">Go to your library</a></div></body></html>`;

// Fill the cache up front. Skips anything the auth proxy bounced to its login
// portal -- caching that would pin a sign-in page under "/" forever.
async function precache() {
  const c = await caches.open(CACHE);
  await Promise.all(SHELL.map(async (path) => {
    try {
      const res = await fetch(path, { cache: "reload", credentials: "same-origin" });
      if (res.ok && !res.redirected) await c.put(path, res);
    } catch (_) {}
  }));
}

self.addEventListener("install", (e) =>
  e.waitUntil(precache().then(() => self.skipWaiting(), () => self.skipWaiting())));
self.addEventListener("activate", (e) => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (k !== CACHE) await caches.delete(k);
  await self.clients.claim();
})()));

// Cache only successful, same-origin, non-redirected responses (never a 202
// "generating", never a login page the auth proxy redirected us to -- caching
// that under an /api/* key is what makes a re-login show a stale library).
// The library listing is deliberately never cached: a stale copy served offline
// makes the hub render every book on the server as if it were readable, hiding
// the saved-books view and handing the user cards that die when tapped. Letting
// the request fail is what lets hub.html fall back to the offline library.
//
// Cache-busted requests are skipped too. `dl=` (the offline download), `r=`
// (image retry / manual refresh) are single-use by construction, so an entry
// stored under one can never be matched again -- the reader asks for
// .../image?v=<seg>, never .../image?v=<seg>&dl=3. Caching them wrote a second,
// unreachable copy of every picture: a 21 MB book cost 43 MB of quota, 21 MB of
// it dead weight. That matters beyond disk, since browsers evict per origin
// under pressure and can take the saved books' IndexedDB with it.
function cacheable(req) {
  const u = new URL(req.url);
  if (u.pathname === "/api/books") return false;
  return !u.searchParams.has("dl") && !u.searchParams.has("r");
}

function maybeCache(req, res) {
  try {
    if (res && res.ok && !res.redirected && cacheable(req) &&
        new URL(res.url).origin === location.origin) {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
    }
  } catch (_) {}
  return res;
}

// Tell every open page that an /api request bounced to the login portal, so the
// shared auth overlay pops immediately -- crucial for <img> loads, which aren't
// guarded fetches and so can never surface the sign-in prompt on their own.
async function notifyAuthBounce(loginUrl) {
  try {
    const cs = await self.clients.matchAll({ includeUncontrolled: true, type: "window" });
    for (const c of cs) c.postMessage({ type: "auth-bounce", loginUrl });
  } catch (_) {}
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
        // An expired session makes the auth proxy 302 this /api request to its
        // login portal; fetch follows it, so we get a 200 login page with
        // res.redirected === true (never a real /api body). Treat it like being
        // offline: serve the saved copy so a downloaded book keeps reading, and
        // ping open pages to raise the sign-in overlay (images can't do that
        // themselves -- they're <img> loads, not Auth.bounced fetches).
        if (res.redirected) {
          notifyAuthBounce(res.url);
          return (await fromOffline(url)) || res;
        }
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

  e.respondWith((async () => {                       // stale-while-revalidate
    const cached = await caches.match(req);
    const net = fetch(req).then((res) => maybeCache(req, res));
    if (cached) { net.catch(() => {}); return cached; }
    try {
      return await net;
    } catch (_) {
      // Nothing cached and no network. A saved book's reader HTML lives in the
      // offline store (put there by the download), so serve that; otherwise give
      // navigations a real page instead of resolving to undefined, which is what
      // turned an uncached offline tap into the browser's network-error screen.
      return (await fromOffline(url)) ||
        (req.mode === "navigate"
          ? new Response(OFFLINE_PAGE, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } })
          : Response.error());
    }
  })());
});
