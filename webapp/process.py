"""Per-book preprocessing, run as a subprocess so it can use the env-driven
pipeline (config freezes book/style/cadence at import) without polluting the
long-lived server process.

    python -m webapp.process <book_id>

Env (set by the server before spawning): STORY_PDF (working copy of the upload),
STORY_BOOK / STORY_AUTHOR / STORY_STYLE / STORY_WORDS_PER_PAGE / STORY_AGE,
STORY_OUT / STORY_REGISTRY / STORY_ASSETS (per-book scratch dirs), STORY_APP_DB.

Stages, each recorded to the DB so the hub can show live progress:
  registry   -> one pass over the whole book -> entity registry (the "roster")
  roster     -> a canonical reference sheet image per registry entity/variant
  segmenting -> per chapter, segment into read-aloud pages (text + brief + cast)

Scene images are NOT made here; they are generated lazily while reading.
"""
import json
import os
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import db

# Every epub is laid out differently (NCX present or not, spine vs TOC mismatch,
# odd front/back matter), so rather than trust the format we hand the model a
# cheap *skeleton* of the book and let it confirm which spine documents are real
# story chapters and what to call them. Heuristic TOC parsing is the fallback.
CHAPTER_PLAN_PROMPT = """You are given the structure of an EPUB e-book as an ordered list of its spine \
documents (the files it is made of). For each you get: its index `i`, its filename, its \
table-of-contents title (if any), its word count, and the opening text.

Identify which documents are the actual STORY chapters to read aloud, IN READING ORDER, and \
skip non-story front/back matter: cover, endorsements/"praise", title page, copyright, \
dedication, table of contents, introduction/foreword/preface written ABOUT the book, \
acknowledgments, about-the-author, "also by"/ads, newsletter sign-ups, index, etc. KEEP a \
prologue or epilogue if it is part of the story itself.

For each real chapter return its index `i` (exactly as given) and a clean human `title` \
(prefer the table-of-contents title; otherwise derive a short title from the opening text). \
Do NOT invent or merge; one entry per real-chapter document, preserving order.

Return JSON only:
{{"chapters": [{{"i": <index from the list>, "title": "<chapter title>"}}]}}

BOOK STRUCTURE:
{skeleton}
"""


def _epub_skeleton():
    """(full units, compact skeleton) for the current epub: one skeleton row per
    spine document with just enough for the model to classify it."""
    from pipeline import extract
    from pipeline.config import PDF
    units = extract._epub_units(PDF)          # [(archive_path, text)]
    toc = extract._epub_toc_titles(PDF)       # {archive_path: title}
    skel = []
    for i, (p, text) in enumerate(units):
        head = " ".join(text.split())[:240]
        skel.append({"i": i, "file": p.rsplit("/", 1)[-1],
                     "toc_title": toc.get(p, ""), "words": len(text.split()),
                     "head": head})
    return units, skel


def _llm_chapter_units(units, skel):
    """Ask the text model which spine docs are real chapters; map back to texts."""
    from pipeline import gem
    from pipeline.config import CHAPTER_MODEL
    data = gem.text_json(CHAPTER_PLAN_PROMPT.format(
        skeleton=json.dumps(skel, ensure_ascii=False)), model=CHAPTER_MODEL)
    out = []
    for c in data.get("chapters", []):
        i = c.get("i")
        if isinstance(i, int) and 0 <= i < len(units) and units[i][1].strip():
            title = (c.get("title") or "").strip() or f"Chapter {len(out) + 1}"
            out.append((title, units[i][1]))
    return out


class Progress:
    """Thread-safe aggregator so concurrently-running stages (roster image gen +
    segmentation) share one status line instead of clobbering each other."""

    def __init__(self, book_id):
        self._book_id = book_id
        self._lock = threading.Lock()
        self._parts: dict[str, str] = {}

    def update(self, key, msg):
        with self._lock:
            self._parts[key] = msg
            db.set_status(self._book_id, "processing",
                          " · ".join(self._parts[k] for k in sorted(self._parts)))


