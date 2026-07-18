"""Build a self-contained EPUB (text + illustrations) for a book, so it can be
posted on GitHub Pages / loaded into any ereader:

    python -m webapp.epub <book_id> <out.epub> [--maxw 1000] [--quality 78]

The reading layout (text with each illustration at its in-text anchor) is shared
with the live reader and the static-site exporter via `flow.chapter_nodes`, so
the book reads identically. Illustrations are stored as WebP q80 @1152px, which
is NOT an EPUB core media type -- Kindle and many older readers won't render it
-- so every image is re-encoded to JPEG (a core media type) and downscaled to a
6" e-ink-friendly width, which also keeps a full illustrated book near ~30MB.
"""
import argparse
import html
import io
import re
import zipfile
from pathlib import Path

from PIL import Image

from . import db, flow

# JPEG so every ereader (incl. Kindle) can display it; ~1000px longest side is
# plenty for a 6" e-ink screen (~1072px native) and keeps a full book ~30MB.
DEFAULT_MAXW = 1000
DEFAULT_QUALITY = 78

CSS = """\
html, body { margin: 0; padding: 0; }
body { font-family: Georgia, 'Times New Roman', serif; line-height: 1.5;
       padding: 0 6% ; text-align: left; }
h1.chapter { font-size: 1.5em; margin: 1.4em 0 0.8em; text-align: center;
             font-weight: normal; }
p { margin: 0 0 0.9em; text-indent: 1.4em; }
p.first { text-indent: 0; }
figure { margin: 1.1em 0; text-align: center; page-break-inside: avoid; }
figure img { max-width: 100%; height: auto; }
.title-page { text-align: center; margin-top: 22%; }
.title-page h1 { font-size: 2em; font-weight: normal; margin: 0 0 0.3em; }
.title-page .author { font-size: 1.1em; color: #444; margin: 0 0 2em; }
.title-page .style { font-style: italic; color: #666; }
.cover img { max-width: 100%; height: auto; display: block; margin: 0 auto; }
"""


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _paragraphs(text: str) -> str:
    """Same split as the reader/exporter: blank line separates paragraphs."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    out = []
    for i, p in enumerate(parts):
        cls = ' class="first"' if i == 0 else ""
        # collapse single newlines inside a paragraph to spaces
        body = _esc(re.sub(r"\s*\n\s*", " ", p))
        out.append(f"<p{cls}>{body}</p>")
    return "\n".join(out)


def _to_jpeg(data: bytes, max_w: int, quality: int) -> bytes:
    im = Image.open(io.BytesIO(data)).convert("RGB")
    if max_w and im.width > max_w:
        im = im.resize((max_w, round(max_w * im.height / im.width)), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue()


def _xhtml(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">\n'
        f'<head><meta charset="utf-8"/><title>{_esc(title)}</title>'
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        f'<body>{body}</body>\n</html>\n'
    )


def build_epub(book_id: int, max_w: int = DEFAULT_MAXW,
               quality: int = DEFAULT_QUALITY) -> dict:
    """Assemble the EPUB in memory. Returns {"data": bytes, "images": n,
    "pages_missing_image": [...], "chapters": n}."""
    book = db.get_book(book_id)
    if not book:
        raise ValueError(f"no book {book_id}")

    title = book["title"] or "Untitled"
    author = book["author"] or ""
    style = (book["style"] or "").replace("_", " ")
    uid = f"urn:storyteller:book:{book_id}"

    images: dict[int, dict] = {}   # page idx -> {"name","jpeg"}
    missing: list[int] = []

    def src_for(p):
        idx = p["idx"]
        if idx in images:
            return images[idx]["name"]
        data = db.scene_data(book_id, idx)
        if not data:
            missing.append(idx)
            return None
        name = f"images/p{idx}.jpg"
        images[idx] = {"name": name, "jpeg": _to_jpeg(data, max_w, quality)}
        return name

    # Build each chapter's XHTML from the shared node layout.
    chapters = []   # {"file","title","body"}
    for ci, ch in enumerate(db.get_chapters(book_id)):
        nodes = flow.chapter_nodes(book_id, ch["idx"], src_for)
        if not nodes:
            continue
        parts = [f'<h1 class="chapter">{_esc(ch["title"] or "")}</h1>']
        for n in nodes:
            if n["type"] == "text":
                parts.append(_paragraphs(n["text"]))
            else:
                parts.append(
                    f'<figure><img src="{n["src"]}" alt="{_esc(n.get("alt") or "")}"/>'
                    f'</figure>')
        chapters.append({"file": f"chap{ci + 1}.xhtml",
                         "title": ch["title"] or f"Chapter {ci + 1}",
                         "body": "\n".join(parts)})

    cover_page = next(iter(images.values()), None)   # first illustrated page

    # ---- assemble the zip ----
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype MUST be first and stored (uncompressed), per the OCF spec.
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0" encoding="utf-8"?>\n'
                   '<container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                   '  <rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles>\n'
                   '</container>\n')
        z.writestr("OEBPS/style.css", CSS)

        # title page
        title_body = (
            '<div class="title-page">'
            f'<h1>{_esc(title)}</h1>'
            + (f'<div class="author">{_esc(author)}</div>' if author else "")
            + (f'<div class="style">illustrated · {_esc(style)}</div>' if style else "")
            + '</div>')
        z.writestr("OEBPS/title.xhtml", _xhtml(title, title_body))

        # cover page (first illustration)
        if cover_page:
            z.writestr("OEBPS/cover.xhtml", _xhtml(
                "Cover",
                f'<div class="cover"><img src="{cover_page["name"]}" '
                f'alt="{_esc(title)}"/></div>'))

        # chapters
        for ch in chapters:
            z.writestr(f"OEBPS/{ch['file']}", _xhtml(ch["title"], ch["body"]))

        # images
        for info in images.values():
            z.writestr(f"OEBPS/{info['name']}", info["jpeg"])

        # spine / reading order -> manifest item ids
        spine = ([("coverpage", "cover.xhtml")] if cover_page else []) \
            + [("title", "title.xhtml")] \
            + [(f"chap{i + 1}", ch["file"]) for i, ch in enumerate(chapters)]

        # manifest items
        man = [
            '<item id="css" href="style.css" media-type="text/css"/>',
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
            'properties="nav"/>',
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>',
        ]
        if cover_page:
            man.append('<item id="coverpage" href="cover.xhtml" '
                       'media-type="application/xhtml+xml"/>')
            man.append(f'<item id="cover-img" href="{cover_page["name"]}" '
                       'media-type="image/jpeg" properties="cover-image"/>')
        for i, ch in enumerate(chapters):
            man.append(f'<item id="chap{i + 1}" href="{ch["file"]}" '
                       'media-type="application/xhtml+xml"/>')
        for idx, info in images.items():
            if cover_page and info is cover_page:
                continue   # already declared as cover-img
            man.append(f'<item id="img{idx}" href="{info["name"]}" '
                       'media-type="image/jpeg"/>')

        itemrefs = "\n    ".join(
            f'<itemref idref="{item_id}"/>' for item_id, _ in spine)

        cover_meta = ('<meta name="cover" content="cover-img"/>' if cover_page else "")
        opf = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="bookid" xml:lang="en">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            f'    <dc:identifier id="bookid">{uid}</dc:identifier>\n'
            f'    <dc:title>{_esc(title)}</dc:title>\n'
            '    <dc:language>en</dc:language>\n'
            + (f'    <dc:creator>{_esc(author)}</dc:creator>\n' if author else "")
            + '    <meta property="dcterms:modified">2020-01-01T00:00:00Z</meta>\n'
            f'    {cover_meta}\n'
            '  </metadata>\n'
            '  <manifest>\n    ' + "\n    ".join(man) + '\n  </manifest>\n'
            '  <spine toc="ncx">\n    ' + itemrefs + '\n  </spine>\n'
            '</package>\n')
        z.writestr("OEBPS/content.opf", opf)

        # EPUB3 nav
        nav_items = "\n      ".join(
            f'<li><a href="{ch["file"]}">{_esc(ch["title"])}</a></li>'
            for ch in chapters)
        z.writestr("OEBPS/nav.xhtml", _xhtml("Contents",
            '<nav epub:type="toc" id="toc"><h1>Contents</h1>\n'
            f'    <ol>\n      {nav_items}\n    </ol>\n</nav>'))

        # EPUB2 NCX (Kindle / older readers)
        navpoints = "\n".join(
            f'    <navPoint id="np{i + 1}" playOrder="{i + 1}">'
            f'<navLabel><text>{_esc(ch["title"])}</text></navLabel>'
            f'<content src="{ch["file"]}"/></navPoint>'
            for i, ch in enumerate(chapters))
        z.writestr("OEBPS/toc.ncx",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            f'  <head><meta name="dtb:uid" content="{uid}"/></head>\n'
            f'  <docTitle><text>{_esc(title)}</text></docTitle>\n'
            f'  <navMap>\n{navpoints}\n  </navMap>\n</ncx>\n')

    return {"data": buf.getvalue(), "images": len(images), "chapters": len(chapters),
            "pages_missing_image": sorted(set(missing))}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book_id", type=int)
    ap.add_argument("out")
    ap.add_argument("--maxw", type=int, default=DEFAULT_MAXW)
    ap.add_argument("--quality", type=int, default=DEFAULT_QUALITY)
    a = ap.parse_args()
    res = build_epub(a.book_id, a.maxw, a.quality)
    Path(a.out).write_bytes(res["data"])
    mb = len(res["data"]) / 1e6
    print(f"wrote {a.out}: {mb:.1f}MB, {res['images']} images, "
          f"{res['chapters']} chapters, "
          f"{len(res['pages_missing_image'])} pages missing an image")
