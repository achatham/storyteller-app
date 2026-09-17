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

from . import markup
from .config import PDF, PAGES, LABEL, BODY_PAGES, IS_EPUB, CHAPTERS


# ---------------- EPUB ----------------

def _xhtml_to_text(raw: str, dividers=(), breaks=()) -> str:
    """One XHTML document as story markup (see pipeline/markup.py): paragraph
    breaks plus the emphasis, headings and block quotes the source marks up."""
    return markup.from_html(raw, dividers, breaks)


_IMG = re.compile(r"<img\b[^>]*>", re.I)
_SRC = re.compile(r"""\bsrc\s*=\s*["\']([^"\']+)["\']""", re.I)
# ornaments the book names for what they are, however few times they are used
_ORNAMENT = re.compile(r"dingbat|ornament|fleuron|flourish|divider|asterism|"
                       r"(scene|section)[-_]?break", re.I)
# ...and ones it doesn't: an ornament is the SAME image over and over, while a
# real illustration (or the art at the head of a chapter) is used once
_ORNAMENT_USES = 3


def _divider_images(docs: list[str]) -> set[str]:
    """File names of the book's scene-break ornaments -- the little images print
    puts between two sections of a chapter, in place of a rule. Many epubs mark a
    scene break with nothing else, so dropping the image drops the break: the two
    sections then run together as one, which is what the reader sees."""
    uses: dict[str, int] = {}
    for raw in docs:
        for tag in _IMG.findall(raw):
            m = _SRC.search(tag)
            if m:
                name = m.group(1).split("#", 1)[0].rsplit("/", 1)[-1]
                uses[name] = uses.get(name, 0) + 1
    return {name for name, n in uses.items()
            if n >= _ORNAMENT_USES or _ORNAMENT.search(name)}


# ---- scene breaks the book sets in type rather than drawing ----
# Print marks a break between two sections by giving the paragraph that follows
# it extra air and no first-line indent. The stylesheet says which class that is,
# so we ask it instead of guessing: an empty paragraph is NOT a usable signal --
# the same books use one to space a letter or a stanza of verse.

_CSS_RULE = re.compile(r"([^{}@]+)\{([^{}]*)\}")
_LENGTH = re.compile(r"^(-?[\d.]+)\s*([a-z%]*)$")
# rough px equivalents -- enough to compare one paragraph's spacing with another's
_UNIT_PX = {"": 1.0, "px": 1.0, "em": 16.0, "rem": 16.0, "ex": 8.0, "pt": 16 / 12,
            "pc": 16.0, "%": 16.0, "in": 96.0, "cm": 37.8, "mm": 3.78}
# a break is set with space alone: a class that also changes the font, the
# alignment or the case is a heading, a caption or a block extract
_SPACING_ONLY = {"text-indent", "margin", "margin-top", "margin-bottom",
                 "margin-left", "margin-right", "display", "line-height",
                 "widows", "orphans", "page-break-inside"}
# ...and it is the space ABOVE AN ORDINARY PARAGRAPH that makes it one, by this
# much. Books converted by calibre give every paragraph a small top margin.
_BREAK_GAP_PX = 8.0
# A class that almost always dresses a document's opening paragraph is the start
# of a CHAPTER, set the same way. It divides nothing inside the chapter.
_OPENER_WITHIN = 2      # paragraphs into the document
_OPENER_SHARE = 0.8


def _px(value) -> float | None:
    m = _LENGTH.match((value or "").strip().lower())
    return float(m.group(1)) * _UNIT_PX.get(m.group(2), 1.0) if m else None


def _declarations(body: str) -> dict:
    out = {}
    for decl in body.split(";"):
        prop, sep, value = decl.partition(":")
        if sep:
            out[prop.strip().lower()] = value.strip().lower()
    return out


def _margin_top(decls: dict) -> float | None:
    if "margin-top" in decls:
        return _px(decls["margin-top"])
    parts = (decls.get("margin") or "").split()
    return _px(parts[0]) if parts else None


def _css_rules(css: str) -> list[tuple[str, dict]]:
    """(selector, declarations) for every rule in the book's stylesheets, one
    entry per comma-separated selector."""
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    return [(sel.strip(), _declarations(body))
            for raw_sel, body in _CSS_RULE.findall(css)
            for sel in raw_sel.split(",")]


