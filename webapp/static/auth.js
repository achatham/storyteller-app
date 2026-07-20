// Shared "session expired / couldn't load" overlay.
//
// Auth is enforced entirely by the reverse proxy (caddy-security + Google
// OAuth). When a session expires the proxy 302s an /api/* XHR to its
// same-origin login portal; fetch follows that transparently, so the client
// sees r.redirected === true with r.url = the portal URL. An XHR can never
// carry that 302 into the OAuth handshake, so we surface an overlay whose
// button does a top-level navigation to the captured login URL (which then
// bounces to Google and back to where the user was).
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

  window.Auth = {
    // Call immediately after a fetch(). Returns true (and shows the overlay)
    // when the response is an expired-session bounce to the login portal.
    bounced: function (r) {
      if (r && r.redirected) { show(r.url, false); return true; }
      return false;
    },
    // Call from a catch block (or a bad-body branch) to surface the overlay.
    // Pass {offline:true} for a connectivity failure vs. an auth failure.
    fail: function (opts) {
      opts = opts || {};
      show(opts.loginUrl || null, !!opts.offline);
    },
  };
})();
