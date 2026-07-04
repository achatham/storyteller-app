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
import hashlib
import mimetypes
import os
import re
import subprocess
import sys
from pathlib import Path

mimetypes.add_type("application/manifest+json", ".webmanifest")

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pipeline.config import STYLES
from . import db, flow, scene

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"


def _compute_version() -> str:
    """A build id that changes whenever the front-end (static files) changes, so the
    installed PWA can tell when a newer version is available."""
    h = hashlib.md5()
    for p in sorted(STATIC.glob("*")):
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


APP_VERSION = _compute_version()
WORK = Path(os.environ.get("STORY_WORK", str(ROOT / "output" / "work")))
LOGS = Path(os.environ.get("STORY_LOGS", str(ROOT / "output" / "logs")))
PREFETCH = int(os.environ.get("STORY_PREFETCH", "2"))
GEN_CONCURRENCY = int(os.environ.get("STORY_GEN_CONCURRENCY", "3"))

app = FastAPI(title="Storyteller")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/sw.js")
def service_worker():
    # served from root so its scope covers the whole app (/read, /book, ...)
    return Response((STATIC / "sw.js").read_text(), media_type="application/javascript")

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
        "STORY_RUN": f"book:{book_id}",   # tag all processing usage to this book
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
        "words_per_page": b["words_per_page"], "age": b["age"],
        "position": db.get_progress(book_id),
        "chapters": db.get_chapters(book_id),
        "scenes_done": db.scene_progress(book_id).get("done", 0),
    }


@app.delete("/api/books/{book_id}")
def api_delete(book_id: int):
    db.delete_book(book_id)
    return {"ok": True}


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


@app.get("/api/books/{book_id}/cost")
def api_book_cost(book_id: int):
    from pipeline import costs
    from pipeline.run import PASS_THRESHOLD
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
    report = costs.book_report(book_id)

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


@app.post("/api/books/{book_id}/sheet/{entity_id}/{variant_id}/redraw")
async def api_sheet_redraw(book_id: int, entity_id: str, variant_id: str):
    """Force-redraw one roster sheet so it regenerates with a fresh debug trace."""
    if not db.get_book(book_id):
        raise HTTPException(404, "no such book")
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
    if book["status"] != "ready":
        raise HTTPException(409, f"book not ready ({book['status']})")
    if not db.get_page(book_id, idx):
        raise HTTPException(404, "no such page")
    data = await asyncio.to_thread(db.scene_data, book_id, idx)
    schedule_prefetch(book_id, idx + 1, PREFETCH, book["num_pages"])
    if data:
        return _image_response(data, request)
    # Not drawn yet: a scene takes ~30-40s (sheets + critic + revise). Don't hold the
    # connection that long (it trips reverse-proxy timeouts and stalls reading-ahead).
    # Kick off a background render and tell the client to poll; it shows a placeholder.
    if await asyncio.to_thread(db.scene_status, book_id, idx) != "generating":
        asyncio.create_task(_safe_ensure(book_id, idx))
    return Response(status_code=202, headers={"Retry-After": "2", "Cache-Control": "no-store"})


@app.put("/api/books/{book_id}/progress")
async def api_set_progress(book_id: int, body: dict):
    pos = int(body.get("position", 0))
    db.set_progress(book_id, pos)
    return {"ok": True}
