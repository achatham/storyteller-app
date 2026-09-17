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


def test_an_ornament_between_sections_is_a_scene_break():
    # print marks a scene break with a little image and nothing else, so dropping
    # the image runs the two sections together (Fablehaven's dingbat)
    html = ('<p>He could be sneakier than Kendra knew.</p>'
            '<div><div><span><img src="image/dingbat-11.png" alt=""/></span></div></div>'
            '<p>The fairy balanced on a twig.</p>')
    assert markup.from_html(html, markup.Style(dividers={"dingbat-11.png"})) == (
        "He could be sneakier than Kendra knew.\n\n---\n\nThe fairy balanced on a twig.")
    # an image nobody identified as an ornament is still just dropped
    assert markup.from_html(html) == ("He could be sneakier than Kendra knew.\n\n"
                                      "The fairy balanced on a twig.")


def test_a_divider_with_nothing_before_it_is_dropped():
    # the art at the head of a chapter uses the same markup as the ornament, and
    # a rule before the first paragraph divides nothing
    text = markup.from_html('<div><img src="orn.png"/></div><hr/><p>One.</p>'
                            '<p>Two.</p><hr/><div><img src="orn.png"/></div>',
                            markup.Style(dividers={"orn.png"}))
    assert text == "One.\n\nTwo."


def test_an_ornament_beside_text_is_decoration_not_a_break():
    assert markup.from_html('<p><img src="orn.png"/>A dropped cap.</p>',
                            markup.Style(dividers={"orn.png"})) == "A dropped cap."
    # ...and a signature at the foot of a letter is not a scene break either
    assert markup.from_html("<blockquote><p>Yours sincerely,</p>"
                            '<figure><img src="orn.png"/></figure></blockquote>'
                            "<p>Harry reread the letter.</p>",
                            markup.Style(dividers={"orn.png"})) == (
        "> Yours sincerely,\n\nHarry reread the letter.")


def test_a_line_of_ornament_is_a_scene_break():
    # the ornament between two sections, which the reader's font may not even
    # have a glyph for ("■ ■ ■" in The Westing Game, "•••" in The Martian)
    text = markup.from_html("<p>He left.</p><p>•••</p><p>She stayed.</p>")
    assert text == "He left.\n\n---\n\nShe stayed."
    assert markup.from_html("<p>He left.</p><p>■ ■ ■</p><p>She stayed.</p>") == text
    # ...and one that merely repeats a rule the source already drew is not a
    # second break
    assert markup.from_html("<p>He left.</p><hr/><p>* * *</p><p>She stayed.</p>") == text


def test_a_paragraph_the_book_sets_apart_starts_a_new_section():
    # print marks a scene break by how the NEXT paragraph is set: space above it
    # and no first-line indent, which the stylesheet names (Harry Potter's
    # p.break). The class comes from pipeline/extract._break_classes.
    html = ('<p>Aunt Petunia had to run and get him a large brandy.</p>'
            '<p class="break">Harry lay in his dark cupboard much later.</p>')
    assert markup.from_html(html, markup.Style(breaks={"break"})) == (
        "Aunt Petunia had to run and get him a large brandy.\n\n---\n\n"
        "Harry lay in his dark cupboard much later.")
    assert "---" not in markup.from_html(html)
    # inside a quotation the same setting is just how a letter is laid out
    assert "---" not in markup.from_html(
        '<blockquote><p>Dear Harry,</p><p class="break">Yours,</p></blockquote>',
        markup.Style(breaks={"break"}))


def test_emphasis_the_stylesheet_carries_instead_of_a_tag():
    # InDesign and calibre write <span class="italic"> rather than <i>, so the
    # book's classes come in through Style (see pipeline/extract._emphasis_classes)
    style = markup.Style(inline={"italic": "*", "bold": "**"}, block={"Letter": "*"})
    html = ('<p>He said <span class="koboSpan"><span class="italic">no</span></span>'
            ' to <span class="bold">that</span>.</p>')
    assert markup.from_html(html, style) == "He said *no* to **that**."
    # ...and a book that says nothing keeps every span as plain text, as before
    assert markup.from_html(html) == "He said no to that."
    # a whole paragraph the book sets in italics (a letter, an epigraph)
    assert markup.from_html('<p class="Letter">Be vigilant.</p><p>She read it.</p>',
                            style) == "*Be vigilant.*\n\nShe read it."


def test_a_drop_cap_is_not_emphasis():
    # "<b>G</b>regor" is how a drop cap is written; keeping it would split the
    # word in two for everything that matches on words (reflow, image anchors)
    assert markup.from_html("<p><b>G</b>regor had pressed his forehead.</p>") == \
        "Gregor had pressed his forehead."
    # a run that ends where the word does is ordinary emphasis and stays
    assert markup.from_html("<p>The <b>Gregor</b> had pressed.</p>") == \
        "The **Gregor** had pressed."
