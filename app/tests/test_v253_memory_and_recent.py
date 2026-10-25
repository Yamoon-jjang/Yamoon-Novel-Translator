"""v2.5.3

1. Translation memory: a paragraph is reused only at its own place (same paragraph number and
   same text). A short line repeated elsewhere in the book is translated there with its own
   context, so stopping and restarting a big book gives the same result as one run.
2. Recent files (최근 연 파일): up to 20, newest first, the way 야문 클린리더 shows them."""
from __future__ import annotations
import json
from dataclasses import replace

from novel_translator.chunking import split_paragraphs, mark_translatable
from novel_translator.ui import main_window as ui
from test_refusal_recovery import ScriptedProvider
from test_alpha17 import _worker_for, _no_repair, _window, _wait, qt_app, sandbox  # noqa: F401 (pytest fixtures)


# ---------------------------------------------------------------- translation memory
TEXT = '他静静地看着窗外的雨，很久没有说话。\n\n“好。”\n\n她也沉默着，轻轻叹了口气，然后转身离开。\n\n“好。”'


def _mark(b):
    return 'U' + '-'.join(f'{ord(c):x}' for c in b[:4])


def test_a_repeated_line_is_not_taken_from_another_place(tmp_path):
    marks = mark_translatable(split_paragraphs(TEXT), 'zh')
    second = [p.pid for p in marks if p.text == '“好。”'][1]
    # First run: everything but the second “好。” translated (as if the job stopped before it).
    old = [replace(p, translatable=p.pid != second) for p in marks]
    first = ScriptedProvider(lambda p, s, b: '이전번역 ' + _mark(b))
    w, _, _ = _worker_for(tmp_path, TEXT, old, first); _no_repair(); w.run()
    # Restart: the second “好。” is requested, not copied from the first one.
    again = ScriptedProvider(lambda p, s, b: '새번역 ' + _mark(b))
    w, _, events = _worker_for(tmp_path, TEXT, marks, again); _no_repair(); w.run()
    assert len(again.requests) == 1
    sent = again.requests[0].user_prompt.split('[SOURCE', 1)[1]
    assert f'<P{second:06d}>' in sent
    assert '他静静地' not in sent and '她也沉默' not in sent, 'paragraphs translated at their own place are still reused'
    out = (tmp_path / 'out' / 'novel.ko.txt').read_text(encoding='utf-8-sig')
    assert out.count('이전번역 ' + _mark('“好。”')) == 1 and out.count('새번역 ' + _mark('“好。”')) == 1


def test_resume_reuses_every_paragraph_at_its_place(tmp_path):
    """A finished book run again with a different chunk layout requests nothing."""
    marks = mark_translatable(split_paragraphs(TEXT), 'zh')
    first = ScriptedProvider(lambda p, s, b: '이전번역 ' + _mark(b))
    w, _, _ = _worker_for(tmp_path, TEXT, marks, first); _no_repair(); w.run()
    from novel_translator.storage import Database
    db = Database(tmp_path / 'data' / 'translations.db')
    with db.connect() as con:      # a different job identity (settings changed): only the memory can help
        con.execute("UPDATE projects SET job_fingerprint='older'")
    again = ScriptedProvider(lambda p, s, b: '새번역 ' + _mark(b))
    w, _, events = _worker_for(tmp_path, TEXT, marks, again); _no_repair(); w.run()
    assert again.requests == []
    assert any('문단 단위로 재사용' in e for e in events)


def test_memory_keys_are_place_and_text(tmp_path):
    marks = mark_translatable(split_paragraphs(TEXT), 'zh')
    w, _, _ = _worker_for(tmp_path, TEXT, marks, ScriptedProvider(lambda p, s, b: 'x'))
    rows = [{'status': 'COMPLETED', 'source_text': '<P000002>\n“好。”', 'translation': '<P000002>\n"좋아."', 'kept_pids': '', 'error_message': ''}]
    mem = w._paragraph_memory(rows)
    assert mem == {(2, '“好。”'): '"좋아."'}
    chunk = type('C', (), {'paragraphs': [m for m in marks if m.text == '“好。”']})()
    base = w._base_from_memory(chunk, mem)
    assert base.count('"좋아."') == 1 and base.count('“好。”') == 1


# ---------------------------------------------------------------- recent files
def _books(root, n):
    out = []
    for i in range(n):
        p = root / f'book{i:02d}.txt'; p.write_text(f'第{i}章\n\n他看着她。“走吧。”', encoding='utf-8'); out.append(p)
    return out


def test_recent_files_newest_first_no_duplicates_max_20(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    try:
        books = _books(root, 23)
        for p in books:
            w._open_txt_path(str(p))
        w._open_txt_path(str(books[5]))               # opened again: moves to the top, no duplicate
        paths = [r['path'] for r in w._recent_files()]
        assert len(paths) == 20 and paths[0] == str(books[5]) and paths.count(str(books[5])) == 1
        assert paths[1] == str(books[22]) and str(books[0]) not in paths
        saved = json.loads((ui.DATA_DIR / w.RECENT_FILE).read_text(encoding='utf-8'))
        assert [r['path'] for r in saved['files']] == paths
    finally:
        w.close()


def test_recent_menu_numbers_grays_missing_and_clears(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    try:
        books = _books(root, 12)
        for p in books:
            w._open_txt_path(str(p))
        w._set_recent_progress(str(books[11]), 42)
        books[3].unlink()
        menu = w._build_recent_menu()
        acts = [a for a in menu.actions() if not a.isSeparator()]
        assert acts[0].text().startswith('  1  book11.txt') and acts[0].text().endswith('\t번역 42%')
        assert acts[9].text().startswith('10  book02.txt')                  # two-digit numbers line up
        missing = next(a for a in acts if 'book03.txt' in a.text())
        assert not missing.isEnabled() and acts[0].isEnabled()
        assert acts[-1].text() == '목록 비우기' and acts[0].toolTip() == str(books[11])
        opened = []
        monkeypatch.setattr(w, '_open_txt_path', lambda p: opened.append(p))
        acts[1].trigger()
        assert opened == [str(books[10])]
        acts[-1].trigger()
        assert w._recent_files() == []
        empty = [a for a in w._build_recent_menu().actions()]
        assert len(empty) == 1 and not empty[0].isEnabled() and '최근에 연 파일이 없습니다' in empty[0].text()
    finally:
        w.close()


def test_progress_is_remembered_for_the_open_book(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    try:
        p = _books(root, 1)[0]
        w._open_txt_path(str(p))
        w._note_recent_progress(3, 10)
        w._note_recent_progress(10, 10)
        assert w._recent_files()[0]['done'] == 100
        w._open_txt_path(str(p))                     # opening again keeps the progress
        assert w._recent_files()[0]['done'] == 100
    finally:
        w.close()


def test_recent_button_follows_the_input_lock(qt_app, sandbox, monkeypatch):
    w = _window(monkeypatch)
    try:
        w._lock_inputs(True); assert not w.recent_btn.isEnabled()
        w._lock_inputs(False); assert w.recent_btn.isEnabled()
    finally:
        w.close()
