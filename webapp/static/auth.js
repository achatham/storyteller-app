// Shared "session expired / couldn't load" overlay.
//
// Auth is enforced entirely by the reverse proxy (Caddy forward_auth ->
// oauth2-proxy -> Google). No fetch() can ever complete the handshake itself:
// the sign-in lives on auth.294page.net, a DIFFERENT origin, so the only cure
// is a top-level navigation. That is all this overlay is -- a button that
// performs one, and then lands back where the user was.
//
// The gate refuses an expired session in one of two shapes, and both have to be
// recognised or the app hangs:
//
//   401 + X-Auth-Login    What a fetch/XHR/service-worker request gets today.
//                         The header carries the login URL; the JSON body says
//                         the same thing for a human reading a log.
//   200 + r.redirected    The older shape, still what a client too old to send
//                         Sec-Fetch-* (Safari < 16.4) receives, since the gate
//                         only 401s requests it can prove are not navigations.
//
// Missing the 401 shape is what stranded the installed PWA: the app saw a bare
// failure, showed "couldn't reach the server", and its Retry called reload() --
// which the service worker answers from cache, so no request ever reached the
// network and the session could never be renewed. The only escape was opening
// the site in a real browser tab. Keep both branches.
//
// Usage:
//   const r = await fetch(url, {cache:"no-store"});
//   if (Auth.bounced(r)) return;          // expired session -> overlay shown
//   ...                                    // (still handle !r.ok / bad body)
//   catch (e) { Auth.fail(); }             // load failed -> offline overlay
(function () {
  let up = false;

  function injectStyle() {
    if (document.getElementById("auth-overlay-style")) return;
    const s = document.createElement("style");
    s.id = "auth-overlay-style";
    s.textContent =
      "#auth-overlay{position:fixed;inset:0;z-index:2147483647;display:flex;" +
      "align-items:center;justify-content:center;padding:24px;" +
      "background:rgba(0,0,0,.5);font-family:system-ui,sans-serif}" +
      "#auth-overlay .auth-card{background:var(--panel,#fffdf8);color:var(--ink,#2b2622);" +
      "max-width:340px;width:100%;padding:26px 24px;border-radius:16px;text-align:center;" +
      "box-shadow:0 16px 48px rgba(0,0,0,.28)}" +
      "#auth-overlay .auth-title{font-size:1.15rem;font-weight:600;margin:0 0 8px}" +
      "#auth-overlay .auth-msg{color:var(--muted,#9a8a70);font-size:.95rem;" +
      "line-height:1.45;margin:0 0 20px}" +
      "#auth-overlay .auth-btn{display:block;width:100%;box-sizing:border-box;" +
      "background:var(--accent,#7a5c3e);color:#fff;border:0;border-radius:10px;" +
      "padding:12px 20px;font-size:1rem;font-weight:600;cursor:pointer}";
    document.head.appendChild(s);
  }

  // loginUrl: captured portal URL (navigate straight into OAuth) or null
  //           (offline / unknown -> reload the current gated page instead).
  // offline:  true when the fetch failed outright (connectivity, not auth).
  function show(loginUrl, offline) {
    if (up) return;
    up = true;
    injectStyle();
    const el = document.createElement("div");
    el.id = "auth-overlay";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.innerHTML =
      '<div class="auth-card">' +
        '<p class="auth-title">' +
          (offline ? "Couldn’t reach the server" : "Your session expired") +
        "</p>" +
        '<p class="auth-msg">' +
          (offline
            ? "Check your connection and try again."
            : "Sign in again to get back to your books.") +
        "</p>" +
        '<button class="auth-btn" id="auth-go" type="button">' +
          (offline ? "Retry" : "Sign in") +
        "</button>" +
      "</div>";
    document.body.appendChild(el);
    document.getElementById("auth-go").onclick = function () {
      if (loginUrl) location.href = loginUrl;
      else location.reload();
    };
  }

  // The service worker detects login bounces on requests the page can't guard --
  // above all <img> image loads -- and messages us here so the overlay still pops.
  try {
    navigator.serviceWorker?.addEventListener("message", function (e) {
      if (e.data && e.data.type === "auth-bounce") show(e.data.loginUrl || null, false);
    });
  } catch (_) {}

  // The gate emits X-Auth-Login on an /api/* request, so the `rd` it chose is
  // the site root -- it cannot know which book the user had open. Point it at
  // this page instead, so signing in resumes reading rather than dumping
  // everyone back at the library.
  function withReturn(loginUrl) {
    try {
      const u = new URL(loginUrl, location.origin);
      u.searchParams.set("rd", location.href);
      return u.toString();
    } catch (_) {
      return loginUrl;
    }
  }

  // The login URL for an expired-session response, or null if this response is
  // not one. Deliberately synchronous and body-free: callers check it inline on
  // a Response they still intend to read, and the service worker checks it on
  // responses it must pass through untouched.
  function loginUrlFor(r) {
    if (!r) return null;
    // Cross-origin redirect the fetch could not follow into OAuth.
    if (r.redirected) return r.url;
    if (r.status !== 401 && r.status !== 403) return null;
    try {
      const h = r.headers && r.headers.get && r.headers.get("X-Auth-Login");
      return h ? withReturn(h) : null;
    } catch (_) {
      return null;
    }
  }

  window.Auth = {
    // Call immediately after a fetch(). Returns true (and shows the overlay)
    // when the response is the gate refusing an expired session.
    bounced: function (r) {
      const login = loginUrlFor(r);
      if (login) { show(login, false); return true; }
      return false;
    },
    // Same test without the overlay, for callers that only want to know.
    loginUrl: loginUrlFor,
    // Call from a catch block (or a bad-body branch) to surface the overlay.
    // Pass {offline:true} for a connectivity failure vs. an auth failure.
    fail: function (opts) {
      opts = opts || {};
      show(opts.loginUrl || null, !!opts.offline);
    },
  };
})();
