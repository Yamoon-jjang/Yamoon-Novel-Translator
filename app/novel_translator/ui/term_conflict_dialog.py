"""Bounded, scrollable per-term review of conflicting extraction proposals."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QTableWidget,
    QTableWidgetItem,QHeaderView,QPushButton,QDialogButtonBox,QLineEdit)
from .table_combo import TableComboBox


class TermConflictDialog(QDialog):
    def __init__(self, conflicts, parent=None):
        super().__init__(parent)
        self.setWindowTitle('확정 용어 충돌 일괄 검토')
        self.resize(900,520)
        self._conflicts=list(conflicts)
        root=QVBoxLayout(self)
        note=QLabel(f'다른 표기 제안 {len(conflicts)}건 · 기본은 기존 표기 유지·고정입니다. 변경할 항목만 선택하세요.')
        note.setWordWrap(True);root.addWidget(note)
        search=QLineEdit();search.setPlaceholderText('원문 · 기존 표기 · 새 제안 검색')
        root.addWidget(search)
        self.table=QTableWidget(len(conflicts),4,self)
        self.table.setHorizontalHeaderLabels(['원문','기존 확정 표기','새 분석 제안','결정'])
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setHorizontalScrollMode(QTableWidget.ScrollPerPixel)
        self.table.setVerticalScrollMode(QTableWidget.ScrollPerPixel)
        header=self.table.horizontalHeader()
        header.setSectionResizeMode(0,QHeaderView.Stretch)
        header.setSectionResizeMode(1,QHeaderView.Stretch)
        header.setSectionResizeMode(2,QHeaderView.Stretch)
        header.setSectionResizeMode(3,QHeaderView.Fixed)
        # Keep the decision menu comfortably readable instead of allocating the
        # entire wide desktop equally to four short-text columns.
        header.resizeSection(3,205)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        for row,(src,old,new) in enumerate(conflicts):
            for col,value in enumerate((src,old,new)):
                item=QTableWidgetItem(value);item.setFlags(Qt.ItemIsEnabled|Qt.ItemIsSelectable)
                item.setToolTip(value);self.table.setItem(row,col,item)
            choose=TableComboBox(self.table)
            choose.addItem('기존 유지·고정','keep')
            choose.addItem('새 제안·고정','replace')
            self.table.setCellWidget(row,3,choose)
        root.addWidget(self.table,1)
        search.textChanged.connect(self._filter)
        actions=QHBoxLayout()
        keep=QPushButton('전체 기존 유지')
        change=QPushButton('전체 새 제안')
        keep.clicked.connect(lambda:self._all('keep'))
        change.clicked.connect(lambda:self._all('replace'))
        actions.addWidget(keep);actions.addWidget(change);actions.addStretch()
        root.addLayout(actions)
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('선택 사항 적용')
        buttons.button(QDialogButtonBox.Cancel).setText('취소')
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        # Keep a short conflict list compact; large lists gain table scrolling.
        row_height=max(28,self.table.verticalHeader().defaultSectionSize())
        desired_height=max(290,min(720,220+min(len(conflicts),12)*row_height))
        screen=self.screen()
        if screen:
            available=screen.availableGeometry()
            self.setMaximumSize(max(1,min(1020,available.width()-24)),max(1,available.height()-24))
            self.resize(min(900,self.maximumWidth()),min(desired_height,self.maximumHeight()))
        else:
            self.setMaximumWidth(1020)
            self.resize(900,desired_height)

    def accept(self):
        super().accept()

    def reject(self):
        super().reject()

    def _filter(self,needle):
        text=needle.strip().casefold()
        for row,values in enumerate(self._conflicts):
            self.table.setRowHidden(row,bool(text and not any(text in value.casefold() for value in values)))

    def _all(self,choice):
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                combo=self.table.cellWidget(row,3)
                combo.setCurrentIndex(combo.findData(choice))

    def decisions(self):
        return {src:(new if self.table.cellWidget(i,3).currentData()=='replace' else old)
                for i,(src,old,new) in enumerate(self._conflicts)}
