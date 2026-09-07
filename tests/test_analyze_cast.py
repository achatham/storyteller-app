import importlib


def test_blank_variant_ids_become_default(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("STORY_OUT", str(tmp_path))
    import pipeline.analyze as analyze
    importlib.reload(analyze)
    bible = {
        "cast": [
            {"entity_id": "dale_burgess", "variant_id": "", "from_registry": True},
            {"entity_id": "kendra", "variant_id": "summer", "from_registry": True},
            {"entity_id": "lena", "from_registry": True},            # key missing
        ],
        "spreads": [
            {"id": 1, "cast": [{"entity_id": "dale_burgess", "variant_id": "  "}]},
            {"id": 2, "cast": [{"entity_id": "kendra", "variant_id": "summer"}]},
        ],
    }
    assert analyze.normalize_cast_variants(bible) == 3
    assert [m["variant_id"] for m in bible["cast"]] == ["default", "summer", "default"]
    assert bible["spreads"][0]["cast"][0]["variant_id"] == "default"
    assert bible["spreads"][1]["cast"][0]["variant_id"] == "summer"
    assert analyze.normalize_cast_variants(bible) == 0      # idempotent
