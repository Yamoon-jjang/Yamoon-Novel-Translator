"""v1.62 logging is explanatory only: requests and recovery are untouched."""
from pathlib import Path
from novel_translator import __version__, VERSION_LABEL


def test_v162_version_and_exact_model_trace():
    assert (__version__, VERSION_LABEL) == ('2.5.8', 'v2.5.8')
    src=(Path(__file__).resolve().parents[1] / 'novel_translator' / 'ui' / 'worker.py').read_text(encoding='utf-8')
    assert "'실제 번역 모델 · '" in src
    assert "self._model_for(n)" in src
    assert "실제 번역 모델" in src


def test_cooldown_message_does_not_claim_total_wait():
    src=(Path(__file__).resolve().parents[1] / 'novel_translator' / 'orchestrator.py').read_text(encoding='utf-8')
    assert '{format_log_wait(delay)}간 사용 보류' in src
    assert '다른 프로젝트 이용 가능' in src
    assert '즉시 사용 가능한 다른 프로젝트 없음' in src
    assert 'has_independent_ready' in src
    assert "self._cool_project(key, delay)" in src
    assert "if e.kind == ErrorKind.QUOTA and not has_independent_ready:" in src

def test_compact_duration_and_retry_messages():
    import importlib
    from novel_translator.orchestrator import format_log_wait
    assert format_log_wait(600) == '10분'
    assert format_log_wait(300) == '5분'
    assert format_log_wait(2.4) == '2.4초'
    assert format_log_wait(40) == '40초'
    worker=(Path(__file__).resolve().parents[1] / 'novel_translator' / 'ui' / 'worker.py').read_text(encoding='utf-8')
    orchestrator=(Path(__file__).resolve().parents[1] / 'novel_translator' / 'orchestrator.py').read_text(encoding='utf-8')
    assert '초 소요 (일시정지 시간 제외)' not in worker
    assert "POLICY':'정책 관련 응답 실패'" in worker
    assert "다른 프로젝트 이용 가능" in orchestrator
    assert "서버 일시적 오류" in orchestrator
    assert "self.sleep_fn(delay)" in orchestrator
