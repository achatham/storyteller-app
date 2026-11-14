import importlib

import pytest

pytestmark = pytest.mark.real_headword


def setup(monkeypatch, tmp_path, answers):
    """Server + DB with the text model answering headwords from `answers`."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    monkeypatch.setenv("STORY_COST_DB", str(tmp_path / "costs.db"))
    import webapp.db
    importlib.reload(webapp.db)
    import webapp.lemma as lemma
    lemma = importlib.reload(lemma)
    import webapp.server as server
    server = importlib.reload(server)
    server.db.init()
    asked = []

    def fake(prompt, schema=None, model=None, thinking_level=None):
        form = prompt.split('"')[1]
        asked.append(form)
        if form not in answers:
            raise RuntimeError("model down")
        return {"headword": answers[form]}

    monkeypatch.setattr(lemma.gem, "text_json", fake)
    bid = server.db.create_book("B", "", "b.pdf", "watercolor", 200, "application/pdf", b"%PDF-")
    kid = server.api_kid_add({"name": "Ada"})["id"]
    server.api_book_kid(bid, {"kid_id": kid})
    return server, lemma, bid, asked


def test_forms_of_a_word_share_one_card(monkeypatch, tmp_path):
    server, _, bid, _ = setup(monkeypatch, tmp_path,
                              {"devoured": "devour", "devouring": "devour", "attentively": "attentive"})
    a = server.api_vocab_add(bid, {"word": "devoured", "context": "instantly devoured."})
    b = server.api_vocab_add(bid, {"word": "Devouring", "context": "devouring the cake"})
    assert a["new"] and a["word"] == "devour" and a["forms"] == ["devoured"]
    assert not b["new"] and b["id"] == a["id"] and b["forms"] == ["devoured", "devouring"]
    assert b["context"] == "instantly devoured."            # first-met sentence kept
    assert server.api_vocab_add(bid, {"word": "attentively"})["word"] == "attentive"
    assert sorted(server.api_book_vocab(bid)["words"]) == \
        ["attentive", "attentively", "devour", "devoured", "devouring"]


def test_bad_or_failed_answers_fall_back_to_the_printed_form(monkeypatch, tmp_path):
    server, lemma, bid, _ = setup(monkeypatch, tmp_path,
                                  {"clutched": "to clutch", "gloomier": "  Gloomy. "})
    assert server.api_vocab_add(bid, {"word": "clutched"})["word"] == "clutched"   # a phrase
    assert server.api_vocab_add(bid, {"word": "gloomier"})["word"] == "gloomy"     # cleaned up
    assert server.api_vocab_add(bid, {"word": "satyrs"})["word"] == "satyrs"       # model down


def test_backfill_refiles_old_words_and_merges_duplicates(monkeypatch, tmp_path):
    server, lemma, bid, asked = setup(monkeypatch, tmp_path,
                                      {"devoured": "devour", "devour": "devour", "residual": "residual"})
    db = server.db
    old = db.vocab_add(bid, "devoured", "instantly devoured.", 3, kid_id=1)
    base = db.vocab_add(bid, "devour", "they devour it", 5, kid_id=1)
    keep = db.vocab_add(bid, "residual", "residual effects", 7, kid_id=1)
    server.api_vocab_review(old["id"], {"rating": "good"})
    with db.conn() as c:                                       # as saved before headwords
        c.execute("UPDATE vocab SET forms=NULL")

    assert lemma.backfill() == 3
    rows = {w["word"]: w for w in server.api_vocab_list()}
    assert set(rows) == {"devour", "residual"}
    assert rows["devour"]["id"] == base["id"] and rows["devour"]["seen_count"] == 2
    assert sorted(rows["devour"]["forms"]) == ["devour", "devoured"]
    assert rows["devour"]["reviews"] == 1                      # took over the graded card
    assert rows["residual"]["id"] == keep["id"] and rows["residual"]["forms"] == ["residual"]
    asked.clear()
    assert lemma.backfill() == 0 and asked == []               # nothing left to do
