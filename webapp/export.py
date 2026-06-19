"""Export a book as a self-contained static site for GitHub Pages (or any static
host): one index.html with the book's text inlined and its illustrations written
out as image files. No backend needed to read the result.

    python -m webapp.export <book_id> <out_dir> [--pages N]

`--pages N` exports only the first N pages (a short demo slice) so you can iterate
on the export without illustrating the whole book.
"""
import argparse
import json
import re
from pathlib import Path

from . import db, flow

STATIC = Path(__file__).resolve().parent / "static"


def slugify(text: str) -> str:
    """Turn a title into a stable URL slug: lowercase, alphanumerics, hyphens."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "book"


def export_book(book_id: int, out_dir, max_pages: int | None = None,
                title: str | None = None, author: str | None = None,
                slug: str | None = None) -> dict:
    book = db.get_book(book_id)
    if not book:
        raise SystemExit(f"no book {book_id}")
    out = Path(out_dir)
    (out / "images").mkdir(parents=True, exist_ok=True)

    page_idxs = [p["idx"] for p in db.get_pages(book_id)]      # reading order
    allowed = set(page_idxs[:max_pages]) if max_pages else None
    include = (lambda p: p["idx"] in allowed) if allowed is not None else None

    exported, missing = {}, []

    def src_for(p):
        data = db.scene_data(book_id, p["idx"])
        if not data:
            missing.append(p["idx"])
            return None
        rel = f"images/p{p['idx']}.webp"
        if p["idx"] not in exported:
            (out / rel).write_bytes(data)
            exported[p["idx"]] = rel
        return rel

    chapters_out = []
    for ch in db.get_chapters(book_id):
        nodes = flow.chapter_nodes(book_id, ch["idx"], src_for, include)
        if nodes:
            chapters_out.append({"title": ch["title"], "nodes": nodes})

    book_title = title or book["title"] or "Untitled"
    book_author = author or book["author"] or ""
    book_style = (book["style"] or "").replace("_", " ")
    book_json = {"title": book_title, "author": book_author,
                 "style": book_style, "chapters": chapters_out}
    # inline the data; escape </ so book text can never break out of the <script>
    payload = json.dumps(book_json, ensure_ascii=False).replace("</", "<\\/")
    html = (STATIC / "export.html").read_text().replace("__BOOK_JSON__", payload)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / ".nojekyll").write_text("")   # let GitHub Pages serve files verbatim

    # book.json — the manifest the root landing page is built from. `cover` is the
    # first illustrated page (in reading order); a relative path within this dir.
    book_slug = slug or slugify(book_title)
    cover = next(iter(exported.values()), None)
    manifest = {"title": book_title, "author": book_author, "style": book_style,
                "slug": book_slug, "cover": cover}
    (out / "book.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"out": str(out), "slug": book_slug, "chapters": len(chapters_out),
            "images": len(exported), "cover": cover,
            "pages_missing_image": sorted(set(missing))}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("book_id", type=int)
    ap.add_argument("out_dir")
    ap.add_argument("--pages", type=int, default=None, help="export only the first N pages")
    ap.add_argument("--title", default=None, help="override the book title")
    ap.add_argument("--author", default=None, help="override the author")
    ap.add_argument("--slug", default=None,
                    help="URL slug for the book dir (default: derived from title)")
    a = ap.parse_args()
    print(export_book(a.book_id, a.out_dir, a.pages, a.title, a.author, a.slug))
