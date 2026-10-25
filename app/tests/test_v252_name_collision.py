"""v2.5.2 보완: different originals that got the same Korean name (Susan · Suzanne -> 스잔).

- The extraction (automatic at the translation start, or the button) asks to fix or keep them.
- A fixed name is what the speech table (인물 말투표) uses.
- A Korean name renamed in the glossary is renamed in the speech table too; a name split in
  two asks to rebuild the table.
- Nothing changes when there is no collision."""
from __future__ import annotations
import json

from PySide6.QtWidgets import QTableWidgetItem, QMessageBox

from novel_translator.providers.base import TranslationResponse
from novel_translator.term_extraction import korean_collisions
from novel_translator.ui import main_window as ui
from novel_translator.ui import name_collision_dialog as ncd
from test_alpha17 import _window, _fake_worker_class, _wait, qt_app, sandbox  # noqa: F401 (pytest fixtures)

NAMES = {'Susan': '스잔', 'Suzanne': '스잔', 'Jonathan': '조너선', 'Margaret': '마거릿'}
_PEOPLE = list(NAMES)
_LINES = []
for i in range(120):
    a, b = _PEOPLE[i % 4], _PEOPLE[(i + 1) % 4]
    _LINES += [f'{a} looked at {b} and spoke slowly.', f'"{b}, shall we go to the town tomorrow together?"']
BOOK = '\n\n'.join(_LINES)


def _setup(qt_app, sandbox, monkeypatch, names=NAMES, auto_terms=True, auto_speech=True):
    root, cfg = sandbox
    app_cfg = json.loads((cfg / 'app.json').read_text(encoding='utf-8'))
    app_cfg['auto_extract_terms'] = auto_terms; app_cfg['auto_speech_table'] = auto_speech
    (cfg / 'app.json').write_text(json.dumps(app_cfg, ensure_ascii=False), encoding='utf-8')
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _fake_worker_class(started))
    w = _window(monkeypatch)
    monkeypatch.setattr(ui.QMessageBox, 'warning', lambda *a, **k: None)
    calls = []

    class P:
        def __init__(self, pid): self.pid = pid
        def translate(self, req, secret):
            calls.append((req.purpose, req.user_prompt))
            if req.purpose == 'json':
                return TranslationResponse(json.dumps(names, ensure_ascii=False), 'STOP', {})
            return TranslationResponse('조너선\t마거릿\t반말\t친구\n마거릿\t조너선\t해요체\t후배', 'STOP', {})
    monkeypatch.setattr(ui, '_make_provider', lambda pid, *a, **k: P(pid))
    monkeypatch.setattr(w, '_extraction_engines', lambda purpose='speech': [('gemini', 'k', 'm', '', None)])
    p = root / 'book.txt'; p.write_text(BOOK, encoding='utf-8')
    w._open_txt_path(str(p)); w._apply_profile('en-ko')
    return w, calls, started


class _FakeDialog:
    """Stands in for the window: answers with `changes` (None = [그대로 두기])."""
    seen = []

    def __init__(self, changes=None, rebuild=True, timed_out=False):
        self._changes, self._rebuild, self._timed_out = changes, rebuild, timed_out

    def __call__(self, groups, counts, held, timeout=0, parent=None):
        _FakeDialog.seen.append((groups, counts, set(held), timeout))
        outer = self

        class D:
            changes = dict(outer._changes or {})
            timed_out = outer._timed_out
            rebuild_offered = any(k in set(held) for k, _ in groups)
            class rebuild:          # noqa: N801 (mimics the QCheckBox)
                @staticmethod
                def isChecked(): return outer._rebuild
            def exec(self):
                return ui.QDialog.DialogCode.Accepted if outer._changes else ui.QDialog.DialogCode.Rejected
        return D()


def _log(w):
    return w.error_view.toPlainText()


def _speech_prompts(calls):
    return [p for purpose, p in calls if purpose != 'json']


# ---------------------------------------------------------------- the pure check
def test_collisions_are_found_and_spellings_of_one_word_are_not():
    t = {'Susan': '스잔', 'Suzanne': '스잔', 'John': '존', 'SUSAN': '수잔', 'susan': '수잔', '宋清': '송청', '宋卿': '송 청'}
    assert korean_collisions(t) == [('스잔', ['Susan', 'Suzanne']), ('송청', ['宋清', '宋卿'])]
    assert korean_collisions(t, focus=['Suzanne']) == [('스잔', ['Susan', 'Suzanne'])]
    assert korean_collisions(t, focus=['John']) == []
    assert korean_collisions({'Susan': '수잔', 'Suzanne': '스잔느'}) == []


