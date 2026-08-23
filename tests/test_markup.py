"""Story markup: what survives extraction, how it renders, and where it may be cut."""
from pipeline import markup


def test_from_html_keeps_emphasis_headings_and_quotes():
    text = markup.from_html("""
        <html><head><title>skip me</title></head><body>
        <h2 class="cn">Six</h2>
        <p><span>He </span><i>was</i> a dragon, and <b>no</b> mistake.</p>
        <hr/>
        <blockquote><p>Once upon a time</p></blockquote>
        <p>Roses are red<br/>Violets are blue</p>
        </body></html>""")
    assert text.splitlines()[0] == "## Six"
    assert "He *was* a dragon, and **no** mistake." in text
    assert "\n---\n" in text
    assert "> Once upon a time" in text
    assert "Roses are red\\\nViolets are blue" in text   # <br> = a real line break
    assert "skip me" not in text


def test_from_html_escapes_markers_the_book_used_literally():
    text = markup.from_html("<p>3 * 4 = 12</p><p># not a heading</p>")
    assert "3 \\* 4 = 12" in text
    assert "\\# not a heading" in text
    # ...and they come back out as plain characters, not markup
    assert markup.to_html(text) == "<p>3 * 4 = 12</p>\n<p># not a heading</p>"


def test_from_html_moves_spaces_out_of_an_emphasis_run():
    # "* word *" is not emphasis in any renderer; the spaces belong outside
    assert markup.from_html("<p>a<i> word </i>b</p>") == "a *word* b"
    assert markup.from_html("<p>a<i> </i>b</p>") == "a b"


def test_to_html_renders_each_construct():
    text = ("## Stave One\n\nHe *was* a dragon.\n\n> quoted\\\n> lines\n\n---\n\n"
            "Verse one\\\nVerse two")
    html = markup.to_html(text, first_class="first")
    assert "<h2>Stave One</h2>" in html
    assert '<p class="first">He <em>was</em> a dragon.</p>' in html
    assert "<blockquote><p>quoted<br/>lines</p></blockquote>" in html
    assert '<hr class="scene"/>' in html
    assert "<p>Verse one<br/>Verse two</p>" in html


def test_to_html_escapes_html_in_the_book_text():
    assert markup.to_html("a <b>tag</b> & co") == "<p>a &lt;b&gt;tag&lt;/b&gt; &amp; co</p>"


def test_plain_strips_markup_for_the_model():
    text = markup.from_html("<h2>Six</h2><p>He <i>was</i> a dragon (3 * 4).</p><hr/>")
    assert markup.plain(text) == "Six\n\nHe was a dragon (3 * 4)."


def test_unformatted_text_renders_exactly_as_before():
    # books stored before this format (and every PDF) wrap their paragraphs
    # mid-sentence: a bare newline is a wrap, and must not become a line break
    text = "First paragraph, wrapped\nacross two lines.\n\nSecond paragraph."
    assert markup.to_html(text) == ("<p>First paragraph, wrapped across two lines.</p>\n"
                                    "<p>Second paragraph.</p>")


def test_safe_split_never_cuts_an_emphasis_run_in_half():
    text = "He said *the whole thing was over.* Then he left."
    inside = text.index("whole")
    assert markup.safe_split(text, inside) == text.index("*", 10) + 1
    outside = text.index("Then")
    assert markup.safe_split(text, outside) == outside
    assert markup.safe_split(text, 0) == 0


def test_page_breaks_and_image_anchors_use_safe_split():
    from pipeline import analyze
    from webapp import flow

    chapter = "He said *this is all over now.* She stared. Nobody moved at all."
    bible = analyze.apply_anchors(chapter, {"spreads": [
        {"id": 1, "start_anchor": "He said this is"},
        {"id": 2, "start_anchor": "all over now She stared"},
    ]})
    for spread in bible["spreads"]:
        assert spread["read_text"].count("*") % 2 == 0

    pos = flow.image_offset(chapter, "this is all over")
    assert chapter[:pos].count("*") % 2 == 0


def test_emphasis_that_spans_blocks_survives_as_emphasis():
    # a whole song inside one <i>: every block must come out self-contained
    text = markup.from_html("<i><p>Weasley is our King,</p>"
                            "<p>He didn't let the Quaffle in,</p></i>")
    assert text == "*Weasley is our King,*\n\n*He didn't let the Quaffle in,*"
    assert markup.to_html(text).count("<em>") == 2


def test_emphasis_that_spans_a_line_break_renders_as_one_run():
    text = markup.from_html("<p><i>Weasley is our King,<br/>He didn't let it in,</i></p>")
    assert markup.to_html(text) == ("<p><em>Weasley is our King,<br/>"
                                    "He didn't let it in,</em></p>")


def test_balance_repairs_a_run_a_cut_left_hanging():
    # text sliced at boundaries chosen before this format existed (reflow)
    assert markup.balance("It's *outrageous") == "It's *outrageous*"
    assert markup.balance("outrageous!* she said") == "*outrageous!* she said"
    assert markup.balance("a *b* and *c") == "a *b* and *c*"
    assert markup.balance("nothing to repair") == "nothing to repair"
    # each block is repaired on its own, and matched runs are left alone
    assert markup.balance("*whole* one\n\n*half") == "*whole* one\n\n*half*"


def test_bold_italic_is_one_run():
    text = markup.from_html("<p><b><i>Just in case.</i></b></p>")
    assert text == "***Just in case.***"
    assert markup.to_html(text) == "<p><strong><em>Just in case.</em></strong></p>"
    assert markup.balance(text) == text        # nothing hanging to repair


def test_balance_repairs_inside_a_block_prefix():
    # the marker belongs after the "> ", not in front of it
    assert (markup.balance("> *Treat your taste buds\\\n> before it melts")
            == "> *Treat your taste buds\\\n> before it melts*")
    assert markup.balance("## *A heading cut in half") == "## *A heading cut in half*"


def test_an_unpairable_marker_never_reaches_the_reader():
    # source markup that interleaves runs across a tag boundary leaves one behind
    assert markup.to_html("look at the* Daily Prophet *tomorrow") == \
        "<p>look at the Daily Prophet tomorrow</p>"
    # ...but a marker the book itself used stays, because it is escaped
    assert markup.to_html(markup.from_html("<p>3 * 4</p>")) == "<p>3 * 4</p>"
