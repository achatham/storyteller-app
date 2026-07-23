// Shared IndexedDB core for offline book storage. Loaded both in the page
// (`<script src>`) and in the service worker (`importScripts`), so it must run in
// either scope -- hence everything hangs off `self` and uses no DOM APIs.
//
// Two stores in one DB:
//   responses : one row per downloaded URL (keyed by pathname, query stripped),
//               holding the raw bytes as a Blob + its content type. The service
//               worker rebuilds a Response from these when the network is gone,
//               so a saved book's /api/* requests resolve exactly as they would
//               online -- no reader-side changes needed to read offline.
//   books     : one manifest row per saved book (title, page count, size, when),
//               so the hub can list saved books while offline and the reader can
//               show "saved / remove" state.
(function (scope) {
  const DB = "storyteller-offline";
  const VER = 1;
  let _db = null;

  function open() {
    if (_db) return Promise.resolve(_db);
    return new Promise((res, rej) => {
      const rq = indexedDB.open(DB, VER);
      rq.onupgradeneeded = () => {
        const db = rq.result;
        if (!db.objectStoreNames.contains("responses"))
          db.createObjectStore("responses", { keyPath: "url" });
        if (!db.objectStoreNames.contains("books"))
          db.createObjectStore("books", { keyPath: "id" });
      };
      rq.onsuccess = () => { _db = rq.result; res(_db); };
      rq.onerror = () => rej(rq.error);
    });
  }

  function store(name, mode) {
    return open().then((db) => db.transaction(name, mode).objectStore(name));
  }
  function done(r) {
    return new Promise((res, rej) => {
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
  }
  // Normalize any URL/path to the key we store under: same-origin pathname only.
  // Stripping the query means an image saved as .../image is still found when the
  // reader requests .../image?v=<segVer> (the version param only changes on a
  // re-segment, which is exactly when you'd re-download anyway).
  function keyFor(url) {
    try { return new URL(url, scope.location.origin).pathname; }
    catch (_) { return String(url).split("?")[0]; }
  }

  scope.OfflineDB = {
    open,
    keyFor,
    getResponse: (url) => store("responses", "readonly").then((s) => done(s.get(keyFor(url)))),
    putResponse: (rec) => store("responses", "readwrite").then((s) => done(s.put(rec))),
    getBook: (id) => store("books", "readonly").then((s) => done(s.get(id))),
    putBook: (rec) => store("books", "readwrite").then((s) => done(s.put(rec))),
    delBook: (id) => store("books", "readwrite").then((s) => done(s.delete(id))),
    allBooks: () => store("books", "readonly").then((s) => done(s.getAll())),
    // Delete every stored response belonging to one book (its meta, page text,
    // chapter flows and images) via a cursor over the book index we keep on each row.
    delResponsesFor: (id) =>
      store("responses", "readwrite").then((s) => new Promise((res, rej) => {
        const cur = s.openCursor();
        cur.onsuccess = () => {
          const c = cur.result;
          if (!c) return res();
          if (c.value && c.value.book === id) c.delete();
          c.continue();
        };
        cur.onerror = () => rej(cur.error);
      })),
  };
})(self);
