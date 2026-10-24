"""정보 (About) dialog and the app icon, drawn in code (no image files).

Layout and type sizes follow 야문 클린리더's About window exactly (480×430 logical
pixels, 118 px header band, 64 px icon, 맑은 고딕: title 16 pt bold, body 9 pt,
small print 8.5 pt, 96×34 확인 button) so the two programs look the same.
"""
from __future__ import annotations
from PySide6.QtCore import Qt, QRectF, QPointF, QRect
from PySide6.QtGui import QPainter, QPixmap, QIcon, QColor, QPen, QFont
from PySide6.QtWidgets import QDialog, QPushButton
from .. import APP_NAME, VERSION_LABEL, RELEASE_DATE
from ..build_identity import build_display, build_detail

BLUE = QColor('#2f5aa8')
WIN, PANEL, PANEL_LINE = QColor(255, 255, 255), QColor(247, 249, 252), QColor(220, 226, 235)
TEXT, SUB, FAINT = QColor(28, 33, 43), QColor(70, 78, 92), QColor(108, 116, 130)
FAMILY = ['Malgun Gothic', '맑은 고딕', 'Apple SD Gothic Neo', 'Noto Sans CJK KR', 'Noto Sans KR']

W, H = 480, 460
BAND = 118
TITLE_PT, UI_PT, SMALL_PT, BTN_PT = 16.0, 9.0, 8.5, 9.0

DESCRIPTION = ('중국어·일본어·영어 소설과 문서를 꾸미거나 빠뜨리지 않고 원문 그대로 한국어로 옮기는 로컬 LLM 중심 '
               '번역 프로그램입니다. 막힌 문단은 엔진 순서대로 이어 번역하고, 인명·용어와 말투를 책 전체에서 같게 유지합니다.')
NOTICE = ("이 프로그램은 누구나 무료로 사용할 수 있습니다. 원본 파일은 수정하지 않고 '.ko.txt'로 따로 저장하지만, "
          "중요한 파일은 미리 백업해 두시기 바랍니다. 이 프로그램은 있는 그대로 제공되며, 사용으로 인해 생긴 문제에 대해 "
          "제작자는 책임지지 않습니다.")
COPYRIGHT = 'Copyright © 2026 야문 짱. MIT License.'


def _font(pt: float, bold: bool = False) -> QFont:
    f = QFont(); f.setFamilies(FAMILY); f.setPointSizeF(pt); f.setBold(bold)
    return f


def app_pixmap(size: int = 128) -> QPixmap:
    """A page with text lines (same family as 야문 클린리더) and a '가' badge for Korean."""
    pm = QPixmap(size, size); pm.fill(Qt.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)
    s = size / 128.0
    if size < 32:
        # Small sizes (title bar, taskbar): bolder strokes, three lines, badge without text.
        pen = QPen(BLUE, max(1.5, 10 * s)); pen.setJoinStyle(Qt.RoundJoin); p.setPen(pen); p.setBrush(Qt.white)
        p.drawRoundedRect(QRectF(18 * s, 8 * s, 80 * s, 108 * s), 12 * s, 12 * s)
        p.setPen(Qt.NoPen); p.setBrush(BLUE)
        for i, w in enumerate((56, 56, 40)):
            p.drawRect(QRectF(34 * s, (32 + i * 22) * s, w * s, 11 * s))
        p.setBrush(QColor('#2563eb')); p.setPen(QPen(Qt.white, max(1.0, 6 * s)))
        p.drawEllipse(QPointF(94 * s, 94 * s), 30 * s, 30 * s)
        p.end()
        return pm
    pen = QPen(BLUE, 7 * s); pen.setJoinStyle(Qt.RoundJoin); p.setPen(pen); p.setBrush(Qt.white)
    p.drawRoundedRect(QRectF(20 * s, 10 * s, 76 * s, 104 * s), 10 * s, 10 * s)
    p.setPen(Qt.NoPen); p.setBrush(BLUE)
    for i, w in enumerate((52, 52, 40, 52, 30)):
        p.drawRoundedRect(QRectF(33 * s, (30 + i * 15) * s, w * s, 6 * s), 3 * s, 3 * s)
    p.setBrush(QColor('#2563eb')); p.setPen(QPen(Qt.white, 4 * s))
    p.drawEllipse(QPointF(92 * s, 92 * s), 27 * s, 27 * s)
    f = QFont(); f.setFamilies(FAMILY); f.setBold(True); f.setPixelSize(max(6, int(30 * s))); p.setFont(f); p.setPen(Qt.white)
    p.drawText(QRectF(65 * s, 65 * s, 54 * s, 54 * s), Qt.AlignCenter, '가')
    p.end()
    return pm


