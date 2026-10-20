"""v2.4: name/speech analysis quality.

* Shared-glossary entries found in a book are only candidates: the model confirms which
  are names of THIS book (an earlier book's 慢慢→만만 must not force "slowly" into a name).
* The speech roster is the characters most present in this book's dialogue, not the
  first rows of the glossary table.
* A local model's cut-off or looping analysis answer is used for what it holds, and the
  speech answer is capped so a rambling model cannot run for minutes.
"""
from __future__ import annotations
import json

from PySide6.QtWidgets import QTableWidgetItem

from novel_translator import common_terms as ct
from novel_translator.providers.base import TranslationRequest, TranslationResponse
from novel_translator.providers.local_llm import LocalLLMProvider
from novel_translator.speech_levels import rank_names, build_prompts, parse_table, ANSWER_TOKENS
from novel_translator.term_extraction import parse_glossary, build_prompts as term_prompts
from novel_translator.ui import main_window as ui
from test_alpha17 import _window, _wait, qt_app, sandbox  # noqa: F401 (pytest fixtures)
from test_v23_speech_progress import _setup, TABLE

BOOK = '\n\n'.join(
    [f'秦岚慢慢走过来。“小葵，你来了。”秦岚说。“秦岚姐，我在等你。”小葵慢慢笑了。{i}' for i in range(30)])


