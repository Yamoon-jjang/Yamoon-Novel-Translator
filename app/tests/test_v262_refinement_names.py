"""v2.6.2 보완 3 (same-version redeploy): a Korean TXT with no translation record gets its
reference table the way the translation makes its glossary and 인물 말투표.

- Names are counted over the WHOLE book (Hangul stems before a particle, an honorific or a
  speech verb), then one request picks the names; address words never become names.
- The speech table uses the translation's own prompt (JSON for a local LLM), validation and
  split-batch fallback; pairs the batches disagree on are dropped.
- The run log says what was found, and the window shows the table while the run goes on,
  as rows and columns like the translation window's 인물 말투 and 용어.
- A free-text analysis kept by an older stopped run is made again; edited paragraphs stay.
- A translation record is still used as before (Korean names and the Korean speech table),
  and the translation's own speech prompts are unchanged.
"""
from __future__ import annotations

import json
import re

import pytest
from PySide6.QtWidgets import QApplication, QTabWidget, QTableWidget

from novel_translator.refinement_analysis import (name_candidates, parse_names, build_reference,
                                                    render_table, NAME_MIN_COUNT)
from novel_translator.refinement_links import guidance_tables, guidance_counts, record_translation
from novel_translator.refinement_runtime import (_ask, run_refinement, peek_guidance, content_checkpoint_path,
                                                   old_style_analysis)
from novel_translator.providers.base import TranslationResponse, ProviderError, ErrorKind
from novel_translator.config import load_json, save_json
from test_refinement_reference_and_safety import Editor, write


def novel(repeat=5):
    """A small Korean novel: four people, their dialogue, and plenty of ordinary words."""
    lines = []
    for i in range(repeat):
        lines += [
            '시즈코는 창밖을 내다보며 한숨을 쉬었다.',
            '다미코가 시즈코에게 말했다.', '“싫어, 저리 가.”',
            '시즈코가 다미코를 보며 입을 열었다.', '“다미코, 이리 와요.”',
            '다시로가 시즈코에게 말했다.', '“부인, 이쪽으로 오십시오.”',
            '시즈코는 다시로를 바라보았다.', '“다시로 씨, 그만두세요.”',
            '모리타가 다시로에게 말했다.', '“형님, 준비 다 됐습니다.”',
            '그녀의 얼굴이 붉게 달아올랐다. 엄마는 아무 말도 하지 않았다.',
        ]
    return '\n\n'.join(lines)


PEOPLE = ('시즈코', '다미코', '다시로', '모리타')
PAIRS = [('시즈코', '다미코', '해요체'), ('다미코', '시즈코', '반말'), ('다시로', '시즈코', '합쇼체'),
         ('시즈코', '다시로', '해요체'), ('모리타', '다시로', '합쇼체')]


class Model:
    """Answers the two analysis questions like a model would; `name` picks local JSON or text."""

    def __init__(self, name='local', pairs=PAIRS, names_reply=None):
        self.name = name; self.asked = []; self.pairs = pairs; self.names_reply = names_reply

    def translate(self, req, key):
        self.asked.append(req)
        if 'WORDS (word' in req.user_prompt:
            if self.names_reply is not None:
                return TranslationResponse(self.names_reply)
            words = re.findall(r'(?m)^([가-힣]+)\t', req.user_prompt)
            names = [{'name': w, 'kind': '인물'} for w in words if w in PEOPLE] + [{'name': '엄마', 'kind': '인물'}]
            return TranslationResponse(json.dumps({'names': names}, ensure_ascii=False))
        ex = req.user_prompt.split('[DIALOGUE EXCERPTS')[1]
        rows = [(a, b, lv) for a, b, lv in self.pairs if a in ex and b in ex]
        if req.purpose == 'json':
            return TranslationResponse(json.dumps({'pairs': [{'speaker': a, 'listener': b, 'level': lv, 'relation': ''}
                                                             for a, b, lv in rows]}, ensure_ascii=False))
        return TranslationResponse('\n'.join(f'{a}\t{b}\t{lv}\t' for a, b, lv in rows))


def build(text, model, stop=lambda: False):
    events = []
    table = build_reference(text, model, 'm', 'k', ask=_ask, stop=stop, event=events.append)
    return table, events


