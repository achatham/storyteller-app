"""FastAPI backend + reader UI for the storyteller.

  GET  /                          hub (upload / pick a book)
  GET  /read/{id}                 reader SPA
  GET  /api/styles                available art styles
  GET  /api/books                 list books (+ status + progress)
  POST /api/books                 upload a book -> kicks off processing subprocess
  GET  /api/books/{id}            book detail (status, chapters, progress)
  GET  /api/books/{id}/cover      the book's cover illustration (404 if not drawn)
  POST /api/books/{id}/cover      draw / redraw the cover
  DELETE /api/books/{id}          remove a book
  GET  /api/books/{id}/pages      page text (no images)
  GET  /api/books/{id}/pages/{i}/image   scene image (generated lazily; prefetches ahead)
  GET  /api/books/{id}/pages/{i}/status  scene generation status
  PUT  /api/books/{id}/progress   save reading position (server-side) + log a session
  GET  /api/history               reading history (recent sessions across all books)
  GET  /api/history/export        reading history as JSON for export (?start=&end=)
  GET  /history                   reading-history viewer page

Scene images are generated on demand and prefetched N pages ahead (PREFETCH).
Concurrent generations are capped by a semaphore; duplicate work is coalesced by
a per-(book,page) lock.
"""
import asyncio
import hashlib
import io
import mimetypes
import os
import re
import subprocess
import sys
import time
import zipfile
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from pipeline.config import STYLES

from . import cover, db, flow, scene

mimetypes.add_type("application/manifest+json", ".webmanifest")

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"


def _compute_version() -> str:
    """A build id that changes whenever the user-visible front end changes, so the
    installed PWA can tell when a newer version is available. This covers the static
    files AND backend-defined UI surfaces that a static-only hash would miss -- e.g.
    the art-style list, which lives in pipeline/config.py, so adding a style still
    invalidates the cached app."""
    h = hashlib.md5()
    for p in sorted(STATIC.glob("*")):
        if p.is_file():
            h.update(p.read_bytes())
    h.update(repr(sorted(STYLES.items())).encode())
    return h.hexdigest()[:10]


APP_VERSION = _compute_version()
WORK = Path(os.environ.get("STORY_WORK", str(ROOT / "output" / "work")))
LOGS = Path(os.environ.get("STORY_LOGS", str(ROOT / "output" / "logs")))
PREFETCH = int(os.environ.get("STORY_PREFETCH", "2"))
GEN_CONCURRENCY = int(os.environ.get("STORY_GEN_CONCURRENCY", "3"))
MAX_BOOK_BYTES = int(os.environ.get("STORY_MAX_BOOK_BYTES", str(50 * 1024 * 1024)))
MAX_UPLOADS_PER_MINUTE = int(os.environ.get("STORY_MAX_UPLOADS_PER_MINUTE", "6"))

if MAX_BOOK_BYTES <= 0 or MAX_UPLOADS_PER_MINUTE <= 0:
    raise RuntimeError("STORY_MAX_BOOK_BYTES and STORY_MAX_UPLOADS_PER_MINUTE must be positive")

_sem = asyncio.Semaphore(GEN_CONCURRENCY)
_locks: dict[tuple[int, int], asyncio.Lock] = {}
_children: dict[tuple[str, int], subprocess.Popen] = {}
_upload_attempts: dict[str, deque[float]] = defaultdict(deque)


def _launch(kind: str, book_id: int, args: list[str], env: dict, log_name: str) -> bool:
    """Launch one durable job at a time per book/kind in this server process.

    The database remains the source of truth across restarts; this guard avoids
    accidental duplicate API clicks creating competing subprocesses meanwhile.
    """
    key = (kind, book_id)
    old = _children.get(key)
    if old and old.poll() is None:
        return False
    log_path = LOGS / log_name
    with open(log_path, "ab") as logf:
        _children[key] = subprocess.Popen(args, cwd=str(ROOT), env=env,
                                          stdout=logf, stderr=logf)
    db.job_start(book_id, kind, _children[key].pid)
    return True


def _cancel_local_jobs(book_id: int, kinds: set[str] | None = None) -> list[str]:
    """Terminate selected local workers and persist a cancellation result.

    Cancellation intentionally preserves uploaded source, registry, generated art,
    and any previously-built EPUB so the user can safely retry later.
    """
    cancelled = []
    for key, proc in list(_children.items()):
        kind, bid = key
        if bid != book_id or (kinds is not None and kind not in kinds):
            continue
        if proc.poll() is None:
            proc.terminate()
            db.job_finish(book_id, kind, "cancelled", "cancelled by user")
            cancelled.append(kind)
        _children.pop(key, None)
    return cancelled


