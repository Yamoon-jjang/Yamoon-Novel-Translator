"""v2.5.5 보완: 마우스를 올렸을 때 나오는 긴 안내 문구를 읽기 좋은 폭에서 줄바꿈."""
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QHelpEvent
from PySide6.QtWidgets import QApplication, QPushButton, QTableWidget, QTableWidgetItem, QToolTip

from novel_translator.ui import tooltips as tt
from novel_translator.ui.tooltips import wrap_tooltip, _columns, MAX_COLUMNS

LONG = ('번역 결과를 야문 클린리더(문단 정리기)로 엽니다. 폴더 번역이면 결과 파일 전체를 일괄 변환 창으로 엽니다. '
        '클린리더가 없으면 내려받을 곳을 알려 줍니다 · 결과 파일은 그대로 둡니다.')


def test_long_line_is_wrapped_at_spaces_without_losing_text():
    out = wrap_tooltip(LONG)
    lines = out.split('\n')
    assert len(lines) >= 3 and all(_columns(l) <= MAX_COLUMNS + 4 for l in lines)
    assert ' '.join(lines).split() == LONG.split(), 'only spaces became line breaks'
    assert not any(l.startswith(('·', '→')) for l in lines)


def test_short_and_existing_breaks_are_kept():
    assert wrap_tooltip('최근 연 파일 (최대 20개)') == '최근 연 파일 (최대 20개)'
    two = '첫 줄입니다.\n둘째 줄입니다.'
    assert wrap_tooltip(two) == two
    mixed = '。→.  ，、→,  ！？：；→!?:;\n' + LONG
    out = wrap_tooltip(mixed)
    assert out.split('\n')[0] == '。→.  ，、→,  ！？：；→!?:;' and len(out.split('\n')) > 2


def test_long_path_without_spaces_is_cut():
    path = 'C:\\Users\\someone\\Documents\\' + 'novels\\' * 20 + 'book.ko.txt'
    lines = wrap_tooltip(path).split('\n')
    assert ''.join(lines) == path and all(_columns(l) <= MAX_COLUMNS for l in lines)


def _hover(qt_app, widget, pos=QPoint(5, 5)):
    QToolTip.hideText()
    ev = QHelpEvent(QEvent.ToolTip, pos, widget.mapToGlobal(pos))
    QApplication.sendEvent(widget, ev)
    qt_app.processEvents()
    return QToolTip.text()


def test_hovering_shows_wrapped_tooltip(qt_app):
    tt.install()
    btn = QPushButton('클린리더로 열기'); btn.setToolTip(LONG); btn.resize(200, 30); btn.show()
    try:
        shown = _hover(qt_app, btn)
        assert shown == wrap_tooltip(LONG) and '\n' in shown
        assert btn.toolTip() == LONG, 'the widget keeps its own text'
        btn.setToolTip('<b>준비 상태</b><br>' + LONG)          # HTML: Qt wraps it itself
        assert _hover(qt_app, btn) == btn.toolTip()
    finally:
        btn.close(); QToolTip.hideText()


def test_table_cell_tooltip_is_wrapped(qt_app):
    tt.install()
    table = QTableWidget(1, 1); item = QTableWidgetItem('左京'); item.setToolTip(LONG); table.setItem(0, 0, item)
    table.resize(300, 120); table.show(); qt_app.processEvents()
    try:
        pos = table.visualItemRect(item).center()
        assert _hover(qt_app, table.viewport(), pos) == wrap_tooltip(LONG)
    finally:
        table.close(); QToolTip.hideText()


import pytest


@pytest.fixture
def qt_app():
    return QApplication.instance() or QApplication([])
