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
    bid = db.create_book("The Book", "", "book.epub", "watercolor", 200, "7",
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