def _cancel_batch_api_jobs(book_id: int):
    """Best-effort cancellation of provider jobs created by a bake/EPUB worker."""
    from pipeline import gem
    with db.conn() as c:
        rows = c.execute("SELECT job_name, state FROM batch_jobs WHERE book_id=?",
                         (book_id,)).fetchall()
    for row in rows:
        if row["job_name"] and row["state"] not in gem.BATCH_TERMINAL:
            try:
                gem._client.batches.cancel(name=row["job_name"])
            except Exception:  # noqa: BLE001 -- remote cancellation is best-effort
                pass


def _check_upload_rate(request: Request):
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = _upload_attempts[client]
    while attempts and attempts[0] <= now - 60:
        attempts.popleft()
    if len(attempts) >= MAX_UPLOADS_PER_MINUTE:
        raise HTTPException(429, "too many uploads; try again in a minute")
    attempts.append(now)


def _valid_upload(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf" and data.startswith(b"%PDF-"):
        return "application/pdf"
    if suffix == ".epub" and data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                if "mimetype" in zf.namelist() and zf.read("mimetype") == b"application/epub+zip":
                    return "application/epub+zip"
        except (OSError, zipfile.BadZipFile):
            pass
    raise HTTPException(400, "upload must be a valid PDF or unencrypted EPUB file")


def _resume_interrupted_work():
    db.init()
    n_jobs = db.interrupt_running_jobs()
    if n_jobs:
        print(f"[server] marked {n_jobs} interrupted worker job(s)", flush=True)
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
    # resume any whole-book batch bake interrupted by a restart. batch_bake reattaches
    # to in-flight Batch API jobs (batch_jobs table) instead of resubmitting.
    for bid in db.books_baking():
        start_bake(bid)
        print(f"[server] resumed interrupted bake for book {bid}", flush=True)
    # resume any EPUB build interrupted mid-way. make_epub reuses the (also-resumed)
    # bake if pages are still missing, else just rebuilds the file.
    for bid in db.epubs_pending():
        start_epub(bid)
        print(f"[server] resumed interrupted EPUB build for book {bid}", flush=True)


def _shutdown_local_jobs():
    """Stop child workers before the server exits and leave durable diagnostics.

    A restarted server's normal resume paths will pick eligible work up again;
    waiting briefly avoids orphaned workers competing with that new process.
    """
    for (kind, book_id), proc in list(_children.items()):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            db.job_finish(book_id, kind, "interrupted", "server shutting down")
        _children.pop((kind, book_id), None)
    _locks.clear()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _resume_interrupted_work()
    try:
        yield
    finally:
        await asyncio.to_thread(_shutdown_local_jobs)


app = FastAPI(title="Storyteller", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# The reverse proxy remains the authentication boundary, but these headers are
# useful even on a trusted LAN and keep an accidental direct deployment safer.
@app.middleware("http")
async def harden_http(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


@app.get("/sw.js")
def service_worker():
    # served from root so its scope covers the whole app (/read, /book, ...)
    return Response((STATIC / "sw.js").read_text(), media_type="application/javascript")


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
    key = (book_id, idx)
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
        finally:
            _locks.pop(key, None)


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
        "STORY_RUN": f"book:{book_id}",   # tag all processing usage to this book
    })
    _launch("process", book_id, [sys.executable, "-m", "webapp.process", str(book_id)],
            env, f"book_{book_id}.log")


def start_bake(book_id: int):
    """Launch the whole-book Batch-API bake as a detached subprocess (long-running,
    polls batch jobs). It reads book/style/pages/roster straight from the DB, so it
    only needs the app DB path in its env."""
    env = dict(os.environ)
    env["STORY_APP_DB"] = str(db.DB)
    env["STORY_RUN"] = f"book:{book_id}"
    _launch("bake", book_id, [sys.executable, "-m", "webapp.batch_bake", str(book_id)],
            env, f"bake_{book_id}.log")


def start_epub(book_id: int):
    """Launch the EPUB build as a detached subprocess. It illustrates any missing
    pages (via the bake) first, then assembles the .epub -- long-running, so it
    runs out-of-band and the UI polls the epub_jobs row for progress."""
    env = dict(os.environ)
    env["STORY_APP_DB"] = str(db.DB)
    env["STORY_RUN"] = f"book:{book_id}"
    _launch("epub", book_id, [sys.executable, "-m", "webapp.make_epub", str(book_id)],
            env, f"epub_{book_id}.log")


# ---------------- pages (UI) ----------------

@app.get("/", response_class=HTMLResponse)
def hub():
    # __BUILD__ so the library can tell it is running an older build than the server
    # and offer the update itself. It is the page you land on, and the one the
    # service worker is most likely to be serving stale.
    return (STATIC / "hub.html").read_text().replace("__BUILD__", APP_VERSION)


@app.get("/history", response_class=HTMLResponse)
def history_page():
    return (STATIC / "history.html").read_text()


@app.get("/read/{book_id}", response_class=HTMLResponse)
def reader(book_id: int):
    html = (STATIC / "reader.html").read_text()
    return html.replace("__BOOK_ID__", str(book_id))


@app.get("/api/version")
def api_version():
    """Current front-end build id; the PWA polls this to offer an update."""
    return {"version": APP_VERSION}


@app.get("/book/{book_id}", response_class=HTMLResponse)
def book_reader(book_id: int):
    html = (STATIC / "book.html").read_text()
    return html.replace("__BOOK_ID__", str(book_id)).replace("__BUILD__", APP_VERSION)


@app.get("/settings/{book_id}", response_class=HTMLResponse)
def book_settings(book_id: int):
    html = (STATIC / "settings.html").read_text()
    return html.replace("__BOOK_ID__", str(book_id))


@app.get("/roster/{book_id}", response_class=HTMLResponse)
def book_roster(book_id: int):
    html = (STATIC / "roster.html").read_text()
    return html.replace("__BOOK_ID__", str(book_id))


@app.get("/debug/{book_id}", response_class=HTMLResponse)
def book_debug(book_id: int, page: int = -1):
    html = (STATIC / "debug.html").read_text()
    return html.replace("__BOOK_ID__", str(book_id)).replace("__PAGE__", str(page))


# ---------------- API ----------------

def _image_response(data: bytes, request: Request) -> Response:
    """Serve regenerable image bytes cache-resiliently: a content ETag plus
    no-cache, so the URL can stay stable while the bytes change. The browser (and
    the network-first service worker) revalidate on every view -- a regenerated
    scene/sheet is picked up immediately, and the unchanged case is a cheap 304.
    Far safer than immutable max-age, which froze old images in place for a year."""
    etag = '"' + hashlib.md5(data).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type="image/webp", headers=headers)

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
    covered = db.books_with_covers()
    for b in db.list_books():
        sp = db.scene_progress(b["id"])
        out.append({
            "id": b["id"], "title": b["title"] or b["filename"] or "Untitled",
            "author": b["author"], "style": b["style"], "status": b["status"],
            "detail": b["detail"], "num_pages": b["num_pages"],
            "position": b["position"], "scenes_done": sp.get("done", 0),
            "has_cover": b["id"] in covered,
        })
    return out


