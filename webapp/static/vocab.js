// Long-press a word in the reader to save it to the book's vocabulary list.
//
//   Vocab.attach({ root, bookId, getPage })
//
// root is the element holding the book text, getPage() returns the current
// global page index (or null). Both readers share this file.
//
// After a save the toast offers a child-friendly definition of the word behind a
// tap (/api/vocab/{id}/definition, generated once and cached server-side), so it
// can be read out on the spot. Long-pressing a word already on the list offers it too.
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
//
// Words already on the book's list are marked wherever they appear in the text.
// That does wrap text in <mark>s, but only the few saved words, not every word.
// A MutationObserver re-marks whenever the reader swaps in a new page or chapter,
// so the readers don't need to call anything after they render.
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
      display:flex; align-items:center; gap:14px; width:max-content; max-width:min(560px, 92vw);
      background:rgba(43,38,34,.94); color:#fffdf8; font-family:system-ui,sans-serif;
      font-size:.9rem; padding:10px 10px 10px 16px; border-radius:16px;
      box-shadow:0 6px 20px rgba(0,0,0,.25); }
    #vocabToast[hidden] { display:none; }
    #vocabToast .txt { flex:1; min-width:0; }
    #vocabToast b { font-family:Georgia,serif; font-size:1.05rem; }
    #vocabToast i { color:#c9b896; font-size:.8rem; margin-left:4px; }
    #vocabToast .def { font-family:Georgia,serif; font-size:1rem; line-height:1.4; margin-top:4px; }
    #vocabToast .def:empty { display:none; }
    #vocabToast .def.wait { color:#c9b896; font-family:system-ui,sans-serif; font-size:.85rem; }
    #vocabToast button { font:inherit; font-size:.85rem; color:#f0c45a; background:none; align-self:center;
      border:1px solid rgba(240,196,90,.5); border-radius:999px; padding:5px 12px; cursor:pointer; }
    #vocabToast button[hidden] { display:none; }
    mark.vocab-word { background:rgba(240,196,90,.3); color:inherit; border-radius:3px;
      box-shadow:0 1px 0 rgba(200,150,40,.55); }`;
  document.head.appendChild(css);

  // The toast says what happened and offers the word's meaning behind a tap (so
  // the child gets a chance to guess first). Each showing gets a token so a
  // definition that comes back late can't land in a newer toast. Tapping the
  // toast (outside Undo) reveals the meaning if one is on offer, else dismisses.
  let toast, toastT, toastTok = 0, reveal = null;
  function showToast(html, undo, ms) {
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "vocabToast";
      toast.hidden = true;
      toast.innerHTML = `<div class="txt"><div class="head"></div><div class="def"></div></div>` +
                        `<button type="button">Undo</button>`;
      // keep taps on the toast from reaching the reader's page-turn handlers
      for (const ev of ["click", "touchstart", "touchend", "pointerdown"])
        toast.addEventListener(ev, e => e.stopPropagation());
      toast.addEventListener("click", e => {
        if (e.target.closest("button")) return;
        if (reveal) { const r = reveal; reveal = null; r(); } else toast.hidden = true;
      });
      document.body.appendChild(toast);
    }
    toast.querySelector(".head").innerHTML = html;
    const def = toast.querySelector(".def");
    def.textContent = ""; def.className = "def";
    reveal = null;
    const btn = toast.querySelector("button");
    btn.hidden = !undo;
    btn.onclick = undo ? () => { undo(); toast.hidden = true; } : null;
    toast.hidden = false;
    holdToast(ms || (undo ? 4500 : 2500));
    return ++toastTok;
  }
  function holdToast(ms) {
    clearTimeout(toastT);
    toastT = setTimeout(() => { toast.hidden = true; }, ms);
  }

  // Offer the word's definition in toast `tok`: fetched straight away so the
  // reveal is instant, shown only when the toast is tapped.
  function offerDefinition(id, tok) {
    const def = toast.querySelector(".def");
    const got = fetch(`/api/vocab/${id}/definition`)
      .then(r => r.ok ? r.json() : null).catch(() => null);
    def.className = "def wait"; def.textContent = "Tap to see what it means";
    holdToast(7000);
    reveal = async () => {
      def.textContent = "Looking it up…";
      holdToast(12000);
      const d = await got;
      if (tok !== toastTok || toast.hidden) return;
      if (!d || !d.definition) { def.textContent = "Couldn’t look that one up just now"; holdToast(2500); return; }
      def.className = "def";
      def.textContent = d.definition;
      if (d.part_of_speech) toast.querySelector(".head").insertAdjacentHTML("beforeend", `<i>${esc(d.part_of_speech)}</i>`);
      // long enough to read aloud: ~4s plus a beat per word
      holdToast(4000 + 350 * d.definition.split(/\s+/).length);
    };
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

  // ---- marking saved words in the text ----
  const saved = new Set();
  let markRoot = null, observer = null;

  // One regex for the whole list, longest words first so "good-humoured" wins over
  // "good". Words are stored lowercase without a possessive, so the possessive is
  // matched here and an edge can't be a word character, apostrophe or hyphen.
  function savedRe() {
    if (!saved.size) return null;
    const alts = [...saved].sort((a, b) => b.length - a.length)
      .map(w => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    return new RegExp(`(?<![\\p{L}\\p{M}\\p{N}'’\\-])(?:${alts.join("|")})(?:['’]s)?` +
                      `(?![\\p{L}\\p{M}\\p{N}'’\\-])`, "giu");
  }

  function markSaved() {
    const root = markRoot;
    if (!root) return;
    observer.disconnect();   // our own edits mustn't re-trigger us
    for (const m of root.querySelectorAll("mark.vocab-word")) {
      const p = m.parentNode;
      p.replaceChild(document.createTextNode(m.textContent), m);
      p.normalize();
    }
    const re = savedRe();
    if (re) {
      const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      const nodes = [];
      for (let n; (n = walk.nextNode());) if (n.data.trim()) nodes.push(n);
      for (const n of nodes) {
        re.lastIndex = 0;
        let m, last = 0, frag = null;
        while ((m = re.exec(n.data))) {
          frag = frag || document.createDocumentFragment();
          frag.append(n.data.slice(last, m.index));
          const el = document.createElement("mark");
          el.className = "vocab-word";
          el.textContent = m[0];
          frag.append(el);
          last = m.index + m[0].length;
        }
        if (frag) { frag.append(n.data.slice(last)); n.replaceWith(frag); }
      }
    }
    observer.observe(root, { childList: true, subtree: true });
  }

  function watch(root, bookId) {
    markRoot = root;
    let queued = false;
    // a microtask, so marks land before the reader's next paint/paginate
    observer = new MutationObserver(() => {
      if (queued) return;
      queued = true;
      queueMicrotask(() => { queued = false; markSaved(); });
    });
    observer.observe(root, { childList: true, subtree: true });
    fetch(`/api/vocab?book_id=${bookId}`).then(r => r.ok ? r.json() : [])
      .then(rows => { for (const w of rows) saved.add(w.word); markSaved(); })
      .catch(() => {});
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
    if (!row.new) {
      offerDefinition(row.id, showToast(`<b>${esc(row.word)}</b> <span style="opacity:.7">· on the list</span>`));
      return;
    }
    saved.add(row.word);
    markSaved();
    const tok = showToast(`Saved <b>${esc(row.word)}</b>`, () => {
      saved.delete(row.word);
      markSaved();
      fetch(`/api/vocab/${row.id}`, { method: "DELETE" }).catch(() => {});
    });
    offerDefinition(row.id, tok);
  }

  function attach(opts) {
    const root = opts.root;
    root.classList.add("vocab-root");
    watch(root, opts.bookId);
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
