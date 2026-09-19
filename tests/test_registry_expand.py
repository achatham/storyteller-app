"""The expand pass resolves each variant's delta into a full appearance. When it
can't -- Gemini's child-safety filter refuses to describe a named child, and no
amount of retrying or rewording moves it -- the fallback still has to give every
variant text of its own, or the whole entity is drawn from one prompt and its
roster sheets come out identical.
"""
import importlib

import pytest


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    import pipeline.registry as reg
    importlib.reload(reg)
    monkeypatch.setattr(reg, "BOOK_REF", "*A Book*")
    return reg


def _luxa():
    return {
        "id": "luxa", "type": "character", "name": "Queen Luxa",
        "summary": "The fierce future queen who rides a golden bat.",
        "canonical_details": "About eleven years old, extremely pale, violet eyes.",
        "variants": [
            {"id": "arena", "label": "Arena Riding Attire",
             "delta": "Long silver-blond hair in a braid; flexible riding garments."},
            {"id": "formal", "label": "High Hall Formal Dress",
             "delta": "An ornate Underland dress, hair loose to her waist."},
            {"id": "shorn", "label": "Quest Attire",
             "delta": "Hair chopped short; battle riding clothes and a sword scabbard."},
        ],
    }


def test_blocked_expand_still_gives_every_variant_its_own_look(registry, monkeypatch):
    def blocked(*a, **kw):
        raise ValueError("empty model response [finish=PROHIBITED_CONTENT]")

    monkeypatch.setattr(registry.gem, "text_json", blocked)
    out = registry.expand_one(_luxa())

    assert out["expand_failed"] is True
    appearances = [v["appearance"] for v in out["variants"]]
    prompts = [v["sheet_prompt"] for v in out["variants"]]
    assert len(set(appearances)) == 3, "variants must not share one description"
    assert len(set(prompts)) == 3
    # each keeps the base look AND what makes this variant different
    for v in out["variants"]:
        assert "violet eyes" in v["appearance"]
        assert v["delta"] in v["appearance"]
        assert v["appearance"] in v["sheet_prompt"]


def test_expand_fills_in_a_variant_the_model_skipped(registry, monkeypatch):
    """The model echoes only some variant ids -- the missing one used to end up with
    an empty appearance, which every caller silently replaces with base_appearance."""
    monkeypatch.setattr(registry.gem, "text_json", lambda *a, **kw: {
        "base_appearance": "A pale girl with violet eyes.",
        "base_sheet_prompt": "sheet of a pale girl",
        "variants": [{"id": "arena", "appearance": "braided hair, riding leathers",
                      "sheet_prompt": "sheet: riding leathers"}],
    })
    out = registry.expand_one(_luxa())

    assert "expand_failed" not in out
    by_id = {v["id"]: v for v in out["variants"]}
    assert by_id["arena"]["appearance"] == "braided hair, riding leathers"
    for vid in ("formal", "shorn"):
        assert by_id[vid]["appearance"].startswith("A pale girl with violet eyes.")
        assert by_id[vid]["delta"] in by_id[vid]["appearance"]
        assert by_id[vid]["sheet_prompt"]
    assert len({v["appearance"] for v in out["variants"]}) == 3


def test_often_counts_as_a_hedge(registry, capsys):
    """A delta reading "often carrying knitting needles and misshapen elf hats" put
    knitting in Hermione's hands on all 200 pages of her prefect variant -- as
    unconditional as any other hedge, and it went unflagged."""
    entity = {"id": "hermione_granger", "name": "Hermione Granger"}
    variant = {"id": "prefect_robes",
               "delta": "Neat Hogwarts robes, often carrying knitting needles."}
    assert registry.flag_momentary_variant(entity, variant) is True
    assert variant["momentary_warning"]
    assert "MOMENTARY" in capsys.readouterr().out

    durable = {"id": "quest", "delta": "Hair chopped short; battle robes and a scabbard."}
    assert registry.flag_momentary_variant(entity, durable) is False
    assert "momentary_warning" not in durable


def _miri():
    """The Princess Academy protagonist, as the discover pass left her: an entity whose
    canonical_details are narrative prose, with no age anywhere but the variant deltas."""
    return {
        "id": "miri_larendaughter", "type": "character", "name": "Miri Larendaughter",
        "summary": "Fourteen-year-old protagonist of Mount Eskel.",
        "canonical_details": (
            "Tiny from birth, noticeably smaller than girls years younger than her. "
            "She has bright blue eyes and dark brown hair the color of a hawk feather, "
            "typically kept in a braid. Described as the spitting image of her deceased "
            "mother. Her laugh is low and throaty."),
        "variants": [
            {"id": "age_fourteen", "kind": "outfit", "label": "Fourteen in worn boots",
             "delta": "Age fourteen, wearing rough mountain wool leggings and thin-soled boots."},
            {"id": "age_fifteen", "kind": "outfit", "label": "Fifteen in sturdy boots",
             "delta": "Turns fifteen at midsummer; the same wool shirt, but new thick-soled boots."},
        ],
    }


def test_drawable_keeps_only_what_can_be_drawn(registry):
    d = registry.drawable(_miri()["canonical_details"])
    assert "bright blue eyes" in d and "hawk feather" in d   # the visual facts survive
    assert "laugh" not in d                                  # nothing to draw
    assert "spitting image" not in d                         # a likeness we cannot draw
    # a sentence that hedges AND carries a visual fact keeps the fact
    assert "apron" in registry.drawable("He often wears a leather apron.")
    # ... but a pure habit goes
    assert registry.drawable("Often rubs her forehead when perplexed.") == ""