def app_icon() -> QIcon:
    icon = QIcon()
    for n in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(app_pixmap(n))
    return icon


class AboutDialog(QDialog):
    """Painted like 야문 클린리더's About window (same coordinates and type sizes)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'{APP_NAME} 정보')
        self.setWindowIcon(app_icon())
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setFixedSize(W, H)
        self._title_font, self._ui_font, self._small_font = _font(TITLE_PT, True), _font(UI_PT), _font(SMALL_PT)
        self._icon = app_pixmap(64)
        self._build_display = build_display()
        self._build_detail = build_detail()
        self.texts = [APP_NAME, f'버전 {VERSION_LABEL[1:]}   ·   {RELEASE_DATE}', 'Windows 10 이상  ·  설치 없이 실행',
                      DESCRIPTION, '제작', '야문 짱  &  Claude  (협동작)', '버전', f'{VERSION_LABEL[1:]}  ({RELEASE_DATE})',
                      '라이선스', '무료 프로그램', NOTICE, COPYRIGHT]
        self.setToolTip(self._build_detail)
        ok = QPushButton('확인', self); ok.setDefault(True); ok.setFont(_font(BTN_PT, True))
        ok.setGeometry(W - 24 - 96, H - 20 - 34, 96, 34)
        ok.setStyleSheet('QPushButton{background:#2563eb;color:white;border:none;border-radius:6px;}'
                         'QPushButton:hover{background:#1d4ed8;}QPushButton:pressed{background:#1e40af;}')
        ok.clicked.connect(self._close_dialog)

    def _close_dialog(self):
        self.accept()

    def paintEvent(self, _event):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing); p.setRenderHint(QPainter.TextAntialiasing)
        p.fillRect(self.rect(), WIN)
        p.fillRect(QRect(0, 0, W, BAND), PANEL)
        p.fillRect(QRect(0, BAND, W, 1), PANEL_LINE)
        p.drawPixmap(28, 27, self._icon)
        x = 110
        self._text(p, QRect(x, 30, W - x - 20, 32), self.texts[0], self._title_font, TEXT)
        self._text(p, QRect(x, 64, W - x - 20, 22), self.texts[1], self._ui_font, SUB)
        self._text(p, QRect(x, 84, W - x - 20, 20), self.texts[2], self._ui_font, FAINT)
        lx, rw = 28, W - 56
        y = 138
        y = self._para(p, lx, y, rw, DESCRIPTION, self._ui_font, TEXT) + 14
        row = 22   # same row pitch as 야문 클린리더
        for k, v in (('제작', self.texts[5]), ('버전', self.texts[7]), ('빌드', self._build_display), ('라이선스', self.texts[9])):
            self._text(p, QRect(lx, y, 70, row), k, self._ui_font, FAINT)
            self._text(p, QRect(lx + 65, y, rw - 65, row), v, self._ui_font, TEXT)
            y += row
        y += 8
        p.fillRect(QRect(lx, y, rw, 1), PANEL_LINE); y += 12
        y = self._para(p, lx, y, rw, NOTICE, self._small_font, SUB) + 10
        self._para(p, lx, y, rw, COPYRIGHT, self._small_font, SUB)
        p.end()

    @staticmethod
    def _text(p, rect, text, font, color):
        p.setFont(font); p.setPen(color)
        p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine, text)

    @staticmethod
    def _para(p, x, y, w, text, font, color) -> int:
        p.setFont(font); p.setPen(color)
        r = p.boundingRect(QRect(x, y, w, 2000), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, text)
        p.drawText(QRect(x, y, w, r.height()), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, text)
        return y + r.height()
