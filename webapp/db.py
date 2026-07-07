"""SQLite storage for the storyteller web app.

Everything the app serves lives here -- book metadata, the uploaded book file,
the entity registry, roster reference sheets (image blobs), the per-page text,
and lazily-generated scene images (image blobs) -- so a single database file (a
mounted volume in Docker) is the whole persistent state.

Connections are opened per-call (WAL mode handles the server's threads + the
processing subprocess writing concurrently). Image bytes are stored as BLOBs.
"""
import json
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("STORY_APP_DB", str(ROOT / "output" / "storyteller.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT,
    author        TEXT,
    filename      TEXT,
    style         TEXT,
    words_per_page INTEGER,
    age           TEXT,
    status        TEXT,        -- queued|extracting|registry|roster|segmenting|ready|failed
    detail        TEXT,        -- human progress detail / error message
    num_pages     INTEGER DEFAULT 0,
    seg_ver       INTEGER DEFAULT 0,   -- bumps on re-segmentation (cache-busts image URLs)
    created_at    REAL
);
CREATE TABLE IF NOT EXISTS book_files (
    book_id  INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
    mime     TEXT,
    data     BLOB
);
CREATE TABLE IF NOT EXISTS registries (
    book_id  INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
    json     TEXT
);
CREATE TABLE IF NOT EXISTS chapters (
    book_id    INTEGER REFERENCES books(id) ON DELETE CASCADE,
    idx        INTEGER,
    title      TEXT,
    first_page INTEGER,
    cast_json  TEXT,
    PRIMARY KEY (book_id, idx)
);
CREATE TABLE IF NOT EXISTS pages (
    book_id     INTEGER REFERENCES books(id) ON DELETE CASCADE,
    idx         INTEGER,
    chapter_idx INTEGER,
    title       TEXT,
    read_text   TEXT,
    setting     TEXT,
    brief       TEXT,
    cast_json   TEXT,
    image_anchor TEXT,
    PRIMARY KEY (book_id, idx)
);
CREATE TABLE IF NOT EXISTS sheets (
    book_id    INTEGER REFERENCES books(id) ON DELETE CASCADE,
    entity_id  TEXT,
    variant_id TEXT,
    mime       TEXT,
    data       BLOB,
    PRIMARY KEY (book_id, entity_id, variant_id)
);
CREATE TABLE IF NOT EXISTS scenes (
    book_id  INTEGER REFERENCES books(id) ON DELETE CASCADE,
    idx      INTEGER,
    status   TEXT,            -- generating|done|failed
    mime     TEXT,
    data     BLOB,
    score    REAL,
    detail   TEXT,
    updated_at REAL,
    PRIMARY KEY (book_id, idx)
);
-- debug history: every generation run of a scene, and every attempt within it
-- (including rejected candidates), with the prompt and the critic's verdict.
CREATE TABLE IF NOT EXISTS scene_gens (
    book_id    INTEGER REFERENCES books(id) ON DELETE CASCADE,
    idx        INTEGER,
    gen_id     INTEGER,         -- 1,2,3... each (re)generation of this page
    brief      TEXT,
    states     TEXT,            -- JSON {character: state}
    chosen     INTEGER,         -- attempt number that was kept
    final_score REAL,
    created_at REAL,
    PRIMARY KEY (book_id, idx, gen_id)
);
CREATE TABLE IF NOT EXISTS scene_attempts (
    book_id    INTEGER REFERENCES books(id) ON DELETE CASCADE,
    idx        INTEGER,
    gen_id     INTEGER,
    attempt    INTEGER,
    mode       TEXT,            -- fresh|revise
    prompt     TEXT,            -- full image-generation prompt
    mime       TEXT,
    data       BLOB,            -- candidate image bytes (kept even if rejected)
    critique   TEXT,            -- full critic JSON (sub-scores, issues, fix_hint)
    min_score  REAL,
    avg_score  REAL,
    created_at REAL,
    PRIMARY KEY (book_id, idx, gen_id, attempt)
);
-- debug history for roster reference sheets (same idea as scene_gens/attempts,
-- keyed by entity+variant instead of page idx).
CREATE TABLE IF NOT EXISTS sheet_gens (
    book_id    INTEGER REFERENCES books(id) ON DELETE CASCADE,
    entity_id  TEXT,
    variant_id TEXT,
    gen_id     INTEGER,
    descr      TEXT,
    chosen     INTEGER,
    final_score REAL,
    created_at REAL,
    PRIMARY KEY (book_id, entity_id, variant_id, gen_id)
);
CREATE TABLE IF NOT EXISTS sheet_attempts (
    book_id    INTEGER REFERENCES books(id) ON DELETE CASCADE,
    entity_id  TEXT,
    variant_id TEXT,
    gen_id     INTEGER,
    attempt    INTEGER,
    prompt     TEXT,
    mime       TEXT,
    data       BLOB,
    critique   TEXT,
    min_score  REAL,
    avg_score  REAL,
    created_at REAL,
    PRIMARY KEY (book_id, entity_id, variant_id, gen_id, attempt)
);
CREATE TABLE IF NOT EXISTS style_samples (
    style_key  TEXT PRIMARY KEY,
    mime       TEXT,
    data       BLOB
);
-- ===== batch "illustrate the whole book" bake =====
-- One row per book bake: the overall state machine + progress for the hub/roster UI.
CREATE TABLE IF NOT EXISTS batch_bake (
    book_id     INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
    status      TEXT,        -- roster_review|baking|done|failed|cancelled
    round       INTEGER DEFAULT 0,
    total_pages INTEGER DEFAULT 0,
    done_pages  INTEGER DEFAULT 0,
    detail      TEXT,
    created_at  REAL,
    updated_at  REAL
);
-- Live per-page state carried ACROSS async batch rounds (the persisted form of the
-- interactive loop's carry-forward). Image bytes: draft_blob = current img2img seed
-- for a revise (full display quality); best_blob = best candidate so far. Scalar
-- carry-forward (mode/pending_defect/escalate/edit_instr/ref_chars) rides in carry_json.
CREATE TABLE IF NOT EXISTS batch_page_state (
    book_id      INTEGER REFERENCES books(id) ON DELETE CASCADE,
    idx          INTEGER,
    status       TEXT,       -- pending|drafting|critiquing|verifying|revising|done|failed
    round        INTEGER DEFAULT 0,
    attempt      INTEGER DEFAULT 0,   -- attempts used so far (also the scene_attempts counter)
    gen_id       INTEGER,             -- the scene_gens gen_id for this bake
    done         INTEGER DEFAULT 0,
    best_score   REAL,
    best_attempt INTEGER,
    best_blob    BLOB,
    draft_blob   BLOB,
    carry_json   TEXT,
    updated_at   REAL,
    PRIMARY KEY (book_id, idx)
);
-- Submitted Batch API jobs, so a restarted bake reattaches instead of resubmitting.
CREATE TABLE IF NOT EXISTS batch_jobs (
    book_id    INTEGER REFERENCES books(id) ON DELETE CASCADE,
    round      INTEGER,
    kind       TEXT,         -- generate|critique|verify|judge
    job_name   TEXT,
    state      TEXT,         -- last-seen JOB_STATE_*
    created_at REAL,
    updated_at REAL,
    PRIMARY KEY (book_id, round, kind)
);
CREATE TABLE IF NOT EXISTS progress (
    book_id    INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
    position   INTEGER,
    updated_at REAL
);
-- reading history: one row per reading *session* (a stretch of reading with no
-- big gap). Progress reports arrive debounced every page turn; instead of a row
-- per report we coalesce consecutive reports on the same book into one session
-- (start/end position + first/last time + how many turns).
CREATE TABLE IF NOT EXISTS reading_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER REFERENCES books(id) ON DELETE CASCADE,
    started_at  REAL,
    updated_at  REAL,
    start_pos   INTEGER,
    end_pos     INTEGER,
    events      INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS reading_log_book ON reading_log(book_id, updated_at);
"""


def conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB), timeout=60)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=60000")
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        # migrate older DBs that predate columns added above
        cols = {r["name"] for r in c.execute("PRAGMA table_info(books)")}
        if "seg_ver" not in cols:
            c.execute("ALTER TABLE books ADD COLUMN seg_ver INTEGER DEFAULT 0")
        if "illustration_mode" not in cols:   # 'lazy' (default) | 'batch'
            c.execute("ALTER TABLE books ADD COLUMN illustration_mode TEXT DEFAULT 'lazy'")
        pcols = {r["name"] for r in c.execute("PRAGMA table_info(pages)")}
        if "image_anchor" not in pcols:
            c.execute("ALTER TABLE pages ADD COLUMN image_anchor TEXT")
        scols = {r["name"] for r in c.execute("PRAGMA table_info(scenes)")}
        if "trace" not in scols:   # per-attempt critique/revise log (JSON)
            c.execute("ALTER TABLE scenes ADD COLUMN trace TEXT")


# ---------------- books ----------------

def create_book(title, author, filename, style, words_per_page, age,
                mime, data) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO books(title,author,filename,style,words_per_page,age,"
            "status,detail,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (title, author, filename, style, words_per_page, age,
             "queued", "queued for processing", time.time()))
        bid = cur.lastrowid
        c.execute("INSERT INTO book_files(book_id,mime,data) VALUES (?,?,?)",
                  (bid, mime, data))
        c.execute("INSERT INTO progress(book_id,position,updated_at) VALUES (?,?,?)",
                  (bid, 0, time.time()))
    return bid


def set_status(book_id, status, detail=None):
    with conn() as c:
        c.execute("UPDATE books SET status=?, detail=? WHERE id=?",
                  (status, detail, book_id))


def set_num_pages(book_id, n):
    with conn() as c:
        c.execute("UPDATE books SET num_pages=? WHERE id=?", (n, book_id))


def fill_metadata(book_id, title=None, author=None, force=False) -> dict:
    """Fill in a book's title/author from extracted metadata. By default only
    fills fields left blank (so a title/author the user typed at upload is never
    overwritten); force=True overwrites even a non-blank field (used by the
    one-off backfill to re-clean auto-derived values). Returns what changed."""
    changed = {}
    with conn() as c:
        r = c.execute("SELECT title, author FROM books WHERE id=?",
                      (book_id,)).fetchone()
        if not r:
            return changed
        title = (title or "").strip()
        author = (author or "").strip()
        if title and (force or not (r["title"] or "").strip()) and title != r["title"]:
            changed["title"] = title
        if author and (force or not (r["author"] or "").strip()) and author != r["author"]:
            changed["author"] = author
        if changed:
            c.execute("UPDATE books SET title=COALESCE(?,title), "
                      "author=COALESCE(?,author) WHERE id=?",
                      (changed.get("title"), changed.get("author"), book_id))
    return changed


def get_book(book_id) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        return dict(r) if r else None


def list_books() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT b.*, COALESCE(p.position,0) AS position "
            "FROM books b LEFT JOIN progress p ON p.book_id=b.id "
            "ORDER BY b.created_at DESC").fetchall()
        return [dict(r) for r in rows]


def delete_book(book_id):
    with conn() as c:
        c.execute("DELETE FROM books WHERE id=?", (book_id,))


IN_PROGRESS = ("queued", "extracting", "registry", "roster", "segmenting",
               "processing", "warming")


def books_in_progress() -> list[int]:
    """Books whose processing was interrupted (non-terminal status) -- used to
    re-launch them after a restart."""
    qs = ",".join("?" * len(IN_PROGRESS))
    with conn() as c:
        rows = c.execute(f"SELECT id FROM books WHERE status IN ({qs})",
                         IN_PROGRESS).fetchall()
        return [r["id"] for r in rows]


def clear_segmentation(book_id):
    """Drop a book's chapters/pages so segmentation can be redone cleanly (global
    page numbering depends on every chapter, so a partial redo can't be stitched).
    Scenes are dropped too: re-segmentation can change a page's text, which would
    make any image keyed to that page index stale."""
    with conn() as c:
        c.execute("DELETE FROM pages WHERE book_id=?", (book_id,))
        c.execute("DELETE FROM chapters WHERE book_id=?", (book_id,))
        c.execute("DELETE FROM scenes WHERE book_id=?", (book_id,))
        # bump the segmentation version so cached image URLs (?v=) refresh
        c.execute("UPDATE books SET seg_ver = seg_ver + 1 WHERE id=?", (book_id,))


def get_book_file(book_id) -> tuple[str, bytes] | None:
    with conn() as c:
        r = c.execute("SELECT mime,data FROM book_files WHERE book_id=?",
                      (book_id,)).fetchone()
        return (r["mime"], r["data"]) if r else None


# ---------------- registry ----------------

def save_registry(book_id, registry: dict):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO registries(book_id,json) VALUES (?,?)",
                  (book_id, json.dumps(registry, ensure_ascii=False)))


def get_registry(book_id) -> dict:
    with conn() as c:
        r = c.execute("SELECT json FROM registries WHERE book_id=?",
                      (book_id,)).fetchone()
        return json.loads(r["json"]) if r else {"entities": []}


# ---------------- chapters + pages ----------------

def add_chapter(book_id, idx, title, first_page, cast):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO chapters(book_id,idx,title,first_page,"
                  "cast_json) VALUES (?,?,?,?,?)",
                  (book_id, idx, title, first_page,
                   json.dumps(cast, ensure_ascii=False)))


def get_chapters(book_id) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT idx,title,first_page FROM chapters "
                         "WHERE book_id=? ORDER BY idx", (book_id,)).fetchall()
        return [dict(r) for r in rows]


def get_chapter_cast(book_id, chapter_idx) -> list[dict]:
    with conn() as c:
        r = c.execute("SELECT cast_json FROM chapters WHERE book_id=? AND idx=?",
                      (book_id, chapter_idx)).fetchone()
        return json.loads(r["cast_json"]) if r and r["cast_json"] else []


def add_page(book_id, idx, chapter_idx, title, read_text, setting, brief, cast,
             image_anchor=None):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO pages(book_id,idx,chapter_idx,title,"
                  "read_text,setting,brief,cast_json,image_anchor) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (book_id, idx, chapter_idx, title, read_text, setting, brief,
                   json.dumps(cast, ensure_ascii=False), image_anchor))


def get_pages(book_id) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT idx,chapter_idx,title,read_text,image_anchor FROM pages "
            "WHERE book_id=? ORDER BY idx", (book_id,)).fetchall()
        return [dict(r) for r in rows]


def get_page(book_id, idx) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM pages WHERE book_id=? AND idx=?",
                      (book_id, idx)).fetchone()
        return dict(r) if r else None


# ---------------- sheets (roster reference images) ----------------

def save_sheet(book_id, entity_id, variant_id, data, mime="image/webp"):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO sheets(book_id,entity_id,variant_id,"
                  "mime,data) VALUES (?,?,?,?,?)",
                  (book_id, entity_id, variant_id, mime, data))


def get_sheet(book_id, entity_id, variant_id) -> bytes | None:
    with conn() as c:
        r = c.execute("SELECT data FROM sheets WHERE book_id=? AND entity_id=? "
                      "AND variant_id=?", (book_id, entity_id, variant_id)).fetchone()
        return r["data"] if r else None


def get_any_sheet(book_id, entity_id, exclude_variant_id=None) -> bytes | None:
    """Any already-drawn *real* variant sheet for this entity (optionally excluding
    one variant) -- used as an identity reference when drawing another of its
    variants. Synthetic aspect refs (variant ids starting with '__', e.g. the
    interior/aboard view) are excluded so they don't cross-contaminate."""
    with conn() as c:
        if exclude_variant_id is not None:
            r = c.execute("SELECT data FROM sheets WHERE book_id=? AND entity_id=? "
                          "AND variant_id != ? AND substr(variant_id,1,2) != '__' LIMIT 1",
                          (book_id, entity_id, exclude_variant_id)).fetchone()
        else:
            r = c.execute("SELECT data FROM sheets WHERE book_id=? AND entity_id=? "
                          "AND substr(variant_id,1,2) != '__' LIMIT 1",
                          (book_id, entity_id)).fetchone()
        return r["data"] if r else None


def list_sheets(book_id) -> list:
    """(entity_id, variant_id) for every already-drawn sheet of this book."""
    with conn() as c:
        return [(r["entity_id"], r["variant_id"]) for r in c.execute(
            "SELECT entity_id, variant_id FROM sheets WHERE book_id=? AND length(data)>0",
            (book_id,))]


def iter_scene_blobs(book_id) -> list:
    """(idx, data) for every stored scene image of this book."""
    with conn() as c:
        return [(r["idx"], r["data"]) for r in c.execute(
            "SELECT idx, data FROM scenes WHERE book_id=? AND length(data)>0", (book_id,))]


def update_scene_blob(book_id, idx, data, mime="image/webp"):
    with conn() as c:
        c.execute("UPDATE scenes SET data=?, mime=? WHERE book_id=? AND idx=?",
                  (data, mime, book_id, idx))


def has_sheet(book_id, entity_id, variant_id) -> bool:
    with conn() as c:
        r = c.execute("SELECT 1 FROM sheets WHERE book_id=? AND entity_id=? AND "
                      "variant_id=?", (book_id, entity_id, variant_id)).fetchone()
        return r is not None


# ---------------- scenes (lazily generated page images) ----------------

def scene_row(book_id, idx) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT status,score,detail FROM scenes WHERE book_id=? "
                      "AND idx=?", (book_id, idx)).fetchone()
        return dict(r) if r else None


def scene_status(book_id, idx) -> str | None:
    r = scene_row(book_id, idx)
    return r["status"] if r else None


def scene_score(book_id, idx):
    r = scene_row(book_id, idx)
    return r["score"] if r else None


def scene_data(book_id, idx) -> bytes | None:
    """The finished image bytes, or None if not generated yet."""
    with conn() as c:
        r = c.execute("SELECT data FROM scenes WHERE book_id=? AND idx=? AND "
                      "status='done'", (book_id, idx)).fetchone()
        return r["data"] if r and r["data"] else None


def delete_scene(book_id, idx):
    """Drop one page's stored scene image so it regenerates on next request."""
    with conn() as c:
        c.execute("DELETE FROM scenes WHERE book_id=? AND idx=?", (book_id, idx))


def scene_set_status(book_id, idx, status, detail=None):
    with conn() as c:
        c.execute("INSERT INTO scenes(book_id,idx,status,detail,updated_at) "
                  "VALUES (?,?,?,?,?) ON CONFLICT(book_id,idx) DO UPDATE SET "
                  "status=excluded.status, detail=excluded.detail, "
                  "updated_at=excluded.updated_at",
                  (book_id, idx, status, detail, time.time()))


def scene_store(book_id, idx, data, score, mime="image/webp", trace=None):
    with conn() as c:
        c.execute("INSERT INTO scenes(book_id,idx,status,mime,data,score,trace,updated_at) "
                  "VALUES (?,?,'done',?,?,?,?,?) ON CONFLICT(book_id,idx) DO UPDATE SET "
                  "status='done', mime=excluded.mime, data=excluded.data, "
                  "score=excluded.score, trace=excluded.trace, detail=NULL, "
                  "updated_at=excluded.updated_at",
                  (book_id, idx, mime, data, score, trace, time.time()))


def scene_trace(book_id, idx):
    """The per-attempt critique/revise log (JSON dict) for a drawn scene, or None."""
    with conn() as c:
        r = c.execute("SELECT trace FROM scenes WHERE book_id=? AND idx=?",
                      (book_id, idx)).fetchone()
    if not r or not r["trace"]:
        return None
    try:
        return json.loads(r["trace"])
    except (ValueError, TypeError):
        return None


# ---- debug generation history (scene_gens / scene_attempts) ----

def next_gen_id(book_id, idx) -> int:
    with conn() as c:
        r = c.execute("SELECT COALESCE(MAX(gen_id),0)+1 AS g FROM scene_gens "
                      "WHERE book_id=? AND idx=?", (book_id, idx)).fetchone()
    return r["g"]


def scene_attempt_add(book_id, idx, gen_id, attempt, mode, prompt, data,
                      critique, min_score, avg_score, mime="image/webp"):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO scene_attempts(book_id,idx,gen_id,attempt,mode,"
                  "prompt,mime,data,critique,min_score,avg_score,created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (book_id, idx, gen_id, attempt, mode, prompt, mime, data,
                   critique, min_score, avg_score, time.time()))


