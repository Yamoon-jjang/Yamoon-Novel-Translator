"""v2.5.5: what used to be shown light red is left out, and polite speech stays polite.

- English family/address words and endearments (Sister, Bro, Dear, My Dear, Big Brother)
  are no longer added by the extraction (they were kept and shown light red). Names that
  only start with such a word (Brother Tuck, Sister Mary, Mr Son) are kept.
- A half-converted rendering (엠ily) the repair request could not fix is left out.
  Latin letters a Chinese/Japanese source itself has (阿Q -> 아Q) are not half converted.
- Each extraction removes light-red rows an earlier run put in; rows the user typed stay.
- Speech levels: a Korean level name decides first; English descriptions are read as
  meant ("informal polite" = 해요체, which was read as 반말); keys such as speechLevel and
  level words in Chinese/Japanese are understood.
"""
from __future__ import annotations
import json

from PySide6.QtWidgets import QTableWidgetItem

from novel_translator.term_extraction import parse_glossary, is_doubtful_term, valid_rendering
from novel_translator.speech_levels import parse_table, normalize_level, LEVELS, _ALIASES
from novel_translator.ui import main_window as ui
from novel_translator.providers.base import TranslationResponse
from test_alpha17 import _wait, qt_app, sandbox  # noqa: F401 (pytest fixtures)
import test_v252_name_collision as v252

ADDRESS = ['Sister', 'Bro', 'Dear', 'Brother', 'Sis', 'Honey', 'Aunt', 'Uncle', 'Mister', 'Miss',
           'My Dear', 'Big Brother', 'Little Sis', 'Young Master', 'Old Man', 'Dear Sister', 'Second Brother']
NAMES = ['Brother Tuck', 'Sister Mary', 'Mr Son', 'Mr. Son', 'Lord Godric', 'Father Brown', 'Sonny', 'Grace',
         'Will', 'Rose', 'Dearborn', 'Missy Elliott', 'Mason', 'Auntie Em']


def test_address_words_are_doubtful_and_names_are_not():
    for w in ADDRESS:
        assert is_doubtful_term(w), w
    for w in NAMES:
        if w == 'Auntie Em':
            continue          # "Auntie" + a name: kept, see below
        assert not is_doubtful_term(w), w
    assert not is_doubtful_term('Auntie Em')
    for w in ('姐姐', '林若雪', 'お兄ちゃん'):
        assert not is_doubtful_term(w)


def test_extraction_leaves_out_address_words_and_keeps_names():
    answer = {**{w: '언니' for w in ADDRESS}, **{w: f'이름{i}' for i, w in enumerate(NAMES)}}
    rep = {}
    got = parse_glossary(json.dumps(answer, ensure_ascii=False), [(k, 9) for k in answer], lang='en', report=rep)
    assert set(got) == set(NAMES)
    assert rep['common_word'] == len(ADDRESS)


def test_chinese_and_japanese_extraction_is_unchanged():
    answer = {'林若雪': '린뤄쉐', '陈屿': '천위', '公司': '회사', '佐藤': '사토'}
    for lang in ('zh', 'ja'):
        got = parse_glossary(json.dumps(answer, ensure_ascii=False), [(k, 9) for k in answer], lang=lang)
        assert got == {'林若雪': '린뤄쉐', '陈屿': '천위', '佐藤': '사토'}


def test_latin_letters_of_a_chinese_name_are_not_half_converted():
    assert valid_rendering('阿Q', '아Q') and valid_rendering('A君', 'A군')
    assert not valid_rendering('Emily', '엠ily') and not valid_rendering('林雨', '린yu')
    got = parse_glossary(json.dumps({'阿Q': '아Q', '林雨': '린yu'}, ensure_ascii=False), [('阿Q', 9), ('林雨', 9)], lang='zh')
    assert got == {'阿Q': '아Q'}


def test_half_converted_chinese_rendering_is_left_out(qt_app, monkeypatch):
    class P:
        def translate(self, req, secret):
            return TranslationResponse(json.dumps({'林雨': '린yu', '苏晴': '쑤칭'}, ensure_ascii=False), 'STOP', {})
    monkeypatch.setattr(ui, '_make_provider', lambda *a, **k: P())
    t = ui.TermExtractThread('local', 'k', 'm', '', 'S', 'U', [('林雨', 9), ('苏晴', 9)])
    t.lang = 'zh'
    out = []
    t.done.connect(lambda ok, res: out.append((ok, res)))
    t.run()
    assert out[-1] == (True, {'苏晴': '쑤칭'}) and t.report['half_dropped'] == 1


def _finish(qt_app, w, started):
    """Let the (fake) translation end inside this test, so its "번역 완료" message never
    lands in a later test."""
    for t in started:
        t.wait(5000)
    _wait(qt_app, lambda: '완료' in w.status.text() or not started, 5)
    for _ in range(20):
        qt_app.processEvents()


def _rows(w):
    return {w.term_table.item(r, 0).text(): w.term_table.item(r, 1).text() for r in range(w.term_table.rowCount())}


def _put(w, a, b, typed):
    w._term_internal_edit = not typed
    r = w.term_table.rowCount(); w.term_table.insertRow(r)
    w.term_table.setItem(r, 0, QTableWidgetItem(a)); w.term_table.setItem(r, 1, QTableWidgetItem(b))
    w._term_internal_edit = False


