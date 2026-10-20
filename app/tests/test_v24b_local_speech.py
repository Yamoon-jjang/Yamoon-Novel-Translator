"""v2.4 (rebuild): speech tables with local LLMs.

* A local model's answer is read whatever shape it takes (it often rewrites the requested
  tab-separated lines); the content checks (known people, one of six levels) stay strict.
* A local engine is asked for Ollama JSON; cloud engines keep the original request.
* An unusable answer is shown in the log (start) and kept in logs/analysis_answers.txt.
* First-person books: the narrator (나) and family roles (엄마 …) are speech-table roles.
"""
from __future__ import annotations
import json

import pytest

from novel_translator.providers.base import TranslationResponse
from novel_translator.speech_levels import (parse_table, role_entries, role_parse_names, build_prompts,
                                            render_instruction, NARRATOR)
from novel_translator.ui import main_window as ui
from test_alpha17 import _window, _wait, qt_app, sandbox  # noqa: F401 (pytest fixtures)

NAMES = {'妈咪': '마미', '小妹': '소매', '单单姐': '단단저'}


@pytest.mark.parametrize('answer', [
    '마미\t소매\t반말\t모녀',
    '마미<TAB>소매<TAB>반말<TAB>모녀',
    '마미 소매 반말 모녀',
    '마미 → 소매: 반말 (모녀)',
    '| 마미 | 소매 | 반말 | 모녀 |',
    '마미, 소매, 반말, 모녀',
    '1. 마미 - 소매 - 반말 - 모녀',
    '마미(妈咪)\t소매(小妹)\t반말\t모녀',
    '妈咪→小妹：반말',
    '마미가 소매에게 반말을 쓴다 (모녀)',
    '{"pairs": [{"speaker": "마미", "listener": "소매", "level": "반말", "relation": "모녀"}]}',
    '{"pairs": [{"speaker": "마미", "listener": "소매", "level": "반말"}, {"speaker": "소매", "listen',
])
def test_local_answer_shapes_are_read(answer):
    rows = parse_table(answer, NAMES)
    assert rows and (rows[0]['speaker'], rows[0]['listener'], rows[0]['level']) == ('마미', '소매', '반말')


def test_other_wordings_of_the_level():
    assert parse_table('마미\t소매\t존대\t모녀', NAMES)[0]['level'] == '해요체'
    assert parse_table('마미\t소매\t하대', NAMES)[0]['level'] == '반말'
    # the FIRST level named is the level, not one mentioned in the note
    assert parse_table('마미 → 소매: 반말 (단단저와 달리 해요체 아님)', NAMES)[0]['level'] == '반말'


@pytest.mark.parametrize('answer', [
    '화자\t상대\t말투\t관계', '|---|---|---|---|', '마미\t마미\t반말', '마미\t모르는사람\t반말', '마미\t소매\t친구',
    '대사를 보면 마미와 소매는 모녀 사이로 보이며 서로 반말과 존댓말을 섞어 쓰는 것 같지만 확실하지 않습니다 그래서 말투를 정하기 어렵습니다',
])
def test_content_checks_stay_strict(answer):
    assert parse_table(answer, NAMES) == []


MEMOIR = '\n'.join(['我走进厨房，我看见妈妈在做饭。我心里想着很多事情。'] * 30 +
                   ['“妈妈，我回来了。”', '“儿子，快去洗手。”妈妈笑着说。'] * 5)
THIRD = '\n'.join(['他走进厨房，看见母亲在做饭。他心里想着很多事情。'] * 30 + ['“妈妈，我回来了。”'] * 5)


def test_first_person_book_gets_narrator_and_family_roles():
    roles = role_entries(MEMOIR, 'zh')
    assert NARRATOR in roles.values() and roles.get('妈妈') == '엄마' and roles.get('儿子') == '아들'
    assert NARRATOR not in role_entries(THIRD, 'zh').values()
    names = role_parse_names(roles)
    assert names['我'] == '나' and names['妈妈'] == '엄마' and names['엄마'] == '엄마'
    rows = parse_table('나\t엄마\t해요체\t모자\n妈妈<TAB>我<TAB>반말', names)
    assert [(r['speaker'], r['listener'], r['level']) for r in rows] == [('나', '엄마', '해요체'), ('엄마', '나', '반말')]
    assert '1인칭' not in render_instruction(rows) and 'first-person narrator' in render_instruction(rows)
    _, user = build_prompts('“妈妈，我回来了。”', {**roles, '秦岚': '진란'}, 'zh')
    assert '[ROLES IN THE LIST]' in user and '我 (1인칭 화자) = 나' in user


def test_a_glossary_name_is_not_turned_into_a_role():
    roles = role_entries(MEMOIR.replace('妈妈', '妈咪'), 'zh', taken={'妈咪'})
    assert '엄마' not in roles.values()


def _memoir_window(qt_app, sandbox, monkeypatch, reply):
    root, cfg = sandbox
    w = _window(monkeypatch)
    monkeypatch.setattr(ui.QMessageBox, 'warning', lambda *a, **k: None)
    calls = []

    class P:
        def __init__(self, pid): self.pid = pid
        def translate(self, req, secret):
            calls.append((self.pid, req))
            return TranslationResponse(reply(self.pid, req), 'STOP', {})
    monkeypatch.setattr(ui, '_make_provider', lambda pid, *a, **k: P(pid))
    monkeypatch.setattr(w, '_extraction_engines', lambda purpose='speech': [('local', 'local', 'gemma', 'http://x', {}),
                                                                             ('openai', 'k', 'gpt', '', None)])
    p = root / 'memoir.txt'; p.write_text(MEMOIR, encoding='utf-8')
    w._open_txt_path(str(p)); w._apply_profile('zh-ko')
    return w, calls


