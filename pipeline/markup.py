"""Story markup: the tiny Markdown subset book text is stored in, and its renderers.

EPUB source is real HTML -- italicised words, bold, section headings, block
quotes, verse line breaks. Extraction used to throw all of that away, so an
emphasised word read like any other and a chapter's headings ran together with
its first paragraph. Instead `from_html` keeps that formatting as markers:

    *italic*  **bold**  `code`      inline
    # ... ###### heading            (whole line)
    > quoted line                   (line prefix)
    ---                             scene divider (line alone)
    trailing backslash              hard line break (verse, letters)
    blank line                      block break
    \\*                              a literal marker character from the book

A bare newline inside a block is only a wrap, not a break -- PDF extraction and
every book stored before this format existed wrap their paragraphs mid-sentence,
and those must keep reading as running prose. A break the book really asked for
(a `<br/>`) is marked with a backslash at the end of the line.

The markers ride through the rest of the pipeline untouched: analyze slices page
text verbatim out of the chapter text, so whatever the chapter carries ends up in
the page. Everything that shows text to a person renders it (`to_html`, or its
browser twin `webapp/static/markup.js` -- keep the two in step); everything that
feeds text to a model strips it (`plain`). Anything that cuts text in half
(page breaks, in-text illustration anchors) goes through `safe_split` so a slice
can never end mid-emphasis and leak a stray marker.
"""
import html as _html
import re
from html.parser import HTMLParser

# characters that mean something in this format, so a book that uses them
# literally has them backslash-escaped on the way in
_SPECIAL = "\\*`#>-"
_ESCAPED = re.compile(r"\\([" + re.escape(_SPECIAL) + r"])")
# an escaped character is pulled out of the stream entirely while we render, so
# it can never take part in a marker match; \x00 cannot occur in book text
_SENTINEL = "\x00"
_CODE = {c: chr(ord("a") + i) for i, c in enumerate(_SPECIAL)}
_DECODE = {v: k for k, v in _CODE.items()}

# inline runs, longest marker first so **bold** wins over *italic*
_SPAN = re.compile(
    r"\*\*(?=\S)(?:.+?)(?<=\S)\*\*"
    r"|\*(?=\S)[^*\n]+?(?<=\S)\*"
    r"|`[^`\n]+`")

_HEADING = re.compile(r"(#{1,6})\s+(.*)")
_QUOTE = re.compile(r"^\s*>\s?")


# ---------------- HTML -> markup ----------------

# inline tags that carry meaning we can express; everything else inline
# (span, a, sup...) contributes only its text
_INLINE = {"i": "*", "em": "*", "cite": "*", "var": "*", "dfn": "*",
           "b": "**", "strong": "**",
           "code": "`", "tt": "`", "kbd": "`", "samp": "`"}
# tags whose content is not part of the book
_DROP = {"script", "style", "head", "title", "nav"}
# tags that end the current block of text
_BLOCK = {"p", "div", "section", "article", "blockquote", "li", "ul", "ol",
          "tr", "td", "th", "table", "figure", "figcaption", "pre", "body",
          "h1", "h2", "h3", "h4", "h5", "h6", "aside", "header", "footer"}


def escape(text: str) -> str:
    """Escape the marker characters in text that came from the book itself."""
    return re.sub(r"([" + re.escape("\\*`") + r"])", r"\\\1", text)


def _escape_line_start(line: str) -> str:
    """Escape a line-leading marker so book text can't fake a heading/quote/rule."""
    if line.strip() == "---":
        return "\\" + line
    return re.sub(r"^(\s*)([#>])", r"\1\\\2", line)


def from_plain(text: str) -> str:
    """Story markup for text that carries no formatting to keep (PDF extraction):
    the escapes only, so a literal asterisk -- or a line the book happens to start
    with `#` or `>` -- stays literal instead of turning into markup."""
    return "\n".join(_escape_line_start(escape(ln)) for ln in (text or "").split("\n"))


