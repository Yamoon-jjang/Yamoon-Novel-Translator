"""A choice box placed inside a table cell (인물 말투표 말투 칸, API 키 등급, 용어 충돌 선택)."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox


class TableComboBox(QComboBox):
    """The mouse wheel never changes the choice: scrolling over the table would otherwise
    switch 반말 → 해요체 by accident. The wheel scrolls the table instead; the choice is
    changed only by clicking the box and picking from the list (or with the keyboard)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        event.ignore()   # handed on to the table, which scrolls
