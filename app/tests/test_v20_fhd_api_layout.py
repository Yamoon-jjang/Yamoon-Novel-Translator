"""Keep the v2.0 API settings entirely accessible on Full HD screens."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QScrollArea
from novel_translator.ui.api_dialog import ApiDialog


def test_full_hd_dialog_has_scrollable_settings_and_fixed_actions():
    app=QApplication.instance() or QApplication([])
    d=ApiDialog([], 'gemini-3.1-flash-lite', {}, '', local_settings={},
                engines=[{'id':'gemini','enabled':True}])
    try:
        d.show()
        app.processEvents()
        assert d.settings_scroll.widgetResizable()
        assert d.settings_scroll.verticalScrollBarPolicy()==Qt.ScrollBarAsNeeded
        assert d.gemini_table.table.verticalScrollBarPolicy()==Qt.ScrollBarAsNeeded
        assert d.openai_table.table.verticalScrollBarPolicy()==Qt.ScrollBarAsNeeded
        assert d.gemini_table.table.minimumHeight()==165
        assert d.findChild(QDialogButtonBox).isVisible()
        d.resize(700,500)
        app.processEvents()
        footer=d.findChild(QDialogButtonBox)
        assert footer.geometry().bottom()<=d.height()
        assert d.settings_scroll.geometry().bottom()<=footer.geometry().top()
        assert d.settings_scroll.verticalScrollBar().maximum()>0
    finally:
        d.close()


def test_api_fhd_screen_limit_source_contract():
    from pathlib import Path
    p=Path(__file__).resolve().parents[1]/'novel_translator'/'ui'/'api_dialog.py'
    s=p.read_text(encoding='utf-8')
    assert 'available.height()-24' in s
    assert 'self.setMaximumSize(limit_w,limit_h)' in s
    assert 'outer.addWidget(box)' in s
    assert 'self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)' in s