def test_restart_run_removes_earlier_red_rows_and_keeps_typed_ones(qt_app, sandbox, monkeypatch):
    names = {'Jonathan': '조너선', 'Margaret': '마거릿', 'Susan': '수잔', 'Suzanne': '수잔느'}
    w, calls, started = v252._setup(qt_app, sandbox, monkeypatch, names=names, auto_speech=False)
    try:
        _put(w, 'Sister', '언니', typed=False)          # put in by an earlier extraction
        _put(w, 'Mom', '엄마', typed=False)
        _put(w, 'Bro', '형', typed=True)                # typed by the user: kept (and shown red)
        w.start_translation()
        assert _wait(qt_app, lambda: bool(started) and not w._extracting(), 15)
        terms = started[0].terms
        assert 'Sister' not in terms and 'Mom' not in terms
        assert terms.get('Bro') == '형' and terms['Jonathan'] == '조너선'
        assert 'Sister' not in _rows(w) and _rows(w)['Bro'] == '형'
        log = w.error_view.toPlainText()
        assert '빨간) 항목 2개 자동 삭제' in log and 'Sister→언니' in log
        assert 'Bro' in {src for _, src, _ in w._suspicious_terms()}   # still light red
    finally:
        _finish(qt_app, w, started)
        w.close()


def test_manual_extraction_removes_earlier_red_rows_too(qt_app, sandbox, monkeypatch):
    names = {'Jonathan': '조너선', 'Margaret': '마거릿'}
    w, calls, started = v252._setup(qt_app, sandbox, monkeypatch, names=names, auto_terms=False, auto_speech=False)
    monkeypatch.setattr(ui.QMessageBox, 'information', lambda *a, **k: None)
    try:
        _put(w, 'Dear', '자기', typed=False)
        _put(w, 'Jonathan', '조너선', typed=False)
        w._extract_terms()
        assert _wait(qt_app, lambda: '인명·용어 추출 완료' in w.error_view.toPlainText(), 15)
        assert 'Dear' not in _rows(w) and _rows(w)['Jonathan'] == '조너선' and 'Margaret' in _rows(w)
        assert not started
    finally:
        w.close()


def test_no_red_rows_means_no_removal_note(qt_app, sandbox, monkeypatch):
    names = {'Jonathan': '조너선', 'Margaret': '마거릿'}
    w, calls, started = v252._setup(qt_app, sandbox, monkeypatch, names=names, auto_speech=False)
    try:
        w.start_translation()
        assert _wait(qt_app, lambda: bool(started) and not w._extracting(), 15)
        assert '자동 삭제' not in w.error_view.toPlainText()
    finally:
        _finish(qt_app, w, started)
        w.close()


# ---------------------------------------------------------------- speech levels
NAMES_KO = {'林雨': '린위', '苏晴': '쑤칭', '师父': '사부'}


def _one(level, key='level'):
    rows = parse_table(json.dumps({'pairs': [{'speaker': '린위', 'listener': '사부', key: level, 'relation': '제자'}]},
                                  ensure_ascii=False), NAMES_KO)
    return rows[0]['level'] if rows else ''


def test_polite_speech_described_in_english_stays_polite():
    assert _one('informal polite (해요체)') == '해요체'          # was 반말
    assert _one('Informal polite') == '해요체'
    assert _one('casual polite') == '해요체'
    assert _one('formal polite') == '합쇼체'
    assert _one('haeyo-che') == '해요체' and _one('hapsyo-che') == '합쇼체'
    assert _one('informal') == '반말' and _one('casual') == '반말' and _one('banmal') == '반말'
    assert _one('polite') == '해요체' and _one('formal') == '합쇼체' and _one('honorific') == '합쇼체'


def test_korean_level_names_decide_as_before():
    for lv in LEVELS:
        assert _one(lv) == lv
    for raw, ko in _ALIASES.items():
        assert normalize_level(raw) == ko, raw
    assert _one('존댓말(해요체)') == '해요체' and _one('해요체/반말') == '해요체' and _one('반말 (informal)') == '반말'
    assert _one('합쇼체 (formal)') == '합쇼체'


def test_other_key_names_and_level_words_are_understood():
    assert _one('합쇼체', key='speechLevel') == '합쇼체'
    assert _one('해요체', key='speech level') == '해요체'
    assert _one('敬语') == '해요체' and _one('非敬语') == '반말' and _one('タメ口') == '반말'
    rows = parse_table(json.dumps({'pairs': [{'说话者': '린위', '听者': '사부', '语气': '敬语'}]}, ensure_ascii=False), NAMES_KO)
    assert rows == [{'speaker': '린위', 'listener': '사부', 'level': '해요체', 'note': ''}]


def test_tab_lines_are_read_as_before():
    rows = parse_table('린위\t사부\t합쇼체\t제자\n사부\t린위\t반말\t스승\n쑤칭\t린위\t해요체\t후배', NAMES_KO)
    assert [(r['speaker'], r['listener'], r['level']) for r in rows] == [
        ('린위', '사부', '합쇼체'), ('사부', '린위', '반말'), ('쑤칭', '린위', '해요체')]


def test_the_speech_answer_is_kept_for_checking(qt_app, monkeypatch):
    saved = []
    monkeypatch.setattr(ui, '_save_analysis_answer', lambda *a: saved.append(a))

    class P:
        def translate(self, req, secret):
            return TranslationResponse('{"pairs":[{"speaker":"린위","listener":"사부","level":"informal polite"}]}', 'STOP', {})
    monkeypatch.setattr(ui, '_make_provider', lambda *a, **k: P())
    t = ui.SpeechExtractThread([('local', 'local', 'qwen3.5:35b', 'http://x', None)], [('S', 'U')],
                               lambda text: parse_table(text, NAMES_KO), local_prompts=[('S', 'U')])
    out = []
    t.done.connect(lambda ok, res: out.append((ok, res)))
    t.run()
    assert out[-1][0] and out[-1][1]['rows'][0]['level'] == '해요체'
    assert saved and saved[0][0] == '인물 말투표' and '1쌍 사용' in saved[0][1] and 'informal polite' in saved[0][4]
