import importlib

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A book whose chapters named one minor character twice (`annie` in ch 0,
    `annie_captive` in ch 1), with an unused registry entity from another book and an
    armor prop that duplicates the bear's armored variant."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_APP_DB", str(tmp_path / "storyteller.db"))
    monkeypatch.setenv("STORY_COST_DB", str(tmp_path / "costs.db"))
    import webapp.db as db
    importlib.reload(db)
    db.init()
    import webapp.roster_review as rr
    importlib.reload(rr)
    bid = db.create_book("T", "A", "book.epub", "watercolor", 200,
                         "application/epub+zip", b"PK-test")
    db.save_registry(bid, {"entities": [
        {"id": "lyra", "type": "character", "name": "Lyra", "importance": 5,
         "base_appearance": "a girl", "variants": [
             {"id": "furs", "label": "Furs", "appearance": "girl in furs", "sheet_prompt": "s"},
             {"id": "grown", "label": "Grown", "appearance": "a woman", "sheet_prompt": "s"}]},
        {"id": "bear", "type": "character", "name": "Bear", "importance": 5,
         "base_appearance": "a bear", "variants": [
             {"id": "armored", "label": "Armored", "appearance": "bear in armor",
              "sheet_prompt": "s"}]},
        {"id": "armor", "type": "prop", "name": "Armor", "importance": 4,
         "base_appearance": "iron armor", "base_sheet_prompt": "armor", "variants": []},
        {"id": "will", "type": "character", "name": "Will", "importance": 3,
         "base_appearance": "a boy", "variants": []},
    ]})

    def local(eid, look):
        return {"entity_id": eid, "variant_id": "default", "name": "Annie",
                "appearance": look, "sheet_prompt": f"sheet of {look}", "from_registry": False}

    reg = {"entity_id": "lyra", "variant_id": "furs", "from_registry": True}
    db.add_chapter(bid, 0, "Ch 1", 0, [reg, local("annie", "girl in a smock")])
    db.add_chapter(bid, 1, "Ch 2", 2, [reg, local("annie_captive", "girl in grey")])
    casts = [[{"entity_id": "lyra", "variant_id": "furs"},
              {"entity_id": "annie", "variant_id": "default"}],
             [{"entity_id": "annie", "variant_id": "default"}],
             [{"entity_id": "annie_captive", "variant_id": "default"},
              {"entity_id": "bear", "variant_id": "armored"},
              {"entity_id": "armor", "variant_id": "default"}],
             [{"entity_id": "armor", "variant_id": "default"}]]
    for i, cast in enumerate(casts):
        db.add_page(bid, i, 0 if i < 2 else 1, f"p{i}", "text", "", "brief", cast)
    db.save_sheet(bid, "annie", "default", b"annie-sheet")
    db.save_sheet(bid, "annie_captive", "default", b"captive-sheet")
    return db, rr, bid


RAW = {
    "same_as_registry": [],
    "merge_locals": [{"local_ids": ["annie", "annie_captive"], "new_id": "Annie",
                      "name": "Annie", "keep_id": "annie_captive", "type": "character",
                      "reason": "same girl"}],
    "drop": [{"entity_id": "will", "variant_id": "", "reason": "sequel"},
             {"entity_id": "lyra", "variant_id": "grown", "reason": "sequel"},
             {"entity_id": "lyra", "variant_id": "furs", "reason": "wrong"}],
    "redundant_props": [{"prop_id": "armor",
                         "covered_by": [{"entity_id": "bear", "variant_id": "armored"}],
                         "reason": "variant wears it"}],
    "redraw": [{"entity_id": "ghost", "variant_id": "default", "problem": "x",
                "correction": "y"}],
    "notes": ["Pan's form is pinned per outfit"],
}


def test_validate_keeps_safe_fixes_and_rejects_the_rest(env):
    db, rr, bid = env
    rv = rr.validate(RAW, rr.gather(bid))
    assert rv["merge_locals"][0]["new_id"] == "annie"            # slugged
    assert [(d["entity_id"], d["variant_id"]) for d in rv["drop"]] == [("will", ""),
                                                                       ("lyra", "grown")]
    whys = {r["kind"]: r["why"] for r in rv["rejected"]}
    assert whys["drop"] == "still used by pages"                 # lyra/furs is on page 0
    assert whys["redraw"] == "no such drawn sheet"


def test_apply_merges_drops_and_strips_redundant_props(env):
    db, rr, bid = env
    rv = rr.validate(RAW, rr.gather(bid))
    summary = rr.apply(bid, rv, log=lambda *_: None, draw=False)
    reg = {e["id"]: e for e in db.get_registry(bid)["entities"]}
    assert "will" not in reg
    assert [v["id"] for v in reg["lyra"]["variants"]] == ["furs"]
    # the merged entity takes the KEPT member's look and sheet; the others' sheets go
    assert reg["annie"]["base_appearance"] == "girl in grey"
    assert db.get_sheet(bid, "annie", "default") == b"captive-sheet"
    assert db.get_sheet(bid, "annie_captive", "default") is None
    casts = dict(db.page_casts(bid))
    assert {c["entity_id"] for c in casts[2]} == {"annie", "bear"}   # armor stripped
    assert casts[3] == [{"entity_id": "armor", "variant_id": "default"}]  # no bear: kept
    ch = db.get_chapter_cast(bid, 1)
    assert {"entity_id": "annie", "variant_id": "default", "name": "Annie",
            "from_registry": True} in ch
    assert summary["merged"] == 1 and summary["prop_refs_removed"] == 1


def test_redraw_folds_the_correction_into_the_look(env):
    db, rr, bid = env
    db.save_sheet(bid, "bear", "armored", b"bear-sheet")
    raw = {**{k: [] for k in RAW}, "redraw": [
        {"entity_id": "bear", "variant_id": "armored", "problem": "no helmet",
         "correction": "He wears a pointed iron helmet."}]}
    rv = rr.validate(raw, rr.gather(bid))
    rr.apply(bid, rv, log=lambda *_: None, draw=False)
    var = next(e for e in db.get_registry(bid)["entities"] if e["id"] == "bear")["variants"][0]
    assert var["appearance"].endswith("Correction: He wears a pointed iron helmet.")
    assert var["sheet_prompt"].endswith("Correction: He wears a pointed iron helmet.")