def chunk_text(text: str, size: int = 1800) -> list[str]:
    """Split a long text into ~`size`-word chunks at paragraph boundaries, so a
    chapterless PDF still segments into bounded analyze calls (the model's output
    cap, not its input window, is the real limit)."""
    chunks, cur, n = [], [], 0
    for para in text.split("\n\n"):
        w = len(para.split())
        if n and n + w > size:
            chunks.append("\n\n".join(cur))
            cur, n = [], 0
        cur.append(para)
        n += w
    if cur:
        chunks.append("\n\n".join(cur))
    return [c for c in chunks if c.strip()] or [text]


def chapter_units() -> list[tuple[str, str]]:
    """Ordered (title, text) units for the whole book. EPUB -> real story
    chapters with their table-of-contents titles (front/back matter skipped);
    PDF -> word-bounded chunks of the whole body."""
    from pipeline import extract
    from pipeline.config import IS_EPUB, PDF
    if IS_EPUB:
        # primary: let the model confirm chapters from a skeleton of the book;
        # fall back to TOC/heuristic parsing if that call fails or returns nothing
        try:
            units, skel = _epub_skeleton()
            chosen = _llm_chapter_units(units, skel)
            if chosen:
                print(f"[chapters] LLM confirmed {len(chosen)}/{len(units)} "
                      f"spine docs as chapters", flush=True)
                return chosen
            print("[chapters] LLM returned no chapters; using TOC heuristic", flush=True)
        except Exception as ex:  # noqa: BLE001
            print(f"[chapters] LLM plan failed ({type(ex).__name__}: {ex}); "
                  "using TOC heuristic", flush=True)
        return extract.epub_chapters_titled(PDF)
    body = extract.full_story_text()
    return [(f"Part {i + 1}", t) for i, t in enumerate(chunk_text(body))]


def gen_roster(book_id, registry, workers: int = 4, report=None):
    """A canonical reference sheet image for every registry entity/variant, drawn
    in parallel (the Gemini client, the shared Budget, and per-row DB writes are
    all thread-safe; each sheet writes a distinct file/row)."""
    from pipeline import gem, sheets

    def emit(done, n):
        msg = f"roster {done}/{n}"
        report("roster", msg) if report else db.set_status(book_id, "roster", msg)

    # flatten to one task per (entity, variant)
    tasks = []
    for e in registry.get("entities", []):
        variants = e.get("variants") or [{
            "id": "default",
            "appearance": e.get("base_appearance", ""),
            "sheet_prompt": e.get("base_sheet_prompt", ""),
        }]
        entity_arg = {
            "id": e["id"], "type": e.get("type", "character"),
            "base_appearance": e.get("base_appearance", ""),
            "base_sheet_prompt": e.get("base_sheet_prompt", ""),
        }
        for v in variants:
            variant_arg = {
                "id": v["id"],
                "appearance": v.get("appearance") or e.get("base_appearance", ""),
                "sheet_prompt": v.get("sheet_prompt") or e.get("base_sheet_prompt", ""),
            }
            tasks.append((e["id"], v["id"], entity_arg, variant_arg))

    budget = gem.Budget(500)
    done = 0
    lock = threading.Lock()

    def draw(task):
        nonlocal done
        eid, vid, entity_arg, variant_arg = task
        if db.has_sheet(book_id, eid, vid):   # resume: don't redraw a saved sheet
            with lock:
                done += 1
                emit(done, len(tasks))
            return
        try:
            path = sheets.ensure_sheet(entity_arg, variant_arg, budget)
        except Exception as ex:  # noqa: BLE001 -- one bad sheet shouldn't sink the roster
            print(f"[roster] sheet {eid}/{vid} failed: {ex}", flush=True)
            path = None
        if path and path.exists():
            db.save_sheet(book_id, eid, vid, path.read_bytes())
            with lock:
                done += 1
                emit(done, len(tasks))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(draw, tasks))
    return done


