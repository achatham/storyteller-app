import importlib

import pytest


def test_per_book_budget_blocks_next_call(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_COST_DB", str(tmp_path / "costs.db"))
    monkeypatch.setenv("STORY_BOOK_BUDGET_USD", "0.01")
    import pipeline.costs as costs
    importlib.reload(costs)

    costs.record("gemini-3.5-flash", "text", 0, 1_000, run="book:7")
    with pytest.raises(costs.BudgetExceeded):
        costs.record("gemini-3.5-flash", "text", 0, 1_000, run="book:7")


def test_budget_does_not_apply_to_non_book_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_COST_DB", str(tmp_path / "costs.db"))
    monkeypatch.setenv("STORY_BOOK_BUDGET_USD", "0.000001")
    import pipeline.costs as costs
    importlib.reload(costs)

    assert costs.record("gemini-3.5-flash", "text", 0, 1_000, run="style_sample") > 0


def test_per_book_override_beats_the_global_cap(monkeypatch):
    from pipeline import costs
    monkeypatch.setenv("STORY_BOOK_BUDGET_USD", "100")
    monkeypatch.setenv("STORY_BOOK_BUDGETS", "28:140, 3:20")
    assert costs._book_budget("book:28") == 140
    assert costs._book_budget("book:3") == 20
    assert costs._book_budget("book:280") == 100     # no prefix match