def scene_gen_add(book_id, idx, gen_id, brief, states, chosen, final_score):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO scene_gens(book_id,idx,gen_id,brief,states,"
                  "chosen,final_score,created_at) VALUES (?,?,?,?,?,?,?,?)",
                  (book_id, idx, gen_id, brief, states, chosen, final_score, time.time()))


def debug_pages(book_id) -> list[dict]:
    """Pages that have generation history: idx, title, #gens, #attempts, best score."""
    with conn() as c:
        rows = c.execute(
            "SELECT g.idx AS idx, COUNT(DISTINCT g.gen_id) AS gens, "
            "COUNT(a.attempt) AS attempts, MAX(g.final_score) AS score "
            "FROM scene_gens g LEFT JOIN scene_attempts a "
            "ON a.book_id=g.book_id AND a.idx=g.idx AND a.gen_id=g.gen_id "
            "WHERE g.book_id=? GROUP BY g.idx ORDER BY g.idx", (book_id,)).fetchall()
        pages = {p["idx"]: p["title"] for p in
                 c.execute("SELECT idx,title FROM pages WHERE book_id=?", (book_id,))}
    return [{"idx": r["idx"], "title": pages.get(r["idx"], ""), "gens": r["gens"],
             "attempts": r["attempts"], "score": r["score"]} for r in rows]


