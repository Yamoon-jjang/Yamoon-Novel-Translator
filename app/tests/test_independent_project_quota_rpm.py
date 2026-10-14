"""Project quota cooling must not penalize unrelated ready projects."""
from pathlib import Path

from novel_translator.orchestrator import KeyState, TranslationOrchestrator


def test_independent_project_remains_ready_after_quota_cooldown():
    now=[1000.0]
    orch=TranslationOrchestrator(
        provider=None,
        keys=[KeyState('one','secret1','P1'), KeyState('two','secret2','P2')],
        model='test', target_rpm=10, min_rpm=2, max_rpm=30,
        clock_fn=lambda:now[0], sleep_fn=lambda t:now.__setitem__(0,now[0]+t),
    )
    first=orch._pick_key()
    assert first.project_group == 'P1'
    orch._cool_project(first, 600.0)
    assert orch._pick_key().project_group == 'P2'
    assert orch._adaptive_rpm == 10
    assert orch._effective_ready_at(first) == 1600.0


def test_global_quota_rpm_guard_remains_conservative_when_all_projects_cool():
    # This control-flow assertion complements the independent-key scheduling test:
    # only the explicit QUOTA + independent-ready case may skip global slowdown.
    src=(Path(__file__).resolve().parents[1]/'novel_translator'/'orchestrator.py').read_text(encoding='utf-8')
    assert "if e.kind == ErrorKind.RATE_LIMIT or not has_independent_ready:" in src
    assert "self._note_rate_limit()" in src
    assert "if e.kind == ErrorKind.QUOTA and not has_independent_ready:" in src
    assert "self._cool_project(key, delay)" in src