@app.post("/api/books")
async def api_upload(request: Request, file: UploadFile = File(...), title: str = Form(""),
                     author: str = Form(""), style: str = Form("watercolor"),
                     words_per_page: int = Form(200), age: str = Form("5"),
                     illustration_mode: str = Form("lazy")):
    if style not in STYLES:
        raise HTTPException(400, f"unknown style {style!r}")
    if words_per_page < 50 or words_per_page > 2_000:
        raise HTTPException(400, "words_per_page must be between 50 and 2000")
    if not age.isdigit() or not 0 <= int(age) <= 120:
        raise HTTPException(400, "audience age must be a whole number between 0 and 120")
    if illustration_mode not in ("lazy", "batch"):
        raise HTTPException(400, f"unknown illustration_mode {illustration_mode!r}")
    _check_upload_rate(request)
    if not file.filename:
        raise HTTPException(400, "file name is required")
    if len(title) > 300 or len(author) > 300 or len(age) > 20:
        raise HTTPException(400, "metadata field is too long")
    chunks, total = [], 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_BOOK_BYTES:
            raise HTTPException(413, f"book exceeds the {MAX_BOOK_BYTES // (1024 * 1024)} MiB upload limit")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(400, "empty file")
    mime = _valid_upload(file.filename, data)
    book_id = db.create_book(title.strip(), author.strip(), Path(file.filename).name, style,
                             words_per_page, age, mime, data)
    if illustration_mode != "lazy":
        db.set_illustration_mode(book_id, illustration_mode)
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
        "words_per_page": b["words_per_page"], "age": b["age"],
        "position": db.get_progress(book_id),
        "position_at": db.get_progress_at(book_id),
        "chapters": db.get_chapters(book_id),
        "scenes_done": db.scene_progress(book_id).get("done", 0),
        "illustration_mode": b["illustration_mode"] if "illustration_mode" in b.keys() else "lazy",
        "jobs": db.jobs_for_book(book_id),
        "bake": (lambda bk: {"status": bk["status"], "round": bk["round"],
                             "total_pages": bk["total_pages"],
                             "done_pages": db.bps_counts(book_id).get("done", 0)}
                 if bk else None)(db.bake_get(book_id)),
    }


@app.delete("/api/books/{book_id}")
def api_delete(book_id: int):
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    _cancel_local_jobs(book_id)
    db.delete_book(book_id)
    return {"ok": True}