def test_memoir_with_no_named_characters_gets_a_speech_table_from_a_local_model(qt_app, sandbox, monkeypatch):
    answer = json.dumps({'pairs': [{'speaker': '나', 'listener': '엄마', 'level': '해요체', 'relation': '모자'},
                                   {'speaker': '엄마', 'listener': '나', 'level': '반말', 'relation': '모자'}]}, ensure_ascii=False)
    w, calls = _memoir_window(qt_app, sandbox, monkeypatch, lambda pid, req: answer)
    try:
        assert w._terms() == {} and w._speech_people() >= 2
        w._generate_speech()
        assert _wait(qt_app, lambda: w._term_thread is None, 10)
        assert [(r['speaker'], r['listener'], r['level']) for r in w._speech_rows()] == [('나', '엄마', '해요체'), ('엄마', '나', '반말')]
        pid, req = calls[0]
        assert pid == 'local' and req.purpose == 'json' and '"pairs"' in req.user_prompt   # Ollama JSON mode
        assert len(calls) == 1
        assert '화자·가족 호칭' in w.error_view.toPlainText()
    finally:
        w.close()


def test_cloud_engine_keeps_the_original_request_and_bad_answers_are_logged(qt_app, sandbox, monkeypatch):
    def reply(pid, req):
        return '모르겠습니다. 관계가 분명하지 않아요.' if pid == 'local' else '나\t엄마\t해요체\t모자'
    w, calls = _memoir_window(qt_app, sandbox, monkeypatch, reply)
    try:
        w._generate_speech()
        assert _wait(qt_app, lambda: w._term_thread is None, 10)
        assert [c[0] for c in calls] == ['local', 'openai']
        cloud = calls[1][1]
        assert cloud.purpose == 'analysis' and '화자<TAB>상대<TAB>말투' in cloud.user_prompt and '"pairs"' not in cloud.user_prompt
        log = w.error_view.toPlainText()
        assert '답 앞부분: "모르겠습니다. 관계가 분명하지 않아요."' in log
        saved = (ui.DATA_DIR if False else __import__('novel_translator.config', fromlist=['LOG_DIR']).LOG_DIR) / 'analysis_answers.txt'
        assert '모르겠습니다. 관계가 분명하지 않아요.' in saved.read_text(encoding='utf-8')
        assert w._speech_rows()[0]['speaker'] == '나'
    finally:
        w.close()


# ---------------------------------------------------------------- glossary answers
from novel_translator.term_extraction import parse_glossary  # noqa: E402

CANDS = [('秦岚', 20), ('单单姐', 15), ('小葵', 12), ('台湾', 8)]


@pytest.mark.parametrize('answer,expected', [
    ('{"秦岚":"진란","小葵":"샤오쿠이"}', {'秦岚': '진란', '小葵': '샤오쿠이'}),
    ('{"秦岚":"진란(秦岚)","小葵":"샤오쿠이"}', {'秦岚': '진란', '小葵': '샤오쿠이'}),
    ('[{"source":"秦岚","korean":"진란"},{"source":"小葵","korean":"샤오쿠이"}]', {'秦岚': '진란', '小葵': '샤오쿠이'}),
    ('{"terms":[{"source":"秦岚","korean":"진란","type":"person"}]}', {'秦岚': '진란'}),
    ('{"秦岚":{"korean":"진란","type":"name"}}', {'秦岚': '진란'}),
    ('{"인물":{"秦岚":"진란","小葵":"샤오쿠이"},"지명":{"台湾":"대만"}}', {'秦岚': '진란', '小葵': '샤오쿠이', '台湾': '대만'}),
    ('{"秦岚":"Qin Lan (진란)"}', {'秦岚': '진란'}),
    ('{"秦岚":"진란","小葵":"샤오쿠', {'秦岚': '진란'}),
])
def test_glossary_answer_shapes_are_read(answer, expected):
    assert parse_glossary(answer, CANDS, strict=True) == expected


def test_glossary_checks_stay_strict_and_are_reported():
    rep = {}
    got = parse_glossary('{"秦岚":"진란","公司":"회사","王八":"王팔","不存在":"부존재","台湾":"台灣"}',
                         CANDS + [('公司', 30), ('王八', 9)], strict=True, report=rep)
    assert got == {'秦岚': '진란'}
    assert rep == {'answer': 5, 'kept': 1, 'not_candidate': 1, 'bad_rendering': 2, 'common_word': 1, 'phrase': 0, 'address': 0}


def test_term_run_logs_where_the_entries_went(qt_app, sandbox, monkeypatch):
    answer = json.dumps([{'source': '妈妈', 'korean': '엄마'}, {'source': '单单姐', 'korean': '단단 언니(单单姐)'},
                         {'source': '없는말', 'korean': '없음'}], ensure_ascii=False)
    w, calls = _memoir_window(qt_app, sandbox, monkeypatch, lambda pid, req: answer)
    monkeypatch.setattr(ui, 'term_candidates', lambda text, lang: [('单单姐', 9), ('厨房', 7)])
    try:
        w._extract_terms()
        assert _wait(qt_app, lambda: w._term_thread is None, 10)
        # 单单姐 = "단단 언니" depends on who speaks (누나 from a son): only the name is fixed.
        assert w._terms() == {'单单': '단단'}
        log = w.error_view.toPlainText()
        assert '답에 든 항목 3개 중 1개 사용' in log and '후보 목록에 없는 원문 2개' in log
        assert '호칭(姐·哥 등)을 뗀 이름만 확정 1개' in log
    finally:
        w.close()