def page_attempt_rows(book_id) -> list[dict]:
    """For each page's current (latest) scene generation: how many image attempts
    it took (n), which attempt was kept (chosen), and the kept image's score.
    Feeds the settings page's 'succeeded on the Nth try' breakdown."""
    with conn() as c:
        rows = c.execute(
            "SELECT g.idx AS idx, g.chosen AS chosen, g.final_score AS score, "
            "  (SELECT COUNT(*) FROM scene_attempts a "
            "     WHERE a.book_id=g.book_id AND a.idx=g.idx AND a.gen_id=g.gen_id) AS n "
            "FROM scene_gens g "
            "JOIN (SELECT idx, MAX(gen_id) AS mg FROM scene_gens "
            "        WHERE book_id=? GROUP BY idx) m ON m.idx=g.idx AND m.mg=g.gen_id "
            "WHERE g.book_id=? ORDER BY g.idx", (book_id, book_id)).fetchall()
    return [{"idx": r["idx"], "chosen": r["chosen"], "score": r["score"], "n": r["n"]}
            for r in rows]


def scene_history(book_id, idx) -> list[dict]:
    """Full history for one page: a list of generations (newest first), each with
    its attempts (prompt + critique + metadata, no image blobs)."""
    with conn() as c:
        gens = c.execute("SELECT * FROM scene_gens WHERE book_id=? AND idx=? "
                         "ORDER BY gen_id DESC", (book_id, idx)).fetchall()
        atts = c.execute("SELECT book_id,idx,gen_id,attempt,mode,prompt,critique,"
                         "min_score,avg_score,created_at FROM scene_attempts "
                         "WHERE book_id=? AND idx=? ORDER BY gen_id DESC, attempt",
                         (book_id, idx)).fetchall()
    by_gen = {}
    for a in atts:
        by_gen.setdefault(a["gen_id"], []).append({
            "attempt": a["attempt"], "mode": a["mode"], "prompt": a["prompt"],
            "critique": json.loads(a["critique"]) if a["critique"] else None,
            "min": a["min_score"], "avg": a["avg_score"], "created_at": a["created_at"],
        })
    out = []
    for g in gens:
        out.append({
            "gen_id": g["gen_id"], "brief": g["brief"],
            "states": json.loads(g["states"]) if g["states"] else {},
            "chosen": g["chosen"], "final_score": g["final_score"],
            "created_at": g["created_at"], "attempts": by_gen.get(g["gen_id"], []),
        })
    return out


