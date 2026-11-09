// Long-press a word in the reader to save it to the book's vocabulary list.
//
//   Vocab.attach({ root, bookId, getPage })
//
// root is the element holding the book text, getPage() returns the current
// global page index (or null). Both readers share this file.
//
// The word under the finger is found with the caret-from-point APIs rather than
// by wrapping every word in a <span>: the text is laid out in CSS columns and
// re-paginated on resize, and thousands of extra nodes would make that slower
// for no gain. The press highlights the word with an overlay, never by editing
// the text, for the same reason.
//
// The readers turn pages on tap, so the click that can follow a long press is
// swallowed here (capture phase, before their handlers see it). Text selection
// and the iOS callout are turned off inside root, or the browser's own
// long-press (select + magnifier) would fight this one.
(function () {
  const HOLD_MS = 500;
  const SLOP_PX = 10;
  const WORD_CH = /[\p{L}\p{M}\p{N}'’\-]/u;

  function caretAt(x, y) {
    if (document.caretPositionFromPoint) {
      const p = document.caretPositionFromPoint(x, y);
      return p && { node: p.offsetNode, off: p.offset };
    }
    if (document.caretRangeFromPoint) {
      const r = document.caretRangeFromPoint(x, y);
      return r && { node: r.startContainer, off: r.startOffset };
    }
    return null;
  }

  // The word around (x, y): its text node, start/end offsets and screen rect,
  // or null when the press wasn't really on a word. The caret APIs snap to the
  // nearest text even from a margin, so the word's own box must contain the
  // point.
  function wordAt(root, x, y) {
    const c = caretAt(x, y);
    if (!c || !c.node || c.node.nodeType !== Node.TEXT_NODE || !root.contains(c.node)) return null;
    const s = c.node.data;
    let off = c.off;
    if (!WORD_CH.test(s[off] || "") && WORD_CH.test(s[off - 1] || "")) off--;   // caret just past the word
    if (!WORD_CH.test(s[off] || "")) return null;
    let a = off, b = off + 1;
    while (a > 0 && WORD_CH.test(s[a - 1])) a--;
    while (b < s.length && WORD_CH.test(s[b])) b++;
    const range = document.createRange();
    range.setStart(c.node, a);
    range.setEnd(c.node, b);
    const rect = [...range.getClientRects()].find(r =>
      x >= r.left - 4 && x <= r.right + 4 && y >= r.top - 4 && y <= r.bottom + 4);
    if (!rect) return null;
    const word = s.slice(a, b).replace(/^['’\-]+|['’\-]+$/g, "");
    if (!/\p{L}/u.test(word)) return null;
    return { word, node: c.node, start: a, rect };
  }

  // The sentence the word sits in, from its paragraph-level block.
  function sentenceOf(root, node, start) {
    const block = node.parentElement.closest("p,li,blockquote,h1,h2,h3,h4,h5,h6") || root;
    const walk = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
    let full = "", at = -1;
    for (let n; (n = walk.nextNode());) {
      if (n === node) at = full.length + start;
      full += n.data;
    }
    if (at < 0) return "";
    const re = /[^.!?]*(?:[.!?]+["'”’)\]]*|$)\s*/g;
    for (let m; (m = re.exec(full)) && m[0];) {
      if (at < m.index + m[0].length) return m[0].replace(/\s+/g, " ").trim().slice(0, 400);
    }
    return full.replace(/\s+/g, " ").trim().slice(0, 400);
  }

  const css = document.createElement("style");
  css.textContent = `
    .vocab-root { -webkit-user-select:none; user-select:none; -webkit-touch-callout:none; }
    #vocabMark { position:fixed; z-index:45; pointer-events:none; border-radius:4px;
      background:rgba(240,196,90,.55); transition:opacity .6s ease .5s; }
    #vocabToast { position:fixed; left:50%; bottom:44px; transform:translateX(-50%); z-index:55;
      display:flex; align-items:center; gap:14px; max-width:92vw;
      background:rgba(43,38,34,.92); color:#fffdf8; font-family:system-ui,sans-serif;
      font-size:.9rem; padding:9px 10px 9px 16px; border-radius:999px;
      box-shadow:0 6px 20px rgba(0,0,0,.25); }
    #vocabToast[hidden] { display:none; }
    #vocabToast b { font-family:Georgia,serif; font-size:1rem; }
    #vocabToast button { font:inherit; font-size:.85rem; color:#f0c45a; background:none;
      border:1px solid rgba(240,196,90,.5); border-radius:999px; padding:5px 12px; cursor:pointer; }
    #vocabToast button[hidden] { display:none; }`;
  document.head.appendChild(css);

  let toast, toastT;
  function showToast(html, undo) {
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "vocabToast";
      toast.hidden = true;
      toast.innerHTML = `<span></span><button type="button">Undo</button>`;
      // keep taps on the toast from reaching the reader's page-turn handlers
      for (const ev of ["click", "touchstart", "touchend", "pointerdown"])
        toast.addEventListener(ev, e => e.stopPropagation());
      document.body.appendChild(toast);
    }
    toast.firstElementChild.innerHTML = html;
    const btn = toast.lastElementChild;
    btn.hidden = !undo;
    btn.onclick = undo ? () => { undo(); toast.hidden = true; } : null;
    toast.hidden = false;
    clearTimeout(toastT);
    toastT = setTimeout(() => { toast.hidden = true; }, undo ? 4500 : 2500);
  }

  function mark(rect) {
    const m = document.createElement("div");
    m.id = "vocabMark";
    Object.assign(m.style, { left: rect.left - 3 + "px", top: rect.top - 1 + "px",
      width: rect.width + 6 + "px", height: rect.height + 2 + "px" });
    document.getElementById("vocabMark")?.remove();
    document.body.appendChild(m);
    requestAnimationFrame(() => requestAnimationFrame(() => { m.style.opacity = "0"; }));
    setTimeout(() => m.remove(), 1400);
  }

  const esc = s => s.replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  async function save(opts, hit) {
    const body = { word: hit.word, context: sentenceOf(opts.root, hit.node, hit.start),
                   page: opts.getPage ? opts.getPage() : null };
    let r, row;
    try {
      r = await fetch(`/api/books/${opts.bookId}/vocab`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (window.Auth && Auth.bounced(r)) return;   // expired session -> sign-in overlay
      if (!r.ok) throw new Error("HTTP " + r.status);
      row = await r.json();
    } catch (e) {
      showToast(navigator.onLine === false ? "Offline — couldn’t save that word"
                                           : "Couldn’t save that word");
      return;
    }
    if (!row.new) { showToast(`<b>${esc(row.word)}</b> is already on the list`); return; }
    showToast(`Saved <b>${esc(row.word)}</b>`, () =>
      fetch(`/api/vocab/${row.id}`, { method: "DELETE" }).catch(() => {}));
  }

  function attach(opts) {
    const root = opts.root;
    root.classList.add("vocab-root");
    let timer = null, sx = 0, sy = 0, fired = false, swallowUntil = 0;
    const cancel = () => { clearTimeout(timer); timer = null; };
    // a long press just ended: eat the click the browser may send after it
    const release = () => {
      cancel();
      if (fired) { fired = false; swallowUntil = Date.now() + 600; }
    };

    root.addEventListener("pointerdown", e => {
      if (!e.isPrimary || e.button > 0 || e.target.closest("figure,img")) return;
      cancel();
      sx = e.clientX; sy = e.clientY;
      timer = setTimeout(() => {
        timer = null;
        const hit = wordAt(root, sx, sy);
        if (!hit) return;
        fired = true;
        try { navigator.vibrate && navigator.vibrate(15); } catch (_) {}
        mark(hit.rect);
        save(opts, hit);
      }, HOLD_MS);
    });
    root.addEventListener("pointermove", e => {
      if (timer && Math.hypot(e.clientX - sx, e.clientY - sy) > SLOP_PX) cancel();
    });
    for (const ev of ["pointerup", "pointercancel"]) root.addEventListener(ev, release);
    root.addEventListener("pointerleave", cancel);
    // Android raises a context menu on long press; that is ours now.
    root.addEventListener("contextmenu", e => e.preventDefault());
    // the tap that ends a long press must not also turn the page
    window.addEventListener("click", e => {
      if (Date.now() < swallowUntil) {
        swallowUntil = 0;
        e.stopPropagation(); e.preventDefault();
      }
    }, true);
  }

  window.Vocab = { attach };
})();