def segment(book_id, registry, workers: int = 4, report=None):
    """Segment every chapter into read-aloud pages and store them.

    The per-chapter LLM calls are independent (each depends only on its own text
    + the shared registry), so they run in parallel. Only the GLOBAL page
    numbering is order-dependent, so we collect the per-chapter results first,
    then walk chapters in order to assign page indices and write them."""
    from pipeline import analyze

    units = [(ci, t, txt) for ci, (t, txt) in enumerate(chapter_units()) if txt.strip()]
    bibles: dict[int, dict] = {}
    done = 0
    lock = threading.Lock()

    def emit(done, n):
        msg = f"pages {done}/{n} chapters"
        report("segment", msg) if report else db.set_status(book_id, "segmenting", msg)

    def build(unit):
        nonlocal done
        ci, _title, text = unit
        try:
            bible = analyze.build_bible(text, registry)
        except Exception as ex:  # noqa: BLE001 -- one bad chapter shouldn't sink the book
            print(f"[segment] chapter {ci} failed: {ex}", flush=True)
            bible = None
        with lock:
            done += 1
            if bible is not None:
                bibles[ci] = bible
            emit(done, len(units))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(build, units))

    # assign global page numbers in chapter order (deterministic)
    page_idx = 0
    for ci, title, _text in units:
        bible = bibles.get(ci)
        if not bible:
            continue
        # use the real chapter title from the source, NOT the model's echoed
        # section label (which leaked the frozen STORY_LABEL default)
        db.add_chapter(book_id, ci, title, page_idx, bible.get("cast", []))
        for s in sorted(bible.get("spreads", []), key=lambda x: x.get("id", 0)):
            db.add_page(book_id, page_idx, ci, s.get("title", ""),
                        s.get("read_text", ""), s.get("setting", ""),
                        s.get("illustration_brief", ""), s.get("cast", []))
            page_idx += 1
    return page_idx


def prewarm(book_id, k, workers: int = 2):
    """Eagerly draw the first `k` page images during import so the book opens
    instantly. Best-effort: a failed warm page is left for lazy generation."""
    from . import scene

    def one(idx):
        if db.scene_data(book_id, idx):
            return
        try:
            db.scene_set_status(book_id, idx, "generating")
            scene.generate_scene(book_id, idx)
        except Exception as ex:  # noqa: BLE001 -- don't fail the import over a warm page
            print(f"[warm] page {idx} failed: {ex}", flush=True)
            db.scene_set_status(book_id, idx, "failed", str(ex)[:200])

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, range(k)))


def run(book_id: int):
    """Process a book end to end. Safe to re-run after an interrupted import:
    the (expensive) registry + roster are reused from the DB; only the cheaper
    text segmentation is redone from scratch."""
    from pipeline import registry as registry_mod

    # 1. registry -- reuse if a previous run already saved one
    registry = db.get_registry(book_id)
    if registry.get("entities"):
        print(f"[process] reusing saved registry ({len(registry['entities'])} entities)", flush=True)
    else:
        db.set_status(book_id, "registry", "cataloguing characters & settings")
        registry = registry_mod.build()
        db.save_registry(book_id, registry)

    # 2 + 3. roster (pro image model) and segmentation (text model) both depend
    # only on the registry, not on each other, so run them concurrently. gen_roster
    # skips sheets already in the DB; segmentation is redone wholesale (global page
    # numbering depends on every chapter, so a partial run can't be stitched).
    db.set_status(book_id, "processing", "drawing roster + segmenting")
    db.clear_segmentation(book_id)
    progress = Progress(book_id)
    roster_workers = int(os.environ.get("STORY_ROSTER_WORKERS", "4"))
    segment_workers = int(os.environ.get("STORY_SEGMENT_WORKERS", "4"))
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_roster = ex.submit(gen_roster, book_id, registry, roster_workers, progress.update)
        f_segment = ex.submit(segment, book_id, registry, segment_workers, progress.update)
        n = f_segment.result()    # re-raises if segmentation crashed
        f_roster.result()         # re-raises if the roster stage crashed
    db.set_num_pages(book_id, n)

    # 4. warm the first few page images so the book opens instantly
    warm = min(int(os.environ.get("STORY_WARM_PAGES", "2")), n)
    if warm > 0:
        db.set_status(book_id, "warming", f"drawing first {warm} pages")
        prewarm(book_id, warm)

    db.set_status(book_id, "ready", f"{n} pages ready to read")
    print(f"[process] book {book_id} ready: {n} pages", flush=True)


def main():
    book_id = int(sys.argv[1])
    try:
        run(book_id)
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc()
        db.set_status(book_id, "failed", f"{type(ex).__name__}: {str(ex)[:300]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