def test_drawable_strips_distress_worn_as_an_expression(registry):
    """Gerti's only sheet came out as a frightened child with a hand to her forehead,
    and that expression then rode all 18 of her pages."""
    d = registry.drawable("Small and petite, with round, frightened dark eyes.")
    assert "frightened" not in d
    assert "round" in d and "dark eyes" in d


def test_age_is_read_per_variant(registry):
    ent = _miri()
    assert registry.find_age(ent["canonical_details"]) is None   # the details never say
    assert registry.find_age(ent["summary"]) == 14
    assert registry.find_age(ent["variants"][1]["delta"]) == 15  # "Turns fifteen"
    assert registry._figure_noun(ent, 14) == "A 14-year-old girl."
    assert registry._figure_noun({"type": "prop"}, 14) == ""     # only characters


def test_fallback_leads_with_an_age(registry, monkeypatch):
    """Without an age anchor the generator drew the protagonist as a toddler."""
    monkeypatch.setattr(registry.gem, "text_json", _blocked)
    out = registry.expand_one(_miri())
    by_id = {v["id"]: v for v in out["variants"]}
    assert by_id["age_fourteen"]["appearance"].startswith("A 14-year-old girl.")
    assert by_id["age_fifteen"]["appearance"].startswith("A 15-year-old girl.")
    # the narrative noise never reaches the image prompt
    assert "throaty" not in by_id["age_fourteen"]["sheet_prompt"]
    # "Turns fifteen at midsummer" is an event, not a look -- the age was hoisted instead
    assert "midsummer" not in by_id["age_fifteen"]["appearance"]


def _blocked(*a, **kw):
    raise ValueError("empty model response [finish=PROHIBITED_CONTENT]")


def test_wardrobe_rescue_when_the_full_expand_is_refused(registry, monkeypatch):
    """The safety filter refuses a child's BODY, not their clothes: measured live, the
    same model answers in full once build/face/skin are out of the request."""
    calls = []

    def fake(prompt, **kw):
        calls.append(prompt)
        if "costume designer" not in prompt:
            raise ValueError("empty model response [finish=PROHIBITED_CONTENT]")
        return {"base_outfit": "a loose unbleached wool tunic over grey leggings",
                "variants": [
                    {"id": "age_fourteen", "outfit": "rough wool leggings, thin-soled boots"},
                    {"id": "age_fifteen", "outfit": "the same wool shirt, new thick-soled boots"}]}

    monkeypatch.setattr(registry.gem, "text_json", fake)
    out = registry.expand_one(_miri())

    assert len(calls) == 2 and out["wardrobe_only"] is True
    assert "expand_failed" not in out
    by_id = {v["id"]: v for v in out["variants"]}
    a14 = by_id["age_fourteen"]["appearance"]
    assert a14.startswith("A 14-year-old girl.")      # age, from the delta
    assert "bright blue eyes" in a14                  # body, from the book's own words
    assert "thin-soled boots" in a14                  # clothes, from the rescue pass
    assert "throaty" not in a14                       # narrative noise still dropped
    assert by_id["age_fourteen"]["appearance"] != by_id["age_fifteen"]["appearance"]


def test_wardrobe_rescue_failing_too_falls_back(registry, monkeypatch):
    monkeypatch.setattr(registry.gem, "text_json", _blocked)
    out = registry.expand_one(_miri())
    assert out["expand_failed"] is True
    assert "wardrobe_only" not in out
    assert "bright blue eyes" in out["variants"][0]["appearance"]


def test_a_variant_that_changes_the_age_does_not_contradict_the_base(registry, monkeypatch):
    """When the expand pass wrote the base look (with an age in it) but skipped a
    variant, the fallback must not open with a second, different age."""
    monkeypatch.setattr(registry.gem, "text_json", lambda *a, **kw: {
        "base_appearance": "A petite fourteen-year-old girl with bright blue eyes.",
        "base_sheet_prompt": "sheet of a petite girl",
        "variants": [],                       # the model echoed neither variant id
    })
    out = registry.expand_one(_miri())
    by_id = {v["id"]: v for v in out["variants"]}
    same = by_id["age_fourteen"]["appearance"]
    older = by_id["age_fifteen"]["appearance"]
    assert same.startswith("A petite fourteen-year-old girl")   # no second age bolted on
    assert "15-year-old" not in same
    assert older.startswith("A petite fourteen-year-old girl")
    assert "In this look: now 15 years old;" in older           # the override, stated once


def test_wardrobe_rescue_does_not_restate_an_age_the_book_already_gave(registry, monkeypatch):
    """Gerti's details open with "Barely twelve years old" -- prefixing "A 12-year-old
    girl." in front of that is noise in a prompt that is already long."""
    gerti = {"id": "gerti", "type": "character", "name": "Gerti",
             "summary": "The youngest academy student.",
             "canonical_details": "Barely twelve years old, small and petite, with round, "
                                  "frightened dark eyes. Often rubs her forehead when perplexed.",
             "variants": [{"id": "young_student", "delta": "Oversized homespun leggings."}]}

    def fake(prompt, **kw):
        if "costume designer" not in prompt:
            raise ValueError("empty model response [finish=PROHIBITED_CONTENT]")
        return {"base_outfit": "a cream wool tunic",
                "variants": [{"id": "young_student", "outfit": "an oversized cream wool tunic"}]}

    monkeypatch.setattr(registry.gem, "text_json", fake)
    app = registry.expand_one(gerti)["variants"][0]["appearance"]
    assert app.startswith("Barely twelve years old")
    assert "12-year-old" not in app
    assert "frightened" not in app and "perplexed" not in app
    assert "oversized cream wool tunic" in app
