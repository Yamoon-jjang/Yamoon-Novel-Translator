"""Focused regression coverage for non-noisy recovery explanations."""
from pathlib import Path

from novel_translator.prompting import CONTENT_RECOVERY_NOTE, BASE_POLICY


def test_recovery_prompt_stays_paragraph_scoped():
    assert "Apply the SCOPE exclusion to the affected paragraph only" in CONTENT_RECOVERY_NOTE
    assert "do not omit or summarize other paragraphs" in CONTENT_RECOVERY_NOTE
    assert "⟦NT_SKIP⟧" in BASE_POLICY
    assert "Keep every <P######> paragraph ID exactly once" in BASE_POLICY


def test_recovery_logging_does_not_change_split_contract():
    # Guard against accidentally replacing split/retry/fallback calls with diagnostic-only behavior.
    source = (Path(__file__).resolve().parents[1] / "novel_translator" / "ui" / "worker.py").read_text(encoding="utf-8")
    assert "앞 문맥 없이 1회 재시도" in source
    assert "부분 분할 재시도 (문단별 복구)" in source
    assert "이전 실패 문단" in source
    assert ("return self._translate_parts(orch,parts,'' if content_failure else prev,depth)" in source or
            ("result=self._translate_parts(orch,parts,'' if content_failure else prev,depth)" in source
             and "self.recovery_metrics['split_successes']+=1" in source))
    assert ("return self._call_orch(orch, chunk, '', True, 'content')" in source or
            ("result=self._call_orch(orch, chunk, '', True, 'content')" in source
             and "self.recovery_metrics['reframe_successes']+=1" in source))
    assert "if depth == 0:" in source
