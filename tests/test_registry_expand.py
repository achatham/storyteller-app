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
