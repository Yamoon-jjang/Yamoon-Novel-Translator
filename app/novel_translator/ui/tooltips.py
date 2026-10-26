"""Readable tooltips: long one-line hover texts are wrapped at a comfortable width.

Qt shows a plain-text tooltip as one line as long as the text, so a long help text ran
across the whole screen. One application-wide filter wraps every plain tooltip when it is
shown (buttons, boxes, labels, table cells, list items in a drop-down), including tooltips
that change while the program runs. The texts themselves are not changed; line breaks the
text already has are kept, and rich-text (HTML) tooltips are left to Qt.
"""
from __future__ import annotations

import unicodedata

from PySide6.QtCore import QEvent, QObject, QRect, Qt
from PySide6.QtGui import Qt as _GuiQt   # mightBeRichText lives in QtGui's Qt namespace
from PySide6.QtWidgets import QAbstractItemView, QApplication, QToolTip, QWidget

# Width of one tooltip line in display columns (a Hangul/CJK character is two columns):
# about 42 Korean characters.
MAX_COLUMNS = 84

# Short marks that read badly at the start of a line: kept at the end of the line before.
_KEEP_WITH_PREVIOUS = {'·', '→', '-', '—', '–', '/', ':', ')', '」', '』', '”', '’', ',', '.', '…'}


def _columns(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1 for ch in text)


def _hard_split(word: str, limit: int) -> list[str]:
    """A word longer than a line (a long path or URL) cut into line-sized pieces."""
    parts, cur, width = [], '', 0
    for ch in word:
        w = _columns(ch)
        if cur and width + w > limit:
            parts.append(cur); cur, width = '', 0
        cur += ch; width += w
    if cur:
        parts.append(cur)
    return parts


def _wrap_line(line: str, limit: int) -> list[str]:
    width = _columns(line)
    if width <= limit:
        return [line]
    # Even lines: with the fewest lines that fit, aim each at the same length, so a long
    # sentence does not end in a lone word ("적용됩니다.") on its own line.
    count = -(-width // limit)
    even = _greedy(line, min(limit, -(-width // count) + 6))
    return even if len(even) <= count else _greedy(line, limit)


def _greedy(line: str, limit: int) -> list[str]:
    lines: list[str] = []
    cur = ''
    for word in line.split(' '):
        candidate = f'{cur} {word}' if cur else word
        if not cur or _columns(candidate) <= limit or word in _KEEP_WITH_PREVIOUS:
            cur = candidate
            continue
        lines.append(cur.rstrip())
        cur = word
    if cur:
        lines.append(cur.rstrip())
    out: list[str] = []
    for ln in lines:
        out.extend(_hard_split(ln, limit) if _columns(ln) > limit + 4 else [ln])
    return out


def wrap_tooltip(text: str, limit: int = MAX_COLUMNS) -> str:
    """The tooltip text with long lines broken at spaces; existing line breaks are kept."""
    if not text:
        return text
    return '\n'.join(w for line in str(text).split('\n') for w in _wrap_line(line, limit))


class TooltipWrapper(QObject):
    """Application-wide event filter that shows plain tooltips wrapped."""

    @staticmethod
    def _text_for(obj: QWidget, event) -> str:
        parent = obj.parentWidget()
        if isinstance(parent, QAbstractItemView) and obj is parent.viewport():
            index = parent.indexAt(event.pos())
            return str(index.data(Qt.ToolTipRole) or '') if index.isValid() else ''
        return obj.toolTip()

    def eventFilter(self, obj, event):   # noqa: N802 (Qt name)
        if event.type() != QEvent.ToolTip or not isinstance(obj, QWidget):
            return False
        try:
            text = self._text_for(obj, event)
            if not text or _GuiQt.mightBeRichText(text):
                return False             # nothing to show here / HTML: Qt's own handling
            wrapped = wrap_tooltip(text)
            if wrapped == text:
                return False             # already short: exactly as before
            QToolTip.showText(event.globalPos(), wrapped, obj, QRect(), obj.toolTipDuration())
            return True
        except Exception:
            return False                 # never let a tooltip break the window


_installed: TooltipWrapper | None = None


def install() -> None:
    """Install the wrapper once for the running application (safe to call again)."""
    global _installed
    app = QApplication.instance()
    if app is None or _installed is not None:
        return
    _installed = TooltipWrapper(app)
    app.installEventFilter(_installed)