def scene_attempt_image(book_id, idx, gen_id, attempt):
    with conn() as c:
        r = c.execute("SELECT mime,data FROM scene_attempts WHERE book_id=? AND idx=? "
                      "AND gen_id=? AND attempt=?", (book_id, idx, gen_id, attempt)).fetchone()
    return (r["mime"], r["data"]) if r else (None, None)


# ---- debug generation history for roster sheets ----

def delete_sheet(book_id, entity_id, variant_id):
    with conn() as c:
        c.execute("DELETE FROM sheets WHERE book_id=? AND entity_id=? AND variant_id=?",
                  (book_id, entity_id, variant_id))


def next_sheet_gen_id(book_id, entity_id, variant_id) -> int:
    with conn() as c:
        r = c.execute("SELECT COALESCE(MAX(gen_id),0)+1 AS g FROM sheet_gens "
                      "WHERE book_id=? AND entity_id=? AND variant_id=?",
                      (book_id, entity_id, variant_id)).fetchone()
    return r["g"]


def sheet_attempt_add(book_id, entity_id, variant_id, gen_id, attempt, prompt, data,
                      critique, min_score, avg_score, mime="image/webp"):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO sheet_attempts(book_id,entity_id,variant_id,gen_id,"
                  "attempt,prompt,mime,data,critique,min_score,avg_score,created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (book_id, entity_id, variant_id, gen_id, attempt, prompt, mime, data,
                   critique, min_score, avg_score, time.time()))


