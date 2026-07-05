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
indicate the ship.

For each one that appears, choose the best-fitting variant id by what THIS page actually \
depicts. An entity's variant can change PARTWAY through the chapter (a ship is undamaged early \
on and only gets wrecked later; a setting is decorated, then bare). The pages below are in \
reading order: keep the EARLIER variant for pages before the change, and switch to the new \
variant only from the page where the change actually happens onward -- match each variant's \
described difference to the page text. The chapter spans are a rough hint only (this is chapter \
{chapter_num}); the page's own description wins, even within a single chapter.

Also, for each SETTING (a place you can go inside or stand on -- a building, ship, room, cave), say \
HOW the scene shows it, as a "view":
- Leave "view" as "" if the scene shows the WHOLE place FROM OUTSIDE (the entire building or ship in \
view, e.g. a ship sailing on the sea, a house seen from the street).
- Otherwise the scene happens INSIDE or ON a specific PART of it. Set "view" to a SHORT lowercase \
label naming that exact spot the way the STORY depicts it -- e.g. "lobby", "rooftop", "the kitchen", \
"the fifth-floor apartment", "ship's deck", "the captain's cabin", "the cellar". Pick the labels from \
what the pages actually show -- do NOT invent a part (a rooftop, a deck) the story never uses. Use the \
SAME label every time the same spot appears, so it gets ONE consistent reference.
An ordinary object you merely look at -- a painting, a book, a sword, a chair -- is never a place: \
always leave its "view" as "".

Return JSON only:
{{"pages": [{{"id": <page id>, "props": [{{"entity_id": "<id>", "variant_id": "<variant id>", "view": "<short label or empty>"}}]}}]}}
Only include a page if it has props; omit anything that does not actually appear.

SETTINGS/PROPS:
{props}

PAGES:
{pages}
"""


def _llm_map_props(props, spreads, chapter_num):
    """Ask the (cheap) model which settings/props appear on each page, and -- for a
    setting -- which specific named view the scene needs (or "" for the whole thing
    from outside). Returns {page_id: [(entity_id, variant_id, view), ...]}."""
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
        out[pg.get("id")] = [(p.get("entity_id"), p.get("variant_id"), p.get("view", ""))
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

    def _norm_view(view, e):
        # only a SETTING has a non-whole view; an object you look at is always whole
        if e.get("type") != "setting":
            return ""
        v = (view or "").strip().lower()
        if v in ("", "whole", "exterior", "outside", "none", "the whole thing"):
            return ""
        if v == "aboard":     # legacy two-value tag
            v = "deck"
        return v[:40]

    def add(spread, eid, vid, view, force_view=True):
        e = prop_by_id.get(eid)
        if not e or spread is None:
            return
        if vid not in {v["id"] for v in e.get("variants", [])}:
            vid = _variant_for_chapter(e, chapter_num)   # validate / fall back
        view = _norm_view(view, e)
        cast = spread.setdefault("cast", [])
        # view = "" means show the whole thing from outside; a short label (e.g.
        # "lobby", "deck") means the scene is at that specific spot -> drives which
        # reference sheet the renderer attaches. Update an entry the segmenter added.
        existing = next((c for c in cast if c.get("entity_id") == eid), None)
        if existing is None:
            cast.append({"entity_id": eid, "variant_id": vid, "view": view})
        elif force_view or not existing.get("view"):
            existing["view"] = view
            existing.setdefault("variant_id", vid)
        if eid not in in_chapter:
            chapter_cast.append({"entity_id": eid, "variant_id": vid,
                                 "name": e.get("name", ""), "from_registry": True})
            in_chapter.add(eid)

    # primary: model pass (best-effort) -- authoritative on the view
    try:
        mapped = _llm_map_props(props, bible.get("spreads", []), chapter_num)
        for pid, items in mapped.items():
            for eid, vid, view in items:
                add(spread_by_id.get(pid), eid, vid, view)
    except Exception as ex:  # noqa: BLE001
        print(f"[props] model pass failed: {type(ex).__name__}: {ex}", flush=True)

    # floor: literal name/alias match (strip leading articles to be less brittle).
    # Only fills a prop the model pass missed -- never overwrites its view.
    for e in props:
        names = [e.get("name", "")] + (e.get("aliases") or [])
        pats = [re.compile(r"\b" + re.escape(re.sub(r"(?i)^(the|a|an)\s+", "", n)) + r"\b", re.I)
                for n in names if n and len(re.sub(r"(?i)^(the|a|an)\s+", "", n)) >= 4]
        vid = _variant_for_chapter(e, chapter_num)
        for s in bible.get("spreads", []):
            text = f"{s.get('setting','')} {s.get('illustration_brief','')}"
            if any(p.search(text) for p in pats):
                add(s, e["id"], vid, "", force_view=False)

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


_GUT_START = re.compile(r"\*\*\*\s*START OF TH(?:E|IS) PROJECT GUTENBERG EBOOK.*?\*\*\*", re.I | re.S)
_GUT_END = re.compile(r"\*\*\*\s*END OF TH(?:E|IS) PROJECT GUTENBERG EBOOK.*?\*\*\*", re.I | re.S)


def _strip_gutenberg(text: str) -> str:
    """Keep only the text between the Project Gutenberg START/END markers (drops the
    license header/footer). No-op for non-Gutenberg books."""
    m = _GUT_START.search(text)
    if m:
        text = text[m.end():]
    m = _GUT_END.search(text)
    if m:
        text = text[:m.start()]
    return text


FRONT_MATTER_PROMPT = """Here is the BEGINNING of a book's text. It may open with FRONT MATTER that is NOT \
the story -- a title page, a table of contents, a list of illustrations, a dedication, an epigraph, a \
publisher/copyright notice, a Project Gutenberg notice -- before the actual narrative starts.

