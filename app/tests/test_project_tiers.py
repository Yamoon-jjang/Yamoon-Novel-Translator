"""Regression cases for the opt-in Gemini free/paid project labels.

No network calls or billable requests; scheduling, legacy schema and accounting only.
"""
from novel_translator.api_keys import ApiKeyRecord, normalize_key_records
from novel_translator.orchestrator import KeyState, TranslationOrchestrator
from novel_translator.ui.worker import TranslationWorker


def test_old_api_key_records_default_to_unknown_without_reordering():
    old = [
        {"id": "A", "secret": "secret-a", "project_group": "A", "enabled": True},
        {"id": "B", "secret": "secret-b", "project_group": "B", "enabled": True},
    ]
    keys = normalize_key_records(old)
    assert [k.id for k in keys] == ["A", "B"]
    assert [k.tier for k in keys] == ["unknown", "unknown"]
    assert [k.tier for k in normalize_key_records([k.to_dict() for k in keys])] == ["unknown", "unknown"]


def test_free_paid_keys_are_metadata_not_prefix_guesses():
    rows = [
        {"id": "A", "secret": "AIza-legacy", "project_group": "A", "tier": "free"},
        {"id": "B", "secret": "AQ.newer", "project_group": "B", "tier": "paid"},
        {"id": "C", "secret": "other", "project_group": "C", "tier": "INVALID"},
    ]
    assert [x.tier for x in normalize_key_records(rows)] == ["free", "paid", "unknown"]


def test_free_a_then_free_b_then_paid_c_then_free_a_recovers():
    clock = [1000.0]
    keys = [
        KeyState("A", "a", "free-a", tier="free"),
        KeyState("B", "b", "free-b", tier="free"),
        KeyState("C", "c", "paid-c", tier="paid"),
    ]
    orch = TranslationOrchestrator(None, keys, "model", clock_fn=lambda: clock[0], sleep_fn=lambda _: None)
    assert orch._pick_key().key_id == "A"
    orch._cool_project(keys[0], 60)
    assert orch._pick_key().key_id == "B"
    orch._cool_project(keys[1], 60)
    assert orch._pick_key().key_id == "C"
    clock[0] += 61
    assert orch._pick_key().key_id == "A"


def test_sibling_keys_do_not_multiply_project_quota():
    clock = [1000.0]
    keys = [
        KeyState("A1", "a1", "A", tier="free"),
        KeyState("A2", "a2", "A", tier="free"),
        KeyState("B1", "b1", "B", tier="paid"),
    ]
    orch = TranslationOrchestrator(None, keys, "model", clock_fn=lambda: clock[0], sleep_fn=lambda _: None)
    orch._cool_project(keys[0], 60)
    assert orch._pick_key().key_id == "B1"


def test_free_paid_cost_accounting_and_unknown_legacy_estimate():
    class Totals:
        _on_usage = TranslationWorker._on_usage
        _cost = TranslationWorker._cost
        usage_summary = TranslationWorker.usage_summary

    t = Totals()
    t._chunk_requests = 0
    t._prices = {"model": (0.25, 1.50)}
    t.usage_totals = {}
    t.gemini_project_usage = {}
    t._on_usage("gemini", "model", {"input": 1_000_000, "output": 1_000_000}, True,
                KeyState("A", "a", "A", tier="free"))
    t._on_usage("gemini", "model", {"input": 1_000_000, "output": 1_000_000}, True,
                KeyState("B", "b", "B", tier="paid"))
    summary = t.usage_summary()
    assert "A(무료) 1회 · $0" in summary
    assert "B(유료) 1회 · 약 $1.750" in summary
    assert "추정 비용 약 $1.750" in summary
    legacy = Totals()
    legacy._chunk_requests = 0
    legacy._prices = t._prices
    legacy.usage_totals = {}
    legacy.gemini_project_usage = {}
    legacy._on_usage("gemini", "model", {"input": 1_000_000, "output": 1_000_000}, True,
                     KeyState("old", "old", "old"))
    assert "추정 비용 약 $1.750" in legacy.usage_summary()