def sheet_gen_add(book_id, entity_id, variant_id, gen_id, descr, chosen, final_score):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO sheet_gens(book_id,entity_id,variant_id,gen_id,"
                  "descr,chosen,final_score,created_at) VALUES (?,?,?,?,?,?,?,?)",
                  (book_id, entity_id, variant_id, gen_id, descr, chosen, final_score, time.time()))


def debug_sheets(book_id) -> list[dict]:
    """Roster sheets that have generation history: entity/variant, #gens, best score."""
    with conn() as c:
        rows = c.execute(
            "SELECT g.entity_id AS eid, g.variant_id AS vid, COUNT(DISTINCT g.gen_id) AS gens, "
            "COUNT(a.attempt) AS attempts, MAX(g.final_score) AS score "
            "FROM sheet_gens g LEFT JOIN sheet_attempts a ON a.book_id=g.book_id "
            "AND a.entity_id=g.entity_id AND a.variant_id=g.variant_id AND a.gen_id=g.gen_id "
            "WHERE g.book_id=? GROUP BY g.entity_id, g.variant_id ORDER BY g.entity_id, g.variant_id",
            (book_id,)).fetchall()
    return [{"entity_id": r["eid"], "variant_id": r["vid"], "gens": r["gens"],
             "attempts": r["attempts"], "score": r["score"]} for r in rows]


