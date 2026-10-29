"""Asked once a long job has finished, only when the option in 고급 설정 is on: shut the PC
down in 60 seconds? [예] is the default and nobody answering means yes (the user is away),
[아니오] keeps the PC on. Never shown for a stopped or failed job."""
from __future__ import annotations
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout)
from ..config import CONFIG_DIR, load_json
from ..power import SHUTDOWN_COUNTDOWN_SECONDS

OPTION_KEY = 'confirm_shutdown_after_finish'


def shutdown_offer_enabled() -> bool:
    """The 고급 설정 option. Off on a new install and after 설정 초기화; once turned on it
    stays on (app.json) until it is turned off again."""
    try:
        return load_json(CONFIG_DIR/'app.json').get(OPTION_KEY, False) is True
    except Exception:
        return False


class ShutdownCountdownDialog(QDialog):
    def __init__(self, parent, title: str, summary: str, seconds: int = SHUTDOWN_COUNTDOWN_SECONDS):
        super().__init__(parent)
        self.setWindowTitle(title+' · PC 종료 확인')
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self._left = max(1, int(seconds))
        v = QVBoxLayout(self)
        body = QLabel(summary.strip())
        body.setWordWrap(True); body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(body)
        self.question = QLabel()
        self.question.setStyleSheet('font-weight:600;color:#b91c1c;')
        v.addWidget(self.question)
        note = QLabel('응답이 없으면 시간이 끝날 때 PC를 종료합니다. 결과는 이미 저장되어 있습니다.\n'
                      '다른 프로그램에 저장하지 않은 문서가 있으면 Windows가 저장 여부를 묻고 종료를 기다립니다.')
        note.setWordWrap(True); note.setStyleSheet('color:#475569;')
        v.addWidget(note)
        row = QHBoxLayout(); row.addStretch(1)
        self.yes = QPushButton('예 (지금 종료)'); self.no = QPushButton('아니오')
        self.yes.clicked.connect(self._answer_yes); self.no.clicked.connect(self._answer_no)
        row.addWidget(self.yes); row.addWidget(self.no); v.addLayout(row)
        self.no.setAutoDefault(False)
        self.yes.setAutoDefault(True); self.yes.setDefault(True); self.yes.setFocus()
        self.setMinimumWidth(460)
        self._timer = QTimer(self); self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._show_left()

    def _answer_yes(self):
        self.accept()

    def _answer_no(self):
        self.reject()

    def _show_left(self):
        self.question.setText(f'{self._left}초 후 PC를 종료할까요?')

    def _tick(self):
        self._left -= 1
        if self._left <= 0:
            self._timer.stop(); self.accept(); return
        self._show_left()

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()
        self.raise_(); self.activateWindow()
        try:
            QApplication.alert(self, 0)
        except Exception:
            pass

    def done(self, result):
        self._timer.stop()
        super().done(result)


def ask_shutdown(parent, title: str, summary: str) -> bool:
    """True when the PC should be shut down (예, or no answer within the countdown)."""
    return ShutdownCountdownDialog(parent, title, summary).exec() == QDialog.Accepted
