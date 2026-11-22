"""v2.6.10: 확정 용어집 · 인물 말투 tabs drew their buttons over the table in a small window.

The tab area has a fixed minimum height (260) smaller than these pages need (help text,
table, button rows). Qt then squeezed the page below its own minimum and placed the widgets
on top of each other. Each page now sits in a scroll area (like 언어별 설정): a small window
scrolls instead of overlapping, a big one looks exactly as before (no scroll bar, the table
grows with the window)."""
import json
import shutil
from unittest.mock import patch

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QScrollArea

from novel_translator import translation_settings as settings
from novel_translator.ui import advanced_dialog as advanced
from novel_translator.ui import main_window as ui


@pytest.fixture(scope='module')
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, tmp_path):
    cfg = tmp_path / 'config'; shutil.copytree(ui.CONFIG_DIR, cfg)
    app_cfg = json.loads((cfg / 'app.json').read_text(encoding='utf-8')); app_cfg['auto_extract_terms'] = False
    (cfg / 'app.json').write_text(json.dumps(app_cfg, ensure_ascii=False), encoding='utf-8')
    with patch.object(ui, 'CONFIG_DIR', cfg), patch.object(settings, 'CONFIG_DIR', cfg), \
         patch.object(advanced, 'CONFIG_DIR', cfg), patch.object(ui, 'DATA_DIR', tmp_path / 'data'):
        w = ui.MainWindow(); w.show(); qt_app.processEvents()
        try:
            yield w
        finally:
            w.close()


def _span(widget, page):
    top = widget.mapTo(page, QPoint(0, 0)).y()
    return top, top + widget.height()


def _rows(w, which):
    if which == 'terms':
        return w.terms_tab, [w.name_option, w.term_table, w.term_add, w.use_common]
    return w.speech_tab, [w.speech_table, w.speech_add]


def _no_overlap(page, widgets):
    spans = [_span(x, page) for x in widgets]
    for (_, bottom), (top, _) in zip(spans, spans[1:]):
        assert bottom <= top, spans


@pytest.mark.parametrize('which', ['terms', 'speech'])
def test_small_window_scrolls_instead_of_drawing_buttons_over_the_table(qt_app, window, which):
    w = window
    tab, widgets = _rows(w, which)
    assert isinstance(tab, QScrollArea) and w.tabs.indexOf(tab) >= 0
    w.tabs.setCurrentWidget(tab)
    w.resize(1000, 300); qt_app.processEvents()           # as small as the window goes
    page = tab.widget()
    _no_overlap(page, widgets)
    for x in widgets:
        assert x.height() > 0 and x.isVisible()
    if tab.viewport().height() < page.minimumSizeHint().height():
        assert tab.verticalScrollBar().maximum() > 0          # reachable by scrolling


@pytest.mark.parametrize('which', ['terms', 'speech'])
def test_big_window_looks_as_before(qt_app, window, which):
    w = window
    tab, widgets = _rows(w, which)
    w.tabs.setCurrentWidget(tab)
    w.resize(1200, 1400); qt_app.processEvents()
    table = widgets[0] if which == 'speech' else widgets[1]
    small = table.height()
    w.resize(1200, 1700); qt_app.processEvents()
    assert table.height() > small                              # the table still grows
    assert tab.verticalScrollBar().maximum() == 0             # no scroll bar when it fits
    _no_overlap(tab.widget(), widgets)


def test_tab_titles_and_jumps_still_find_the_tabs(qt_app, window):
    w = window
    w._add_term_row(); qt_app.processEvents(); w._update_terms_tab_title()
    assert w.tabs.tabText(w.tabs.indexOf(w.terms_tab)).startswith('확정 용어집')
    w._add_speech_row({'speaker': '가', 'listener': '나', 'level': '반말'}); w._update_speech_tab_title()
    assert w.tabs.tabText(w.tabs.indexOf(w.speech_tab)) == '인물 말투 (1)'
    w.tabs.setCurrentIndex(0); w.tabs.setCurrentWidget(w.terms_tab)
    assert w.tabs.currentWidget() is w.terms_tab and w.terms_tab.widget().isAncestorOf(w.term_table)
    assert w.speech_tab.widget().isAncestorOf(w.speech_table)