def sheet_history(book_id, entity_id, variant_id) -> list[dict]:
    """Full history for one sheet: generations (newest first), each with its attempts."""
    with conn() as c:
        gens = c.execute("SELECT * FROM sheet_gens WHERE book_id=? AND entity_id=? AND "
                         "variant_id=? ORDER BY gen_id DESC", (book_id, entity_id, variant_id)).fetchall()
        atts = c.execute("SELECT gen_id,attempt,prompt,critique,min_score,avg_score,created_at "
                         "FROM sheet_attempts WHERE book_id=? AND entity_id=? AND variant_id=? "
                         "ORDER BY gen_id DESC, attempt", (book_id, entity_id, variant_id)).fetchall()
    by_gen = {}
    for a in atts:
        by_gen.setdefault(a["gen_id"], []).append({
            "attempt": a["attempt"], "prompt": a["prompt"],
            "critique": json.loads(a["critique"]) if a["critique"] else None,
            "min": a["min_score"], "avg": a["avg_score"], "created_at": a["created_at"],
        })
    return [{"gen_id": g["gen_id"], "descr": g["descr"], "chosen": g["chosen"],
             "final_score": g["final_score"], "created_at": g["created_at"],
             "attempts": by_gen.get(g["gen_id"], [])} for g in gens]


def sheet_attempt_image(book_id, entity_id, variant_id, gen_id, attempt):
    with conn() as c:
        r = c.execute("SELECT mime,data FROM sheet_attempts WHERE book_id=? AND entity_id=? "
                      "AND variant_id=? AND gen_id=? AND attempt=?",
                      (book_id, entity_id, variant_id, gen_id, attempt)).fetchone()
    return (r["mime"], r["data"]) if r else (None, None)


def _critique_issues(blob) -> list:
    if not blob:
        return []
    try:
        return json.loads(blob).get("issues", []) or []
    except (ValueError, TypeError):
        return []


def flagged_scenes(book_id, threshold: float = 4.0) -> list[dict]:
    """Pages whose best kept score stayed BELOW the critic's pass threshold -- i.e.
    no attempt ever passed. Includes the latest run's critic issues when recorded."""
    with conn() as c:
        rows = c.execute(
            "SELECT s.idx AS idx, s.score AS score, p.title AS title FROM scenes s "
            "LEFT JOIN pages p ON p.book_id=s.book_id AND p.idx=s.idx "
            "WHERE s.book_id=? AND s.score IS NOT NULL AND s.score < ? "
            "ORDER BY s.score, s.idx", (book_id, threshold)).fetchall()
        out = []
        for r in rows:
            g = c.execute("SELECT gen_id,chosen FROM scene_gens WHERE book_id=? AND idx=? "
                          "ORDER BY gen_id DESC LIMIT 1", (book_id, r["idx"])).fetchone()
            issues = []
            if g:
                a = c.execute("SELECT critique FROM scene_attempts WHERE book_id=? AND idx=? "
                              "AND gen_id=? AND attempt=?", (book_id, r["idx"], g["gen_id"],
                                                            g["chosen"])).fetchone()
                issues = _critique_issues(a["critique"] if a else None)
            out.append({"idx": r["idx"], "title": r["title"] or "", "score": r["score"],
                        "issues": issues})
    return out


