"""v2.0: the local LLM fixes reach every path that uses it (Korean refinement, glossary
and speech-table extraction), analysis keeps its steady sampling, and the term-conflict
window applies the user's choices instead of stopping with an error."""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from novel_translator.providers.base import TranslationRequest, TranslationResponse
from novel_translator.refinement_runtime import run_refinement
from test_v20_review_fixes import _FakeStream
from test_alpha17 import qt_app, sandbox  # noqa: F401,F811  (pytest fixtures)


# ---------------------------------------------------------------- local provider: purposes

def _capture(monkeypatch, reply):
    import novel_translator.providers.local_llm as L
    sent = []

    def post(url, headers=None, json=None, stream=False, timeout=None):
        sent.append(json)
        return _FakeStream(reply(json) if callable(reply) else reply)
    monkeypatch.setattr(L.requests, 'post', post)
    return L.LocalLLMProvider('http://localhost:11434/v1/chat/completions'), sent


def test_analysis_requests_keep_the_steady_setting_even_for_gemma(monkeypatch):
    p, sent = _capture(monkeypatch, '린위\t쑤칭\t반말\t친구')
    p.translate(TranslationRequest('gemma4-26b', 'S', 'dialogue', purpose='analysis'), 'local')
    assert sent[-1]['options']['temperature'] == 0.2 and 'top_k' not in sent[-1]['options']
    assert 'format' not in sent[-1]


def test_glossary_requests_get_json_only_mode_and_steady_setting(monkeypatch):
    p, sent = _capture(monkeypatch, '{"林雨": "린위"}')
    r = p.translate(TranslationRequest('gemma4-26b', 'S', 'CANDIDATES', purpose='json'), 'local')
    assert sent[-1]['format'] == 'json' and sent[-1]['options']['temperature'] == 0.2
    assert json.loads(r.text) == {'林雨': '린위'}


def test_refinement_edit_requests_require_every_paragraph_id(monkeypatch):
    reply = json.dumps({'P000001': '첫 문단.', 'P000002': '둘째 문단.', 'P000003': '셋째 문단.'}, ensure_ascii=False)
    p, sent = _capture(monkeypatch, reply)
    req = TranslationRequest('gemma4-26b', 'S', '교정 대상:\n<P000001>\n가\n\n<P000002>\n나\n\n<P000003>\n다',
                             purpose='edit', paragraph_ids=('P000001', 'P000002', 'P000003'))
    r = p.translate(req, 'local')
    assert sent[-1]['format']['required'] == ['P000001', 'P000002', 'P000003']
    assert '[출력 형식]' in sent[-1]['messages'][1]['content']
    assert sent[-1]['options']['temperature'] == 1.0          # prose keeps the model's own sampling
    assert r.text == '<P000001>\n첫 문단.\n\n<P000002>\n둘째 문단.\n\n<P000003>\n셋째 문단.'


def test_translation_requests_are_unchanged_by_the_purpose_field():
    import novel_translator.providers.local_llm as L
    req = TranslationRequest('m', 'S', '[SOURCE - q]\n<P000001>\n他\n\n<P000002>\n她')
    assert req.purpose == 'translate'
    assert L.LocalLLMProvider.source_ids(req) == ['P000001', 'P000002']
    assert L.LocalLLMProvider.source_ids(TranslationRequest('m', 'S', '[SOURCE - q]\n<P000001>\n他', purpose='analysis')) == []


def test_every_local_path_reads_the_same_settings(monkeypatch):
    import novel_translator.ui.main_window as mw
    monkeypatch.setattr(mw, 'load_json', lambda path: {'local_llm': {'structured_output': False, 'sampling': 'fixed'}})
    p = mw._make_provider('local', 'http://localhost:11434/v1/chat/completions', {})
    assert p.structured is False and p.sampling == 'fixed'


# ---------------------------------------------------------------- refinement with a 26B-like model

class EditLocal:
    """Behaves like the 26B: drops the paragraph containing MERGEME from a multi-paragraph
    answer (merges it into the one before), answers single paragraphs correctly."""
    name = 'local'

    def __init__(self):
        self.edit_sizes = []; self.purposes = []

    def translate(self, req, key):
        self.purposes.append(req.purpose)
        if req.purpose == 'analysis':
            return TranslationResponse('SAME' if '수정문' in req.user_prompt else '인물표')
        ids = list(req.paragraph_ids)
        self.edit_sizes.append(len(ids))
        body = req.user_prompt.split('교정 대상:\n', 1)[1]
        paras = dict(zip(ids, [b.split('\n', 1)[1].strip() for b in body.split('\n\n')]))
        rows = []
        for pid in ids:
            text = paras[pid] + ' 다듬음'
            if 'MERGEME' in paras[pid] and len(ids) > 1 and rows:
                rows[-1] += ' ' + text
            else:
                rows.append(f'<{pid}>\n{text}')
        return TranslationResponse('\n\n'.join(rows))


