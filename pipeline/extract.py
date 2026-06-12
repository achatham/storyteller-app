"""Extract a section's (or the whole book's) text from the source PDF.

pypdf drops some inter-word spaces on many PDFs; we apply a light repair pass.
The goal is faithful, readable text for the LLM to segment -- not perfection.
The book, page ranges, etc. all come from config (env-driven) so this module is
not tied to any one book.
"""
import html as _html
import os
import re
import zipfile
from pathlib import Path
from pypdf import PdfReader

from .config import PDF, PAGES, LABEL, BODY_PAGES, IS_EPUB, CHAPTERS


# ---------------- EPUB ----------------

def _xhtml_to_text(raw: str) -> str:
    """Plain text from one XHTML document, preserving paragraph breaks."""
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)</(p|div|h[1-6]|br|li|tr)\s*>", "\n", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = _html.unescape(raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n[ \t]+", "\n", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def _norm(base: str, src: str) -> str:
    """Resolve a TOC/spine href to a normalized archive path (no #fragment)."""
    src = src.split("#", 1)[0]
    return os.path.normpath(os.path.join(base, src)).replace(os.sep, "/")


def _clean_title(raw: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", raw))).strip()


def _epub_units(path: Path) -> list[tuple[str, str]]:
    """Ordered (archive_path, text) for every spine document in the epub."""
    z = zipfile.ZipFile(str(path))
    container = z.read("META-INF/container.xml").decode("utf-8", "ignore")
    opf = re.search(r'full-path="([^"]+)"', container).group(1)
    opf_txt = z.read(opf).decode("utf-8", "ignore")
    base = os.path.dirname(opf)
    spine = re.findall(r'<itemref[^>]*idref="([^"]+)"', opf_txt)
    href = {}
    for tag in re.findall(r"<item\b[^>]*>", opf_txt):
        i = re.search(r'id="([^"]+)"', tag)
        h = re.search(r'href="([^"]+)"', tag)
        if i and h:
            href[i.group(1)] = h.group(1)
    units = []
    for sid in spine:
        h = href.get(sid, "")
        if not h:
            continue
        p = _norm(base, h)
        try:
            raw = z.read(p).decode("utf-8", "ignore")
        except KeyError:
            continue
        units.append((p, _xhtml_to_text(raw)))
    return units


def _epub_toc_titles(path: Path) -> dict[str, str]:
    """Map archive_path -> human chapter title, from the epub's NCX (or nav)."""
    z = zipfile.ZipFile(str(path))
    names = z.namelist()
    titles: dict[str, str] = {}
    ncx = next((n for n in names if n.lower().endswith(".ncx")), None)
    if ncx:
        base = os.path.dirname(ncx)
        doc = z.read(ncx).decode("utf-8", "ignore")
        for t, s in re.findall(
                r"<navPoint[^>]*>.*?<text>(.*?)</text>.*?<content[^>]*src=\"([^\"]+)\"",
                doc, re.S):
            titles.setdefault(_norm(base, s), _clean_title(t))
        return titles
    nav = next((n for n in names if "nav" in n.lower()
                and n.lower().endswith((".xhtml", ".html"))), None)
    if nav:
        base = os.path.dirname(nav)
        doc = z.read(nav).decode("utf-8", "ignore")
        for s, t in re.findall(r"<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", doc, re.S):
            titles.setdefault(_norm(base, s), _clean_title(t))
    return titles


# Titles (or filenames) that mark non-story front/back matter to skip.
_FRONT_MATTER = re.compile(
    r"\b(cover|praise|also by|title page|copyright|dedication|contents|"
    r"introduction|acknowledg|about the author|about the publisher|foreword|"
    r"preface|index|colophon|newsletter|teaser|excerpt|advertisement|adcard)\b",
    re.I)


def epub_chapters_titled(path: Path = PDF, min_words: int = 150) -> list[tuple[str, str]]:
    """The book's real story chapters as (title, text), in reading order, with
    cover/copyright/intro/etc. front+back matter skipped.

    Titles come from the epub's table of contents (NCX/nav); front matter is
    dropped by title keyword or by being too short to be a chapter."""
    units = _epub_units(path)
    toc = _epub_toc_titles(path)
    out, n = [], 0
    for p, text in units:
        title = toc.get(p) or ""
        if _FRONT_MATTER.search(title) or _FRONT_MATTER.search(p):
            continue
        if len(text.split()) < min_words:
            continue
        n += 1
        if not title:
            first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
            title = (first[:60] or f"Chapter {n}")
        out.append((title, text))
    return out


def epub_chapters(path: Path = PDF) -> list[str]:
    """Texts of the book's real story chapters (front/back matter skipped)."""
    return [t for _title, t in epub_chapters_titled(path)]


def raw_pages(pdf_path: Path, first: int, last: int) -> list[str]:
    reader = PdfReader(str(pdf_path))
    out = []
    for i in range(first - 1, last):
        t = reader.pages[i].extract_text() or ""
        lines = [l for l in t.split("\n") if "Licensed to" not in l]
        out.append("\n".join(lines))
    return out


def repair_spacing(text: str) -> str:
    """Re-insert spaces that pypdf swallowed (e.g. 'hiseyes' -> 'his eyes').

    Heuristic and conservative: only split on clear lowercase->Uppercase and
    digit boundaries, plus a small dictionary of very common run-ons. We do NOT
    aggressively segment, to avoid mangling real words. The LLM tolerates the
    remainder.
    """
    # space before an interior capital: "andItell" -> "and Itell" (partial help)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    # space between letter and digit and vice versa
    text = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", text)
    text = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", text)
    # collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chapter_text(first=PAGES[0], last=PAGES[1]) -> str:
    """Text for THIS section. PDF: page range. EPUB: chapter range (CHAPTERS)."""
    if IS_EPUB:
        chaps = epub_chapters(PDF)
        c0, c1 = CHAPTERS
        return "\n\n".join(chaps[c0 - 1:c1])
    pages = raw_pages(PDF, first, last)
    return repair_spacing("\n".join(pages))


def full_story_text() -> str:
    """The whole story body. PDF: BODY_PAGES (a non-positive end offsets from the
    last page). EPUB: every story chapter."""
    if IS_EPUB:
        return "\n\n".join(epub_chapters(PDF))
    reader = PdfReader(str(PDF))
    first, last = BODY_PAGES
    if last <= 0:
        last = len(reader.pages) + last
    pages = raw_pages(PDF, first, last)
    return repair_spacing("\n".join(pages))


if __name__ == "__main__":
    from .config import OUT
    txt = chapter_text(PAGES[0], PAGES[1])
    out = OUT / "chapter.txt"
    out.write_text(txt)
    where = (f"chapters {CHAPTERS[0]}-{CHAPTERS[1]}" if IS_EPUB
             else f"pages {PAGES[0]}-{PAGES[1]}")
    print(f"[{LABEL}] {where} -> {out} ({len(txt)} chars, ~{len(txt.split())} words)")
    print("\n--- first 900 chars ---\n")
    print(txt[:900])
