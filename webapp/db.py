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
CREATE TABLE IF NOT EXISTS style_samples (
    style_key  TEXT PRIMARY KEY,
    mime       TEXT,
    data       BLOB
);
CREATE TABLE IF NOT EXISTS progress (
    book_id    INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
    position   INTEGER,
    updated_at REAL
);
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
        pcols = {r["name"] for r in c.execute("PRAGMA table_info(pages)")}
        if "image_anchor" not in pcols:
            c.execute("ALTER TABLE pages ADD COLUMN image_anchor TEXT")


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
    """Any already-drawn sheet for this entity (optionally excluding one variant)
    -- used as an identity reference when drawing another of its variants."""
    with conn() as c:
        if exclude_variant_id is not None:
            r = c.execute("SELECT data FROM sheets WHERE book_id=? AND entity_id=? "
                          "AND variant_id != ? LIMIT 1",
                          (book_id, entity_id, exclude_variant_id)).fetchone()
        else:
            r = c.execute("SELECT data FROM sheets WHERE book_id=? AND entity_id=? "
                          "LIMIT 1", (book_id, entity_id)).fetchone()
        return r["data"] if r else None


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


def scene_data(book_id, idx) -> bytes | None:
    """The finished image bytes, or None if not generated yet."""
    with conn() as c:
        r = c.execute("SELECT data FROM scenes WHERE book_id=? AND idx=? AND "
                      "status='done'", (book_id, idx)).fetchone()
        return r["data"] if r and r["data"] else None


def scene_set_status(book_id, idx, status, detail=None):
    with conn() as c:
        c.execute("INSERT INTO scenes(book_id,idx,status,detail,updated_at) "
                  "VALUES (?,?,?,?,?) ON CONFLICT(book_id,idx) DO UPDATE SET "
                  "status=excluded.status, detail=excluded.detail, "
                  "updated_at=excluded.updated_at",
                  (book_id, idx, status, detail, time.time()))


def scene_store(book_id, idx, data, score, mime="image/webp"):
    with conn() as c:
        c.execute("INSERT INTO scenes(book_id,idx,status,mime,data,score,updated_at) "
                  "VALUES (?,?,'done',?,?,?,?) ON CONFLICT(book_id,idx) DO UPDATE SET "
                  "status='done', mime=excluded.mime, data=excluded.data, "
                  "score=excluded.score, detail=NULL, updated_at=excluded.updated_at",
                  (book_id, idx, mime, data, score, time.time()))


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