def flagged_sheets(book_id, threshold: float = 4.0) -> list[dict]:
    """Roster sheets whose latest generation's best score stayed below the pass
    threshold (only sheets with recorded history -- drawn since debug logging)."""
    with conn() as c:
        rows = c.execute(
            "SELECT g.entity_id AS eid, g.variant_id AS vid, g.final_score AS score, "
            "g.gen_id AS gen, g.chosen AS chosen FROM sheet_gens g WHERE g.book_id=? "
            "AND g.gen_id=(SELECT MAX(gen_id) FROM sheet_gens WHERE book_id=g.book_id "
            "AND entity_id=g.entity_id AND variant_id=g.variant_id) "
            "AND g.final_score IS NOT NULL AND g.final_score < ? "
            "ORDER BY g.final_score", (book_id, threshold)).fetchall()
        out = []
        for r in rows:
            a = c.execute("SELECT critique FROM sheet_attempts WHERE book_id=? AND entity_id=? "
                          "AND variant_id=? AND gen_id=? AND attempt=?",
                          (book_id, r["eid"], r["vid"], r["gen"], r["chosen"])).fetchone()
            out.append({"entity_id": r["eid"], "variant_id": r["vid"], "score": r["score"],
                        "issues": _critique_issues(a["critique"] if a else None)})
    return out


def clear_art(book_id) -> dict:
    """Drop a book's roster sheets and scene images so they redraw (lazily) with
    the current prompts/style. Bumps seg_ver to bust cached scene image URLs.
    Keeps registry + pages (text) intact."""
    with conn() as c:
        sheets = c.execute("DELETE FROM sheets WHERE book_id=?", (book_id,)).rowcount
        scenes = c.execute("DELETE FROM scenes WHERE book_id=?", (book_id,)).rowcount
        c.execute("UPDATE books SET seg_ver = seg_ver + 1 WHERE id=?", (book_id,))
    return {"sheets": sheets, "scenes": scenes}


def reset_generating():
    """Drop scene rows stuck in 'generating' (a generation killed by a restart
    never committed an image). Removing them makes those pages eligible for
    prefetch/regeneration again instead of being skipped forever."""
    with conn() as c:
        n = c.execute("DELETE FROM scenes WHERE status='generating'").rowcount
    return n


def scene_progress(book_id) -> dict:
    """How many scenes are done / generating, for the hub's progress display."""
    with conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM scenes WHERE book_id=? "
                         "GROUP BY status", (book_id,)).fetchall()
        return {r["status"]: r["n"] for r in rows}


# ---------------- style samples (gallery thumbnails) ----------------

def get_style_sample(style_key) -> bytes | None:
    with conn() as c:
        r = c.execute("SELECT data FROM style_samples WHERE style_key=?",
                      (style_key,)).fetchone()
        return r["data"] if r else None


def save_style_sample(style_key, data, mime="image/webp"):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO style_samples(style_key,mime,data) "
                  "VALUES (?,?,?)", (style_key, mime, data))


def styles_with_samples() -> set:
    with conn() as c:
        return {r["style_key"] for r in c.execute("SELECT style_key FROM style_samples")}


# ---------------- batch bake ----------------

def set_illustration_mode(book_id, mode):
    with conn() as c:
        c.execute("UPDATE books SET illustration_mode=? WHERE id=?", (mode, book_id))


def bake_upsert(book_id, status, round=None, total_pages=None, done_pages=None, detail=None):
    """Create/update a book's bake row. Only the fields passed (non-None) are changed."""
    now = time.time()
    with conn() as c:
        c.execute("INSERT INTO batch_bake(book_id,status,round,total_pages,done_pages,"
                  "detail,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(book_id) DO UPDATE SET status=excluded.status, "
                  "round=COALESCE(?,batch_bake.round), "
                  "total_pages=COALESCE(?,batch_bake.total_pages), "
                  "done_pages=COALESCE(?,batch_bake.done_pages), "
                  "detail=COALESCE(?,batch_bake.detail), updated_at=excluded.updated_at",
                  (book_id, status, round or 0, total_pages or 0, done_pages or 0, detail,
                   now, now, round, total_pages, done_pages, detail))


def bake_get(book_id) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM batch_bake WHERE book_id=?", (book_id,)).fetchone()
        return dict(r) if r else None


def books_baking() -> list[int]:
    """Books whose bake was interrupted (status 'baking') -- relaunched on restart."""
    with conn() as c:
        return [r["book_id"] for r in
                c.execute("SELECT book_id FROM batch_bake WHERE status='baking'")]


def bps_init(book_id, idxs: list[int]):
    """Seed per-page bake state for every page (idempotent: keeps existing rows so a
    resume doesn't wipe progress)."""
    now = time.time()
    with conn() as c:
        for idx in idxs:
            c.execute("INSERT OR IGNORE INTO batch_page_state(book_id,idx,status,round,"
                      "attempt,done,updated_at) VALUES (?,?,'pending',0,0,0,?)",
                      (book_id, idx, now))


def bps_get(book_id, idx) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM batch_page_state WHERE book_id=? AND idx=?",
                      (book_id, idx)).fetchone()
        return dict(r) if r else None


def bps_all(book_id) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM batch_page_state WHERE book_id=? ORDER BY idx", (book_id,))]


def bps_actionable(book_id) -> list[int]:
    """Page indices still needing work (not done and not permanently failed)."""
    with conn() as c:
        return [r["idx"] for r in c.execute(
            "SELECT idx FROM batch_page_state WHERE book_id=? AND done=0 AND status!='failed' "
            "ORDER BY idx", (book_id,))]