# ---------------------------------------------------------------- counting names
def test_names_are_counted_over_the_whole_book_without_particles():
    cands = dict(name_candidates(novel()))
    for name in PEOPLE:
        assert name in cands                        # 시즈코는 · 시즈코에게 · 시즈코가 → 시즈코
    assert not any(k.endswith(('는', '에게', '가', '를')) and k[:-1] in cands for k in cands)
    assert '엄마' not in cands and '그녀' not in cands and '얼굴' not in cands   # address words, pronouns
    assert '이쪽으' not in cands and '쪽으' not in cands                          # a cut-off 으로


def test_rare_words_are_not_candidates():
    text = novel(1) + '\n\n가와다가 웃었다.'
    assert '가와다' not in dict(name_candidates(text))           # fewer than NAME_MIN_COUNT uses
    assert NAME_MIN_COUNT >= 3


def test_names_answer_is_checked_strictly():
    text = novel()
    reply = json.dumps({'names': [{'name': '시즈코', 'kind': '인물'}, {'name': '엄마', 'kind': '인물'},
                                  {'name': 'Shizuko', 'kind': 'person'}, {'name': '없는이름', 'kind': '인물'},
                                  {'name': '다시로', 'kind': 'person'}, {'name': '창밖', 'kind': 'place'}]},
                       ensure_ascii=False)
    assert parse_names(reply, text) == {'시즈코': '인물', '다시로': '인물', '창밖': '장소'}
    assert parse_names('["시즈코", "모리타"]', text) == {'시즈코': '인물', '모리타': '인물'}
    assert parse_names('<think>x</think>{"시즈코": "인물"}', text) == {'시즈코': '인물'}
    assert parse_names('모르겠습니다', text) == {}


# ---------------------------------------------------------------- the table, as in translation
def test_local_llm_builds_the_table_with_json_like_the_translation():
    m = Model('local')
    table, events = build(novel(), m)
    assert [r.purpose for r in m.asked] == ['json', 'json']
    assert '"pairs"' in m.asked[1].user_prompt and 'Book language: Korean' in m.asked[1].user_prompt
    assert 'sentence endings' in m.asked[1].user_prompt                 # Korean: read the level, not choose it
    assert table.startswith('[샘플 분석 기준표')
    speech, terms = guidance_tables(table)
    assert ('시즈코', '다미코', '해요체', '') in speech and ('다미코', '시즈코', '반말', '') in speech
    assert ('엄마', '인물') not in terms and ('시즈코', '인물') in terms
    assert any('이름 후보' in e for e in events)
    assert any(e.startswith('인물·용어 확인 · 인물 4명') for e in events)
    assert any(e.startswith('인물 말투표 · 5쌍 · 전체 문맥 1회 분석') for e in events)


def test_api_engine_gets_the_text_table_prompt():
    m = Model('gemini')
    table, _ = build(novel(), m)
    assert [r.purpose for r in m.asked] == ['analysis', 'analysis']
    assert '화자<TAB>상대<TAB>말투' in m.asked[1].user_prompt
    assert '- 다시로 → 시즈코: 합쇼체' in table


def test_failed_whole_sample_is_split_and_conflicts_are_dropped():
    class Split(Model):
        def translate(self, req, key):
            self.asked.append(req)
            if 'WORDS (word' in req.user_prompt:
                return Model.translate(self, req, key)
            n = sum(1 for r in self.asked if 'DIALOGUE EXCERPTS' in r.user_prompt)
            if n == 1:
                return TranslationResponse('{"pairs": []}')       # the whole sample: nothing usable
            level = '반말' if n == 2 else '해요체'                  # the batches disagree on one pair
            return TranslationResponse(json.dumps({'pairs': [
                {'speaker': '시즈코', 'listener': '다미코', 'level': level},
                {'speaker': '다시로', 'listener': '시즈코', 'level': '합쇼체'}]}, ensure_ascii=False))
    m = Split('local')
    table, events = build(novel(8), m)
    assert sum(1 for r in m.asked if 'DIALOGUE EXCERPTS' in r.user_prompt) >= 3
    speech, _ = guidance_tables(table)
    assert speech == [('다시로', '시즈코', '합쇼체', '')]
    assert any('분할 분석' in e and '말투 충돌 1쌍 제외' in e for e in events)


