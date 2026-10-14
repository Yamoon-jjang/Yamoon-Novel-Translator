"""Recovery observability must be additive to established recovery behavior."""
from pathlib import Path


def test_recovery_stage_counters_guard_success_paths():
    src=(Path(__file__).resolve().parents[1]/'novel_translator'/'ui'/'worker.py').read_text(encoding='utf-8')
    assert "self.recovery_metrics['reframe_attempts']+=1" in src
    assert "self.recovery_metrics['reframe_successes']+=1" in src
    assert "self.recovery_metrics['split_attempts']+=1" in src
    assert "self.recovery_metrics['split_successes']+=1" in src
    assert "result=self._call_orch(orch, chunk, '', True, 'content')" in src
    assert "result=self._translate_parts(orch,parts,'' if content_failure else prev,depth)" in src
    assert "return result" in src
    assert "성공은 로컬 LLM 보조 문단을 포함할 수 있음" in src
