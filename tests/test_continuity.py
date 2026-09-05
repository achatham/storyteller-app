import importlib
import json

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fresh DB with one book: a registry (a character with two variants, a
    setting, a prop), one chapter, and three pages with drawn scenes."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    monkeypatch.setenv("STORY_COST_DB", str(tmp_path / "costs.db"))
    import webapp.db as db
    importlib.reload(db)
    db.init()
    import webapp.continuity as cont
    importlib.reload(cont)
    bid = db.create_book("T", "A", "book.pdf", "watercolor", 200, "5",
                         "application/pdf", b"%PDF-test")
    db.save_registry(bid, {"entities": [
        {"id": "kid", "type": "character", "name": "Kid", "importance": 5,
         "base_appearance": "a kid", "base_sheet_prompt": "sheet of a kid",
         "variants": [{"id": "school", "label": "School", "appearance": "kid in uniform",
                       "sheet_prompt": "x"},
                      {"id": "pyjamas", "label": "Pyjamas", "appearance": "kid in pyjamas",
                       "sheet_prompt": "y"}]},
        {"id": "castle", "type": "setting", "name": "Castle", "importance": 4,
         "base_appearance": "a castle", "base_sheet_prompt": "castle sheet", "variants": []},
        {"id": "stone", "type": "prop", "name": "Stone", "importance": 3,
         "base_appearance": "a red stone", "base_sheet_prompt": "stone sheet", "variants": []},
    ]})
    db.add_chapter(bid, 0, "Ch 1", 0, [{"entity_id": "kid", "variant_id": "school",
                                         "from_registry": True},
                                        {"entity_id": "nurse", "variant_id": "default",
                                         "name": "Nurse", "appearance": "a nurse",
                                         "from_registry": False}])
    for i in range(3):
        db.add_page(bid, i, 0, f"p{i}", f"Text of page {i}.", "The ward",
                    f"brief {i}", [{"entity_id": "kid", "variant_id": "school"},
                                   {"entity_id": "castle", "variant_id": "default", "view": ""}])
        db.scene_store(bid, i, b"img%d" % i, 4.0)
    return db, cont, bid


def test_window_groups_fold_a_short_tail():
    from webapp import continuity as cont
    assert cont.window_groups(list(range(10, 22)), 5) == [[10, 11, 12, 13, 14],
                                                           [15, 16, 17, 18, 19, 20, 21]]
    assert cont.window_groups([1, 2, 3, 4, 5, 6, 7, 8], 5) == [[1, 2, 3, 4, 5], [6, 7, 8]]
    assert cont.window_groups([3], 5) == [[3]]


def test_build_window_lists_pages_images_and_registry(env):
    db, cont, bid = env
    win = cont.build_window(bid, [0, 1, 2])
    assert [p["idx"] for p in win["pages"]] == [0, 1, 2]
    assert win["missing_images"] == []
    assert "kid [character" in win["registry_text"]
    assert "school \"School\"" in win["registry_text"]
    assert "castle [setting" in win["registry_text"]
    parts = cont.review_contents(win)
    assert "=== PAGE 1:" in parts[0]
    assert "kid/school (Kid, School)" in parts[0]
    # the three page images follow their labels, in order
    assert parts[parts.index("--- Illustration for page 2 ---") + 1] == b"img2"


def test_merge_reviews_dedups_proposals_by_id():
    from webapp import continuity as cont
    a = {"summary": "a", "pages": [0, 1], "continuity_issues": [{"pages": [0], "issue": "x",
         "root_cause": "brief", "severity": 1}],
         "new_entities": [{"id": "ward", "type": "setting", "name": "Ward", "appearance": "w",
                           "sheet_prompt": "s", "pages": [0, 1]}],
         "new_variants": [], "page_edits": [{"idx": 1, "action": "keep", "problems": []}]}
    b = {"summary": "b", "pages": [2, 3], "continuity_issues": [],
         "new_entities": [{"id": "ward", "type": "setting", "name": "Ward", "appearance": "w2",
                           "sheet_prompt": "s", "pages": [2]}],
         "new_variants": [], "page_edits": [{"idx": 0, "action": "keep", "problems": []}]}
    m = cont.merge_reviews([a, b])
    assert len(m["new_entities"]) == 1 and m["new_entities"][0]["pages"] == [0, 1, 2]
    assert [e["idx"] for e in m["page_edits"]] == [0, 1]
    assert m["pages"] == [0, 1, 2, 3]


