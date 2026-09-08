import importlib

import pytest


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    monkeypatch.setenv("STORY_COST_DB", str(tmp_path / "costs.db"))
    import webapp.db as db
    importlib.reload(db)
    import webapp.scene as sc
    importlib.reload(sc)
    return sc


def test_a_leading_article_does_not_fork_a_view_into_two_sheets(scene):
    """The slug is the sheet key. "the Hall of Prophecy" and "Hall of Prophecy" are
    the same room, and drawing each its own sheet is exactly the inconsistency the
    reference sheets exist to prevent."""
    assert scene._view_slug("the Hall of Prophecy") == scene._view_slug("Hall of Prophecy")
    assert scene._view_slug("The Great Hall") == scene._view_slug("great hall")
    assert scene._view_slug("a kitchen") == scene._view_slug("kitchen")
    assert scene._view_slug("An Aisle") == scene._view_slug("aisle")


def test_view_slug_keeps_a_word_that_merely_starts_with_an_article(scene):
    assert scene._view_slug("theatre balcony") == "theatre_balcony"
    assert scene._view_slug("Antechamber") == "antechamber"
    assert scene._view_slug("") == "inside"
