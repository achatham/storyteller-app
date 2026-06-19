"""Build the root landing page for the static GitHub Pages site.

Scans <site_dir>/books/*/book.json (each written by `webapp.export`) and renders
a single index.html listing every published book, linking to books/<slug>/.

    python -m webapp.build_index <site_dir>

Run this against the gh-pages deploy clone after mirroring a fresh export into
its books/<slug>/ dir; see docs/static_regen.md.
"""
import argparse
import json
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"


def build_index(site_dir) -> dict:
    site = Path(site_dir)
    books = []
    for manifest in sorted((site / "books").glob("*/book.json")):
        meta = json.loads(manifest.read_text(encoding="utf-8"))
        slug = meta.get("slug") or manifest.parent.name
        cover = meta.get("cover")
        books.append({
            "title": meta.get("title") or "Untitled",
            "author": meta.get("author") or "",
            "style": meta.get("style") or "",
            "url": f"books/{slug}/",
            "cover": f"books/{slug}/{cover}" if cover else None,
        })
    books.sort(key=lambda b: b["title"].lower())

    payload = json.dumps(books, ensure_ascii=False).replace("</", "<\\/")
    html = (STATIC / "index.html").read_text().replace("__BOOKS_JSON__", payload)
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text(html, encoding="utf-8")
    (site / ".nojekyll").write_text("")   # serve files verbatim (no Jekyll)

    return {"site": str(site), "books": len(books),
            "slugs": [b["url"] for b in books]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("site_dir", help="root of the gh-pages deploy clone")
    a = ap.parse_args()
    print(build_index(a.site_dir))
