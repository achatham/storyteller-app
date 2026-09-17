"""Which of a book's own devices mark a scene break, decided per book."""
from pipeline import extract

# an ornament image used all through the book, and a stylesheet that sets a new
# section the way print does: space above the paragraph and no indent
CSS = """
p { text-indent: 1em; }
p.break { text-indent: 0; margin-top: 1em; }
p.first { text-indent: 0; margin-top: 3em; }
p.noindent { text-indent: 0; }
p.caption { text-indent: 0; margin-top: 1em; font-size: .8em; text-align: center; }
"""
CHAPTER = """<html><body>
<p class="chaptitle">One</p>
<p class="first">He was a dragon, and no mistake.</p>
<p>The others were washing.</p>
<p class="break">Next morning, nobody moved at all.</p>
<p class="noindent">Nor the morning after.</p>
<p class="caption">A dragon, yesterday</p>
</body></html>"""


def test_break_classes_come_from_the_books_own_stylesheet():
    cls = extract._break_classes(CSS, [CHAPTER])
    # the paragraph set apart mid-chapter, and nothing else: `first` only ever
    # opens a chapter, `noindent` has no air above it, `caption` is not prose
    assert cls == {"break"}


def test_no_break_classes_when_the_book_does_not_indent_its_paragraphs():
    # then "no indent" describes every paragraph and says nothing (calibre
    # conversions set their paragraphs off with a margin instead)
    css = "p { margin-top: 3pt; } p.body { text-indent: 0; margin-top: 3pt; }"
    doc = '<html><body><p class="body">One.</p><p class="body">Two.</p></body></html>'
    assert extract._break_classes(css, [doc]) == set()


def test_divider_images_are_the_ones_used_over_and_over():
    doc = ('<p>One.</p><img src="images/dingbat.png"/><p>Two.</p>'
           '<img src="images/dingbat.png"/><p>Three.</p>'
           '<img src="../images/dingbat.png"/><p>Four.</p>'
           '<img src="images/plate1.jpg"/><p>Five.</p>')
    assert extract._divider_images([doc]) == {"dingbat.png"}