def test_a_failing_engine_never_stops_the_editing():
    class Broken(Model):
        def translate(self, req, key):
            raise ProviderError(ErrorKind.TRANSIENT, '연결 끊김')
    table, events = build(novel(), Broken())
    assert table == '' and any('인물·용어 확인 실패' in e for e in events)

    class Garbage(Model):
        def translate(self, req, key):
            raise KeyError('unexpected')
    assert build(novel(), Garbage())[0] == ''


def test_a_key_problem_is_still_reported():
    class NoKey(Model):
        def translate(self, req, key):
            raise ProviderError(ErrorKind.AUTH, '키 오류')
    with pytest.raises(ProviderError):
        build(novel(), NoKey())


def test_names_without_dialogue_still_make_a_names_table():
    text = '\n\n'.join(['시즈코는 창밖을 보았다.', '다시로가 웃었다.'] * 6)
    table, events = build(text, Model('local'))
    speech, terms = guidance_tables(table)
    assert speech == [] and ('시즈코', '인물') in terms and ('다시로', '인물') in terms
    assert any('대사를 찾지 못해' in e for e in events)


def test_a_book_with_no_names_has_no_table_and_no_request():
    m = Model('local')
    table, events = build('그는 웃었다.\n\n밤이 깊었다.', m)
    assert table == '' and m.asked == [] and any('이름 후보를 찾지 못해' in e for e in events)


# ---------------------------------------------------------------- the run
def test_run_logs_the_table_and_hands_it_to_the_window(tmp_path):
    src = write(tmp_path, 'book.txt', novel())
    stats, events = [], []
    analysis = Model('local')
    res = run_refinement(src, tmp_path / 'r.txt', Editor(), 'm', 'k', checkpoint_dir=tmp_path / 'cp',
                         analysis_engine=(analysis, 'a', 'k'), links_root=tmp_path, event=events.append,
                         on_stats=stats.append)
    assert res['guidance_source'] == 'analysis'
    done = [e for e in events if e.startswith('기준표 준비 완료')]
    assert done and '인물 말투 5쌍' in done[0] and '[기준표 보기]' in done[0]
    first = next(s for s in stats if 'guidance' in s)
    assert first['guidance_source'] == 'analysis' and '- 시즈코 → 다미코: 해요체' in first['guidance']
    assert first['group'] == 0                          # before the first editing request
    report = (tmp_path / 'r.txt.review.txt').read_text(encoding='utf-8-sig')
    assert '--- 윤문에 쓴 기준표 ---' in report and '시즈코 → 다미코: 해요체' in report


def test_a_translation_record_is_used_and_nothing_is_asked(tmp_path):
    src = write(tmp_path, 'book.ko.txt', novel())
    record_translation(src, src.read_text(encoding='utf-8'),
                       [{'speaker': '시즈코', 'listener': '다미코', 'level': '해요체'}], {'静子': '시즈코'}, root=tmp_path)
    analysis, ed = Model('local'), Editor()
    res = run_refinement(src, tmp_path / 'r.txt', ed, 'm', 'k', checkpoint_dir=tmp_path / 'cp',
                         analysis_engine=(analysis, 'a', 'k'), links_root=tmp_path)
    assert res['guidance_source'] == 'translation' and analysis.asked == []
    assert '시즈코 → 다미코: 해요체' in ed.last_user and '静子' not in ed.last_user   # Korean names only


