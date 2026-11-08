"""v2.6.3: the shared glossary remembers when each entry was last used, so a full list can
be tidied from the entries no book has needed for the longest time.

- common_terms.json keeps its format (an older version reads it as before); the dates live
  in common_terms_used.json beside it.
- Entries stored before v2.6.3 start counting from the first day this version opens them.
- An entry gets today's date when it is added, changed, or used by a book whose names are
  extracted (found in it, or already in its glossary).
- [오래 안 쓴 항목 고르기] only selects; nothing is removed until [선택 삭제] and [저장].
"""
from __future__ import annotations

import json

from PySide6.QtWidgets import QDialog, QPushButton, QTableWidget

from novel_translator import common_terms as ct
from novel_translator.ui import main_window as ui

from test_alpha17 import qt_app, sandbox  # noqa: F401
from test_v15_review import _speech_window, _NAMES


def _used(base):
    return json.loads((base / ct.USED_FILE).read_text(encoding='utf-8'))


def test_list_format_is_unchanged_and_dates_live_beside_it(tmp_path):
    ct.add('zh', {'左京': '좌경', '白颖': '백영'}, tmp_path)
    assert json.loads((tmp_path / ct.FILE).read_text(encoding='utf-8')) == {'zh': {'左京': '좌경', '白颖': '백영'}}
    assert set(_used(tmp_path)['zh']) == {'左京', '白颖'}


def test_entries_from_before_start_counting_today(tmp_path):
    (tmp_path / ct.FILE).write_text(json.dumps({'zh': {'左京': '좌경', '白颖': '백영'}}, ensure_ascii=False), encoding='utf-8')
    assert ct.last_used('zh', tmp_path, today='2026-11-08') == {'左京': '2026-11-08', '白颖': '2026-11-08'}
    # kept: a later day does not move them
    assert ct.last_used('zh', tmp_path, today='2027-03-01') == {'左京': '2026-11-08', '白颖': '2026-11-08'}


def test_use_moves_the_date_and_old_entries_are_found(tmp_path):
    (tmp_path / ct.FILE).write_text(json.dumps({'zh': {'左京': '좌경', '白颖': '백영', '李萱诗': '이훤시'}}, ensure_ascii=False), encoding='utf-8')
    ct.last_used('zh', tmp_path, today='2026-01-01')
    ct.touch('zh', ['白颖', '없는항목'], tmp_path, today='2026-09-01')
    ct.touch('zh', ['左京'], tmp_path, today='2026-04-01')
    assert ct.unused_since('zh', 180, tmp_path, today='2026-11-08') == ['李萱诗', '左京']   # longest unused first
    assert ct.unused_since('zh', 30, tmp_path, today='2026-11-08') == ['李萱诗', '左京', '白颖']
    assert ct.unused_since('zh', 400, tmp_path, today='2026-11-08') == []
    assert 'ja' not in _used(tmp_path)


def test_edits_in_the_dialog_keep_the_dates_of_unchanged_entries(tmp_path):
    (tmp_path / ct.FILE).write_text(json.dumps({'zh': {'左京': '좌경', '白颖': '백영'}}, ensure_ascii=False), encoding='utf-8')
    ct.last_used('zh', tmp_path, today='2026-01-01')
    ct.replace('zh', {'左京': '좌경', '白颖': '백잉', '新人': '신인'}, tmp_path)
    used = _used(tmp_path)['zh']
    assert used['左京'] == '2026-01-01' and used['白颖'] == ct._today() and used['新人'] == ct._today()
    ct.replace('zh', {'左京': '좌경'}, tmp_path)
    assert set(_used(tmp_path)['zh']) == {'左京'}


def test_a_broken_dates_file_never_stops_the_glossary(tmp_path):
    ct.add('zh', {'左京': '좌경'}, tmp_path)
    (tmp_path / ct.USED_FILE).write_text('{broken', encoding='utf-8')
    assert ct.load('zh', tmp_path) == {'左京': '좌경'}
    assert ct.touch('zh', ['左京'], tmp_path) in (0, 1)
    assert ct.last_used('zh', tmp_path)['左京']


