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


# ================================================================ v2.5.4 (보완 2): English term detection
# Finds the names the earlier rules missed or broke, keeps everything they found, and keeps
# ordinary words out. Chinese and Japanese are untouched.
from novel_translator.term_extraction import (half_converted, valid_rendering, build_prompts, candidate_contexts,  # noqa: E402
                                              repair_prompt)
from novel_translator.providers.base import TranslationResponse  # noqa: E402
from novel_translator.ui import main_window as ui  # noqa: E402
from novel_translator.ui.name_collision_dialog import NameCollisionDialog  # noqa: E402

_LINES = [
    "Zoë looked at Renée and said nothing.", "Then José laughed while DeShawn waited.", "He told LeBron that McKenna was late.",
    "She saw O'Brien near the gate with Mary-Jane.", "It was Emily's turn, and she knew it.", '"EMILY!" he shouted across the yard.',
    "Harper nodded.", "Harper sighed and turned away.", "Will said he will come back soon.", "Later, Will smiled at her.",
    "Rose rose from her chair.", "He waited for Rose by the door.", '"Oh, come on," she said.', "And Oh, how she laughed.",
    '"If you must," Emily said.', "In the end, Emily ran to the car.", "My God, it was cold.", "She called her Mom twice.",
    "They walked to Blackwater Keep at dawn.", "The Order of the Silver Hand met there.", "Hmm, maybe not.", "He's late again.",
    "Tell DeShawn I said hi.", "Ask José about it.", "We met McKenna at noon.", "Find O'Brien now.", "Łukasz waited for Søren.",
    "The Count's castle stood dark.", "They feared the Count's return.", "At the Count's door she stopped.",
    "Everyone stared.", '"Good-night," said Emily.', "To-morrow, then.", "Don't go.",
]
BOOK = ' '.join(_LINES[i % len(_LINES)] for i in range(len(_LINES) * 6))


def test_english_names_the_old_rules_missed_or_broke_are_found():
    names = {k: n for k, n in candidates(BOOK, 'en')}
    for want in ('Zoë', 'Renée', 'José', 'DeShawn', 'LeBron', 'Łukasz', 'Søren', 'Harper', 'Will', 'Count',
                 'Order of the Silver Hand', "O'Brien", 'Mary-Jane', 'McKenna', 'Emily', 'Rose', 'Blackwater Keep'):
        assert want in names, want
    for piece in ('Zo', 'Ren', 'Jos', 'De', 'Le', "Emily's", "Count's"):
        assert piece not in names, piece                          # no broken pieces any more
    assert names['Emily'] >= 6 * 5                                 # Emily's and EMILY count for Emily


def test_ordinary_english_words_stay_out_of_the_candidates():
    names = {k for k, _ in candidates(BOOK, 'en')}
    for word in ('Oh', 'If', 'In', 'Hmm', "He's", 'Everyone', 'Good-night', 'To-morrow', "Don't", 'Tell DeShawn', 'Ask José'):
        assert word not in names, word


def test_everything_the_old_rules_found_is_still_found():
    """Old rules on this kind of text: these are what they returned (minus the If/Oh kind)."""
    text = ('"My dear Mr. Darcy," said Elizabeth. Lady Catherine frowned at Miss Bennet. Dr. Watson nodded. '
            'Lord Voldemort laughed while Professor McGonagall and Sean O\'Brien watched. Then Jean-Luc smiled at '
            'Alice. On Monday in March, Sir Walter Elliot arrived. Captain Will Turner sailed. ') * 6
    names = {k for k, _ in candidates(text, 'en')}
    assert {'Darcy', 'Elizabeth', 'Catherine', 'Bennet', 'Watson', 'Voldemort', 'McGonagall', "Sean O'Brien",
            'Walter Elliot', 'Jean-Luc', 'Alice', 'Turner'} <= names


def test_half_converted_renderings_are_not_valid():
    assert half_converted('엠ily') and half_converted('제ssica') and not half_converted('Dr. 왓슨')
    assert not valid_rendering('Emily', '엠ily') and valid_rendering('Watson', 'Dr. 왓슨') and valid_rendering('Emily', '에밀리')
    rep = {}
    got = parse_glossary(json.dumps({'Emily': '엠ily', 'Zoë': '조이'}, ensure_ascii=False), [('Emily', 9), ('Zoë', 9)], lang='en', report=rep)
    assert got == {'Zoë': '조이'} and rep['half_entries'] == {'Emily': '엠ily'}


def test_only_the_english_prompt_changes():
    cands = [('林若雪', 9), ('陈屿', 5)]
    for lang in ('zh', 'ja'):
        system, user = build_prompts(cands, lang)
        assert 'FOR THIS ENGLISH NOVEL' not in system and 'EXAMPLE USE' not in user
    system, user = build_prompts([('Will', 9)], 'en', contexts={'Will': 'Later, Will smiled at her.'})
    assert 'FOR THIS ENGLISH NOVEL' in system and 'Susan -> 수전' in system and 'Later, Will smiled' in user
    assert 'EXAMPLE USE' not in build_prompts([('Will', 9)], 'en')[1]


def test_example_lines_only_for_candidates_that_are_also_words():
    ctx = candidate_contexts(BOOK, candidates(BOOK, 'en'))
    assert 'Will' in ctx and 'Rose' in ctx and 'Will' in ctx['Will']
    assert 'Zoë' not in ctx and 'DeShawn' not in ctx                 # plain names need no example
    assert all(len(v) <= 140 for v in ctx.values())