@app.post("/api/books/{book_id}/process/cancel")
async def api_process_cancel(book_id: int):
    """Cancel an import/reprocess without deleting its reusable source or work."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    cancelled = await asyncio.to_thread(_cancel_local_jobs, book_id, {"process"})
    if not cancelled:
        raise HTTPException(409, "no processing job is running")
    await asyncio.to_thread(db.set_status, book_id, "cancelled", "processing cancelled — safe to reprocess")
    return {"ok": True, "cancelled": cancelled}


@app.post("/api/books/{book_id}/reprocess")
async def api_reprocess(book_id: int, fresh: bool = False):
    """Re-run processing for an existing book. Default reuses the saved registry
    (re-segments + re-warms) -- fast, for testing segmentation/scene changes.
    ?fresh=1 also rebuilds the registry from scratch (discover -> repair -> expand)."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    if fresh:
        await asyncio.to_thread(db.save_registry, book_id, {"entities": []})
    db.set_status(book_id, "queued", "reprocessing…" + (" (fresh)" if fresh else ""))
    await asyncio.to_thread(start_processing, book_id)
    return {"ok": True, "fresh": fresh}


@app.post("/api/books/{book_id}/bake")
async def api_bake(book_id: int, fresh: bool = False, retry_failed: bool = False):
    """Illustrate the whole book via the Batch API (~50% cheaper). Kicks off the
    background bake subprocess, which draws only the pages that don't have an image yet
    -- pages already illustrated (lazily as-read, or by a prior bake) are left untouched.
    Valid once the roster is ready (roster_review) or on a ready book you want to bake.
    ?fresh=1 discards prior bake bookkeeping (a clean re-scan); ?retry_failed=1
    regenerates ONLY the pages that ended failed -- cheap way to mop up stragglers."""
    b = db.get_book(book_id)
    if not b:
        raise HTTPException(404, "no such book")
    if b["num_pages"] < 1:
        raise HTTPException(409, "book not segmented yet")
    if retry_failed:
        n = await asyncio.to_thread(db.bake_retry_failed, book_id)
        if not n:
            return {"ok": True, "reopened": 0, "detail": "no failed pages to retry"}
    else:
        # A user-initiated start (fresh or not) re-scans from a clean slate: drop any
        # prior bake bookkeeping so prepare() re-seeds and re-detects which pages still
        # need an image. (Skips this only for a retry-failed mop-up, handled above.)
        prev = await asyncio.to_thread(db.bake_get, book_id)
        if prev and prev["status"] != "baking":
            await asyncio.to_thread(db.bake_clear, book_id)
    await asyncio.to_thread(db.set_illustration_mode, book_id, "batch")
    # round=0: a user-initiated (re)start runs the round loop from the top. Crash
    # resume (server startup -> start_bake) leaves the pointer untouched instead.
    await asyncio.to_thread(db.bake_upsert, book_id, "baking", round=0,
                            total_pages=b["num_pages"])
    await asyncio.to_thread(db.set_status, book_id, "baking", "illustrating the whole book…")
    await asyncio.to_thread(start_bake, book_id)
    return {"ok": True}


@app.post("/api/books/{book_id}/bake/cancel")
async def api_bake_cancel(book_id: int):
    """Ask a running bake to stop. The orchestrator checks this flag between steps;
    in-flight Batch API jobs are also cancelled best-effort."""
    if not db.bake_get(book_id):
        raise HTTPException(404, "no bake for this book")
    await asyncio.to_thread(db.bake_upsert, book_id, "cancelled")
    _cancel_local_jobs(book_id)
    # best-effort: cancel any still-open Batch API jobs so we stop paying for them
    await asyncio.to_thread(_cancel_batch_api_jobs, book_id)
    return {"ok": True}


@app.get("/api/books/{book_id}/bake")
def api_bake_status(book_id: int):
    """Bake progress for the roster/hub UI: overall state + per-status page counts."""
    bake = db.bake_get(book_id)
    if not bake:
        return {"status": None}
    counts = db.bps_counts(book_id)
    return {"status": bake["status"], "round": bake["round"],
            "total_pages": bake["total_pages"], "done_pages": counts.get("done", 0),
            "detail": bake["detail"], "counts": counts}


def _epub_status(book_id: int) -> dict:
    job = db.epub_get(book_id)
    # A cancelled rebuild deliberately keeps an older completed file downloadable.
    ready = bool(job and job["status"] in ("ready", "cancelled") and db.epub_path(book_id).exists())
    have = {idx for idx, data in db.iter_scene_blobs(book_id) if data}
    total = db.get_book(book_id)["num_pages"] or 0
    return {
        "status": job["status"] if job else None,
        "detail": job["detail"] if job else None,
        "size": job["size"] if job else None,
        "ready": ready,
        "illustrated": len(have), "total_pages": total,
        "download_url": f"/api/books/{book_id}/epub/file" if ready else None,
    }


