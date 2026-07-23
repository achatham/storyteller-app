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
      if (r.status === 200) return await r.blob();
      if (r.status === 202) { await sleep(2000, signal); continue; }
      return null;   // 404/409/5xx -> skip this page rather than hang the whole download
    }
    return null;
  }

  const Offline = {
    fmtSize,
    async isDownloaded(id) { return !!(await OfflineDB.getBook(id)); },
    async info(id) { return OfflineDB.getBook(id); },
    async list() {
      const all = await OfflineDB.allBooks().catch(() => []);
      return (all || []).sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
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

      // page text (classic reader) + per-chapter flows (paginated reader)
      aborted(signal);
      await saveResponse(id, base + "/pages", await fetch(base + "/pages", { cache: "no-store", signal }));
      for (const c of (meta.chapters || [])) {
        aborted(signal);
        await saveResponse(id, base + "/chapter/" + c.idx,
          await fetch(base + "/chapter/" + c.idx, { cache: "no-store", signal }));
      }

      // every page image
      let done = 0, bytes = 0;
      onProgress({ phase: "images", done, total, bytes });
      for (let idx = 0; idx < total; idx++) {
        aborted(signal);
        const blob = await fetchImage(base + "/pages/" + idx + "/image?v=" + seg, signal);
        if (blob) {
          await OfflineDB.putResponse({
            url: OfflineDB.keyFor(base + "/pages/" + idx + "/image"),
            book: id, type: "image/webp", body: blob, savedAt: Date.now(),
          });
          bytes += blob.size;
        }
        done++;
        onProgress({ phase: "images", done, total, bytes });
      }

      await OfflineDB.putBook({
        id, title: meta.title || ("Book " + id), seg,
        numPages: total, imgCount: done, bytes, savedAt: Date.now(),
      });
      // Ask the browser not to evict us under storage pressure (best effort).
      try { if (navigator.storage && navigator.storage.persist) navigator.storage.persist(); } catch (_) {}
      return { total, bytes };
    },
  };

  window.Offline = Offline;
})();