def test_apply_review_writes_plan_and_returns_redraws(env):
    db, cont, bid = env
    review = {
        "new_entities": [{"id": "Hospital Ward", "type": "setting", "name": "The Ward",
                          "importance": 4, "summary": "s", "appearance": "white beds",
                          "sheet_prompt": "a ward", "pages": [0, 1, 2], "why": "no ref"}],
        "new_variants": [
            {"entity_id": "kid", "id": "bandaged", "kind": "state", "label": "Bandaged",
             "appearance": "kid with bandage", "sheet_prompt": "b", "pages": [1]},
            {"entity_id": "kid", "id": "pyjamas", "kind": "outfit", "label": "dup",
             "appearance": "dup", "sheet_prompt": "b", "pages": [1]},          # exists
            {"entity_id": "ghost", "id": "x", "kind": "outfit", "label": "x",
             "appearance": "x", "sheet_prompt": "x", "pages": [1]},           # no entity
        ],
        "page_edits": [
            {"idx": 0, "action": "keep", "problems": []},
            {"idx": 1, "action": "revise", "problems": ["arm is wrong"],
             "edit_instruction": "Keep everything; fix the arm.",
             "reference_characters": ["Kid"],
             "cast": [{"entity_id": "kid", "variant_id": "bandaged"},
                      {"entity_id": "hospital_ward", "variant_id": "default", "view": ""},
                      {"entity_id": "nurse", "variant_id": "default"},
                      {"entity_id": "nobody", "variant_id": "default"}]},
            {"idx": 2, "action": "regenerate", "problems": ["wrong room"],
             "brief": "A new brief.", "setting": "The ward, night",
             "cast": [{"entity_id": "kid", "variant_id": "no_such_variant"}]},
        ],
    }
    rep = cont.apply_review(bid, review, log=lambda *_: None)
    assert rep["entities_added"] == ["hospital_ward"]
    assert rep["variants_added"] == ["kid/bandaged"]
    assert any("already exists" in n for n in rep["notes"])
    assert any("ghost" in n for n in rep["notes"])
    reg = db.get_registry(bid)
    ward = next(e for e in reg["entities"] if e["id"] == "hospital_ward")
    assert ward["type"] == "setting" and ward["base_appearance"] == "white beds"
    kid = next(e for e in reg["entities"] if e["id"] == "kid")
    assert [v["id"] for v in kid["variants"]] == ["school", "pyjamas", "bandaged"]

    p1 = db.get_page(bid, 1)
    cast1 = json.loads(p1["cast_json"])
    assert cast1 == [{"entity_id": "kid", "variant_id": "bandaged"},
                     {"entity_id": "hospital_ward", "variant_id": "default", "view": ""},
                     {"entity_id": "nurse", "variant_id": "default"}]   # 'nobody' dropped
    assert p1["brief"] == "brief 1"                                        # untouched
    p2 = db.get_page(bid, 2)
    assert p2["brief"] == "A new brief." and p2["setting"] == "The ward, night"
    assert json.loads(p2["cast_json"]) == [{"entity_id": "kid", "variant_id": "school"}]
    assert any("no variant 'no_such_variant'" in n for n in rep["notes"])

    assert {r["idx"]: r["mode"] for r in rep["redraws"]} == {1: "revise", 2: "regenerate"}
    seed = next(r for r in rep["redraws"] if r["idx"] == 1)["seed"]
    assert seed["instruction"] == "Keep everything; fix the arm." and seed["ref_chars"] == ["Kid"]
    # nothing was drawn or deleted: the pictures are still there
    assert db.scene_data(bid, 1) == b"img1" and db.scene_data(bid, 2) == b"img2"
    assert {p["idx"] for p in rep["pages_updated"]} == {1, 2}


def test_apply_review_retags_a_kept_page_without_redrawing_it(env):
    db, cont, bid = env
    review = {"new_entities": [], "new_variants": [],
              "page_edits": [{"idx": 0, "action": "keep", "problems": [],
                              "cast": [{"entity_id": "kid", "variant_id": "pyjamas"}]}]}
    rep = cont.apply_review(bid, review, log=lambda *_: None)
    assert rep["redraws"] == []
    assert json.loads(db.get_page(bid, 0)["cast_json"]) == [{"entity_id": "kid", "variant_id": "pyjamas"}]


def test_review_rows_round_trip(env):
    db, cont, bid = env
    rv = {"summary": "fine", "pages": [0, 1], "continuity_issues": [],
          "new_entities": [], "new_variants": [{"entity_id": "kid", "id": "z"}],
          "page_edits": [{"idx": 0, "action": "revise", "problems": []},
                         {"idx": 1, "action": "keep", "problems": []}]}
    rid = db.review_add(bid, 0, 1, rv)
    lst = db.reviews_for_book(bid)
    assert lst[0]["id"] == rid and lst[0]["n_edits"] == 1 and lst[0]["n_new_variants"] == 1
    assert lst[0]["applied_at"] is None
    db.review_mark_applied(bid, rid, {"redraws": []})
    got = db.review_get(bid, rid)
    assert got["review"]["summary"] == "fine" and got["applied"] == {"redraws": []}
    assert got["applied_at"] is not None
    db.review_delete(bid, rid)
    assert db.review_get(bid, rid) is None


def test_init_migrates_a_legacy_continuity_table(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    import sqlite3
    c = sqlite3.connect(tmp_path / "storyteller.db")
    c.execute("CREATE TABLE continuity_reviews (id INTEGER PRIMARY KEY, book_id INTEGER, "
              "status TEXT, report TEXT)")
    c.execute("INSERT INTO continuity_reviews VALUES (1, 18, 'done', '{}')")
    c.commit(); c.close()
    import webapp.db as db
    importlib.reload(db)
    db.init()
    with db.conn() as c:
        names = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"continuity_reviews", "continuity_reviews_old"} <= names
        assert c.execute("SELECT report FROM continuity_reviews_old").fetchone()["report"] == "{}"
        cols = {r["name"] for r in c.execute("PRAGMA table_info(continuity_reviews)")}
        assert "json" in cols
