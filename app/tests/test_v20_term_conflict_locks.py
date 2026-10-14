"""Per-workspace translation terms: manual edits and batch decisions remain authoritative."""
from PySide6.QtWidgets import QApplication
from novel_translator.ui.term_conflict_dialog import TermConflictDialog
from test_alpha17 import qt_app, sandbox


def test_conflict_dialog_is_scrollable_and_decisions_are_per_term():
    app=QApplication.instance() or QApplication([])
    conflicts=[(f'原文{i}',f'기존 표기{i}',f'새 제안{i}') for i in range(150)]
    dialog=TermConflictDialog(conflicts)
    try:
        assert dialog.table.rowCount()==150
        assert dialog.table.verticalScrollBar().maximum()>0
        assert dialog.table.horizontalHeader().count()==4
        dialog.table.cellWidget(1,3).setCurrentIndex(1)
        choices=dialog.decisions()
        assert choices['原文0']=='기존 표기0'
        assert choices['原文1']=='새 제안1'
        assert choices['原文149']=='기존 표기149'
        dialog._filter('原文149')
        assert dialog.table.isRowHidden(0)
        assert not dialog.table.isRowHidden(149)
    finally:
        dialog.close()


def test_manual_edit_updates_lock_and_locked_term_is_never_overwritten(qt_app,sandbox,monkeypatch):
    from test_alpha17 import _window
    from PySide6.QtWidgets import QTableWidgetItem
    w=_window(monkeypatch)
    try:
        w.term_table.setRowCount(0)
        w._term_locks={}
        w._merge_terms({'単単姐':'단단 언니','李明':'이명'})
        assert not w._term_locks
        item=w.term_table.item(0,1)
        item.setText('단단 누나')
        assert w._term_locks['単単姐']=='단단 누나'
        w._merge_terms({'単単姐':'단단저','李明':'리밍'},replace=False)
        assert w._terms()['単単姐']=='단단 누나'
        w._merge_terms({'単単姐':'단단저','李明':'리밍'},replace=True)
        assert w._terms()['単単姐']=='단단저'
        assert w._term_locks['単単姐']=='단단저'
        assert w._terms()['李明']=='리밍'
        w.term_table.selectRow(0)
        w._delete_term_rows()
        assert '単単姐' not in w._term_locks
    finally:
        w.close()