def bps_counts(book_id) -> dict:
    with conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM batch_page_state WHERE book_id=? "
                         "GROUP BY status", (book_id,)).fetchall()
        done = c.execute("SELECT COUNT(*) n FROM batch_page_state WHERE book_id=? AND done=1",
                         (book_id,)).fetchone()["n"]
    out = {r["status"]: r["n"] for r in rows}
    out["done"] = done
    return out


def bps_save(book_id, idx, **f):
    """Update per-page bake state. Accepts any of: status, round, attempt, gen_id,
    done, best_score, best_attempt, best_blob, draft_blob, carry_json."""
    cols = ("status", "round", "attempt", "gen_id", "done", "best_score",
            "best_attempt", "best_blob", "draft_blob", "carry_json")
    sets, vals = [], []
    for k in cols:
        if k in f:
            sets.append(f"{k}=?")
            vals.append(f[k])
    if not sets:
        return
    sets.append("updated_at=?")
    vals.append(time.time())
    vals += [book_id, idx]
    with conn() as c:
        c.execute(f"UPDATE batch_page_state SET {', '.join(sets)} WHERE book_id=? AND idx=?", vals)


def bjob_upsert(book_id, round, kind, job_name, state):
    now = time.time()
    with conn() as c:
        c.execute("INSERT INTO batch_jobs(book_id,round,kind,job_name,state,created_at,"
                  "updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(book_id,round,kind) "
                  "DO UPDATE SET job_name=excluded.job_name, state=excluded.state, "
                  "updated_at=excluded.updated_at",
                  (book_id, round, kind, job_name, state, now, now))


def bjob_get(book_id, round, kind) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM batch_jobs WHERE book_id=? AND round=? AND kind=?",
                      (book_id, round, kind)).fetchone()
        return dict(r) if r else None


def bjob_set_state(book_id, round, kind, state):
    with conn() as c:
        c.execute("UPDATE batch_jobs SET state=?, updated_at=? WHERE book_id=? AND round=? "
                  "AND kind=?", (state, time.time(), book_id, round, kind))


def bake_clear(book_id):
    """Drop all bake bookkeeping for a book (e.g. before a fresh re-bake)."""
    with conn() as c:
        c.execute("DELETE FROM batch_page_state WHERE book_id=?", (book_id,))
        c.execute("DELETE FROM batch_jobs WHERE book_id=?", (book_id,))
        c.execute("DELETE FROM batch_bake WHERE book_id=?", (book_id,))


# ---------------- progress ----------------

def set_progress(book_id, position):
    with conn() as c:
        c.execute("INSERT INTO progress(book_id,position,updated_at) VALUES (?,?,?) "
                  "ON CONFLICT(book_id) DO UPDATE SET position=excluded.position, "
                  "updated_at=excluded.updated_at",
                  (book_id, position, time.time()))


def get_progress(book_id) -> int:
    with conn() as c:
        r = c.execute("SELECT position FROM progress WHERE book_id=?",
                      (book_id,)).fetchone()
        return r["position"] if r else 0


def get_progress_at(book_id) -> float:
    """When the server-side position was last written (epoch seconds), or 0.
    Lets a reader decide whether the server copy is newer than its local one and
    resume from wherever the book was most recently read, on any device."""
    with conn() as c:
        r = c.execute("SELECT updated_at FROM progress WHERE book_id=?",
                      (book_id,)).fetchone()
        return (r["updated_at"] or 0.0) if r else 0.0


# ---------------- reading history ----------------

# A gap longer than this between reports starts a new reading session.
SESSION_GAP = 30 * 60


def log_reading(book_id, position):
    """Record a reading report into the history log, coalescing it into the most
    recent session for this book if that session is still recent (< SESSION_GAP)."""
    now = time.time()
    with conn() as c:
        r = c.execute("SELECT id, updated_at, end_pos FROM reading_log "
                      "WHERE book_id=? ORDER BY updated_at DESC LIMIT 1",
                      (book_id,)).fetchone()
        if r and now - r["updated_at"] <= SESSION_GAP:
            # same session: advance its end position and last-seen time
            if position == r["end_pos"]:
                c.execute("UPDATE reading_log SET updated_at=? WHERE id=?",
                          (now, r["id"]))
            else:
                c.execute("UPDATE reading_log SET end_pos=?, updated_at=?, "
                          "events=events+1 WHERE id=?", (position, now, r["id"]))
        else:
            c.execute("INSERT INTO reading_log(book_id,started_at,updated_at,"
                      "start_pos,end_pos,events) VALUES (?,?,?,?,?,1)",
                      (book_id, now, now, position, position))


def reading_history(limit=200) -> list[dict]:
    """Recent reading sessions across all books, newest first, with book title +
    page count for display. Sessions whose book was deleted are dropped (JOIN)."""
    with conn() as c:
        rows = c.execute(
            "SELECT l.book_id, l.started_at, l.updated_at, l.start_pos, l.end_pos, "
            "l.events, b.title AS title, b.num_pages AS num_pages "
            "FROM reading_log l JOIN books b ON b.id=l.book_id "
            "ORDER BY l.updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
