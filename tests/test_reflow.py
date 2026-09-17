"""Re-applying a source book's formatting to pages that were segmented flat."""
import importlib
import io
import zipfile

CHAPTER = """<html><head><title>The Book: A Novel</title></head><body>
<h2>One</h2>
<p>He <i>was</i> a dragon, and no mistake. The others were washing.</p>
<p>Nobody moved at all.</p>
</body></html>"""


def epub_bytes() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/>'
                   "</rootfiles></container>")
        z.writestr("OEBPS/content.opf",
                   '<package><manifest><item id="c1" href="c1.xhtml"/></manifest>'
                   '<spine><itemref idref="c1"/></spine></package>')
        z.writestr("OEBPS/c1.xhtml", CHAPTER)
    return out.getvalue()


def book_with_flat_pages(monkeypatch, tmp_path):
    """A book whose pages hold the text the OLD extraction produced: no markers,
    the document <title> leaked in as a running head, hard-wrapped lines."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    import webapp.db
    db = importlib.reload(webapp.db)
    import webapp.reflow
    reflow = importlib.reload(webapp.reflow)
    db.init()
    bid = db.create_book("The Book", "", "book.epub", "watercolor", 200,
                         "application/epub+zip", epub_bytes())
    db.add_chapter(bid, 0, "One", 0, [])
    db.add_page(bid, 0, 0, "A dragon",
                "The Book: A Novel \n\nOne \n\nHe was a dragon, and no\nmistake.", "", "", [])
    db.add_page(bid, 1, 0, "Washing",
                "The others were washing.\n\nNobody moved at all.", "", "", [])
    return db, reflow, bid


def test_reflow_recovers_formatting_without_touching_page_numbering(monkeypatch, tmp_path):
    db, reflow, bid = book_with_flat_pages(monkeypatch, tmp_path)
    res = reflow.reflow_book(bid)
    # page 1 has nothing to recover, so only page 0 is rewritten
    assert res == {"pages": 2, "changed": 1, "skipped": [], "dropped_words": 4,
                   "sample": res["sample"]}

    pages = db.get_pages(bid)
    assert [p["idx"] for p in pages] == [0, 1]
    # the heading and the italics are back; the leaked running head is gone
    assert pages[0]["read_text"] == "## One\n\nHe *was* a dragon, and no mistake."
    assert pages[1]["read_text"] == "The others were washing.\n\nNobody moved at all."


def test_reflow_leaves_a_page_alone_when_it_cannot_match(monkeypatch, tmp_path):
    db, reflow, bid = book_with_flat_pages(monkeypatch, tmp_path)
    db.add_page(bid, 2, 0, "Not in the book", "This sentence is not in the source.",
                "", "", [])
    res = reflow.reflow_book(bid)
    assert res["skipped"] == [2]
    assert db.get_pages(bid)[2]["read_text"] == "This sentence is not in the source."


ORNAMENT_CHAPTER = """<html><body>
<div><img src="images/dingbat.png" alt=""/></div>
<h2>One</h2>
<p>He was a dragon, and no mistake.</p>
<div><img src="images/dingbat.png" alt=""/></div>
<p>The others were washing. Nobody moved at all.</p>
<div><img src="images/dingbat.png" alt=""/></div>
<p>Then it was morning.</p>
</body></html>"""


def book_broken_at_a_scene_break(monkeypatch, tmp_path):
    """A book whose pages were split exactly where the source puts a scene break
    -- so the break falls between two pages, outside either one's text."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    import webapp.db
    db = importlib.reload(webapp.db)
    import webapp.reflow
    reflow = importlib.reload(webapp.reflow)
    db.init()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/>'
                   "</rootfiles></container>")
        z.writestr("OEBPS/content.opf",
                   '<package><manifest><item id="c1" href="c1.xhtml"/></manifest>'
                   '<spine><itemref idref="c1"/></spine></package>')
        z.writestr("OEBPS/c1.xhtml", ORNAMENT_CHAPTER)
    bid = db.create_book("The Book", "", "book.epub", "watercolor", 200,
                         "application/epub+zip", out.getvalue())
    db.add_chapter(bid, 0, "One", 0, [])
    db.add_page(bid, 0, 0, "A dragon", "One\n\nHe was a dragon, and no mistake.",
                "", "", [])
    db.add_page(bid, 1, 0, "Washing",
                "The others were washing.\n\nNobody moved at all.", "", "", [])
    db.add_page(bid, 2, 0, "Morning", "Then it was morning.", "", "", [])
    return db, reflow, bid


def test_reflow_recovers_a_scene_break_that_falls_between_two_pages(monkeypatch, tmp_path):
    db, reflow, bid = book_broken_at_a_scene_break(monkeypatch, tmp_path)
    reflow.reflow_book(bid)
    pages = db.get_pages(bid)
    # the ornament before the chapter's first line divides nothing, so page 0
    # opens on its heading; the other two each open on the break above them
    assert pages[0]["read_text"] == "## One\n\nHe was a dragon, and no mistake."
    assert pages[1]["read_text"].startswith("---\n\nThe others were washing.")
    assert pages[2]["read_text"] == "---\n\nThen it was morning."


def test_reflow_keeps_the_punctuation_a_page_breaks_off_on(monkeypatch, tmp_path):
    """A page that stops mid-sentence stops on a dash or an ellipsis, and the
    source often sets that off with a space. The span runs word to word, so
    without help the page would come back a beat short."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    import webapp.db
    db = importlib.reload(webapp.db)
    import webapp.reflow
    reflow = importlib.reload(webapp.reflow)
    db.init()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/>'
                   "</rootfiles></container>")
        z.writestr("OEBPS/content.opf",
                   '<package><manifest><item id="c1" href="c1.xhtml"/></manifest>'
                   '<spine><itemref idref="c1"/></spine></package>')
        z.writestr("OEBPS/c1.xhtml",
                   "<html><body><p>He crept toward the door —</p>"
                   "<p>“AAAAARRRGH!”</p></body></html>")
    bid = db.create_book("The Book", "", "book.epub", "watercolor", 200,
                         "application/epub+zip", out.getvalue())
    db.add_chapter(bid, 0, "One", 0, [])
    db.add_page(bid, 0, 0, "Creeping", "He crept toward the door —", "", "", [])
    reflow.reflow_book(bid)
    assert db.get_pages(bid)[0]["read_text"] == "He crept toward the door —"