def test_shared_entries_are_candidates_and_only_confirmed_ones_are_added(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    ct.add('zh', {'慢慢': '만만', '秦岚': '진란', '杨过': '양과'}, ui.DATA_DIR)
    prompts = []

    class P:
        def translate(self, req, secret):
            prompts.append(req.user_prompt)
            # The model confirms 秦岚 (with another spelling) and the new 小葵; not the word 慢慢.
            return TranslationResponse(json.dumps({'秦岚': '친란', '小葵': '샤오쿠이'}, ensure_ascii=False), 'STOP', {})
    monkeypatch.setattr(ui, '_make_provider', lambda pid, *a, **k: P())
    monkeypatch.setattr(w, '_extraction_engines', lambda purpose='speech': [('local', 'local', 'm', 'http://x', {})])
    p = root / 'book.txt'; p.write_text(BOOK, encoding='utf-8')
    try:
        w._open_txt_path(str(p)); w._apply_profile('zh-ko')
        assert w._terms() == {}
        w._extract_terms()
        assert _wait(qt_app, lambda: w._term_thread is None and w._terms(), 10)
        assert w._terms() == {'秦岚': '진란', '小葵': '샤오쿠이'}   # shared rendering kept; 慢慢 not added
        assert 'SEEN AS NAMES/TERMS' in prompts[0] and '慢慢 => 만만' in prompts[0] and '杨过' not in prompts[0]
        log = w.error_view.toPlainText()
        assert '공용 용어집 항목 2개 중 이 책의 이름·용어로 확인된 1개 가져옴' in log
    finally:
        w.close()


def test_speech_names_never_include_unconfirmed_shared_entries(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    ct.add('zh', {'慢慢': '만만', '小葵': '샤오쿠이'}, ui.DATA_DIR)
    p = root / 'book.txt'; p.write_text(BOOK, encoding='utf-8')
    try:
        w._open_txt_path(str(p)); w._apply_profile('zh-ko')
        r = w.term_table.rowCount(); w.term_table.insertRow(r)
        w.term_table.setItem(r, 0, QTableWidgetItem('秦岚')); w.term_table.setItem(r, 1, QTableWidgetItem('진란'))
        assert w._speech_names() == {'秦岚': '진란'}
    finally:
        w.close()


def test_roster_is_the_dialogue_characters_not_the_first_table_rows():
    text = '\n'.join(['远方的山城很大。山城有河。'] * 50 +
                     [f'林雨看着苏晴。\n“苏晴，走吧。”\n“林雨，等等。”{i}' for i in range(10)])
    names = {f'地名{i:02d}': f'지명{i}' for i in range(65)}   # 65 table rows first (places, sects …)
    names.update({'山城': '산성', '林雨': '린위', '苏晴': '쑤칭'})
    text += '\n' + ''.join(f'地名{i:02d}' for i in range(65))
    roster = rank_names(text, names)
    assert list(roster)[:2] == ['林雨', '苏晴'] or list(roster)[:2] == ['苏晴', '林雨']
    assert len(roster) == 60 and '山城' in roster
    _, user = build_prompts('“苏晴，走吧。”', roster, 'zh')
    assert '林雨 = 린위' in user and '苏晴 = 쑤칭' in user


def test_speech_request_is_capped_and_marked_partial_ok(qt_app, sandbox, monkeypatch):
    got = []
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, lambda pid, req: (got.append(req), TABLE)[1])
    try:
        w._generate_speech()
        assert _wait(qt_app, lambda: w._term_thread is None and w._speech_rows(), 10)
        req = got[0]
        assert req.max_output == ANSWER_TOKENS and req.partial_ok and req.purpose == 'analysis'
    finally:
        w.close()


class _Stream:
    def __init__(self, pieces, done=None):
        self.pieces = pieces; self.done = done; self.status_code = 200; self.closed = False
    def iter_lines(self):
        for p in self.pieces:
            yield json.dumps({'message': {'content': p}}).encode()
        if self.done:
            yield json.dumps(dict(self.done, done=True)).encode()
    def close(self): self.closed = True


def _local_with(monkeypatch, stream, sent):
    p = LocalLLMProvider('http://localhost:11434', 300)
    def post(url, request, key, payload, stream_=False, **k):
        sent.append(payload); return stream
    monkeypatch.setattr(p, '_post', lambda url, request, key, payload, stream=False: post(url, request, key, payload))
    return p


def test_cut_off_analysis_answer_keeps_its_complete_rows(monkeypatch):
    rows = '린위\t쑤칭\t반말\t친구\n쑤칭\t린위\t해요체\t후배\n린위\t쑤'
    sent = []
    p = _local_with(monkeypatch, _Stream([rows], {'done_reason': 'length'}), sent)
    req = TranslationRequest('m', 'sys', 'user ' * 50, purpose='analysis', max_output=ANSWER_TOKENS, partial_ok=True)
    resp = p.translate(req, 'local')
    assert resp.finish_reason == 'length'
    assert [(r['speaker'], r['level']) for r in parse_table(resp.text, {'林雨': '린위', '苏晴': '쑤칭'})] == [('린위', '반말'), ('쑤칭', '해요체')]
    opts = sent[0]['options']
    assert opts['num_predict'] <= ANSWER_TOKENS and opts['num_ctx'] >= ANSWER_TOKENS


def test_looping_analysis_answer_is_cut_at_the_loop_and_used(monkeypatch):
    head = '린위\t쑤칭\t반말\t친구\n'
    sent = []
    p = _local_with(monkeypatch, _Stream([head] + ['야야야야'] * 400), sent)
    req = TranslationRequest('m', 'sys', 'user', purpose='analysis', max_output=ANSWER_TOKENS, partial_ok=True)
    resp = p.translate(req, 'local')
    assert parse_table(resp.text, {'林雨': '린위', '苏晴': '쑤칭'})[0]['level'] == '반말'


def test_translation_requests_still_fail_on_a_cut_off_answer(monkeypatch):
    import pytest
    from novel_translator.providers.base import ProviderError
    sent = []
    p = _local_with(monkeypatch, _Stream(['일부'], {'done_reason': 'length'}), sent)
    with pytest.raises(ProviderError):
        p.translate(TranslationRequest('m', 'sys', 'user'), 'local')
    assert 'num_predict' in sent[0]['options'] and sent[0]['options']['num_predict'] > 0


def test_cut_off_glossary_json_keeps_complete_pairs():
    cands = [('林雨', 9), ('苏晴', 8), ('白颖', 7)]
    assert parse_glossary('{"林雨": "린위", "苏晴": "쑤칭", "白颖": "백', cands, strict=True) == {'林雨': '린위', '苏晴': '쑤칭'}
    assert parse_glossary('{"林雨": "린위"}', cands, strict=True) == {'林雨': '린위'}


def test_term_prompt_without_shared_entries_is_unchanged():
    a = term_prompts([('林雨', 9)], 'zh', '', {})
    b = term_prompts([('林雨', 9)], 'zh', '', {}, earlier={})
    assert a == b and 'SEEN AS NAMES' not in a[1]
