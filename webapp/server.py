"""FastAPI backend + reader UI for the storyteller.

  GET  /                          hub (upload / pick a book)
  GET  /read/{id}                 reader SPA
  GET  /api/styles                available art styles
  GET  /api/books                 list books (+ status + progress)
  POST /api/books                 upload a book -> kicks off processing subprocess
  GET  /api/books/{id}            book detail (status, chapters, progress)
  DELETE /api/books/{id}          remove a book
  GET  /api/books/{id}/pages      page text (no images)
  GET  /api/books/{id}/pages/{i}/image   scene image (generated lazily; prefetches ahead)
  GET  /api/books/{id}/pages/{i}/status  scene generation status
  PUT  /api/books/{id}/progress   save reading position (server-side)

Scene images are generated on demand and prefetched N pages ahead (PREFETCH).
Concurrent generations are capped by a semaphore; duplicate work is coalesced by
a per-(book,page) lock.
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from pipeline.config import STYLES
from . import db, scene

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
WORK = Path(os.environ.get("STORY_WORK", str(ROOT / "output" / "work")))
LOGS = Path(os.environ.get("STORY_LOGS", str(ROOT / "output" / "logs")))
PREFETCH = int(os.environ.get("STORY_PREFETCH", "2"))
GEN_CONCURRENCY = int(os.environ.get("STORY_GEN_CONCURRENCY", "3"))

app = FastAPI(title="Storyteller")

_sem = asyncio.Semaphore(GEN_CONCURRENCY)
_locks: dict[tuple[int, int], asyncio.Lock] = {}


@app.on_event("startup")
def _startup():
    db.init()
    for d in (WORK, LOGS):
        d.mkdir(parents=True, exist_ok=True)
    # a generation interrupted by a restart leaves a 'generating' row with no
    # image; clear those so the page can be drawn again (instead of being skipped)
    n = db.reset_generating()
    if n:
        print(f"[server] reset {n} interrupted scene generation(s)", flush=True)
    # resume any import that was interrupted by a restart/crash. start_processing
    # re-runs webapp.process, which reuses the saved registry + roster sheets and
    # only redoes the cheap remaining work.
    for bid in db.books_in_progress():
        db.set_status(bid, "queued", "resuming after restart…")
        start_processing(bid)
        print(f"[server] resumed interrupted processing for book {bid}", flush=True)


# ---------------- lazy scene generation ----------------

def _lock_for(book_id, idx) -> asyncio.Lock:
    key = (book_id, idx)
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


async def ensure_scene(book_id: int, idx: int) -> bytes:
    """Return page `idx`'s image bytes, generating it once if needed. Concurrent
    callers for the same page share a single generation."""
    data = await asyncio.to_thread(db.scene_data, book_id, idx)
    if data:
        return data
    async with _lock_for(book_id, idx):
        data = await asyncio.to_thread(db.scene_data, book_id, idx)
        if data:
            return data
        await asyncio.to_thread(db.scene_set_status, book_id, idx, "generating")
        try:
            async with _sem:
                return await asyncio.to_thread(scene.generate_scene, book_id, idx)
        except Exception as ex:  # noqa: BLE001
            await asyncio.to_thread(db.scene_set_status, book_id, idx, "failed",
                                    f"{type(ex).__name__}: {str(ex)[:200]}")
            raise


async def _safe_ensure(book_id, idx):
    try:
        await ensure_scene(book_id, idx)
    except Exception:  # noqa: BLE001 -- prefetch is best-effort
        pass


def schedule_prefetch(book_id: int, start: int, n: int, total: int):
    """Fire-and-forget generation of the next `n` not-yet-started pages."""
    for i in range(start, min(start + n, total)):
        if db.scene_status(book_id, i) is None:
            asyncio.create_task(_safe_ensure(book_id, i))


# ---------------- processing ----------------

def start_processing(book_id: int):
    book = db.get_book(book_id)
    f = db.get_book_file(book_id)
    if not book or not f:
        return
    _mime, data = f
    workdir = WORK / str(book_id)
    workdir.mkdir(parents=True, exist_ok=True)
    suffix = Path(book["filename"] or "book.pdf").suffix or ".pdf"
    src = workdir / f"source{suffix}"
    src.write_bytes(data)

    env = dict(os.environ)
    env.update({
        "STORY_PDF": str(src),
        "STORY_BOOK": book["title"] or "",
        "STORY_AUTHOR": book["author"] or "",
        "STORY_LABEL": "",   # don't leak the Ender-era default section label
        "STORY_STYLE": book["style"],
        "STORY_WORDS_PER_PAGE": str(book["words_per_page"]),
        "STORY_AGE": str(book["age"] or "5"),
        "STORY_BODY": "1,0",
        "STORY_OUT": str(workdir),
        "STORY_REGISTRY": str(workdir / "registry.json"),
        "STORY_ASSETS": str(workdir / "sheets"),
        "STORY_APP_DB": str(db.DB),
    })
    logf = open(LOGS / f"book_{book_id}.log", "ab")
    subprocess.Popen([sys.executable, "-m", "webapp.process", str(book_id)],
                     cwd=str(ROOT), env=env, stdout=logf, stderr=logf)


# ---------------- pages (UI) ----------------

@app.get("/", response_class=HTMLResponse)
def hub():
    return (STATIC / "hub.html").read_text()


@app.get("/read/{book_id}", response_class=HTMLResponse)
def reader(book_id: int):
    html = (STATIC / "reader.html").read_text()
    return html.replace("__BOOK_ID__", str(book_id))


# ---------------- API ----------------

@app.get("/api/styles")
def api_styles():
    have = db.styles_with_samples()
    return [{"key": k, "label": k.replace("_", " "), "sample_ready": k in have}
            for k in STYLES]


@app.get("/api/styles/{key}/sample")
async def api_style_sample(key: str):
    if key not in STYLES:
        raise HTTPException(404, "unknown style")
    data = await asyncio.to_thread(db.get_style_sample, key)
    if not data:
        try:
            async with _sem:
                data = await asyncio.to_thread(scene.generate_style_sample, key)
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(503, f"sample generation failed: {str(ex)[:160]}")
    if not data:
        raise HTTPException(404, "no sample")
    return Response(content=data, media_type="image/webp",
                    headers={"Cache-Control": "public, max-age=31536000"})


@app.get("/api/books")
def api_books():
    out = []
    for b in db.list_books():
        sp = db.scene_progress(b["id"])
        out.append({
            "id": b["id"], "title": b["title"] or b["filename"] or "Untitled",
            "author": b["author"], "style": b["style"], "status": b["status"],
            "detail": b["detail"], "num_pages": b["num_pages"],
            "position": b["position"], "scenes_done": sp.get("done", 0),
        })
    return out


@app.post("/api/books")
async def api_upload(file: UploadFile = File(...), title: str = Form(""),
                     author: str = Form(""), style: str = Form("watercolor"),
                     words_per_page: int = Form(200), age: str = Form("5")):
    if style not in STYLES:
        raise HTTPException(400, f"unknown style {style!r}")
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    book_id = db.create_book(title.strip(), author.strip(), file.filename, style,
                             words_per_page, age, file.content_type or "application/octet-stream",
                             data)
    await asyncio.to_thread(start_processing, book_id)
    return {"id": book_id}


@app.get("/api/books/{book_id}")
def api_book(book_id: int):
    b = db.get_book(book_id)
    if not b:
        raise HTTPException(404, "no such book")
    return {
        "id": b["id"], "title": b["title"] or b["filename"] or "Untitled",
        "author": b["author"], "style": b["style"], "status": b["status"],
        "detail": b["detail"], "num_pages": b["num_pages"],
        "seg_ver": b["seg_ver"] if "seg_ver" in b.keys() else 0,
        "position": db.get_progress(book_id),
        "chapters": db.get_chapters(book_id),
        "scenes_done": db.scene_progress(book_id).get("done", 0),
    }


@app.delete("/api/books/{book_id}")
def api_delete(book_id: int):
    db.delete_book(book_id)
    return {"ok": True}


@app.post("/api/books/{book_id}/reprocess")
async def api_reprocess(book_id: int):
    """Re-run processing for an existing book (reuses the saved registry + roster
    sheets; re-segments and re-warms). Useful after a pipeline change."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    db.set_status(book_id, "queued", "reprocessing…")
    await asyncio.to_thread(start_processing, book_id)
    return {"ok": True}