# ---------------------------------------------------------------- automatic extraction at the start
def test_auto_extraction_asks_and_the_fixed_name_reaches_the_speech_table(qt_app, sandbox, monkeypatch):
    _FakeDialog.seen = []
    monkeypatch.setattr(ncd, 'NameCollisionDialog', _FakeDialog({'Suzanne': '스잔느'}))
    w, calls, started = _setup(qt_app, sandbox, monkeypatch)
    try:
        w.start_translation()
        assert _wait(qt_app, lambda: bool(started) and not w._extracting(), 15)
        groups, counts, _held, timeout = _FakeDialog.seen[0]
        assert groups == [('스잔', ['Susan', 'Suzanne'])]
        assert counts['Suzanne'] > 0 and timeout == 180      # unattended start never waits for hours
        assert w._terms()['Suzanne'] == '스잔느' and w._terms()['Susan'] == '스잔'
        assert w._term_locks.get('Suzanne') == '스잔느'       # a user choice, like a manual edit
        speech = _speech_prompts(calls)
        assert speech and 'Suzanne = 스잔느' in speech[0] and 'Susan = 스잔' in speech[0]
        assert started[0].terms['Suzanne'] == '스잔느'        # the translation uses it too
        assert '한글 이름 겹침 고침: Suzanne 스잔 → 스잔느' in _log(w)
    finally:
        w.close()


def test_auto_extraction_keep_continues_and_logs(qt_app, sandbox, monkeypatch):
    _FakeDialog.seen = []
    monkeypatch.setattr(ncd, 'NameCollisionDialog', _FakeDialog(None, timed_out=True))
    w, calls, started = _setup(qt_app, sandbox, monkeypatch)
    try:
        w.start_translation()
        assert _wait(qt_app, lambda: bool(started) and not w._extracting(), 15)
        assert w._terms()['Suzanne'] == '스잔'
        assert '한글 이름 겹침 1건 · 3분 동안 선택이 없어 그대로 둠: Susan·Suzanne → 스잔' in _log(w)
    finally:
        w.close()


def test_no_collision_means_no_question_and_the_same_flow(qt_app, sandbox, monkeypatch):
    _FakeDialog.seen = []
    monkeypatch.setattr(ncd, 'NameCollisionDialog', _FakeDialog({'x': 'y'}))
    names = dict(NAMES, Suzanne='수잔느')
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, names=names)
    try:
        w.start_translation()
        assert _wait(qt_app, lambda: bool(started) and not w._extracting(), 15)
        assert _FakeDialog.seen == []
        assert len(_speech_prompts(calls)) >= 1 and w._terms() == names
        assert '이름 겹침' not in _log(w)
    finally:
        w.close()


def test_a_split_name_rebuilds_an_existing_speech_table(qt_app, sandbox, monkeypatch):
    """A speech table made earlier holds 스잔 for one person: after the split it is made
    again (auto speech off and a table present would normally skip it)."""
    _FakeDialog.seen = []
    monkeypatch.setattr(ncd, 'NameCollisionDialog', _FakeDialog({'Suzanne': '스잔느'}, rebuild=True))
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, auto_speech=False)
    try:
        w._set_speech_rows([{'speaker': '스잔', 'listener': '조너선', 'level': '반말', 'note': ''}])
        w.start_translation()
        assert _wait(qt_app, lambda: bool(started) and not w._extracting(), 15)
        assert _FakeDialog.seen[0][2] == {'스잔', '조너선'}
        speech = _speech_prompts(calls)
        assert len(speech) == 1 and 'Suzanne = 스잔느' in speech[0]
        assert all('스잔' != r['speaker'] or r['listener'] != '조너선' for r in w._speech_rows())
        assert w._speech_force_once is False
    finally:
        w.close()


def test_split_without_rebuild_keeps_the_table_and_says_so(qt_app, sandbox, monkeypatch):
    monkeypatch.setattr(ncd, 'NameCollisionDialog', _FakeDialog({'Suzanne': '스잔느'}, rebuild=False))
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, auto_speech=False)
    try:
        rows = [{'speaker': '스잔', 'listener': '조너선', 'level': '반말', 'note': ''}]
        w._set_speech_rows(rows)
        w.start_translation()
        assert _wait(qt_app, lambda: bool(started) and not w._extracting(), 15)
        assert _speech_prompts(calls) == []
        assert w._speech_rows()[0]['speaker'] == '스잔'
        assert "인물 말투표의 '스잔'는 그대로 둠" in _log(w)
    finally:
        w.close()


# ---------------------------------------------------------------- the [인명·용어 자동 추출] button
def test_manual_extraction_asks_too(qt_app, sandbox, monkeypatch):
    _FakeDialog.seen = []
    monkeypatch.setattr(ncd, 'NameCollisionDialog', _FakeDialog({'Susan': '수잔'}))
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, auto_terms=False)
    try:
        w._extract_terms()
        assert _wait(qt_app, lambda: not w._extracting() and bool(_FakeDialog.seen), 15)
        assert _FakeDialog.seen[0][3] == 0                    # no time limit when started by hand
        assert w._terms()['Susan'] == '수잔' and w._terms()['Suzanne'] == '스잔'
        assert not started
    finally:
        w.close()