def test_an_old_free_text_analysis_is_made_again_and_edits_are_kept(tmp_path):
    src = write(tmp_path, 'book.txt', novel())
    ed = Editor()
    stopped = run_refinement(src, tmp_path / 'r.txt', ed, 'm', 'k', checkpoint_dir=tmp_path / 'cp',
                             analysis_engine=(Model('local'), 'a', 'k'), links_root=tmp_path, max_items=6,
                             stop=lambda: ed.calls.count('edit') >= 1)
    assert stopped['status'] == 'stopped' and stopped['done'] == 6
    cp = next((tmp_path / 'cp').glob('*.json'))
    state = load_json(cp)
    state['guidance'] = '| 이름 | 관계 |\n|---|---|\n| 시즈코 | 부인 |\n시즈코 → 다미코: 존댓말'   # an older version's answer
    save_json(cp, state)
    assert old_style_analysis(state)
    assert peek_guidance(src, checkpoint_dir=tmp_path / 'cp', links_root=tmp_path)[0] == 'none'
    analysis, events, ed2 = Model('local'), [], Editor()
    res = run_refinement(src, tmp_path / 'r.txt', ed2, 'm', 'k', checkpoint_dir=tmp_path / 'cp',
                         analysis_engine=(analysis, 'a', 'k'), links_root=tmp_path, max_items=6, event=events.append)
    assert any('예전 방식이라' in e for e in events) and len(analysis.asked) == 2
    assert res['status'] == 'completed' and any('건너뛰고 이어서' in e for e in events)
    assert '[샘플 분석 기준표' in ed2.last_user
    # a new-style table is kept on the next resume (no new analysis)
    assert not old_style_analysis({'guidance_source': 'analysis', 'guidance': render_table([], {'시즈코': '인물'})})


# ---------------------------------------------------------------- the window
def test_reference_table_rows_for_both_kinds_of_table():
    record = ('[번역할 때 확정된 기준표 · 이 작품 번역에 실제로 쓰인 값]\n인물 말투(화자 → 상대: 말투):\n'
              '- 서술자 → 왕선: 반말 (친구)\n확정 이름·용어(표기를 바꾸지 말 것): 왕선, 청운문')
    assert guidance_tables(record) == ([('서술자', '왕선', '반말', '친구')], [('왕선', '확정 표기'), ('청운문', '확정 표기')])
    table = render_table([{'speaker': '시즈코', 'listener': '다미코', 'level': '해요체', 'note': '계모'}],
                         {'시즈코': '인물', '도쿄': '장소'})
    assert guidance_tables(table) == ([('시즈코', '다미코', '해요체', '계모')], [('시즈코', '인물'), ('도쿄', '장소')])
    assert guidance_counts(table) == '인물 말투 1쌍 · 이름·용어 2개'
    old = '| 화자 | 상대 | 말투 |\n|---|---|---|\n| 시즈코 | 다미코 | 존댓말 |\n**인물**\n- 시즈코: 부인'
    assert guidance_tables(old) == ([('시즈코', '다미코', '존댓말', '')], [('시즈코', '부인')])


def test_window_shows_the_table_during_the_run_as_rows(monkeypatch):
    from test_v20_refinement_api_integration import Host
    from novel_translator.ui import refinement_run_dialog as dlg
    app = QApplication.instance() or QApplication([])
    d = dlg.RefinementRunDialog(Host())
    try:
        assert not d.guidance_view.isEnabled()
        table = render_table([{'speaker': '시즈코', 'listener': '다미코', 'level': '해요체', 'note': ''}],
                             {'시즈코': '인물', '다미코': '인물'})
        d._stats_changed({'groups': 9, 'group': 0, 'guidance': table, 'guidance_source': 'analysis'})
        assert d.guidance_view.isEnabled() and d._guidance_text == table
        assert '샘플 분석 · 인물 말투 1쌍 · 이름·용어 2개' in d.guidance_info.text()
        assert 'guidance' not in d._stats                      # the counts line never sees the table
        shown = []
        monkeypatch.setattr(dlg.QDialog, 'exec', lambda self: shown.append(self) or 0)
        d._show_guidance()
        tabs = shown[0].findChild(QTabWidget)
        assert [tabs.tabText(i) for i in range(tabs.count())] == ['인물 말투 (1)', '이름·용어 (2)', '전체 내용']
        speech = tabs.widget(0)
        assert isinstance(speech, QTableWidget) and speech.item(0, 0).text() == '시즈코' and speech.item(0, 2).text() == '해요체'
        d._stats_changed({'groups': 9, 'group': 0, 'guidance': '', 'guidance_source': 'none'})
        assert not d.guidance_view.isEnabled() and '기준표 없이' in d.guidance_info.text()
    finally:
        d.close()