@app.get("/api/books/{book_id}/epub")
def api_epub_status(book_id: int):
    """EPUB build progress: job state + how many of the book's pages are illustrated
    (the build must illustrate any that aren't before it can assemble the file)."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    return _epub_status(book_id)


@app.post("/api/books/{book_id}/epub")
async def api_epub_build(book_id: int):
    """(Re)build the book's illustrated EPUB. If any pages lack an image, this first
    runs the whole-book batch bake to fill them, then assembles the file -- all in a
    background subprocess. Poll GET .../epub for progress; download when ready."""
    b = db.get_book(book_id)
    if not b:
        raise HTTPException(404, "no such book")
    if (b["num_pages"] or 0) < 1:
        raise HTTPException(409, "book not segmented yet")
    job = db.epub_get(book_id)
    if job and job["status"] in ("baking", "building"):
        return {"ok": True, "already_running": True, **_epub_status(book_id)}
    await asyncio.to_thread(db.epub_upsert, book_id, "building", detail="starting…")
    await asyncio.to_thread(start_epub, book_id)
    return {"ok": True, **_epub_status(book_id)}


@app.post("/api/books/{book_id}/epub/cancel")
async def api_epub_cancel(book_id: int):
    """Cancel the local EPUB worker, preserving source art and an old EPUB file."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    cancelled = await asyncio.to_thread(_cancel_local_jobs, book_id, {"epub"})
    if not cancelled:
        raise HTTPException(409, "no EPUB build is running")
    # An EPUB worker may be waiting on or running a Batch API gap-fill bake.
    await asyncio.to_thread(_cancel_batch_api_jobs, book_id)
    await asyncio.to_thread(db.epub_upsert, book_id, "cancelled", detail="EPUB build cancelled")
    return {"ok": True, "cancelled": cancelled}


@app.get("/api/books/{book_id}/epub/file")
def api_epub_file(book_id: int):
    """Download the finished .epub."""
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "no such book")
    path = db.epub_path(book_id)
    if not path.exists():
        raise HTTPException(404, "no epub built yet")
    fname = re.sub(r"[^A-Za-z0-9]+", "-", (book["title"] or f"book{book_id}")).strip("-") or f"book{book_id}"
    return FileResponse(str(path), media_type="application/epub+zip",
                        filename=f"{fname}.epub")


@app.post("/api/books/{book_id}/recompress")
async def api_recompress(book_id: int):
    """Re-encode existing art to the current compression settings (no regeneration)."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    return {"ok": True, **await asyncio.to_thread(scene.recompress_book, book_id)}


@app.post("/api/books/{book_id}/redraw")
def api_redraw(book_id: int):
    """Clear the book's roster sheets + scene images so they redraw with the
    current pipeline (single-figure sheets, style anchor, aspect refs). Text and
    registry are kept; art regenerates lazily as you read."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    return {"ok": True, **db.clear_art(book_id)}


@app.get("/api/books/{book_id}/cover")
def api_cover(book_id: int, request: Request):
    """The book's cover illustration. 404 when it hasn't been drawn -- never
    generates: a cover needs roster sheets and the pro model, which is far too
    slow/expensive to do inside a library-grid image request. The library shows a
    placeholder for a book without one; POST to this URL to draw it."""
    data = db.get_cover(book_id)
    if not data:
        raise HTTPException(404, "no cover")
    return _image_response(data, request)


@app.post("/api/books/{book_id}/cover")
async def api_cover_draw(book_id: int, fresh: bool = False):
    """Draw the cover (?fresh=1 redraws an existing one). Ensures the cover cast's
    roster sheets exist first, so the cover's characters match the interior art."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    async with _sem:
        data = await asyncio.to_thread(cover.ensure_cover, book_id, fresh)
    if not data:
        raise HTTPException(503, "cover not drawn (roster not ready, or generation failed)")
    return {"ok": True, "bytes": len(data), **(db.cover_meta(book_id) or {})}


@app.get("/api/books/{book_id}/roster")
def api_roster(book_id: int):
    """The book's character/setting/prop roster: each entity's variants with a
    flag for whether its reference sheet has been drawn yet (no generation)."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    reg = db.get_registry(book_id)
    drawn = {}
    for eid, vid in db.list_sheets(book_id):
        drawn.setdefault(eid, set()).add(vid)
    out = []
    for e in reg.get("entities", []):
        eid = e["id"]
        variants, seen = [], set()
        for v in e.get("variants", []):
            vid = v["id"]
            seen.add(vid)
            variants.append({"variant_id": vid, "label": v.get("label") or vid.replace("_", " "),
                             "when": v.get("when", ""), "drawn": vid in drawn.get(eid, set())})
        for vid in sorted(drawn.get(eid, set())):   # drawn-but-not-in-registry (default, __aboard)
            if vid in seen:
                continue
            label = "aboard / interior" if vid == "__aboard" else vid.replace("_", " ").strip()
            variants.append({"variant_id": vid, "label": label or "default", "when": "", "drawn": True})
        out.append({"entity_id": eid, "name": e.get("name", eid), "type": e.get("type", "character"),
                    "importance": e.get("importance", 0), "variants": variants})
    out.sort(key=lambda x: (-x["importance"], x["name"]))
    return out


@app.get("/api/books/{book_id}/sheet/{entity_id}/{variant_id}")
def api_sheet(book_id: int, entity_id: str, variant_id: str, request: Request):
    """Serve an already-drawn roster sheet. 404 if not drawn -- never generates."""
    data = db.get_sheet(book_id, entity_id, variant_id)
    if not data:
        raise HTTPException(404, "not drawn yet")
    return _image_response(data, request)


