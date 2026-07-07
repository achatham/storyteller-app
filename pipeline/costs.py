"""Token + cost accounting for every Gemini call, persisted to a cumulative
SQLite DB so we can see per-run and all-time spend, split by text vs image.

Each API response carries usage_metadata (prompt / candidates / total token
counts); we record one row per call, price it from PRICING, and sum on demand.

NOTE: PRICING is USD per 1,000,000 tokens and is an ESTIMATE -- edit these to
match the current Gemini price sheet for exact figures.
"""
import contextlib
import os
import sqlite3
import threading
import time
from pathlib import Path

from .config import ROOT

# A per-thread "run" tag so generation that happens in the long-lived server
# (lazy scenes/sheets) can be attributed to a specific book, where there is no
# per-book process env to read. Falls back to STORY_RUN / STORY_OUT / STORY_LABEL.
_local = threading.local()


@contextlib.contextmanager
def run_as(run: str):
    prev = getattr(_local, "run", None)
    _local.run = run
    try:
        yield
    finally:
        _local.run = prev


def _resolve_run() -> str:
    return (getattr(_local, "run", None) or os.environ.get("STORY_RUN")
            or os.environ.get("STORY_OUT") or os.environ.get("STORY_LABEL", ""))

DB = Path(os.environ.get("STORY_COST_DB", str(ROOT / "output" / "costs.db")))

