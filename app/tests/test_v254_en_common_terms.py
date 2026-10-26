"""v2.5.4 (보완): English ordinary words do not become glossary terms.

A small analysis model (e.g. a 9B local model) answered pronouns, contractions and
exclamations as terms (He's -> 그는, My God -> 마이 갓, It -> 그것). Those can never be a
name, so they are dropped. Words that are usually ordinary but can be a name in some
books (God, Jesus, Son, Boy, Sis) are kept and only shown light red.
"""
from __future__ import annotations

import json

from PySide6.QtWidgets import QTableWidgetItem

from novel_translator import common_terms
from novel_translator.term_extraction import candidates, parse_glossary, is_never_term, is_doubtful_term

from test_alpha17 import qt_app, sandbox, _window  # noqa: F401

NEVER = ["He", "It", "We", "He's", "She's", "He'd", "She'd", "He'll", "She'll", "We're", "We've", "They're",
         "You're", "You've", "You'd", "That's", "There's", "I'm", "Let's", "He’s", "Oh God", "My God", "Good God",
         "Thank God", "Oh my God", "Jesus Christ", "Goddamn", "Oh Lord", "Good Lord"]
DOUBTFUL = ["God", "Jesus", "Son", "Boy", "Sis", "Mom", "Dad", "Honey", "Master"]
NAMES = ["Jamie", "Jamielynn", "Hesper", "Ithaca", "Godric", "Weston", "Sonny", "Theodore", "Shea", "Heddy",
         "Wendell", "Thea", "Ian", "Mr Son", "Son Minho", "Lord Godric", "Itzel", "Youssef", "Thatcher"]


def test_never_terms_and_names_are_told_apart():
    for w in NEVER:
        assert is_never_term(w), w
    for w in DOUBTFUL + NAMES:
        assert not is_never_term(w), w
    for w in DOUBTFUL:
        assert is_doubtful_term(w), w
    for w in NAMES:
        assert not is_doubtful_term(w), w
    for w in ('他', '林若雪', '彼女', '先輩'):        # Chinese / Japanese are not touched
        assert not is_never_term(w) and not is_doubtful_term(w)


def test_model_answer_keeps_names_and_doubtful_words_but_drops_never_terms():
    answer = {**{w: '그는' for w in NEVER}, 'God': '하나님', 'Son': '아들', 'Sis': '언니', 'Jamie': '제이미', 'Godric': '고드릭'}
    cands = [(k, 9) for k in answer]
    report = {}
    got = parse_glossary(json.dumps(answer, ensure_ascii=False), cands, lang='en', report=report)
    assert set(got) == {'God', 'Son', 'Sis', 'Jamie', 'Godric'}
    assert report['common_word'] == len(NEVER)


def test_english_candidates_leave_out_never_terms():
    text = ('Then he said that Jamie would come. "Oh God," she said, and He\'s gone, and We\'re late, '
            'and It was Jamie again. My God, Jamie. Oh God. ') * 10
    names = {k for k, _ in candidates(text, 'en')}
    assert 'Jamie' in names
    assert not any(is_never_term(k) for k in names), names


def test_shared_glossary_takes_neither_never_nor_doubtful_words(tmp_path):
    terms = {"He's": '그는', 'My God': '마이 갓', 'Son': '아들', 'God': '하나님', 'Jamie': '제이미', 'Godric': '고드릭'}
    assert common_terms.new_candidates('en', terms, tmp_path) == {'Jamie': '제이미', 'Godric': '고드릭'}
    common_terms.replace('en', {"He's": '그는', 'It': '그것', 'Son': '손', 'Jamie': '제이미'}, tmp_path)   # stored before
    usable = common_terms.usable(common_terms.load('en', tmp_path))
    assert usable == {'Son': '손', 'Jamie': '제이미'}   # never terms are not used; a name the user kept stays


def _fill(w, rows):
    w.term_table.setRowCount(0)
    for a, b in rows:
        r = w.term_table.rowCount(); w.term_table.insertRow(r)
        w.term_table.setItem(r, 0, QTableWidgetItem(a)); w.term_table.setItem(r, 1, QTableWidgetItem(b))
    w._mark_suspicious_terms()


def test_glossary_rows_are_marked_light_red_and_never_removed(qt_app, sandbox, monkeypatch):
    w = _window(monkeypatch)
    try:
        _fill(w, [("He's", '그는'), ('My God', '마이 갓'), ('Son', '아들'), ('Jamie', '제이미')])
        flagged = {src for _, src, _ in w._suspicious_terms()}
        assert flagged == {"He's", 'My God', 'Son'}
        assert w.term_table.rowCount() == 4                         # nothing is removed by itself
        tips = {w.term_table.item(r, 0).text(): w.term_table.item(r, 0).toolTip() for r in range(4)}
        assert '일반 단어입니다' in tips["He's"] and '이름이 아닌' in tips['Son'] and tips['Jamie'] == ''
    finally:
        w.close()


def test_never_terms_are_no_character_for_the_speech_table(qt_app, sandbox, monkeypatch):
    w = _window(monkeypatch)
    try:
        _fill(w, [("He's", '그는'), ('We', '우리'), ('Jamie', '제이미')])
        assert w._speech_names() == {'Jamie': '제이미'}
    finally:
        w.close()


def test_shared_glossary_window_marks_old_ordinary_words(qt_app, sandbox, monkeypatch):
    from PySide6.QtWidgets import QDialog, QLabel, QTableWidget
    from novel_translator.ui import main_window as ui
    w = _window(monkeypatch)
    try:
        w.pm.set_active('en-ko'); w._apply_profile('en-ko')
        common_terms.replace('en', {"He's": '그는', 'Son': '손', 'Jamie': '제이미'}, ui.DATA_DIR)
        seen = {}
        def fake_exec(dlg):
            t = dlg.findChild(QTableWidget)
            seen['red'] = {t.item(r, 0).text() for r in range(t.rowCount()) if t.item(r, 0).toolTip()}
            seen['note'] = [l.text() for l in dlg.findChildren(QLabel) if l.objectName() == 'commonFlagNote']
            return 0
        monkeypatch.setattr(QDialog, 'exec', fake_exec)
        w._open_common_terms()
        assert seen['red'] == {"He's", 'Son'} and '빨간 줄 2개' in seen['note'][0]
        assert set(common_terms.load('en', ui.DATA_DIR)) == {"He's", 'Son', 'Jamie'}   # cancel: file unchanged
    finally:
        w.close()