@app.get("/api/books/{book_id}/batch")
async def api_batch_outstanding(book_id: int):
    """How many Batch API requests are still in flight for this book (image vs text),
    for the cost page's outstanding-requests indicator. Decided against the LIVE batch
    job list; returns {"known": false} if the API can't be reached so the UI stays quiet
    instead of showing a false zero."""
    from pipeline import gem
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    active = await asyncio.to_thread(gem.active_batch_jobs)
    if active is None:
        return {"known": False}
    out = await asyncio.to_thread(db.outstanding_batch, book_id, active)
    return {"known": True, **out}


@app.get("/api/books/{book_id}/cost")
def api_book_cost(book_id: int):
    from pipeline import costs
    from pipeline.run import PASS_THRESHOLD
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    report = costs.book_report(book_id)
    raw_budget = os.environ.get("STORY_BOOK_BUDGET_USD", "").strip()
    try:
        budget = float(raw_budget) if raw_budget else None
    except ValueError:
        budget = None
    if budget is not None and budget > 0:
        report["budget_usd"] = round(budget, 2)
        report["budget_remaining_usd"] = round(max(0, budget - report["total_usd"]), 4)
        report["budget_exceeded"] = report["total_usd"] >= budget

    # How many of each page's current illustration succeeded on the Nth try, plus
    # the resulting images-per-page multiplier. Pages that never cleared the quality
    # bar used the full try budget and fell back to the best-of judge -- those are
    # the ones that push the multiplier up.
    rows = db.page_attempt_rows(book_id)
    if rows:
        total = sum(r["n"] for r in rows)
        passed = [r for r in rows if (r["score"] or 0) >= PASS_THRESHOLD]
        fellback = [r for r in rows if (r["score"] or 0) < PASS_THRESHOLD]
        max_try = max(r["n"] for r in rows)
        buckets = [{"tries": n, "pages": sum(1 for r in passed if r["n"] == n)}
                   for n in range(1, max_try + 1)]
        report["attempts"] = {
            "pages": len(rows),
            "total": total,
            "multiplier": round(total / len(rows), 2),
            "buckets": [b for b in buckets if b["pages"]],
            "fellback": len(fellback),
            "fellback_tries": max_try,
        }
    return report


@app.get("/api/books/{book_id}/pages")
def api_pages(book_id: int):
    b = db.get_book(book_id)
    if not b:
        raise HTTPException(404, "no such book")
    return [{"idx": p["idx"], "chapter": p["chapter_idx"], "title": p["title"],
             "text": p["read_text"]} for p in db.get_pages(book_id)]




@app.get("/api/books/{book_id}/chapter/{idx}")
def api_chapter_flow(book_id: int, idx: int):
    """A chapter as an ordered stream of nodes (text runs + image placeholders at
    their in-text anchors) for the flowed/paginated reader."""
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "no such book")
    chapters = db.get_chapters(book_id)
    ch = next((c for c in chapters if c["idx"] == idx), None)
    if not ch:
        raise HTTPException(404, "no such chapter")
    seg = book["seg_ver"] if "seg_ver" in book.keys() else 0
    nodes = flow.chapter_nodes(
        book_id, idx,
        src_for=lambda p: f"/api/books/{book_id}/pages/{p['idx']}/image?v={seg}")
    return {"book_id": book_id, "idx": idx, "title": ch["title"],
            "num_chapters": len(chapters), "nodes": nodes}


@app.get("/api/books/{book_id}/pages/{idx}/status")
def api_scene_status(book_id: int, idx: int):
    return {"status": db.scene_status(book_id, idx) or "none"}


@app.get("/api/books/{book_id}/debug/pages")
def api_debug_pages(book_id: int):
    """Pages that have generation history (for the debug UI page list)."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    return {"book_id": book_id, "pages": db.debug_pages(book_id)}


@app.get("/api/books/{book_id}/debug/sheets")
def api_debug_sheets(book_id: int):
    """Roster sheets that have generation history (for the debug UI sheet list)."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    return {"book_id": book_id, "sheets": db.debug_sheets(book_id)}