def _body_paragraph(rules: list[tuple[str, dict]], docs: list[str]) -> dict:
    """How the book sets an ordinary paragraph: the class most of its <p> carry,
    resolved through the stylesheet."""
    uses: dict[str, int] = {}
    for raw in docs:
        for tag in re.findall(r"<p\b[^>]*>", raw):
            m = re.search(r'class="([^"]*)"', tag)
            name = (m.group(1).split() or [""])[0] if m else ""
            uses[name] = uses.get(name, 0) + 1
    cls = max(uses, key=uses.get) if uses else ""
    applies = {"p"} | ({f"p.{cls}", f".{cls}"} if cls else set())
    decls: dict = {}
    for sel, d in rules:
        if sel in applies:
            decls.update(d)
    return decls


def _paragraph_classes(raw: str) -> list[list[str]]:
    """The classes on each <p> of one document, in order."""
    out = []
    for tag in re.findall(r"<p\b[^>]*>", raw):
        m = re.search(r'class="([^"]*)"', tag)
        out.append(m.group(1).split() if m else [])
    return out


def _chapter_opener(name: str, docs: list[str]) -> bool:
    """True if this class is nearly always on a document's first paragraph."""
    opening = total = 0
    for raw in docs:
        for i, names in enumerate(_paragraph_classes(raw)):
            if name in names:
                total += 1
                opening += i <= _OPENER_WITHIN
    return bool(total) and opening / total >= _OPENER_SHARE


def _break_classes(css: str, docs: list[str]) -> set[str]:
    """Paragraph classes that start a new section of a chapter.

    Empty unless the book indents its paragraphs in the first place -- where it
    doesn't (calibre conversions, Gregor), "no indent" distinguishes nothing and
    every paragraph would look like a break."""
    rules = _css_rules(css)
    body = _body_paragraph(rules, docs)
    if not (_px(body.get("text-indent")) or 0) > 0:
        return set()
    floor = (_margin_top(body) or 0) + _BREAK_GAP_PX
    out = set()
    for sel, decls in rules:
        m = re.fullmatch(r"(?:p)?\.([A-Za-z0-9_-]+)", sel)
        if not m or set(decls) - _SPACING_ONLY:
            continue
        if _px(decls.get("text-indent")) == 0 and (_margin_top(decls) or 0) >= floor:
            out.add(m.group(1))
    return {name for name in out if not _chapter_opener(name, docs)}


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
    docs = []
    for sid in spine:
        h = href.get(sid, "")
        if not h:
            continue
        p = _norm(base, h)
        try:
            docs.append((p, z.read(p).decode("utf-8", "ignore")))
        except KeyError:
            continue
    # what marks a scene break is a question about the whole book, not one
    # document, so every spine document is read before any of them is converted
    raws = [raw for _p, raw in docs]
    dividers = _divider_images(raws)
    css = "\n".join(z.read(n).decode("utf-8", "ignore")
                    for n in z.namelist() if n.lower().endswith(".css"))
    breaks = _break_classes(css, raws)
    return [(p, _xhtml_to_text(raw, dividers, breaks)) for p, raw in docs]


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


def _epub_metadata(path: Path) -> dict:
    """{title, author} from the epub's OPF Dublin-Core metadata."""
    z = zipfile.ZipFile(str(path))
    container = z.read("META-INF/container.xml").decode("utf-8", "ignore")
    opf = re.search(r'full-path="([^"]+)"', container).group(1)
    opf_txt = z.read(opf).decode("utf-8", "ignore")

    def tag(name: str) -> str:
        m = re.search(rf"<dc:{name}\b[^>]*>(.*?)</dc:{name}>", opf_txt, re.S | re.I)
        return _clean_title(m.group(1)) if m else ""

    return {"title": tag("title"), "author": tag("creator")}


def _pdf_metadata(path: Path) -> dict:
    """{title, author} from the PDF document-info dictionary (often empty)."""
    meta = PdfReader(str(path)).metadata or {}
    return {"title": _clean_title(meta.title or ""),
            "author": _clean_title(meta.author or "")}


def book_metadata(path: Path = PDF) -> dict:
    """Best-effort book-level {title, author} from the source's own container
    metadata. EPUBs are zips (OPF Dublin Core); PDFs carry a document-info dict.
    Missing fields come back as "". Never raises."""
    path = Path(path)
    try:
        if zipfile.is_zipfile(str(path)):
            return _epub_metadata(path)
        return _pdf_metadata(path)
    except Exception:  # noqa: BLE001 -- metadata is a nicety, never fatal
        return {"title": "", "author": ""}


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
            first = next((ln for ln in markup.plain(text).splitlines() if ln.strip()), "")
            title = (first.strip()[:60] or f"Chapter {n}")
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
    return markup.from_plain(repair_spacing("\n".join(pages)))


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
    return markup.from_plain(repair_spacing("\n".join(pages)))


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
