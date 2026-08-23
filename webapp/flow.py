"""Lay a chapter out as an ordered stream of text + image nodes at their in-text
anchors. Shared by the live flow API (server.py) and the static exporter
(export.py) so both produce identical reading layout."""
import re

from pipeline import markup

from . import db


def image_offset(text: str, anchor: str | None) -> int:
    """Where in `text` to place the illustration: just after the sentence holding
    `anchor` (a verbatim phrase the model chose so the picture follows -- not
    precedes -- the moment it depicts). Falls back to end-of-text if no anchor."""
    if not anchor:
        return len(text)
    toks = re.findall(r"\w+", anchor)[:8]
    if not toks:
        return len(text)
    m = re.compile(r"\W+".join(re.escape(t) for t in toks), re.I).search(text)
    if not m:
        return len(text)
    end = m.end()
    # extend to the end of the sentence holding the anchor -- incl. when it ends
    # the page (no trailing space). (?=\s|$) avoids matching abbreviations/decimals.
    nxt = re.search(r"[.!?]+[\"'”’)\]]*(?=\s|$)", text[end:])
    if nxt:
        end += nxt.end()
    # glob any punctuation / closing quotes that still immediately follow, so a
    # trailing mark (e.g. .”) stays with the text above the picture, not below it.
    g = re.match(r"[.!?,;:…”’\"')\]]+", text[end:])
    end = end + g.end() if g else end
    # keep the picture out of the middle of an emphasis run, so neither the text
    # above it nor the text below it is left holding half a marker
    return markup.safe_split(text, end)


def chapter_nodes(book_id, idx, src_for, include=None) -> list[dict]:
    """Ordered nodes for one chapter. `src_for(page)` returns the image src string
    (or None to omit the image, leaving its text). `include(page)` optionally
    filters which pages appear at all (used to export a short demo slice)."""
    nodes = []
    for p in db.get_pages(book_id):
        if p["chapter_idx"] != idx:
            continue
        if include is not None and not include(p):
            continue
        t = p["read_text"] or ""
        pos = image_offset(t, p.get("image_anchor"))
        before, after = t[:pos].strip(), t[pos:].strip()
        if before:
            nodes.append({"type": "text", "text": before})
        src = src_for(p)
        if src:
            nodes.append({"type": "image", "idx": p["idx"], "alt": p["title"] or "", "src": src})
        if after:
            nodes.append({"type": "text", "text": after})
    return nodes