def test_refinement_keeps_the_rest_when_one_paragraph_is_missing(tmp_path):
    lines = [f'{i}번째 문단입니다. 그는 창밖을 보았다.' for i in range(1, 9)]
    lines[4] = 'MERGEME 다섯째 문단입니다. 그녀가 웃었다.'
    src = tmp_path / 'book.ko.txt'; src.write_text('\n'.join(lines), encoding='utf-8')
    p = EditLocal()
    result = run_refinement(src, tmp_path / 'book.ko.refined.txt', p, 'gemma4-26b', 'local',
                            checkpoint_dir=tmp_path / 'cp')
    assert result['status'] == 'completed' and result['done'] == 8 and result['preserved'] == 0
    # one full request, then only the missing paragraph and the one before it, one each
    assert p.edit_sizes == [8, 1, 1]
    out = (tmp_path / 'book.ko.refined.txt').read_text(encoding='utf-8-sig')
    assert out.count('MERGEME') == 1 and out.count('다듬음') == 8
    assert 'analysis' in p.purposes and 'edit' in p.purposes


# ---------------------------------------------------------------- term-conflict window

def test_term_conflict_choice_is_applied_without_error(qt_app, sandbox, monkeypatch):
    from test_alpha17 import _window
    from PySide6.QtWidgets import QDialog
    import novel_translator.ui.term_conflict_dialog as tcd
    w = _window(monkeypatch)
    w._merge_terms({'林雨': '린위'}, replace=False)
    assert w._terms()['林雨'] == '린위'
    monkeypatch.setattr(tcd.TermConflictDialog, 'exec', lambda self: int(QDialog.DialogCode.Accepted))
    monkeypatch.setattr(tcd.TermConflictDialog, 'decisions', lambda self: {'林雨': '임우'})
    w._term_result = (True, {'林雨': '임우', '苏晴': '쑤칭'}); w._term_thread = None; w._term_replace = False
    w._extract_terms_finished()
    terms = w._terms()
    assert terms['林雨'] == '임우' and terms['苏晴'] == '쑤칭'


# ---------------------------------------------------------------- refinement total time

def test_refinement_reports_total_time_and_engines(tmp_path, monkeypatch):
    import novel_translator.refinement_runtime as rr
    clock = iter([100.0] + [100.0 + 7.5] * 200)
    monkeypatch.setattr(rr.time, 'monotonic', lambda: next(clock))
    src = tmp_path / 'book.ko.txt'; src.write_text('첫 문단입니다.\n둘째 문단입니다.\n셋째 문단입니다.', encoding='utf-8')
    result = run_refinement(src, tmp_path / 'book.ko.refined.txt', EditLocal(), 'gemma4-26b', 'local',
                            checkpoint_dir=tmp_path / 'cp')
    assert result['status'] == 'completed' and result['seconds'] == pytest.approx(7.5)
    report = Path(result['report']).read_text(encoding='utf-8-sig').splitlines()
    assert report[1].startswith('소요 시간 8초') and 'gemma4-26b' in report[1]   # 7.5초는 반올림해 8초


def test_refinement_time_adds_up_across_a_stop_and_resume(tmp_path, monkeypatch):
    import novel_translator.refinement_runtime as rr
    src = tmp_path / 'book.ko.txt'; src.write_text('\n'.join(f'{i}번째 문단입니다.' for i in range(40)), encoding='utf-8')
    t = {'now': 0.0}
    monkeypatch.setattr(rr.time, 'monotonic', lambda: t['now'])
    calls = {'n': 0}

    class Slow(EditLocal):
        def translate(self, req, key):
            t['now'] += 10.0
            calls['n'] += 1
            return super().translate(req, key)
    first = run_refinement(src, tmp_path / 'out.txt', Slow(), 'm', 'k', checkpoint_dir=tmp_path / 'cp',
                           max_chars=60, stop=lambda: calls['n'] >= 3)
    assert first['status'] == 'stopped' and first['seconds'] == pytest.approx(30.0)
    t['now'] = 1000.0                                     # a later session
    second = run_refinement(src, tmp_path / 'out.txt', Slow(), 'm', 'k', checkpoint_dir=tmp_path / 'cp', max_chars=60)
    assert second['status'] == 'completed' and second['seconds'] > first['seconds']


def test_duration_text():
    from novel_translator.refinement_runtime import format_duration
    assert format_duration(7.4) == '7초' and format_duration(245) == '4분 05초' and format_duration(3723) == '1시간 02분 03초'


def test_refinement_window_shows_total_time_and_is_a_little_taller(qt_app):
    from novel_translator.ui.refinement_run_dialog import RefinementRunDialog

    from PySide6.QtWidgets import QWidget
    host = QWidget(); host.secrets = {}; host.source_path = None; host.last_result_path = None
    host._selected_model_id = lambda: ''
    d = RefinementRunDialog(host)
    assert d.height() >= 335
    d._finished({'status': 'completed', 'seconds': 245, 'done': 3, 'total': 3, 'changed': 2, 'identical': 1,
                 'preserved': 0, 'output': 'o.txt', 'report': 'o.txt.review.txt'})
    assert '총 소요 시간 4분 05초' in d.status.text()
    d._finished({'status': 'stopped', 'seconds': 30})
    assert '지금까지 소요 시간 30초' in d.status.text()
