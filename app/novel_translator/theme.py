"""Always use the light theme, whatever the Windows dark-mode setting.

The UI mixes the system palette with hand-set light colours (white panels, light
buttons). Under Windows dark mode Qt switches the palette to dark, which gave
white text on white panels and black text boxes. The app therefore pins a light
palette at start-up.
"""
from __future__ import annotations
import os, sys
from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette, QColor


def prepare_environment() -> None:
    """Before QApplication exists: ask Qt's Windows platform plugin not to follow dark mode."""
    if sys.platform == 'win32' and not os.environ.get('QT_QPA_PLATFORM'):
        os.environ['QT_QPA_PLATFORM'] = 'windows:darkmode=0'


def light_palette() -> QPalette:
    pal = QPalette()
    colours = {
        QPalette.Window: '#f3f4f6', QPalette.WindowText: '#111827',
        QPalette.Base: '#ffffff', QPalette.AlternateBase: '#f7f8fa',
        QPalette.Text: '#111827', QPalette.PlaceholderText: '#6b7280',
        QPalette.Button: '#f7f8fa', QPalette.ButtonText: '#111827', QPalette.BrightText: '#ffffff',
        QPalette.Light: '#ffffff', QPalette.Midlight: '#e5e7eb', QPalette.Mid: '#d1d5db',
        QPalette.Dark: '#9ca3af', QPalette.Shadow: '#6b7280',
        QPalette.Highlight: '#2563eb', QPalette.HighlightedText: '#ffffff',
        QPalette.Link: '#2563eb', QPalette.LinkVisited: '#7c3aed',
        QPalette.ToolTipBase: '#ffffff', QPalette.ToolTipText: '#111827',
    }
    for role, col in colours.items():
        pal.setColor(QPalette.All, role, QColor(col))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor('#9ca3af'))
    pal.setColor(QPalette.Disabled, QPalette.Base, QColor('#f3f4f6'))
    pal.setColor(QPalette.Disabled, QPalette.Button, QColor('#f3f4f6'))
    return pal


def force_light(app) -> None:
    """After QApplication exists: light colour scheme and an explicit light palette."""
    try:
        hints = app.styleHints()
        if hasattr(hints, 'setColorScheme'):      # Qt 6.8+
            hints.setColorScheme(Qt.ColorScheme.Light)
    except Exception:
        pass
    app.setPalette(light_palette())


_QT_TRANSLATORS: list = []   # kept alive for the whole run (Qt does not own them)


def install_korean_qt(app) -> bool:
    """Qt's own Korean texts for its standard buttons and menus: 예/아니요/확인/취소/저장
    instead of Yes/No/OK/Cancel/Save, and the right-click menu of text boxes
    (실행 취소·잘라내기·복사·붙여넣기…). Without it those stayed English while the app's own
    texts are Korean. qtbase_ko.qm ships with PySide6 and is bundled by PyInstaller."""
    from pathlib import Path
    from PySide6.QtCore import QTranslator, QLibraryInfo
    dirs = []
    try:
        dirs.append(QLibraryInfo.path(QLibraryInfo.TranslationsPath))
    except Exception:
        pass
    try:
        import PySide6
        base = Path(PySide6.__file__).resolve().parent
        dirs += [str(base / 'Qt' / 'translations'), str(base / 'translations')]
    except Exception:
        pass
    if getattr(sys, '_MEIPASS', None):
        dirs.append(os.path.join(sys._MEIPASS, 'PySide6', 'Qt', 'translations'))
    for d in dirs:
        tr = QTranslator(app)
        if d and tr.load('qtbase_ko', d):
            app.installTranslator(tr)
            _QT_TRANSLATORS.append(tr)
            return True
    return False