Return the SHORT verbatim snippet (8-15 consecutive words, copied EXACTLY from the text) at which the \
real STORY narrative begins. If the text already begins with the story, return its first ~12 words.

Return JSON only: {{"start": "<verbatim snippet>"}}

TEXT:
{head}
"""


def _story_start_offset(text: str) -> int:
    """Offset to trim leading front matter to, located by the model. 0 if none/unsure."""
    from pipeline import gem
    from pipeline.config import CHAPTER_MODEL
    try:
        data = gem.text_json(FRONT_MATTER_PROMPT.format(head=text[:6000]), model=CHAPTER_MODEL)
        snip = (data.get("start") or "").strip()
    except Exception as ex:  # noqa: BLE001
        print(f"[frontmatter] trim failed: {type(ex).__name__}: {ex}", flush=True)
        return 0
    toks = re.findall(r"\w+", snip)[:8]
    if not toks:
        return 0
    m = re.compile(r"\W+".join(re.escape(t) for t in toks), re.I).search(text)
    off = m.start() if m else 0
    # only trim a genuinely-leading region, and never trim away most of the text
    if 0 < off < len(text) * 0.4 and len(text) - off > 500:
        print(f"[frontmatter] trimmed {off} leading chars of front matter", flush=True)
        return off
    return 0


def _prep_units(units: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Strip front matter from the first unit, then split any oversized unit so each
    segmentation call targets ~PAGES_PER_CALL pages -- the model caps how many page
    anchors it emits per call, so a whole-book-in-one-document must be chunked or it
    truncates to a handful of huge pages."""
    from pipeline.config import WORDS_PER_PAGE
    units = [(t, _strip_gutenberg(txt).strip()) for (t, txt) in units if txt and txt.strip()]
    if units:
        t0, txt0 = units[0]
        off = _story_start_offset(txt0)
        if off:
            units[0] = (t0, txt0[off:].lstrip())
    PAGES_PER_CALL = 14
    chunk_words = max(1200, PAGES_PER_CALL * WORDS_PER_PAGE)
    out = []
    for title, txt in units:
        if len(txt.split()) <= chunk_words * 1.4:   # fits in one call -> keep as-is
            out.append((title, txt))
            continue
        parts = chunk_text(txt, size=chunk_words)
        generic = (not title) or title.lower() in ("story", "part")
        for j, p in enumerate(parts):
            out.append((f"Part {j + 1}" if generic else f"{title} ({j + 1})", p))
    return out


def chapter_units() -> list[tuple[str, str]]:
    """Ordered (title, text) units for the whole book, with front matter trimmed and
    oversized units chunked. EPUB -> real story chapters (front/back matter skipped);
    PDF -> the whole body."""
    from pipeline import extract
    from pipeline.config import IS_EPUB, PDF
    if IS_EPUB:
        # primary: let the model confirm chapters from a skeleton of the book;
        # fall back to TOC/heuristic parsing if that call fails or returns nothing
        units = None
        try:
            spine, skel = _epub_skeleton()
            chosen = _llm_chapter_units(spine, skel)
            if chosen:
                print(f"[chapters] LLM confirmed {len(chosen)}/{len(spine)} "
                      f"spine docs as chapters", flush=True)
                units = chosen
            else:
                print("[chapters] LLM returned no chapters; using TOC heuristic", flush=True)
        except Exception as ex:  # noqa: BLE001
            print(f"[chapters] LLM plan failed ({type(ex).__name__}: {ex}); "
                  "using TOC heuristic", flush=True)
        if units is None:
            units = extract.epub_chapters_titled(PDF)
        return _prep_units(units)
    return _prep_units([("Story", extract.full_story_text())])


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
                        s.get("illustration_brief", ""), s.get("cast", []),
                        image_anchor=s.get("image_anchor"))
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
    from pipeline import extract

    # 0. fill in the book's title/author from the source file's own metadata, for
    # any field the user left blank at upload (best-effort, never clobbers typed
    # values). Populates the library + reading-history labels.
    meta = extract.book_metadata()
    changed = db.fill_metadata(book_id, meta.get("title"), meta.get("author"))
    if changed:
        print(f"[process] filled book metadata from source: {changed}", flush=True)

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


def backfill_metadata():
    """One-off: fill title/author for already-processed books whose fields were
    left blank at upload, reading each book's own source file (stored as a blob)
    back out to a temp file to extract its container metadata."""
    import tempfile
    from pathlib import Path
    from pipeline import extract
    for b in db.list_books():
        if (b["title"] or "").strip() and (b["author"] or "").strip():
            continue
        f = db.get_book_file(b["id"])
        if not f:
            continue
        _mime, data = f
        suffix = Path(b["filename"] or "book.pdf").suffix or ".pdf"
        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            tmp.write(data)
            tmp.flush()
            meta = extract.book_metadata(Path(tmp.name))
        changed = db.fill_metadata(b["id"], meta.get("title"), meta.get("author"))
        print(f"book {b['id']} ({b['filename']}): {changed or 'no metadata found'}",
              flush=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--backfill-metadata":
        backfill_metadata()
        return
    book_id = int(sys.argv[1])
    try:
        run(book_id)
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc()
        db.set_status(book_id, "failed", f"{type(ex).__name__}: {str(ex)[:300]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
