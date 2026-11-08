"""v2.6.3: re-splitting a long book.

- A setting that changes neither the text nor its language (번역 방식, 고급 설정) reuses the
  last paragraph split instead of splitting the whole book again; the chunks are made anew
  each time, exactly as before.
- A new text (paste, another file, another encoding) or another language is split again.
- A long book shows "문단 나누는 중" and the wait cursor while it is split, and both are put
  back afterwards; nothing else runs meanwhile.
"""
from __future__ import annotations

from PySide6.QtWidgets import QApplication

from novel_translator.chunking import split_paragraphs, mark_translatable, make_chunks
from novel_translator.ui import main_window as ui

from test_alpha17 import qt_app, sandbox  # noqa: F401

BOOK = '\n\n'.join(f'第{i}段。他站在窗边看着外面的雨，心里很沉重。' for i in range(400))


def _open(root, w, text=BOOK, lang='zh-ko'):
    src = root / 'book.txt'; src.write_text(text, encoding='utf-8')
    w._open_txt_path(str(src)); w._apply_profile(lang)
    return src


def _counting(monkeypatch):
    calls = []
    real = ui.mark_translatable

    def counted(paras, lang, *a, **k):
        calls.append(lang); return real(paras, lang, *a, **k)
    monkeypatch.setattr(ui, 'mark_translatable', counted)
    return calls


def test_mode_change_reuses_the_split_and_makes_the_same_chunks(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = ui.MainWindow()
    try:
        _open(root, w)
        calls = _counting(monkeypatch)
        before = [c.source_text for c in w.chunks]
        for data in ('quality', 'speed', 'balanced'):
            w.mode.setCurrentIndex(w.mode.findData(data))
            ps = mark_translatable(split_paragraphs(w.source_text), 'zh')
            fresh = make_chunks(ps, 'zh', *w._mode_chunk_settings())
            assert [c.source_text for c in w.chunks] == [c.source_text for c in fresh]
            assert w.paragraphs == ps
        assert calls == []                       # no full split for a mode change
        assert [c.source_text for c in w.chunks] == before
    finally:
        w.close()


def test_a_new_text_or_language_is_split_again(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = ui.MainWindow()
    try:
        _open(root, w)
        calls = _counting(monkeypatch)
        w._apply_profile('ja-ko')                # another language
        assert calls and calls[-1] == 'ja'
        n = len(calls)
        w._apply_profile('zh-ko'); assert len(calls) == n + 1
        w.source_text = w.source_text + '\n\n新的一段。'; w._rechunk()   # another text
        assert len(calls) == n + 2 and w.paragraphs[-1].text == '新的一段。'
    finally:
        w.close()


def test_the_handed_out_list_is_a_copy(qt_app, sandbox):
    root, _ = sandbox
    w = ui.MainWindow()
    try:
        _open(root, w)
        n = len(w.paragraphs)
        w.paragraphs.append(w.paragraphs[0])     # whatever a caller does to its list ...
        w._rechunk()
        assert len(w.paragraphs) == n            # ... never reaches the next split
    finally:
        w.close()


def test_a_long_book_shows_the_split_and_puts_everything_back(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = ui.MainWindow()
    try:
        _open(root, w)
        seen = {}
        real = ui.split_paragraphs

        def watched(text):
            seen['status'] = w.status.text(); seen['cursor'] = QApplication.overrideCursor() is not None
            return real(text)
        monkeypatch.setattr(ui, 'split_paragraphs', watched)
        monkeypatch.setattr(ui.MainWindow, 'SPLIT_NOTICE_CHARS', 100)
        w.source_text = w.source_text + '\n\n又一段。'; w._rechunk()
        assert seen['status'].startswith('문단 나누는 중') and seen['cursor']
        assert QApplication.overrideCursor() is None
        assert not w.status.text().startswith('문단 나누는 중')
        # a small book: no notice at all
        seen.clear(); monkeypatch.setattr(ui.MainWindow, 'SPLIT_NOTICE_CHARS', 10**9)
        w.source_text = w.source_text + '\n\n再一段。'; w._rechunk()
        assert not seen['status'].startswith('문단 나누는 중') and not seen['cursor']
    finally:
        w.close()


def test_a_failed_split_puts_the_cursor_back(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = ui.MainWindow()
    try:
        _open(root, w)
        monkeypatch.setattr(ui.MainWindow, 'SPLIT_NOTICE_CHARS', 100)
        monkeypatch.setattr(ui, 'split_paragraphs', lambda t: (_ for _ in ()).throw(ValueError('x')))
        w.source_text = w.source_text + '\n\n又一段。'
        try:
            w._rechunk()
        except ValueError:
            pass
        assert QApplication.overrideCursor() is None
    finally:
        w.close()
