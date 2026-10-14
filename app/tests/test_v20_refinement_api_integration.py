"""API settings are the sole source of the two refinement model routes."""
from PySide6.QtWidgets import QApplication, QWidget, QComboBox
from novel_translator.ui.refinement_run_dialog import RefinementRunDialog


class Host(QWidget):
    def __init__(self):
        super().__init__()
        self.source_path=None
        self.secrets={
            'local':{'model':'base:latest','endpoint':'http://localhost:11434/v1/chat/completions'},
            'extraction_engines':{'terms':'local','speech':'local','model':'analysis:latest'},
            'refinement_editing':{'provider':'local','model':'editor:latest'},
        }
    def _selected_model_id(self): return 'gemini-3.1-flash-lite'
    def _engine_order(self): return ['local']


def test_refinement_reads_models_directly_without_duplicate_selectors():
    app=QApplication.instance() or QApplication([])
    h=Host()
    d=RefinementRunDialog(h)
    try:
        assert not d.findChildren(QComboBox)
        assert d.analysis_info.text()=='로컬 LLM · analysis:latest'
        assert d.editing_info.text()=='로컬 LLM · editor:latest'
        assert d._analysis_route[:2]==('local','analysis:latest')
        assert d._editing_route[:2]==('local','editor:latest')
        assert d._routes_ready
        assert d.start.isEnabled()
        h.secrets['refinement_editing']['model']='new:latest'
        d._fill_engines()
        assert d.editing_info.text()=='로컬 LLM · new:latest'
        assert d._editing_route[1]=='new:latest'
    finally:
        d.close()


def test_refinement_cannot_start_without_configured_editing_model():
    app=QApplication.instance() or QApplication([])
    h=Host()
    h.secrets['refinement_editing']={'provider':'local','model':''}
    h.secrets['local']['model']=''
    d=RefinementRunDialog(h)
    try:
        assert not d._routes_ready
        assert not d.start.isEnabled()
        assert not d.findChildren(QComboBox)
    finally:
        d.close()


def test_online_engine_is_blocked_without_changing_api_settings():
    app=QApplication.instance() or QApplication([])
    h=Host()
    h.secrets['extraction_engines']['terms']='gemini'
    before=str(h.secrets)
    d=RefinementRunDialog(h)
    try:
        assert not d._routes_ready and not d.start.isEnabled()
        assert '모두 로컬 LLM' in d._route_error
        assert '재정비에 사용 불가' in d.analysis_info.text()
        assert str(h.secrets)==before
    finally:
        d.close()
