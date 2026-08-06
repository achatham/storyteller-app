// Page-side offline library: download a whole book (text + every picture) into
// IndexedDB so it reads with no network, and manage/remove those saved copies.
// Requires offline-idb.js to be loaded first (provides OfflineDB).
//
// The download stores raw /api/* responses keyed by pathname; the service worker
// (sw.js) serves them back when offline, so neither reader needs offline-specific
// code -- a saved book's fetches simply succeed against IndexedDB.
(function () {
  const sleep = (ms, signal) => new Promise((res, rej) => {
    const t = setTimeout(res, ms);
    if (signal) signal.addEventListener("abort", () => { clearTimeout(t); rej(new DOMException("aborted", "AbortError")); }, { once: true });
  });
  const aborted = (signal) => { if (signal && signal.aborted) throw new DOMException("aborted", "AbortError"); };

  function fmtSize(bytes) {
    if (!bytes) return "0 KB";
    if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  async function saveResponse(bookId, path, res) {
    const blob = await res.blob();
    await OfflineDB.putResponse({
      url: OfflineDB.keyFor(path),
      book: bookId,
      type: res.headers.get("Content-Type") || "application/octet-stream",
      body: blob,
      savedAt: Date.now(),
    });
    return blob;
  }

  // Fetch one page image, tolerating the 202 "still drawing" the server returns
  // for a page that hasn't been illustrated yet (~30-40s per page). Polls until
  // it's ready, then returns the bytes; returns null if the page can't be drawn.
  async function fetchImage(path, signal, maxTries) {
    maxTries = maxTries || 60;
    for (let t = 0; t < maxTries; t++) {
      aborted(signal);
      const bust = (path.includes("?") ? "&" : "?") + "dl=" + t;
      let r;
      try { r = await fetch(path + bust, { cache: "no-store", signal }); }
      catch (e) { if (e.name === "AbortError") throw e; return null; }
      // A redirected 200 is the auth proxy's login page, not an image. Saving it
      // as image/webp would store a broken picture; abort the whole download so
      // the user re-signs in and retries rather than silently baking in garbage.
      if (r.redirected) throw new Error("Please sign in first");
      if (r.status === 200) return await r.blob();
      if (r.status === 202) { await sleep(2000, signal); continue; }
      return null;   // 404/409/5xx -> skip this page rather than hang the whole download
    }
    return null;
  }

  // Both readers' HTML pages for one book. They're server-rendered per book (the
  // id and build are substituted in), so they can't live in the service worker's
  // generic shell precache -- and the SW wipes its whole cache on every version
  // bump, which used to leave a fully-saved book with no page left to open
  // ("site can't be reached" offline, with the whole book sitting in IndexedDB).
  // Storing them alongside the book ties their lifetime to it: removed with the
  // book, and never evicted by an app update.
  const shellPaths = (id) => ["/book/" + id, "/read/" + id];

  async function saveShells(id, signal) {
    for (const path of shellPaths(id)) {
      aborted(signal);
      try {
        const r = await fetch(path, { cache: "no-store", signal });
        if (r.ok && !r.redirected) await saveResponse(id, path, r);
      } catch (e) { if (e.name === "AbortError") throw e; }
    }
  }

  // One-line status for a saved book, shared by both readers and the hub so they
  // never drift. Text is saved for every page; only pictures can come up short, so
  // count those and flag any gap (a partial copy the user can re-save to repair).
  // Older manifests predate `imgCount` meaning "saved" -- treat a missing field as
  // complete rather than alarming on copies we can't retro-measure.
  function savedLabel(info) {
    if (!info) return "";
    const total = info.numPages || 0;
    const imgs = info.imgCount != null ? info.imgCount : total;
    const miss = info.missing != null ? info.missing : Math.max(0, total - imgs);
    let s = `${imgs}/${total} pictures · ${fmtSize(info.bytes || 0)}`;
    if (miss > 0) s += ` · ⚠ ${miss} missing — re-save`;
    return s;
  }

  const Offline = {
    fmtSize,
    savedLabel,
    async isDownloaded(id) { return !!(await OfflineDB.getBook(id)); },
    async info(id) { return OfflineDB.getBook(id); },
    async list() {
      const all = await OfflineDB.allBooks().catch(() => []);
      return (all || []).sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
    },
    // Backfill the reader HTML for a book saved before we stored it (and repair a
    // copy whose shell was lost). Cheap: two small documents, and only when
    // missing. No-ops offline -- the fetches just fail and are swallowed.
    async ensureShells(id) {
      try {
        if (await OfflineDB.getResponse(shellPaths(id)[0])) return;
        if (!(await OfflineDB.getBook(id))) return;
        await saveShells(id);
      } catch (_) {}
    },
    async remove(id) {
      await OfflineDB.delResponsesFor(id);
      await OfflineDB.delBook(id);
    },

    // Download book `id` in full. onProgress({phase, done, total, bytes}) is called
    // as it goes; pass an AbortSignal to allow cancelling. Throws on auth/load
    // failure or when cancelled (AbortError).
    async downloadBook(id, opts) {
      opts = opts || {};
      const onProgress = opts.onProgress || function () {};
      const signal = opts.signal;
      const base = "/api/books/" + id;

      onProgress({ phase: "text", done: 0, total: 0, bytes: 0 });

      const metaRes = await fetch(base, { cache: "no-store", signal });
      if (metaRes.redirected) throw new Error("Please sign in first");
      if (!metaRes.ok) throw new Error("Couldn’t load this book");
      const meta = await metaRes.clone().json();
      if (meta.status !== "ready" && meta.status !== "baking")
        throw new Error("Book isn’t ready to read yet");
      await saveResponse(id, base, metaRes);

      const seg = meta.seg_ver || 0;
      const total = meta.num_pages || 0;

      await saveShells(id, signal);

      // page text (classic reader) + per-chapter flows (paginated reader)
      aborted(signal);
      await saveResponse(id, base + "/pages", await fetch(base + "/pages", { cache: "no-store", signal }));
      for (const c of (meta.chapters || [])) {
        aborted(signal);
        await saveResponse(id, base + "/chapter/" + c.idx,
          await fetch(base + "/chapter/" + c.idx, { cache: "no-store", signal }));
      }

      // every page image. `done` counts pages attempted (drives the progress bar);
      // `saved` counts pages whose picture actually landed in IndexedDB. They only
      // diverge when a page is skipped (404/409/5xx or a 202 that never resolved) --
      // and recording `saved`, not `done`, is what lets the UI show a partial copy
      // honestly instead of a reassuring N/N.
      let done = 0, saved = 0, bytes = 0;
      onProgress({ phase: "images", done, saved, total, bytes });
      for (let idx = 0; idx < total; idx++) {
        aborted(signal);
        const blob = await fetchImage(base + "/pages/" + idx + "/image?v=" + seg, signal);
        if (blob) {
          await OfflineDB.putResponse({
            url: OfflineDB.keyFor(base + "/pages/" + idx + "/image"),
            book: id, type: "image/webp", body: blob, savedAt: Date.now(),
          });
          bytes += blob.size;
          saved++;
        }
        done++;
        onProgress({ phase: "images", done, saved, total, bytes });
      }

      await OfflineDB.putBook({
        id, title: meta.title || ("Book " + id), seg,
        numPages: total, imgCount: saved, missing: total - saved,
        bytes, savedAt: Date.now(),
      });
      // Ask the browser not to evict us under storage pressure (best effort).
      try { if (navigator.storage && navigator.storage.persist) navigator.storage.persist(); } catch (_) {}
      return { total, bytes };
    },
  };

  window.Offline = Offline;
})();
