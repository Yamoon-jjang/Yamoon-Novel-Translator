"""Different originals that got the same Korean name: fix them, or keep them as they are."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QPushButton, QCheckBox, QMessageBox)


class NameCollisionDialog(QDialog):
    """groups: [(korean, [source, ...])]. counts: source -> times it occurs in the book.
    `timeout` (seconds, 0 = none) keeps everything as it is when nobody answers, so a
    translation started before leaving the PC never waits for hours."""

    def __init__(self, groups, counts=None, speech_names=(), timeout=0, parent=None, suggestions=None):
        super().__init__(parent)
        self.setWindowTitle('한글 이름 겹침 확인')
        self._groups = [(str(k), [str(s) for s in srcs]) for k, srcs in groups]
        self._rows = []            # (source, korean) per table row
        self._left = int(timeout or 0)
        self.changes = {}
        root = QVBoxLayout(self)
        shown = ' · '.join(f"{'·'.join(s)} → {k}" for k, s in self._groups[:3]) + (' …' if len(self._groups) > 3 else '')
        note = QLabel(f'원문은 다른데 한글 이름이 같은 항목이 {len(self._groups)}건 있습니다: {shown}\n\n'
                      '이대로 번역하면 서로 다른 인물이 한 사람처럼 옮겨집니다. 다른 인물이면 [고칠 한글 이름] 칸을 '
                      '두 번 눌러 서로 다르게 고쳐 주세요. 같은 인물(별명·다른 표기)이면 그대로 두셔도 됩니다.')
        note.setWordWrap(True); root.addWidget(note)
        suggestions = {str(k): str(v) for k, v in (suggestions or {}).items()}
        if suggestions:
            tip = QLabel('철자에 맞춰 구별되는 한글 이름을 [고칠 한글 이름] 칸에 미리 채워 두었습니다(굵은 글씨). '
                         '맞으면 [고친 대로 적용]만 누르세요. 같은 인물이면 [그대로 두기]를 누르세요.')
            tip.setWordWrap(True); tip.setObjectName('collisionSuggestNote'); root.addWidget(tip)
        total = sum(len(s) for _, s in self._groups)
        self.table = QTableWidget(total, 4, self)
        self.table.setHorizontalHeaderLabels(['겹친 한글', '원문', '본문 등장', '고칠 한글 이름'])
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        for col, mode in ((0, QHeaderView.ResizeToContents), (1, QHeaderView.Stretch),
                          (2, QHeaderView.ResizeToContents), (3, QHeaderView.Stretch)):
            header.setSectionResizeMode(col, mode)
        counts = dict(counts or {})
        row = 0
        for g, (ko, sources) in enumerate(self._groups):
            shade = QColor(0, 0, 0, 0) if g % 2 == 0 else QColor(127, 127, 127, 28)
            for i, src in enumerate(sources):
                values = (ko if i == 0 else '', src, f'{counts[src]:,}회' if src in counts else '')
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable); item.setBackground(shade)
                    if col == 2: item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(row, col, item)
                edit = QTableWidgetItem(suggestions.get(src, ko)); edit.setBackground(shade)
                edit.setToolTip('두 번 눌러 고칩니다')
                if src in suggestions:
                    f = edit.font(); f.setBold(True); edit.setFont(f)
                    edit.setToolTip(f'추천: {suggestions[src]} (원래 {ko}) · 두 번 눌러 고칩니다')
                self.table.setItem(row, 3, edit)
                self._rows.append((src, ko)); row += 1
        if total: self.table.setCurrentCell(0, 3)
        self.table.itemChanged.connect(lambda _i: self._stop_countdown())
        self.table.itemDoubleClicked.connect(lambda _i: self._stop_countdown())
        root.addWidget(self.table, 1)
        self.rebuild = QCheckBox('인물 말투표도 고친 이름으로 새로 만들기')
        self.rebuild.setToolTip('지금 말투표에는 겹친 이름이 한 사람 몫으로 들어 있어, 누구의 말투인지 다시 분석해야 합니다.')
        self.rebuild.setChecked(True)
        self.rebuild_offered = any(k in set(speech_names) for k, _ in self._groups)
        self.rebuild.setVisible(self.rebuild_offered)
        root.addWidget(self.rebuild)
        self.countdown = QLabel(''); self.countdown.setVisible(self._left > 0)
        root.addWidget(self.countdown)
        buttons = QHBoxLayout(); buttons.addStretch()
        keep = QPushButton('그대로 두기'); fix = QPushButton('고친 대로 적용')
        fix.setDefault(True)
        keep.clicked.connect(self.reject); fix.clicked.connect(self.accept)
        buttons.addWidget(keep); buttons.addWidget(fix)
        root.addLayout(buttons)
        self._timer = QTimer(self); self._timer.setInterval(1000); self._timer.timeout.connect(self._tick)
        if self._left > 0:
            self._show_left(); self._timer.start()
        height = max(300, min(640, 250 + total * 30))
        self.resize(760, height)

    # -------------------------------------------------------------- countdown
    def _show_left(self):
        m, s = divmod(max(0, self._left), 60)
        self.countdown.setText(f'{m}:{s:02d} 동안 고르지 않으면 그대로 두고 번역을 계속합니다.')

    def _tick(self):
        self._left -= 1
        if self._left <= 0:
            self._timer.stop(); self.timed_out = True; self.reject(); return
        self._show_left()

    def _stop_countdown(self):
        if self._timer.isActive():
            self._timer.stop(); self.countdown.setText('직접 고르는 중 · 시간 제한 없음')

    timed_out = False

    # -------------------------------------------------------------- result
    def accept(self):
        from ..term_extraction import valid_rendering
        self.table.setCurrentItem(None)          # an open cell editor commits first
        changes, bad = {}, []
        for row, (src, ko) in enumerate(self._rows):
            item = self.table.item(row, 3)
            new = (item.text() if item else '').strip()
            if not new:
                continue                         # an emptied cell keeps the name
            if not valid_rendering(src, new):
                bad.append(f'{src} → {new}'); continue
            if new != ko:
                changes[src] = new
        if bad:
            QMessageBox.warning(self, '한글 이름 확인', '한자·가나가 남았거나, 한글과 영문이 섞였거나(엠ily), 원문과 같은 이름은 쓸 수 없습니다:\n\n' + '\n'.join(bad[:8]))
            return
        self.changes = changes
        self._timer.stop()
        super().accept()

    def reject(self):
        self._timer.stop()
        super().reject()
