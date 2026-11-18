"""A completion notice that closes by itself (v2.6.7).

Used only when 번역이 끝나면 재정비 자동 시작 is on, for the notices that can still appear
(the translation finished but the refinement could not start): nobody may be at the PC,
so the window must not wait for a click. Its text stays in the work log either way."""
from __future__ import annotations
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

AUTO_CLOSE_SECONDS = 30


def show_timed_information(parent, title: str, text: str, seconds: int = AUTO_CLOSE_SECONDS) -> None:
    box = QMessageBox(QMessageBox.Information, title, text, QMessageBox.Ok, parent)
    ok = box.button(QMessageBox.Ok)
    left = {'n': max(1, int(seconds))}

    def show_left():
        ok.setText(f'확인 ({left["n"]}초 뒤 자동으로 닫힘)')

    def tick():
        left['n'] -= 1
        if left['n'] <= 0:
            timer.stop(); box.accept(); return
        show_left()

    timer = QTimer(box); timer.setInterval(1000); timer.timeout.connect(tick)
    show_left(); timer.start()
    try:
        box.exec()
    finally:
        timer.stop()
