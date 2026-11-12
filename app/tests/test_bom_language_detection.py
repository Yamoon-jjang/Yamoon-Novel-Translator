"""BOM files over 256KB: the language is judged on opening.

A file is judged by its first 256KB when it is opened. A UTF-8 file without BOM already
tolerated a character cut in half at that point; a UTF-8 BOM file did not, so a BOM file
over 256KB (a TXT saved by 야문 클린리더, for example) was often not judged at all: it
opened under the previous language and the start of the translation then warned that the
source looks like another language. UTF-16 BOM files get the same tolerance."""
from __future__ import annotations

from novel_translator.language_detection import detect_language_bytes
from test_alpha17 import _window, qt_app, sandbox  # noqa: F401 (pytest fixtures)

JA = '「沙耶、こっちへ来て」\n\n彼女は静かに窓の外の雨を見つめていた。長い間、何も言わなかった。\n\n'
ZH = '第一章\n\n　　他静静地看着窗外的雨，很久没有说话。“好吧。”她轻轻叹了口气。\n\n'
EN = 'Chapter 1\n\nHe looked out of the window at the rain for a long time without speaking. "Fine," she sighed.\n\n'
CUT = 262144


def _cut_mid_character(raw: bytes) -> bytes:
    """The same UTF-8 BOM file, made so its first 256KB end in the middle of a character."""
    while True:
        try:
            raw[:CUT].decode('utf-8-sig')
        except UnicodeDecodeError:
            return raw
        raw = raw[:3] + b' ' + raw[3:]


def _cuts(raw):
    return {detect_language_bytes(raw[:c])[0] for c in range(CUT - 4, CUT + 8)}


def test_utf8_bom_file_over_256kb_is_judged_at_every_cut():
    for lang, text in (('ja', JA), ('zh', ZH), ('en', EN)):
        raw = (text * 6000).encode('utf-8-sig')
        assert len(raw) > CUT
        assert _cuts(raw) == {lang}, lang


def test_utf16_bom_file_over_256kb_is_judged_at_every_cut():
    for lang, text in (('ja', JA), ('zh', ZH), ('en', EN)):
        for enc in ('utf-16', 'utf-16-be'):
            raw = (text * 6000).encode(enc)
            if enc == 'utf-16-be':
                raw = b'\xfe\xff' + raw
            assert len(raw) > CUT
            assert _cuts(raw) == {lang}, (lang, enc)


def test_whole_files_are_judged_as_before():
    for lang, text in (('ja', JA), ('zh', ZH), ('en', EN)):
        for enc, label in (('utf-8-sig', 'UTF-8 BOM'), ('utf-8', 'UTF-8')):
            got = detect_language_bytes((text * 50).encode(enc))
            assert got[0] == lang and got[2] == label, (lang, enc, got)
    assert detect_language_bytes(b'\xef\xbb\xbf') == (None, 0.0, None)


def test_damage_inside_a_bom_file_is_not_hidden():
    """Only the end may be cut: a BOM file broken in the middle is still not read as UTF-8."""
    raw = (JA * 50).encode('utf-8-sig')
    broken = raw[:2000] + b'\xff\xfe\xfd' + raw[2000:]
    assert detect_language_bytes(broken)[2] != 'UTF-8 BOM'


def test_opening_a_big_bom_file_switches_the_language(qt_app, sandbox, monkeypatch):
    from novel_translator.ui import main_window as ui
    root, _ = sandbox
    zh = root / 'zh.txt'; zh.write_text(ZH * 30, encoding='utf-8')
    book = root / 'ja_bom.txt'; book.write_bytes(_cut_mid_character((JA * 6000).encode('utf-8-sig')))
    assert book.stat().st_size > CUT
    asked = []
    monkeypatch.setattr(ui.QMessageBox, 'question', lambda *a, **k: (asked.append(a[1]), ui.QMessageBox.No)[1])
    w = _window(monkeypatch)
    try:
        w._open_txt_path(str(zh)); assert w.pm.active.id == 'zh-ko'
        w._open_txt_path(str(book))
        assert w.pm.active.id == 'ja-ko' and w.lang.currentData() == 'ja-ko'
        assert w._language_mismatch_warning() and not asked
    finally:
        w.close(); w.deleteLater()


def test_the_mouse_wheel_never_changes_the_source_language(qt_app, sandbox, monkeypatch):
    """원문 언어 is chosen by clicking only. The wheel passing over it while scrolling changed
    it unnoticed (and a language changed there is remembered as the book's choice); opening
    a file of another language still switches it automatically."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    from novel_translator.ui import main_window as ui
    root, _ = sandbox
    zh = root / 'zh.txt'; zh.write_text(ZH * 30, encoding='utf-8')
    ja = root / 'ja.txt'; ja.write_text(JA * 30, encoding='utf-8')
    monkeypatch.setattr(ui.QMessageBox, 'question', lambda *a, **k: ui.QMessageBox.No)
    w = _window(monkeypatch)
    try:
        w.show(); w._open_txt_path(str(zh)); assert w.lang.currentData() == 'zh-ko'
        w.lang.setFocus()
        for dy in (-120, 120, -120, -120):
            ev = QWheelEvent(QPointF(5, 5), QPointF(w.lang.mapToGlobal(QPoint(5, 5))), QPoint(0, 0), QPoint(0, dy),
                             Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
            QApplication.sendEvent(w.lang, ev)
        assert w.lang.currentData() == 'zh-ko' and w.pm.active.id == 'zh-ko'
        assert not w._lang_chosen, 'the wheel never records a language choice for the book'
        w._open_txt_path(str(ja))
        assert w.lang.currentData() == 'ja-ko' and w.pm.active.id == 'ja-ko'
        w._open_txt_path(str(zh))
        assert w.lang.currentData() == 'zh-ko' and w.pm.active.id == 'zh-ko'
        # a click choice still works and is remembered as before
        w.lang.setCurrentIndex(w.lang.findData('ja-ko'))
        assert w.pm.active.id == 'ja-ko' and w._lang_chosen
    finally:
        w.close(); w.deleteLater()
