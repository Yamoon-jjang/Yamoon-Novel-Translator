"""v2.5.8 (same-version fix): a folder run shows the file being translated in the 원문 window.

* Each file's own text appears under [원문] when its turn comes, the way a single file is
  shown; the window stays read-only and is display only (the run reads every file itself,
  so the folder is translated exactly as before and nothing is translated twice).
* The live translation comes up by itself once per run; after the user presses [원문] it
  stays on 원문 (the next file's text replaces the previous one there).
* While 번역 결과 is on screen a new file is not put into the window (no cost for big files);
  pressing [원문] shows the file being translated now.
* The 파일 관계 box is always wide enough for its longest item.
"""
from __future__ import annotations

import time

from test_alpha17 import qt_app, sandbox  # noqa: F401 (pytest fixtures)
from test_v258_folder_terms_speech import world, book, _new_window, _open_folder, _done, _run, _no_double_translation  # noqa: F401


def _three_books(world):
    books = world.root / 'books'; books.mkdir()
    texts = {'1_A.txt': book(['林雨', '苏晴'], 'A'), '2_B.txt': book(['赵敏', '陈屿'], 'B'), '3_C.txt': book(['周芸', '韩冬'], 'C')}
    for n, t in texts.items():
        (books / n).write_text(t, encoding='utf-8')
    return books, texts


def _norm(t):
    return t.replace('\r\n', '\n').strip()


def test_folder_run_shows_each_files_source_and_keeps_the_users_choice(qt_app, world, monkeypatch):
    books, texts = _three_books(world)
    w = _new_window(world, monkeypatch)
    try:
        _open_folder(w, books)
        w.tabs.setCurrentWidget(w.preview_tab)
        assert w.preview.toPlainText() == '' and w.preview.isReadOnly()
        assert '폴더 번역' in w.preview_hint.text()
        seen = {}          # file -> what [원문] showed while that file was being translated
        modes = []
        pressed = {'done': False}
        orig = w.on_chunk_translated

        def chunk(*a, **k):
            r = orig(*a, **k)
            modes.append(w._preview_mode)
            name = w.source_path.name
            if not pressed['done'] and name == '1_A.txt' and len(modes) >= 2:
                assert w._preview_mode == 'result', 'the live translation comes up by itself once'
                w._show_preview('source'); pressed['done'] = True      # the user presses [원문]
            if w._preview_mode == 'source':
                seen.setdefault(name, w.preview.toPlainText())
                assert w.preview.isReadOnly()
                assert f'{w.batch_index + 1}/3' in w.preview_hint.text() and '번역 중인 파일' in w.preview_hint.text()
            return r
        monkeypatch.setattr(w, 'on_chunk_translated', chunk)
        world.run += 1; world.saved_before[world.run] = ''
        w.last_result_path = None; w.start_translation()
        deadline = time.time() + 60
        while time.time() < deadline and not _done(w):
            qt_app.processEvents(); time.sleep(0.005)
        for _ in range(30): qt_app.processEvents()
        assert _done(w), w.error_view.toPlainText()[-1500:]
        # After the user chose 원문 it stayed there for every later chunk and file ...
        first_source = modes.index('source')
        assert all(m == 'source' for m in modes[first_source:]), modes
        # ... and every file's own text was shown while it was translated.
        for name in ('1_A.txt', '2_B.txt', '3_C.txt'):
            assert _norm(seen[name]) == _norm(texts[name]), name
        # Display only: the folder was translated from the files, each once, no questions.
        assert w.input_mode == 'folder' and w.preview.isReadOnly()
        assert not world.asked, world.asked
        _no_double_translation(world)
        for name, t in texts.items():
            assert world.requests_for(name, 1), name
        assert '마지막으로 번역한 파일' in w.preview_hint.text()
        # The next [번역 시작 / 계속] is the folder again (finished files are not requested).
        before = len(world.translated)
        assert _run(qt_app, w) == 'done'
        assert len(world.translated) == before and w.input_mode == 'folder'
    finally:
        w.close()


def test_result_view_does_not_load_the_file_until_source_is_pressed(qt_app, world, monkeypatch):
    books, texts = _three_books(world)
    w = _new_window(world, monkeypatch)
    try:
        _open_folder(w, books)
        w.tabs.setCurrentWidget(w.preview_tab)
        assert _run(qt_app, w) == 'done'
        # The run ended on the live/total result; the window holds the result, not a source.
        assert w._preview_mode == 'result'
        assert _norm(w.preview.toPlainText()) != _norm(texts['3_C.txt'])
        w._show_preview('source')
        assert _norm(w.preview.toPlainText()) == _norm(texts['3_C.txt']) and w.preview.isReadOnly()
        w._show_preview('result'); w._show_preview('source')
        assert _norm(w.preview.toPlainText()) == _norm(texts['3_C.txt'])
    finally:
        w.close()


def test_choosing_a_folder_or_a_single_file_resets_the_window_and_hint(qt_app, world, monkeypatch):
    books, texts = _three_books(world)
    w = _new_window(world, monkeypatch)
    try:
        _open_folder(w, books)
        assert _run(qt_app, w) == 'done'
        # A single file afterwards: its text, editable, the usual hint.
        w._open_txt_path(str(books / '2_B.txt'))
        assert w.input_mode == 'single' and _norm(w.preview.toPlainText()) == _norm(texts['2_B.txt'])
        assert not w.preview.isReadOnly() and w.preview_hint.text() == w.PREVIEW_HINT
        w._show_preview('result') if w._result_text.strip() else None
        w._show_preview('source')
        assert _norm(w.preview.toPlainText()) == _norm(texts['2_B.txt'])
        # The folder again: an empty, read-only window until a run starts.
        _open_folder(w, books)
        assert w.preview.toPlainText() == '' and w.preview.isReadOnly() and '폴더 번역' in w.preview_hint.text()
    finally:
        w.close()


def test_file_relation_box_fits_its_longest_item(qt_app, world, monkeypatch):
    w = _new_window(world, monkeypatch)
    try:
        box = w.batch_relation
        fm = box.fontMetrics()
        longest = max(fm.horizontalAdvance(box.itemText(i)) for i in range(box.count()))
        assert box.minimumSizeHint().width() >= longest + 40
        assert box.sizeHint().width() >= longest + 40
        w.show(); qt_app.processEvents()
        assert box.width() >= longest + 40, (box.width(), longest)
    finally:
        w.close()
