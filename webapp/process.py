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
import re
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import db


def _when_range(when: str):
    """(low, high) story-chapter numbers a variant's 'when' covers, or None."""
    nums = [int(n) for n in re.findall(r"\d+", when or "")]
    return (min(nums), max(nums)) if nums else None


def _variant_for_chapter(entity: dict, chapter_num: int) -> str:
    """Pick the entity variant whose span covers this chapter (else the first)."""
    variants = entity.get("variants") or []
    if not variants:
        return "default"
    for v in variants:
        rng = _when_range(v.get("when", ""))
        if rng and rng[0] <= chapter_num <= rng[1]:
            return v["id"]
    return variants[0]["id"]


PROP_MAP_PROMPT = """You are tagging which recurring SETTINGS and PROPS appear in each illustration \
of one chapter of a children's picture book, so they can be drawn consistently.

You are given the book's recurring settings/props (each with an id, name, aliases, and its \
variants -- a variant has an id, the chapters it applies to, and what is different about it), \
and this chapter's pages (each with an id, its setting, and a description of its illustration).

For EACH page, decide which of the listed settings/props actually APPEAR IN or DEFINE that \
page's scene -- e.g. a ship the characters are aboard, a castle they are inside, a notable \
object in view. Judge by MEANING, not exact words: "the vessel", "the boat", "on deck" all \
indicate the ship. For each one that appears, choose the best-fitting variant id using the \
page's description and the variant chapter spans (this is chapter {chapter_num}).

Return JSON only:
{{"pages": [{{"id": <page id>, "props": [{{"entity_id": "<id>", "variant_id": "<variant id>"}}]}}]}}
Only include a page if it has props; omit anything that does not actually appear.

SETTINGS/PROPS:
{props}

PAGES:
{pages}
"""


def _llm_map_props(props, spreads, chapter_num):
    """Ask the (cheap) model which settings/props appear on each page. Returns
    {page_id: [(entity_id, variant_id), ...]}."""
    from pipeline import gem
    from pipeline.config import PROP_MODEL
    prop_lines = [{"id": e["id"], "name": e.get("name", ""), "aliases": e.get("aliases", []),
                   "variants": [{"id": v["id"], "when": v.get("when", ""),
                                 "delta": v.get("delta", "")} for v in e.get("variants", [])]}
                  for e in props]
    page_lines = [{"id": s.get("id"), "setting": s.get("setting", ""),
                   "brief": s.get("illustration_brief", "")} for s in spreads]
    data = gem.text_json(PROP_MAP_PROMPT.format(
        chapter_num=chapter_num, props=json.dumps(prop_lines, ensure_ascii=False),
        pages=json.dumps(page_lines, ensure_ascii=False)), model=PROP_MODEL)
    out = {}
    for pg in data.get("pages", []):
        out[pg.get("id")] = [(p.get("entity_id"), p.get("variant_id"))
                             for p in pg.get("props", []) if p.get("entity_id")]
    return out


def enrich_setting_props(bible: dict, registry: dict, chapter_num: int):
    """Make sure recurring SETTINGS/PROPS the segmenter under-lists (e.g. the ship
    the characters are aboard) are in each page's cast, so their canonical sheet is
    drawn + attached and they stay consistent.

    Primary: a focused model pass that maps props -> pages by meaning (robust to
    paraphrase/punctuation). Floor: a literal name/alias regex, so a prop plainly
    named in the text is never dropped even if the model misses it."""
    props = [e for e in registry.get("entities", [])
             if e.get("type") in ("setting", "prop")]
    if not props:
        return
    prop_by_id = {e["id"]: e for e in props}
    spread_by_id = {s.get("id"): s for s in bible.get("spreads", [])}
    chapter_cast = bible.setdefault("cast", [])
    in_chapter = {m.get("entity_id") for m in chapter_cast}

    def add(spread, eid, vid):
        e = prop_by_id.get(eid)
        if not e or spread is None:
            return
        if vid not in {v["id"] for v in e.get("variants", [])}:
            vid = _variant_for_chapter(e, chapter_num)   # validate / fall back
        cast = spread.setdefault("cast", [])
        if not any(c.get("entity_id") == eid for c in cast):
            cast.append({"entity_id": eid, "variant_id": vid})
        if eid not in in_chapter:
            chapter_cast.append({"entity_id": eid, "variant_id": vid,
                                 "name": e.get("name", ""), "from_registry": True})
            in_chapter.add(eid)

    # primary: model pass (best-effort)
    try:
        mapped = _llm_map_props(props, bible.get("spreads", []), chapter_num)
        for pid, items in mapped.items():
            for eid, vid in items:
                add(spread_by_id.get(pid), eid, vid)
    except Exception as ex:  # noqa: BLE001
        print(f"[props] model pass failed: {type(ex).__name__}: {ex}", flush=True)

    # floor: literal name/alias match (strip leading articles to be less brittle)
    for e in props:
        names = [e.get("name", "")] + (e.get("aliases") or [])
        pats = [re.compile(r"\b" + re.escape(re.sub(r"(?i)^(the|a|an)\s+", "", n)) + r"\b", re.I)
                for n in names if n and len(re.sub(r"(?i)^(the|a|an)\s+", "", n)) >= 4]
        vid = _variant_for_chapter(e, chapter_num)
        for s in bible.get("spreads", []):
            text = f"{s.get('setting','')} {s.get('illustration_brief','')}"
            if any(p.search(text) for p in pats):
                add(s, e["id"], vid)

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
        # backstop: make sure recurring settings/props named in a page (the ship,
        # a castle, ...) are in its cast so their canonical sheet is referenced
        enrich_setting_props(bible, registry, ci + 1)
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
    the (expensive) registry is reused from the DB; segmentation is redone from
    scratch; roster sheets are drawn lazily as pages are rendered."""
    from pipeline import registry as registry_mod

    # 1. registry -- reuse if a previous run already saved one
    registry = db.get_registry(book_id)
    if registry.get("entities"):
        print(f"[process] reusing saved registry ({len(registry['entities'])} entities)", flush=True)
    else:
        db.set_status(book_id, "registry", "cataloguing characters & settings")
        registry = registry_mod.build()
        db.save_registry(book_id, registry)

    # 2. segmentation (global page numbering depends on every chapter, so it is
    # redone wholesale -- text-only, cheap). Roster sheets are NOT drawn up front:
    # they are generated lazily when a page that needs them is rendered (and the
    # warm step below draws the first pages' sheets), so we never pay for a
    # variant no read page uses.
    db.set_status(book_id, "segmenting", "splitting into read-aloud pages")
    db.clear_segmentation(book_id)
    n = segment(book_id, registry,
                workers=int(os.environ.get("STORY_SEGMENT_WORKERS", "4")))
    db.set_num_pages(book_id, n)

    # 3. warm the first few page images (drawing just the roster sheets they need)
    # so the book opens instantly.
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
