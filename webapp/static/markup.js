// Story markup -> HTML, in the browser. The twin of pipeline/markup.py's
// `to_html`; both readers and the static export use it, so a book reads the same
// everywhere. Keep the two in step -- the format is described in markup.py:
//
//   *italic*  **bold**  `code`      inline
//   # ... ###### heading            (whole line)
//   > quoted line                   (line prefix)
//   ---                             scene divider (line alone)
//   trailing backslash              hard line break (verse, letters)
//   blank line                      block break
//   \*                              a literal marker character from the book
//
// Text that predates the markup (or came from a PDF) has no markers and renders
// exactly as it always did: one <p> per blank-line-separated block.
window.Markup = (function () {
  const SENTINEL = "\u0000";
  const SPECIAL = "\\*`#>-";
  const CODE = {}, DECODE = {};
  [...SPECIAL].forEach((c, i) => { const k = String.fromCharCode(97 + i); CODE[c] = k; DECODE[k] = c; });

  const HEADING = /^(#{1,6})\s+(.*)$/;
  const QUOTE = /^\s*>\s?/;

  function esc(s) {
    return (s || "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }

  // an escaped character leaves the stream entirely while we render, so it can
  // never take part in a marker match
  const protect = s => s.replace(/\\([\\*`#>-])/g, (_, c) => SENTINEL + CODE[c]);
  const restore = s => s.replace(new RegExp(SENTINEL + "(.)", "g"), (_, c) => DECODE[c] || c);

  function inline(s) {
    s = protect(esc(s));
    s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    // (no lookbehind: still unsupported on older iPad Safari, and a SyntaxError
    // here would take the whole reader down)
    s = s.replace(/\*\*(\S|\S.*?\S)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/\*(\S|\S[^*\n]*?\S)\*/g, "<em>$1</em>");
    return restore(s);
  }

  // A bare newline is only how the source wrapped its text (PDF extraction and
  // every book stored before this format wrap mid-sentence), so it rejoins with a
  // space; a break the book asked for is marked with a trailing backslash -- an
  // ODD number of them, since a literal backslash from the book is stored doubled.
  const breaks = l => (l.length - l.replace(/\\+$/, "").length) % 2 === 1;

  function linesHtml(lines) {
    const out = [];
    lines.forEach((line, i) => {
      let l = line.trim();
      const hard = breaks(l);
      if (hard) l = l.slice(0, -1).replace(/\s+$/, "");
      out.push(inline(l));
      if (i < lines.length - 1) out.push(hard ? "<br>" : " ");
    });
    return out.join("");
  }

  function blocks(text) {
    return (text || "").split(/\n\s*\n/)
      .map(b => b.split("\n").filter(l => l.trim()))
      .filter(b => b.length);
  }

  // Render story markup as HTML. `firstClass` (optional) goes on the first
  // paragraph, for readers that suppress that one's indent.
  function toHtml(text, firstClass) {
    const out = [];
    let quoted = [], first = true;
    const closeQuote = () => {
      if (quoted.length) { out.push("<blockquote>" + quoted.join("") + "</blockquote>"); quoted = []; }
    };
    for (const lines of blocks(text)) {
      if (lines.every(l => QUOTE.test(l))) {         // consecutive quoted blocks
        quoted.push("<p>" + linesHtml(lines.map(l => l.replace(QUOTE, ""))) + "</p>");
        continue;
      }
      closeQuote();
      const head = lines.length === 1 ? lines[0].match(HEADING) : null;
      if (head) {
        const level = Math.max(2, Math.min(6, head[1].length));
        out.push(`<h${level}>${inline(head[2])}</h${level}>`);
        continue;
      }
      if (lines.length === 1 && lines[0].trim() === "---") { out.push('<hr class="scene">'); continue; }
      const cls = first && firstClass ? ` class="${firstClass}"` : "";
      out.push(`<p${cls}>${linesHtml(lines)}</p>`);
      first = false;
    }
    closeQuote();
    return out.join("");
  }

  // Markup removed -- for anywhere plain text is wanted (alt text, titles).
  function plain(text) {
    return blocks(text)
      .filter(lines => !(lines.length === 1 && lines[0].trim() === "---"))
      .map(lines => lines
        .map(l => { const t = l.trim(); return breaks(t) ? t.slice(0, -1).trim() : t; })
        .map(l => restore(protect(l).replace(/^(#{1,6})\s+/, "").replace(QUOTE, "").replace(/[*`]/g, "")).trim())
        .filter(Boolean).join("\n"))
      .join("\n\n");
  }

  return { toHtml, plain, esc };
})();