class _Reader(HTMLParser):
    """Turn one XHTML document into story markup."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.cur = ""
        self.heading = 0
        self.quote = 0
        self.pre = 0
        self.drop = 0
        self.open: list[tuple[str, str, int]] = []   # (tag, marker, offset in cur)

    # -- inline emphasis --------------------------------------------------
    def _open_inline(self, tag: str, marker: str) -> None:
        self.open.append((tag, marker, len(self.cur)))
        self.cur += marker

    def _close_inline(self, tag: str | None = None) -> None:
        """Close the innermost open run (or the innermost matching `tag`), moving
        any leading/trailing space outside the markers -- `* word *` renders as
        literal asterisks, `*word*` does not -- and dropping the run entirely if
        it turned out to hold no text."""
        idx = len(self.open) - 1
        if tag is not None:
            while idx >= 0 and self.open[idx][0] != tag:
                idx -= 1
        if idx < 0:
            return
        _tag, marker, at = self.open.pop(idx)
        inner = self.cur[at + len(marker):]
        body = inner.strip()
        if not body:                       # <i></i> / <i> </i> -> keep the space only
            self.cur = self.cur[:at] + inner
            return
        lead = inner[:len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()):]
        self.cur = self.cur[:at] + lead + marker + body + marker + trail

    # -- blocks -----------------------------------------------------------
    def _flush(self) -> None:
        while self.open:                   # a run never survives its block
            self._close_inline()
        text, self.cur = self.cur, ""
        lines = text.split("\n")
        lines = [ln.rstrip() if self.pre else ln.strip() for ln in lines]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            self.heading = 0
            return
        if self.pre:
            lines = [ln + "\\" for ln in lines[:-1]] + lines[-1:]
        if self.heading:
            level = max(2, min(6, self.heading))   # h1 is the chapter title itself
            body = "#" * level + " " + " ".join(" ".join(lines).split())
        elif self.quote:
            body = "\n".join("> " + ln for ln in lines)
        else:
            body = "\n".join(_escape_line_start(ln) for ln in lines)
        self.blocks.append(body)
        self.heading = 0

    # -- HTMLParser hooks -------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            self.drop += 1
            return
        if self.drop:
            return
        if tag == "br":
            self.cur += "\\\n"        # trailing backslash = a break the book asked for
            return
        if tag == "hr":
            self._flush()
            self.blocks.append("---")
            return
        if tag in _INLINE:
            self._open_inline(tag, _INLINE[tag])
            return
        if tag in _BLOCK:
            self._flush()
            if tag == "blockquote":
                self.quote += 1
            elif tag == "pre":
                self.pre += 1
            elif len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
                self.heading = int(tag[1])

    def handle_endtag(self, tag):
        if tag in _DROP:
            self.drop = max(0, self.drop - 1)
            return
        if self.drop:
            return
        if tag in _INLINE:
            self._close_inline(tag)
            return
        if tag in _BLOCK:
            self._flush()
            if tag == "blockquote":
                self.quote = max(0, self.quote - 1)
            elif tag == "pre":
                self.pre = max(0, self.pre - 1)

    def handle_data(self, data):
        if self.drop or not data:
            return
        if not self.pre:
            data = re.sub(r"\s+", " ", data)
        else:
            data = data.replace("\r\n", "\n").replace("\r", "\n")
        if not self.cur.strip() and not self.pre:
            data = data.lstrip()
        self.cur += escape(data)

    def result(self) -> str:
        self._flush()
        return "\n\n".join(b for b in self.blocks if b.strip())


def from_html(raw: str) -> str:
    """Story markup for one XHTML/HTML document, preserving emphasis, headings,
    block quotes and hard line breaks. Never raises on malformed markup."""
    r = _Reader()
    try:
        r.feed(raw or "")
        r.close()
    except Exception:  # noqa: BLE001 -- a broken document still yields its text
        pass
    text = r.result()
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------- markup -> HTML ----------------

def _protect(s: str) -> str:
    return _ESCAPED.sub(lambda m: _SENTINEL + _CODE[m.group(1)], s)


def _restore(s: str) -> str:
    return re.sub(_SENTINEL + r"(.)", lambda m: _DECODE.get(m.group(1), m.group(1)), s)


def _inline(s: str) -> str:
    """Render one line's inline markup to HTML (escaping the text first)."""
    s = _protect(_html.escape(s, quote=False))
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*(?=\S)([^*\n]+?)(?<=\S)\*", r"<em>\1</em>", s)
    return _restore(s)


