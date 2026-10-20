"""v2.3.1 (rebuild): name/speech analysis only asks engines checked in API 설정 (plus an
explicitly chosen analysis engine), and every analysis run leaves its start, each failed
attempt and its result in 상태 / 오류 기록 — manual or automatic."""
from __future__ import annotations
import json

from novel_translator.ui import main_window as ui
from novel_translator.providers.base import TranslationResponse, ProviderError, ErrorKind
from test_alpha17 import _window, _wait, qt_app, sandbox  # noqa: F401 (pytest fixtures)
from test_v23_speech_progress import _setup, NAMES, TABLE


def _engines_window(monkeypatch, checked, analysis='auto'):
    w = _window(monkeypatch)
    w.secrets = {
        'gemini': {'keys': [{'id': 'g', 'secret': 'dummy-value', 'enabled': True}]},
        'openai': {'model': 'gpt-x', 'keys': [{'id': 'o', 'secret': 'dummy-value', 'enabled': True}]},
        'local': {'model': 'gemma-local', 'endpoint': 'http://localhost:11434'},
        'engines': [{'id': n, 'enabled': n in checked} for n in ('local', 'gemini', 'openai')],
        'extraction_engines': {'terms': analysis, 'speech': analysis},
    }
    return w


def test_local_only_setup_never_asks_cloud_engines_even_with_saved_keys(qt_app, sandbox, monkeypatch):
    w = _engines_window(monkeypatch, {'local'})
    try:
        assert [e[0] for e in w._extraction_engines('speech')] == ['local']
        assert [e[0] for e in w._extraction_engines('terms')] == ['local']
    finally:
        w.close()


def test_auto_uses_the_checked_translation_engines_in_order(qt_app, sandbox, monkeypatch):
    w = _engines_window(monkeypatch, {'local', 'gemini'})
    try:
        assert [e[0] for e in w._extraction_engines('speech')] == ['local', 'gemini']
    finally:
        w.close()


def test_a_chosen_analysis_engine_is_the_only_one_asked(qt_app, sandbox, monkeypatch):
    """The user's setup: all three engines checked for translation, 용어·말투 분석 = 로컬 LLM
    with its own model. Only that local model is asked; Gemini/OpenAI never."""
    w = _engines_window(monkeypatch, {'local', 'gemini', 'openai'}, analysis='local')
    w.secrets['extraction_engines']['model'] = 'hauhau-gemma4-26B_Q3_K-M:latest'
    try:
        for purpose in ('speech', 'terms'):
            got = w._extraction_engines(purpose)
            assert [(e[0], e[2]) for e in got] == [('local', 'hauhau-gemma4-26B_Q3_K-M:latest')]
    finally:
        w.close()
    w = _engines_window(monkeypatch, {'local'}, analysis='gemini')
    try:   # chosen on purpose even though unchecked for translation
        assert [e[0] for e in w._extraction_engines('speech')] == ['gemini']
    finally:
        w.close()


def _two_engine_setup(qt_app, sandbox, monkeypatch, reply, auto_terms=False):
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, reply, auto_terms=auto_terms)
    engines = [('local', 'local', 'gemma', 'http://x', {}), ('gemini', 'k', 'm', '', None)]
    monkeypatch.setattr(w, '_extraction_engines', lambda purpose='speech': list(engines))
    return w, calls, started


def _local_empty_gemini_ok(pid, req):
    return '' if pid == 'local' else TABLE


def test_manual_speech_run_is_logged_with_each_failed_attempt(qt_app, sandbox, monkeypatch):
    w, calls, started = _two_engine_setup(qt_app, sandbox, monkeypatch, _local_empty_gemini_ok)
    try:
        w._generate_speech()
        assert _wait(qt_app, lambda: w._term_thread is None and w._speech_rows(), 10)
        log = w.error_view.toPlainText()
        assert '인물 말투표 만들기 시작 · 인물 4명' in log and '엔진 로컬 LLM(gemma) → Gemini(m)' in log
        assert '인물 말투표 · 전체 대사 분석 · 로컬 LLM (AI 요청 1회째) · 답은 왔지만 쓸 수 있는 인물 관계 0쌍' in log
        assert '인물 말투표 자동 생성 · 2쌍' in log and 'AI 요청 2회' in log
    finally:
        w.close()


def test_manual_speech_failure_is_logged_too(qt_app, sandbox, monkeypatch):
    w, calls, started = _two_engine_setup(qt_app, sandbox, monkeypatch,
                                          lambda pid, req: ProviderError(ErrorKind.POLICY, 'blocked'))
    try:
        w._generate_speech()
        assert _wait(qt_app, lambda: w._term_thread is None, 10)
        log = w.error_view.toPlainText()
        assert 'Gemini (AI 요청 2회째) 실패 · POLICY' in log
        assert '인물 말투표 만들기 실패' in log
    finally:
        w.close()


def test_automatic_run_keeps_its_lines_after_the_translation_starts(qt_app, sandbox, monkeypatch):
    w, calls, started = _two_engine_setup(qt_app, sandbox, monkeypatch, _local_empty_gemini_ok, auto_terms=True)
    monkeypatch.setattr(w, '_extraction_engines', lambda purpose='speech': [('local', 'local', 'gemma', 'http://x', {}), ('gemini', 'k', 'm', '', None)])
    try:
        w.start_translation()
        assert _wait(qt_app, lambda: started and not w._extracting(), 10)
        log = w.error_view.toPlainText()
        order = ['번역 전 인명·용어 추출 시작', '인명·용어 자동 추출 · 새 항목',
                 '번역 전 인물 말투표 만들기 시작', '로컬 LLM (AI 요청 1회째) · 답은 왔지만', '인물 말투표 자동 생성 · 2쌍']
        pos = [log.find(x) for x in order]
        assert all(p >= 0 for p in pos) and pos == sorted(pos), (pos, log)
    finally:
        w.close()


def test_manual_term_extraction_is_logged(qt_app, sandbox, monkeypatch):
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, lambda pid, req: TABLE)
    try:
        for r in range(w.term_table.rowCount() - 1, -1, -1):
            w.term_table.removeRow(r)
        w._extract_terms()
        assert _wait(qt_app, lambda: w._term_thread is None and w._terms(), 10)
        log = w.error_view.toPlainText()
        assert '인명·용어 추출 시작 · 후보' in log and '인명·용어 추출 완료' in log
    finally:
        w.close()