def _thread(monkeypatch, answers, provider_id='local', known=None):
    sent = []

    class P:
        def translate(self, req, secret):
            sent.append(req)
            a = answers[min(len(sent) - 1, len(answers) - 1)]
            if isinstance(a, Exception):
                raise a
            return TranslationResponse(json.dumps(a, ensure_ascii=False), 'STOP', {})
    monkeypatch.setattr(ui, '_make_provider', lambda *a, **k: P())
    cands = [(k, 9) for k in ('Emily', 'Susan', 'Suzanne', 'Zoë')]
    t = ui.TermExtractThread(provider_id, 'k', 'm', '', 'SYS', 'USER', cands)
    t.lang = 'en'; t.known = dict(known or {}); t.user_local = 'USER+EXAMPLES'
    out = []
    t.done.connect(lambda ok, res: out.append((ok, res)))
    t.run()
    return t, out[-1], sent


def test_half_converted_and_colliding_names_are_asked_once_more(qt_app, monkeypatch):
    first = {'Emily': '엠ily', 'Susan': '수잔', 'Suzanne': '수잔', 'Zoë': '조이'}
    second = {'Emily': '에밀리', 'Susan': '수전', 'Suzanne': '수잔'}
    t, (ok, res), sent = _thread(monkeypatch, [first, second])
    assert ok and res['Emily'] == '에밀리' and res['Susan'] == '수잔' and res['Suzanne'] == '수잔'   # the glossary waits for the user
    assert t.suggestions == {'Susan': '수전'}                       # offered in the 겹침 window
    assert len(sent) == 2 and 'GROUP 1' in sent[1].user_prompt and 'Emily\t엠ily' in sent[1].user_prompt
    assert sent[0].user_prompt == 'USER+EXAMPLES'                   # the local model gets the example lines
    assert t.report['repair'] == {'half': 1, 'half_fixed': 1, 'groups': 1, 'suggested': 1}


def test_a_failed_repair_keeps_every_name(qt_app, monkeypatch):
    first = {'Emily': '엠ily', 'Susan': '수잔', 'Suzanne': '수잔'}
    t, (ok, res), sent = _thread(monkeypatch, [first, RuntimeError('server gone')])
    assert ok and res == first and t.suggestions == {}              # nothing lost; 엠ily shows light red


def test_cloud_engines_get_no_prose_and_no_repair_without_a_problem(qt_app, monkeypatch):
    t, (ok, res), sent = _thread(monkeypatch, [{'Emily': '에밀리', 'Zoë': '조이'}], provider_id='gemini')
    assert ok and len(sent) == 1 and sent[0].user_prompt == 'USER'


def test_a_confirmed_name_stays_and_only_the_new_one_gets_a_proposal(qt_app, monkeypatch):
    first = {'Suzanne': '수잔'}
    second = {'Susan': '수잔', 'Suzanne': '쉬잔'}
    t, (ok, res), sent = _thread(monkeypatch, [first, second], known={'Susan': '수잔'})
    assert 'Susan\t수잔\tFIXED' in sent[1].user_prompt
    assert t.suggestions == {'Suzanne': '쉬잔'}


def test_collision_window_comes_with_the_proposals_filled_in(qt_app):
    dlg = NameCollisionDialog([('수잔', ['Susan', 'Suzanne'])], {'Susan': 40, 'Suzanne': 12}, suggestions={'Susan': '수전'})
    cells = {dlg.table.item(r, 1).text(): dlg.table.item(r, 3).text() for r in range(dlg.table.rowCount())}
    assert cells == {'Susan': '수전', 'Suzanne': '수잔'}
    dlg.accept()
    assert dlg.changes == {'Susan': '수전'}
    plain = NameCollisionDialog([('수잔', ['Susan', 'Suzanne'])])
    assert {plain.table.item(r, 3).text() for r in range(2)} == {'수잔'}   # without proposals: as before


def test_repair_prompt_lists_the_problems():
    system, user = repair_prompt({'Emily': '엠ily'}, [('수잔', ['Susan', 'Suzanne'])], {'Susan': '수잔'})
    assert 'different' in system.lower() and 'Emily\t엠ily' in user and 'Suzanne\t수잔' in user and 'Susan\t수잔\tFIXED' in user


def test_window_passes_the_proposals_to_the_collision_window(qt_app, sandbox, monkeypatch):
    import test_v252_name_collision as v252
    first = {'Susan': '수잔', 'Suzanne': '수잔', 'Jonathan': '조너선', 'Margaret': '마거릿'}
    second = {'Susan': '수전', 'Suzanne': '수잔'}
    w, calls, _ = v252._setup(qt_app, sandbox, monkeypatch, names=first, auto_terms=False, auto_speech=False)
    n = {'json': 0}

    class P:
        def translate(self, req, secret):
            n['json'] += 1
            return TranslationResponse(json.dumps(first if n['json'] == 1 else second, ensure_ascii=False), 'STOP', {})
    monkeypatch.setattr(ui, '_make_provider', lambda *a, **k: P())
    seen = {}

    class D:
        def __init__(self, groups, counts, held, timeout=0, parent=None, suggestions=None):
            seen['groups'] = groups; seen['suggestions'] = suggestions; self.changes = {}; self.timed_out = False
        def exec(self):
            return 0
    monkeypatch.setattr('novel_translator.ui.name_collision_dialog.NameCollisionDialog', D)
    try:
        w._extract_terms()
        assert v252._wait(qt_app, lambda: 'groups' in seen, 15)
        assert seen['suggestions'] == {'Susan': '수전'} and n['json'] == 2
    finally:
        w.close()