# ---------------------------------------------------------------- editing the glossary by hand
def _fill(w, terms):
    for a, b in terms.items():
        r = w.term_table.rowCount(); w.term_table.insertRow(r)
        w.term_table.setItem(r, 0, QTableWidgetItem(a)); w.term_table.setItem(r, 1, QTableWidgetItem(b))


def _edit(qt_app, w, source, korean):
    qt_app.processEvents(); w._remember_terms()       # what the table showed before the edit
    for r in range(w.term_table.rowCount()):
        if w.term_table.item(r, 0).text() == source:
            w.term_table.item(r, 1).setText(korean)
    w._term_user_committed()


def test_renaming_a_glossary_name_renames_it_in_the_speech_table(qt_app, sandbox, monkeypatch):
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, auto_terms=False)
    try:
        _fill(w, {'Suzanne': '스잔', 'Jonathan': '조너선'})
        w._set_speech_rows([{'speaker': '조너선', 'listener': '스잔', 'level': '반말', 'note': '친구'},
                            {'speaker': '스잔', 'listener': '조너선', 'level': '해요체', 'note': ''}])
        _edit(qt_app, w, 'Suzanne', '스잔느')
        rows = w._speech_rows()
        assert [(r['speaker'], r['listener'], r['level']) for r in rows] == [('조너선', '스잔느', '반말'), ('스잔느', '조너선', '해요체')]
        assert '스잔 → 스잔느' in w.status.text()
    finally:
        w.close()


def test_splitting_a_shared_name_asks_to_rebuild_and_never_guesses(qt_app, sandbox, monkeypatch):
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, auto_terms=False)
    asked = []
    monkeypatch.setattr(ui.QMessageBox, 'question', lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    try:
        _fill(w, {'Susan': '스잔', 'Suzanne': '스잔', 'Jonathan': '조너선'})
        w._set_speech_rows([{'speaker': '조너선', 'listener': '스잔', 'level': '반말', 'note': ''}])
        _edit(qt_app, w, 'Suzanne', '스잔느')
        assert asked and "'스잔'" in asked[0] and 'Susan(스잔)' in asked[0] and 'Suzanne(스잔느)' in asked[0]
        assert w._speech_rows()[0]['listener'] == '스잔'      # unknown who it was: left alone
    finally:
        w.close()


def test_editing_a_name_the_speech_table_does_not_use_asks_nothing(qt_app, sandbox, monkeypatch):
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, auto_terms=False)
    monkeypatch.setattr(ui.QMessageBox, 'question', lambda *a, **k: (_ for _ in ()).throw(AssertionError('asked')))
    try:
        _fill(w, {'Susan': '스잔', 'Suzanne': '스잔', 'Jonathan': '조너선', 'Margaret': '마거릿'})
        w._set_speech_rows([{'speaker': '조너선', 'listener': '마거릿', 'level': '반말', 'note': ''}])
        _edit(qt_app, w, 'Suzanne', '스잔느')
        assert w._speech_rows()[0] == {'speaker': '조너선', 'listener': '마거릿', 'level': '반말', 'note': ''}
    finally:
        w.close()


# ---------------------------------------------------------------- the window itself
def test_dialog_fix_keep_validation_and_countdown(qt_app, monkeypatch):
    groups = [('스잔', ['Susan', 'Suzanne'])]
    d = ncd.NameCollisionDialog(groups, {'Susan': 12, 'Suzanne': 3}, {'스잔'}, timeout=0)
    assert d.table.rowCount() == 2 and d.table.item(0, 2).text() == '12회'
    assert d.rebuild_offered and d.rebuild.isChecked()
    d.table.item(1, 3).setText('스잔느'); d.accept()
    assert d.changes == {'Suzanne': '스잔느'}

    d = ncd.NameCollisionDialog(groups, {}, set(), timeout=0)
    assert not d.rebuild_offered
    warned = []
    monkeypatch.setattr(ncd.QMessageBox, 'warning', lambda *a, **k: warned.append(a[2]))
    d.table.item(1, 3).setText('苏珊'); d.accept()
    assert warned and d.changes == {} and d.result() == 0
    d.table.item(1, 3).setText(''); d.accept()              # an emptied cell keeps the name
    assert d.changes == {}

    d = ncd.NameCollisionDialog(groups, {}, set(), timeout=2)
    assert '0:02' in d.countdown.text()
    d._tick(); d._tick()
    assert d.timed_out and d.result() == 0

    d = ncd.NameCollisionDialog(groups, {}, set(), timeout=5)
    d.table.item(0, 3).setText('수잔')                        # touching the table stops the clock
    assert not d._timer.isActive() and '시간 제한 없음' in d.countdown.text()
