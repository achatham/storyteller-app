"""Build a book's illustrated EPUB, illustrating any missing pages first.

    python -m webapp.make_epub <book_id>

Runs as its own detached subprocess (launched by the server), because it may
have to wait on a whole-book Batch-API bake (~1-2h) before it can build. Flow:

    1. find pages with no stored image
    2. if any -> run the batch bake (gap-fill only; already-illustrated pages are
       left untouched) and BLOCK until it finishes -- reusing a bake already in
       flight rather than starting a second one
    3. build the EPUB (webapp/epub.py) and write it to db.epub_path(book_id)

Progress is tracked in the epub_jobs table so the settings UI can poll it and,
when ready, offer the finished file for download.
"""
import sys
import time
import traceback

from . import batch_bake, db, epub


def _missing_pages(book_id) -> list[int]:
    have = {idx for idx, data in db.iter_scene_blobs(book_id) if data}
    return [p["idx"] for p in db.get_pages(book_id) if p["idx"] not in have]


def _ensure_illustrated(book_id):
    """Draw any pages that lack an image, blocking until done. Reuses a bake that is
    already running instead of launching a competing one."""
    if not _missing_pages(book_id):
        return
    bake = db.bake_get(book_id)
    if bake and bake["status"] == "baking":
        db.epub_upsert(book_id, "baking", detail="waiting for the running bake…")
        while (db.bake_get(book_id) or {}).get("status") == "baking":
            time.sleep(15)
    else:
        # seed a fresh bake exactly like the /bake endpoint, then run it inline.
        if bake and bake["status"] != "baking":
            db.bake_clear(book_id)
        db.set_illustration_mode(book_id, "batch")
        db.bake_upsert(book_id, "baking", round=0,
                       total_pages=db.get_book(book_id)["num_pages"])
        db.set_status(book_id, "baking", "illustrating the whole book for EPUB…")
        db.epub_upsert(book_id, "baking", detail="illustrating missing pages…")
        batch_bake.run(book_id)   # blocks until the whole-book bake finishes


def build(book_id):
    _ensure_illustrated(book_id)
    db.epub_upsert(book_id, "building", detail="assembling EPUB…")
    res = epub.build_epub(book_id)
    db.EPUB_DIR.mkdir(parents=True, exist_ok=True)
    path = db.epub_path(book_id)
    path.write_bytes(res["data"])
    miss = len(res["pages_missing_image"])
    detail = f"{res['images']} illustrations, {res['chapters']} chapters"
    if miss:
        detail += f" ({miss} pages could not be illustrated)"
    db.epub_upsert(book_id, "ready", detail=detail, size=len(res["data"]))
    print(f"[make_epub] book {book_id}: {path} "
          f"{len(res['data']) / 1e6:.1f}MB, {res['images']} images, {miss} missing",
          flush=True)


def main():
    book_id = int(sys.argv[1])
    try:
        build(book_id)
    except Exception as ex:   # noqa: BLE001
        traceback.print_exc()
        db.epub_upsert(book_id, "failed", detail=f"{type(ex).__name__}: {str(ex)[:200]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
