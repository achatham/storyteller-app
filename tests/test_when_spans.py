"""A variant's 'when' picks the look for a chapter. A character who leaves and comes
back has a split span, and collapsing it to min..max dresses them wrongly for every
chapter in between.
"""
import importlib

import pytest


@pytest.fixture
def process(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    monkeypatch.setenv("STORY_COST_DB", str(tmp_path / "costs.db"))
    import webapp.process as pr
    importlib.reload(pr)
    return pr


def test_a_split_span_does_not_claim_the_gap(process):
    chs = process._when_chapters("Ch 4-15, Ch 24-27")
    assert 4 in chs and 15 in chs and 24 in chs and 27 in chs
    assert not (chs & set(range(16, 24))), "the quest chapters are not in this look"


def test_when_chapters_reads_the_shapes_the_registry_writes(process):
    assert process._when_chapters("Ch 2-3") == {2, 3}
    assert process._when_chapters("Ch 21") == {21}
    assert process._when_chapters("Ch 4, Ch 7, Ch 24-25") == {4, 7, 24, 25}
    assert process._when_chapters("the final voyage") is None
    assert process._when_chapters("") is None


def test_variant_for_chapter_falls_through_the_gap_to_the_first_variant(process):
    entity = {"variants": [{"id": "palace", "when": "Ch 4-15, Ch 24-27"},
                           {"id": "quest", "when": "Ch 16-23"}]}
    assert process._variant_for_chapter(entity, 5) == "palace"
    assert process._variant_for_chapter(entity, 25) == "palace"
    assert process._variant_for_chapter(entity, 19) == "quest"
    assert process._variant_for_chapter({"variants": []}, 3) == "default"