@app.get("/api/books/{book_id}/pages")
def api_pages(book_id: int):
    b = db.get_book(book_id)
    if not b:
        raise HTTPException(404, "no such book")
    return [{"idx": p["idx"], "chapter": p["chapter_idx"], "title": p["title"],
             "text": p["read_text"]} for p in db.get_pages(book_id)]


@app.get("/api/books/{book_id}/pages/{idx}/status")
def api_scene_status(book_id: int, idx: int):
    return {"status": db.scene_status(book_id, idx) or "none"}


@app.get("/api/books/{book_id}/pages/{idx}/image")
async def api_scene_image(book_id: int, idx: int):
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "no such book")
    if book["status"] != "ready":
        raise HTTPException(409, f"book not ready ({book['status']})")
    if not db.get_page(book_id, idx):
        raise HTTPException(404, "no such page")
    try:
        data = await ensure_scene(book_id, idx)
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(503, f"generation failed: {str(ex)[:200]}")
    schedule_prefetch(book_id, idx + 1, PREFETCH, book["num_pages"])
    return Response(content=data, media_type="image/webp",
                    headers={"Cache-Control": "public, max-age=31536000"})


@app.put("/api/books/{book_id}/progress")
async def api_set_progress(book_id: int, body: dict):
    pos = int(body.get("position", 0))
    db.set_progress(book_id, pos)
    return {"ok": True}