@app.get("/api/books/{book_id}/debug/flagged")
def api_debug_flagged(book_id: int, threshold: float = 4.0):
    """Pages and roster sheets whose best score never reached the critic's pass
    threshold -- the images that consistently failed and were kept anyway."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    return {"book_id": book_id, "threshold": threshold,
            "pages": db.flagged_scenes(book_id, threshold),
            "sheets": db.flagged_sheets(book_id, threshold)}


@app.get("/api/books/{book_id}/sheet/{entity_id}/{variant_id}/history")
def api_sheet_history(book_id: int, entity_id: str, variant_id: str):
    """Full generation history for one roster sheet (every run + attempt)."""
    return {"book_id": book_id, "entity_id": entity_id, "variant_id": variant_id,
            "history": db.sheet_history(book_id, entity_id, variant_id)}


@app.get("/api/books/{book_id}/sheet/{entity_id}/{variant_id}/gen/{gen_id}/attempt/{attempt}/image")
def api_sheet_attempt_image(book_id: int, entity_id: str, variant_id: str,
                            gen_id: int, attempt: int, request: Request):
    """One candidate roster-sheet image from history -- including rejected attempts."""
    mime, data = db.sheet_attempt_image(book_id, entity_id, variant_id, gen_id, attempt)
    if not data:
        raise HTTPException(404, "no such attempt image")
    return _image_response(data, request)


@app.post("/api/books/{book_id}/pages/{idx}/redraw")
async def api_page_redraw(book_id: int, idx: int):
    """Clear one page's scene and kick off a fresh render (non-blocking)."""
    if not db.get_book(book_id) or not db.get_page(book_id, idx):
        raise HTTPException(404, "no such page")
    await asyncio.to_thread(db.delete_scene, book_id, idx)
    asyncio.create_task(_safe_ensure(book_id, idx))
    return {"ok": True}


@app.post("/api/books/{book_id}/debug/redraw-flagged")
async def api_redraw_flagged(book_id: int, threshold: float = 4.0):
    """Redraw every page whose best score is below the pass threshold."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    flagged = await asyncio.to_thread(db.flagged_scenes, book_id, threshold)
    for p in flagged:
        await asyncio.to_thread(db.delete_scene, book_id, p["idx"])
        asyncio.create_task(_safe_ensure(book_id, p["idx"]))
    return {"ok": True, "redrawing": [p["idx"] for p in flagged]}


@app.get("/api/books/{book_id}/sheet/{entity_id}/{variant_id}/prompt")
def api_sheet_prompt(book_id: int, entity_id: str, variant_id: str):
    """The editable roster prompt (sheet_prompt + appearance) behind this sheet, so the
    roster UI can show it for a from-scratch regeneration."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    res = scene.get_sheet_prompt(book_id, entity_id, variant_id)
    if not res.get("ok"):
        raise HTTPException(404, res.get("error", "no prompt"))
    return res


@app.post("/api/books/{book_id}/sheet/{entity_id}/{variant_id}/redraw")
async def api_sheet_redraw(book_id: int, entity_id: str, variant_id: str,
                           body: dict | None = Body(None)):
    """Force-redraw one roster sheet. With a body of {sheet_prompt, appearance} it
    redraws FROM SCRATCH using that user-edited prompt (and saves it back to the
    registry); with no body it just regenerates from the existing registry prompt."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    if body and (body.get("sheet_prompt") or body.get("appearance")):
        async with _sem:
            res = await asyncio.to_thread(
                scene.redraw_sheet_from_prompt, book_id, entity_id, variant_id,
                body.get("sheet_prompt", ""), body.get("appearance", ""))
        if not res.get("ok"):
            raise HTTPException(400, res.get("error", "redraw failed"))
        return res
    async with _sem:
        res = await asyncio.to_thread(scene.regenerate_sheet, book_id, entity_id, variant_id)
    return res


@app.post("/api/books/{book_id}/sheet/{entity_id}/{variant_id}/edit")
async def api_sheet_edit(book_id: int, entity_id: str, variant_id: str, body: dict):
    """Apply a written correction to one roster sheet, re-rendered with the chosen
    image model, and replace the cached sheet. Body: {instruction, model}."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(400, "instruction required")
    model_key = body.get("model") or "pro"
    async with _sem:
        res = await asyncio.to_thread(scene.edit_sheet, book_id, entity_id, variant_id,
                                      instruction, model_key)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "edit failed"))
    return res


@app.get("/api/books/{book_id}/pages/{idx}/history")
def api_scene_history(book_id: int, idx: int):
    """Full generation history for one page: every run, every attempt (prompt +
    critique + scores), newest first. Image blobs are fetched separately."""
    page = db.get_page(book_id, idx)
    return {"book_id": book_id, "idx": idx, "title": page["title"] if page else None,
            "history": db.scene_history(book_id, idx)}


@app.get("/api/books/{book_id}/pages/{idx}/gen/{gen_id}/attempt/{attempt}/image")
def api_attempt_image(book_id: int, idx: int, gen_id: int, attempt: int, request: Request):
    """One candidate image from history -- including rejected attempts."""
    mime, data = db.scene_attempt_image(book_id, idx, gen_id, attempt)
    if not data:
        raise HTTPException(404, "no such attempt image")
    return _image_response(data, request)


@app.get("/api/books/{book_id}/pages/{idx}/trace")
def api_scene_trace(book_id: int, idx: int):
    """The generation log for a drawn scene: each attempt's mode (fresh/revise),
    the four critic sub-scores, issues, fix-hint, the per-character states, and
    which attempt was kept. Null until the scene has been drawn at least once."""
    page = db.get_page(book_id, idx)
    return {"book_id": book_id, "idx": idx, "title": page["title"] if page else None,
            "brief": page["brief"] if page else None,
            "score": db.scene_score(book_id, idx), "trace": db.scene_trace(book_id, idx)}


