"""The cast block describes each figure alone, so nothing in the prompt says how big
they are next to each other or that a rider goes ON the animal. Those two omissions
were the commonest defects the scene critic logged on Gregor the Overlander.
"""
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


BOY = {"entity_id": "gregor", "name": "Gregor", "type": "character",
       "appearance": "An eleven-year-old boy in a smoky-blue tunic."}
CRAWLER = {"entity_id": "temp", "name": "Temp", "type": "character",
           "appearance": "A giant insect standing approximately four feet tall, an "
                         "upright cockroach with exactly six jointed legs."}
BAT = {"entity_id": "aurora", "name": "Aurora", "type": "character",
       "appearance": "A giant riding bat with two broad leathery wings, large enough "
                     "to carry a human rider."}
HALL = {"entity_id": "high_hall", "name": "The High Hall", "type": "setting",
        "appearance": "A stone hall with twenty-foot vaulted ceilings."}


def test_an_outsized_creature_gets_a_scale_line(scene):
    note = scene._relation_note([BOY, CRAWLER])
    assert "SCALE" in note and "Temp" in note
    assert "Gregor" not in note, "the boy is the yardstick, not the outsized figure"


def test_a_ridden_animal_gets_a_mount_line(scene):
    note = scene._relation_note([BOY, BAT])
    assert "MOUNTS" in note and "Aurora is an animal that people RIDE" in note
    assert "wings or extra limbs growing from a person" in note


def test_an_all_human_cast_gets_no_note(scene):
    assert scene._relation_note([BOY, dict(BOY, entity_id="luxa", name="Luxa")]) == ""


def test_a_settings_dimensions_are_not_a_creature_scale(scene):
    """A hall with twenty-foot ceilings must not be listed as a figure to size."""
    assert scene._relation_note([BOY, HALL]) == ""
    note = scene._relation_note([BOY, CRAWLER, HALL])
    assert "The High Hall" not in note


def test_both_lines_appear_when_the_cast_needs_both(scene):
    note = scene._relation_note([BOY, CRAWLER, BAT])
    assert "SCALE" in note and "MOUNTS" in note
    assert note.startswith("\n\n")


def test_a_brief_that_calls_for_riders_gets_the_mount_line_without_a_mount_in_the_cast(scene):
    """The page that drew Henry and Luxa with bat wings sprouting from their backs
    listed no bat in its cast at all -- only the brief knew they were flying."""
    brief = ("Henry and Luxa swoop and bank their bats in spirited aerobatics. General "
             "Solovet leans over a map spread across the shoulders of her bat, conferring "
             "with escort fliers in mid-flight.")
    note = scene._relation_note([BOY], brief)
    assert "MOUNTS" in note
    assert "wings or extra limbs growing from a person" in note


def test_an_ordinary_indoor_brief_still_gets_no_note(scene):
    brief = "Gregor sits slumped on a low wooden stool before a roaring fireplace."
    assert scene._relation_note([BOY], brief) == ""