def _breaks(line: str) -> bool:
    """True if this line ends with a hard-break marker -- an ODD number of trailing
    backslashes, since a literal backslash from the book is stored doubled."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _lines_html(lines: list[str]) -> str:
    """One block's lines. A plain newline is just how the source wrapped its text,
    so it rejoins with a space; only a marked break becomes a <br/>."""
    out = []
    for i, ln in enumerate(lines):
        ln = ln.strip()
        hard = _breaks(ln)
        if hard:
            ln = ln[:-1]
        out.append(_inline(ln.rstrip() if hard else ln))
        if i < len(lines) - 1:
            out.append("<br/>" if hard else " ")
    return "".join(out)


def blocks(text: str) -> list[list[str]]:
    """The text split into blocks of non-empty lines (blank line = block break)."""
    out = []
    for block in re.split(r"\n\s*\n", text or ""):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if lines:
            out.append(lines)
    return out


def to_html(text: str, first_class: str = "") -> str:
    """Render story markup as HTML: paragraphs, headings, block quotes, dividers.

    `first_class` is put on the first paragraph (the epub uses it to suppress the
    first line's indent)."""
    out, quoted, first = [], [], True

    def close_quote():
        if quoted:
            out.append("<blockquote>" + "".join(quoted) + "</blockquote>")
            quoted.clear()

    for lines in blocks(text):
        if all(_QUOTE.match(ln) for ln in lines):
            # consecutive quoted blocks are one quotation, not one each
            quoted.append(f"<p>{_lines_html([_QUOTE.sub('', ln) for ln in lines])}</p>")
            continue
        close_quote()
        head = _HEADING.fullmatch(lines[0]) if len(lines) == 1 else None
        if head:
            level = max(2, min(6, len(head.group(1))))
            out.append(f"<h{level}>{_inline(head.group(2))}</h{level}>")
            continue
        if len(lines) == 1 and lines[0].strip() == "---":
            out.append('<hr class="scene"/>')
            continue
        cls = f' class="{first_class}"' if first and first_class else ""
        out.append(f"<p{cls}>{_lines_html(lines)}</p>")
        first = False
    close_quote()
    return "\n".join(out)


# ---------------- markup -> plain text ----------------

def plain(text: str) -> str:
    """The text with all markup removed -- what to hand a model, or count words in."""
    out = []
    for lines in blocks(text):
        if len(lines) == 1 and lines[0].strip() == "---":
            continue
        rendered = []
        for ln in lines:
            s = ln.strip()
            if _breaks(s):
                s = s[:-1].rstrip()
            s = _protect(s)
            s = re.sub(r"^(#{1,6})\s+", "", s.strip())
            s = _QUOTE.sub("", s)
            s = re.sub(r"[*`]", "", s)
            rendered.append(_restore(s).strip())
        out.append("\n".join(r for r in rendered if r))
    return "\n\n".join(out)


# ---------------- slicing ----------------

def safe_split(text: str, pos: int) -> int:
    """`pos` moved out of the middle of an inline run, so both halves of a cut
    keep balanced markers. A page break or an illustration anchor that landed
    inside *an italicised sentence.* would otherwise leave one stray asterisk on
    each side, and both would render literally."""
    if pos <= 0 or pos >= len(text or ""):
        return pos
    for m in _SPAN.finditer(text):
        if m.start() >= pos:
            break
        if m.start() < pos < m.end():
            return m.end()
    return pos