@app.get("/api/books/{book_id}/pages/{idx}/image")
async def api_scene_image(book_id: int, idx: int, request: Request):
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "no such book")
    # A whole-book bake ('baking') is readable: pages already illustrated serve
    # normally; the still-baking ones show a placeholder (below). Only genuinely
    # unready states (uploading/segmenting) block.
    if book["status"] not in ("ready", "baking"):
        raise HTTPException(409, f"book not ready ({book['status']})")
    if not db.get_page(book_id, idx):
        raise HTTPException(404, "no such page")
    data = await asyncio.to_thread(db.scene_data, book_id, idx)
    schedule_prefetch(book_id, idx + 1, PREFETCH, book["num_pages"])
    if data:
        return _image_response(data, request)
    # Not drawn yet: a scene takes ~30-40s (sheets + critic + revise). Don't hold the
    # connection that long (it trips reverse-proxy timeouts and stalls reading-ahead).
    # Tell the client to poll; it shows a placeholder meanwhile. During a bake the
    # batch subprocess owns this page, so DON'T start a competing interactive render --
    # just let the reader poll until the bake stores it.
    if book["status"] != "baking" and \
            await asyncio.to_thread(db.scene_status, book_id, idx) != "generating":
        asyncio.create_task(_safe_ensure(book_id, idx))
    return Response(status_code=202, headers={"Retry-After": "2", "Cache-Control": "no-store"})


@app.put("/api/books/{book_id}/progress")
async def api_set_progress(book_id: int, body: dict):
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "no such book")
    try:
        pos = int(body.get("position", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "position must be an integer")
    pos = max(0, min(pos, max(0, (book["num_pages"] or 1) - 1)))
    db.set_progress(book_id, pos)
    db.log_reading(book_id, pos)
    return {"ok": True}


@app.get("/api/history")
def api_history():
    """Reading history: recent sessions (which book, which pages) across the
    library, newest first."""
    return db.reading_history()


# Accepted `start`/`end` spellings, loosest useful set: a bare date, a local
# datetime, or raw epoch seconds. Bare dates are interpreted in the server's
# local timezone, which is the one the reading actually happened in.
_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S",
                 "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")


def _parse_when(value: str | None, name: str, end_of_day: bool = False) -> float | None:
    """Parse one bound of the export window into epoch seconds, or None if unset.
    A bare date used as `end` means the *whole* of that day, so ?start=2026-08-01
    &end=2026-08-09 reads the way a person means it: inclusive at both ends."""
    if value is None or value == "":
        return None
    try:                                    # raw epoch seconds pass straight through
        return float(value)
    except ValueError:
        pass
    text = value.strip().replace("Z", "+00:00")
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if end_of_day and fmt == "%Y-%m-%d":
            dt += timedelta(days=1)         # exclusive upper bound = next midnight
        return dt.timestamp()
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        raise HTTPException(
            400, f"{name} must be YYYY-MM-DD, an ISO-8601 datetime, or epoch seconds")


@app.get("/api/history/export")
def api_history_export(start: str | None = None, end: str | None = None,
                       limit: int = 5000):
    """Reading history as self-contained JSON, for export into an external log.

    ?start= and ?end= bound the window (YYYY-MM-DD, ISO-8601 datetime, or epoch
    seconds); either may be omitted for an open end. Sessions overlapping the
    window are included. Times are reported both as epoch seconds and as local
    ISO-8601, so the consumer doesn't have to guess a timezone."""
    lo = _parse_when(start, "start")
    hi = _parse_when(end, "end", end_of_day=True)
    if lo is not None and hi is not None and hi < lo:
        raise HTTPException(400, "end must not be before start")
    limit = max(1, min(limit, 50000))

    sessions = []
    for s in db.reading_history(limit=limit, start=lo, end=hi):
        pages = s["num_pages"] or 0
        sessions.append({
            "book_id": s["book_id"],
            "title": s["title"],
            "author": s["author"],
            "started_at": s["started_at"],
            "ended_at": s["updated_at"],
            "started_at_iso": _iso(s["started_at"]),
            "ended_at_iso": _iso(s["updated_at"]),
            "duration_seconds": round(max(0.0, s["updated_at"] - s["started_at"]), 3),
            # positions are 0-based internally; export them as human page numbers
            "start_page": s["start_pos"] + 1,
            "end_page": s["end_pos"] + 1,
            "pages_read": max(0, s["end_pos"] - s["start_pos"]) + 1,
            "book_pages": pages,
            "percent_complete": round((s["end_pos"] + 1) / pages * 100, 1) if pages else None,
            "page_turns": s["events"],
        })
    return {
        "start": _iso(lo), "end": _iso(hi),
        "count": len(sessions), "truncated": len(sessions) == limit,
        "sessions": sessions,
    }


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts).astimezone().isoformat()