# USD per 1,000,000 tokens. From Google's official price sheet (ai.google.dev,
# June 2026). Image output is $120/1M tokens; a 1K or 2K image = 1120 tokens =
# ~$0.134 (4K = 2000 tokens = ~$0.24). Reports recompute from stored token
# counts, so editing these rates retroactively re-prices all past usage.
PRICING = {
    "gemini-3.5-flash":            {"in": 1.50, "out": 9.00},    # text + critique
    "gemini-3.1-flash-lite":       {"in": 0.25, "out": 1.50},    # text (official)
    "gemini-3.1-pro-preview":      {"in": 2.00, "out": 12.00},
    "gemini-3-pro-image-preview":  {"in": 2.00, "out": 120.00},  # pro image: ~$0.134/1K-2K img
    "gemini-3.1-flash-image":      {"in": 0.50, "out": 60.00},   # flash image: ~$0.067/1K img
    "gemini-3.1-flash-image-preview": {"in": 0.50, "out": 60.00},
    "gemini-3.1-flash-lite-image": {"in": 0.30, "out": 40.00},   # "nano banana lite" (estimated)
    "gemini-2.5-flash-image":      {"in": 0.30, "out": 30.00},   # older flash: ~$0.039/img
}
DEFAULT_PRICE = {"in": 0.0, "out": 0.0}

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB), timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS usage(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, run TEXT, model TEXT, kind TEXT,
        input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
        images INTEGER, cost_usd REAL, batch INTEGER DEFAULT 0)""")
    # migrate older DBs that predate the batch column
    cols = {r[1] for r in c.execute("PRAGMA table_info(usage)")}
    if "batch" not in cols:
        c.execute("ALTER TABLE usage ADD COLUMN batch INTEGER DEFAULT 0")
    return c


# The Batch API charges a flat 50% of interactive pricing for the same models.
BATCH_DISCOUNT = 0.5


def cost_for(model: str, input_tokens: int, output_tokens: int,
             batch: bool = False) -> float:
    p = PRICING.get(model, DEFAULT_PRICE)
    cost = input_tokens / 1e6 * p["in"] + output_tokens / 1e6 * p["out"]
    return cost * BATCH_DISCOUNT if batch else cost


def record(model: str, kind: str, input_tokens: int, output_tokens: int,
           total_tokens: int | None = None, images: int = 0,
           run: str | None = None, batch: bool = False) -> float:
    """Record one API call. Returns its USD cost. Thread- and process-safe.
    batch=True prices the call at the 50% Batch-API discount."""
    cost = cost_for(model, input_tokens, output_tokens, batch=batch)
    total = total_tokens if total_tokens is not None else input_tokens + output_tokens
    # key each row by its run (per-book tag / output dir / label) so per-book and
    # per-run reports can be sliced out of the shared usage table
    if run is None:
        run = _resolve_run()
    with _lock:
        with _conn() as c:
            c.execute(
                "INSERT INTO usage(ts,run,model,kind,input_tokens,output_tokens,"
                "total_tokens,images,cost_usd,batch) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), run, model, kind, input_tokens, output_tokens,
                 total, images, cost, 1 if batch else 0))
    return cost


# ---------------- reporting ----------------

def _rows(where: str = "", params: tuple = ()):
    with _conn() as c:
        return c.execute(
            "SELECT model, kind, COUNT(*), SUM(input_tokens), SUM(output_tokens), "
            "SUM(images), SUM(cost_usd), batch FROM usage " + where +
            " GROUP BY model, kind, batch ORDER BY model, kind, batch", params).fetchall()


def report(run: str | None = None) -> str:
    """Human-readable text/image split + total. If run is given, scope to it;
    otherwise report cumulative (all-time)."""
    where, params, scope = "", (), "cumulative (all-time)"
    if run is not None:
        where, params, scope = "WHERE run = ?", (run,), f"run = {run!r}"
    rows = _rows(where, params)
    if not rows:
        return f"No usage recorded yet ({DB})."

    text_cost = img_cost = 0.0
    text_in = text_out = img_in = img_out = img_n = calls = 0
    lines = [f"=== Gemini cost report — {scope} ===", f"db: {DB}", ""]
    lines.append(f"{'model':<30} {'kind':<9} {'calls':>5} {'in_tok':>10} {'out_tok':>10} {'imgs':>5} {'USD':>9}")
    for model, kind, n, sin, sout, simg, _stored, batch in rows:
        sin, sout, simg = sin or 0, sout or 0, simg or 0
        # recompute from current PRICING (retroactive) rather than stored cost;
        # batch rows get the 50% discount applied by cost_for
        scost = cost_for(model, sin, sout, batch=bool(batch))
        calls += n
        tag = f"{kind} (batch)" if batch else kind
        lines.append(f"{model:<30} {tag:<9} {n:>5} {sin:>10,} {sout:>10,} {simg:>5} {scost:>9.4f}")
        # split on what the call actually was (kind=image for every image model,
        # pro or flash), not on a model allow-list that drifts as models change
        if kind == "image":
            img_cost += scost; img_in += sin; img_out += sout; img_n += simg
        else:
            text_cost += scost; text_in += sin; text_out += sout
    total = text_cost + img_cost
    lines += [
        "",
        f"TEXT  : {text_in:>10,} in / {text_out:>10,} out tok   ${text_cost:.4f}",
        f"IMAGE : {img_in:>10,} in / {img_out:>10,} out tok   {img_n} imgs   ${img_cost:.4f}",
        f"{'-'*52}",
        f"TOTAL : {calls} calls   ${total:.4f}",
    ]
    return "\n".join(lines)


def book_report(book_id) -> dict:
    """Cost attributed to one book: the new `book:<id>` tag plus the historical
    processing rows keyed by its work dir (.../work/<id>). Recomputed from tokens."""
    with _conn() as c:
        rows = c.execute(
            "SELECT model, kind, SUM(input_tokens), SUM(output_tokens), SUM(images), batch "
            "FROM usage WHERE run = ? OR run LIKE ? GROUP BY model, kind, batch",
            (f"book:{book_id}", f"%/work/{book_id}")).fetchall()
    text = img = 0.0
    n_img = 0
    by_model = []
    for model, kind, sin, sout, simg, batch in rows:
        sin, sout, simg = sin or 0, sout or 0, simg or 0
        cost = cost_for(model, sin, sout, batch=bool(batch))
        by_model.append({"model": model, "kind": kind, "batch": bool(batch),
                         "in": sin, "out": sout, "images": simg, "usd": round(cost, 4)})
    for m in by_model:
        if m["kind"] == "image":
            img += m["usd"]; n_img += m["images"]
        else:
            text += m["usd"]
    return {"text_usd": round(text, 4), "image_usd": round(img, 4),
            "total_usd": round(text + img, 4), "images": n_img, "by_model": by_model}


def main():
    import sys
    run = sys.argv[1] if len(sys.argv) > 1 else None
    print(report(run))


if __name__ == "__main__":
    main()
