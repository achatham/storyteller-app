"""Re-apply a book's own formatting to a book that was already processed:

    python -m webapp.reflow <book_id> [--dry-run]

Pages segmented before story markup existed hold flat text -- the source's
italics, section headings and verse line breaks were dropped at extraction. Re-
processing the book would recover them, but it re-runs segmentation and throws
away every illustration. This does it for free instead: it re-extracts the
source (no model calls), finds each stored page's text inside that fresh markup,
and swaps in the marked-up slice. Pages, page numbering and images are untouched.

A page is only rewritten when the new slice's words match the stored text's words
EXACTLY -- so the worst case is a page left as it was, never a mangled one.
"""
import argparse
import re
import tempfile
import zipfile
from pathlib import Path

from pipeline import markup

from . import db

_WORDS = re.compile(r"\w+")
# how many leading words of a stored page may be discarded as extraction junk...
MAX_LEADING_DROP = 20
# ...and how many must still be left to match on, so a page that is nothing BUT a
# leaked running head can never be "matched" by one common word
MIN_MATCH_WORDS = 8


def _tokens(text: str) -> list[str]:
    return _WORDS.findall(text or "")


# markers/quotes glued to the first word of a page, then the block prefix behind
# them ("## ", "> "); the token match itself starts at the first WORD, so without
# this a page that opens a heading or an italic run would lose its marker
_OPENERS = "*`\\“‘\"'(«¿¡["
_LINE_PREFIX = " #>"
_TRAILERS = "*`\\.,!?;:…”’\"')]"


def _expand(text: str, start: int, end: int) -> tuple[int, int]:
    """Grow a word-span to the markers and punctuation that belong with it."""
    while start > 0 and text[start - 1] in _OPENERS:
        start -= 1
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start] and not text[line_start:start].strip(_LINE_PREFIX):
        start = line_start
    while end < len(text) and text[end] in _TRAILERS:
        end += 1
    return start, end


def _key(text: str) -> str:
    """A page's words with every separator dropped -- what two extractions of the
    same passage must agree on. The old one broke a few words apart where a tag sat
    inside them ("CO<sub>2</sub>" came out "CO 2", this one keeps "CO2"), so word
    boundaries can legitimately differ; the letters cannot."""
    return "".join(_tokens(text))


def source_markup(book_id: int) -> str:
    """The whole source file re-extracted as story markup, in reading order.

    Deliberately unfiltered (front matter and all): pages are located inside it by
    matching their words, so extra text just gets skipped over."""
    from pipeline import extract

    row = db.get_book_file(book_id)
    if not row:
        raise SystemExit(f"book {book_id} has no stored source file")
    _mime, data = row
    with tempfile.NamedTemporaryFile(suffix=".book") as tmp:
        tmp.write(data)
        tmp.flush()
        path = Path(tmp.name)
        if zipfile.is_zipfile(str(path)):
            return "\n\n".join(text for _p, text in extract._epub_units(path))
        from pypdf import PdfReader
        n = len(PdfReader(str(path)).pages)
        pages = extract.raw_pages(path, 1, n)
        return markup.from_plain(extract.repair_spacing("\n".join(pages)))


def reflow_book(book_id: int, apply: bool = True) -> dict:
    """Rewrite each page's text with its formatted equivalent. Returns
    {"pages": n, "changed": n, "skipped": [idx, ...]}."""
    book = db.get_book(book_id)
    if not book:
        raise SystemExit(f"no book {book_id}")
    fresh = source_markup(book_id)
    pages = db.get_pages(book_id)
    cursor, changed, skipped = 0, [], []
    for p in pages:
        old = p["read_text"] or ""
        toks = _tokens(old)
        if not toks:
            continue
        # The stored page is a verbatim slice of the same extraction, so its words
        # appear here in the same order -- only the markers (and whitespace) differ.
        # A page may open with a few words the old extraction leaked and this one
        # correctly leaves out (the document <title> used as a running head), so
        # allow dropping a short LEADING run; everything after it must match exactly.
        m, kept = None, toks
        max_drop = min(MAX_LEADING_DROP, max(0, len(toks) - MIN_MATCH_WORDS))
        for drop in range(0, max_drop + 1):
            kept = toks[drop:]
            pat = re.compile(r"\W*".join(re.escape(t) for t in kept))
            m = pat.search(fresh, cursor)
            if m:
                break
        if not m:
            skipped.append(p["idx"])
            continue
        span = _expand(fresh, m.start(), m.end())
        cursor = span[1]
        slice_ = fresh[span[0]:span[1]].strip()
        if _key(markup.plain(slice_)) != "".join(kept):   # never trust a fuzzy match
            skipped.append(p["idx"])
            continue
        if slice_ != old:
            changed.append((p["idx"], slice_, len(toks) - len(kept)))
    if apply:
        for idx, text, _dropped in changed:
            db.update_page_text(book_id, idx, text)
    return {"pages": len(pages), "changed": len(changed), "skipped": skipped,
            "dropped_words": sum(d for _i, _t, d in changed),
            "sample": changed[0][1][:200] if changed else ""}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book_id", type=int)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    a = ap.parse_args()
    res = reflow_book(a.book_id, apply=not a.dry_run)
    verb = "would reformat" if a.dry_run else "reformatted"
    print(f"{verb} {res['changed']}/{res['pages']} pages"
          + (f", {len(res['skipped'])} left as-is: {res['skipped'][:10]}"
             if res["skipped"] else "")
          + (f", dropping {res['dropped_words']} leaked running-head words"
             if res["dropped_words"] else ""))
    if res["sample"]:
        print(f"\nfirst rewritten page starts:\n{res['sample']}")