def test_a_book_whose_names_are_extracted_marks_its_shared_entries_used(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w, _ = _speech_window(monkeypatch, root, '{}')
    try:
        ct.replace('zh', {'左京': '좌경', '白颖': '백영', '없는이름': '없음'}, ui.DATA_DIR)
        data = json.loads((ui.DATA_DIR / ct.USED_FILE).read_text(encoding='utf-8'))
        data['zh'] = {a: '2020-01-01' for a in data['zh']}
        (ui.DATA_DIR / ct.USED_FILE).write_text(json.dumps(data), encoding='utf-8')
        w.use_common.setChecked(True)
        monkeypatch.setattr(w, '_extraction_engines', lambda *a: [])   # no request: only the preparation runs
        w._begin_term_extraction(w.source_text, auto=True)
        used = json.loads((ui.DATA_DIR / ct.USED_FILE).read_text(encoding='utf-8'))['zh']
        assert used['左京'] == ct._today() and used['白颖'] == ct._today()   # in the book
        assert used['없는이름'] == '2020-01-01'                               # not in it
    finally:
        if w._term_thread is not None: w._term_thread.wait(5000)
        w.close()


def test_dialog_shows_last_use_and_picks_old_entries_without_removing(qt_app, sandbox, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    root, _ = sandbox
    w, _ = _speech_window(monkeypatch, root, '')
    ct.replace('zh', dict(_NAMES), ui.DATA_DIR)
    data = json.loads((ui.DATA_DIR / ct.USED_FILE).read_text(encoding='utf-8'))
    data['zh']['左京'] = '2020-01-01'
    (ui.DATA_DIR / ct.USED_FILE).write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setattr(QInputDialog, 'getInt', lambda *a, **k: (6, True))
    seen = {}

    def fake_exec(d):
        t = d.findChild(QTableWidget)
        seen['head'] = [t.horizontalHeaderItem(c).text() for c in range(t.columnCount())]
        seen['dates'] = {t.item(r, 0).text(): t.item(r, 2).text() for r in range(t.rowCount())}
        [b for b in d.findChildren(QPushButton) if b.text() == '오래 안 쓴 항목 고르기'][0].click()
        seen['picked'] = sorted({t.item(i.row(), 0).text() for i in t.selectedIndexes()})
        return 0                                     # 취소: nothing is removed
    monkeypatch.setattr(QDialog, 'exec', fake_exec)
    try:
        w._open_common_terms()
        assert seen['head'] == ['원문', '한국어', '마지막 사용']
        assert seen['dates']['左京'] == '2020-01-01' and seen['dates']['白颖'] == ct._today()
        assert seen['picked'] == ['左京']
        assert ct.load('zh', ui.DATA_DIR) == dict(_NAMES)
    finally:
        w.close()


def test_dialog_save_still_reads_only_source_and_korean(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w, _ = _speech_window(monkeypatch, root, '')
    ct.replace('zh', dict(_NAMES), ui.DATA_DIR)

    def fake_exec(d):
        t = d.findChild(QTableWidget)
        [b for b in d.findChildren(QPushButton) if b.text() == '지금 추가'][0].click()   # nothing pending: disabled
        t.selectRow([r for r in range(t.rowCount()) if t.item(r, 0).text() == '白颖'][0])
        [b for b in d.findChildren(QPushButton) if b.text() == '선택 삭제'][0].click()
        return 1
    monkeypatch.setattr(QDialog, 'exec', fake_exec)
    try:
        w._open_common_terms()
        assert ct.load('zh', ui.DATA_DIR) == {a: b for a, b in _NAMES.items() if a != '白颖'}
        assert '白颖' not in json.loads((ui.DATA_DIR / ct.USED_FILE).read_text(encoding='utf-8'))['zh']
    finally:
        w.close()
