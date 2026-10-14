"""v2.0 unified analysis/editing route UI regression checks."""
from PySide6.QtWidgets import QApplication, QWidget
from novel_translator.ui.api_dialog import ApiDialog
from novel_translator.ui.refinement_run_dialog import RefinementRunDialog


def _app():
    return QApplication.instance() or QApplication([])


def _records(prefix):
    return [{'id':prefix+'-1', 'secret':'test-token-for-ui-only', 'enabled':True}]


def test_api_editor_uses_one_analysis_route_and_preserves_explicit_editing_model():
    _app()
    dialog = ApiDialog(
        _records('GEM'), 'gemini-3.1-flash-lite',
        {'model':'gpt-6.1-sol', 'keys':_records('OAI')}, '',
        local_settings={'model':'local-model', 'endpoint':'http://localhost:11434/v1/chat/completions'},
        engines=[{'id':'gemini','enabled':True},{'id':'local','enabled':False},{'id':'openai','enabled':False}],
        extraction_engines={'terms':'gemini', 'speech':'openai'},
        refinement_editing={'provider':'openai','model':'gpt-6.1-sol'})
    try:
        assert dialog._legacy_analysis_conflict
        assert '서로 다릅니다' in dialog.analysis_hint.text()
        assert dialog.editing_engine.currentData()=='openai'
        assert dialog.editing_model.currentData()=='gpt-6.1-sol'
        dialog.analysis_engine.setCurrentIndex(dialog.analysis_engine.findData('local'))
        assert dialog.analysis_model.findData('gpt-6.1-sol')<0, 'OpenAI model leaked into local list'
        dialog._save_and_accept()
        saved=dialog.settings_payload()
        assert saved['extraction_engines']['terms']=='local'
        assert saved['extraction_engines']['speech']=='local'
        assert saved['refinement_editing']=={'provider':'openai','model':'gpt-6.1-sol'}
    finally:
        dialog.close()


def test_refinement_window_uses_shared_saved_routes_without_writing_on_close():
    _app()
    host=QWidget()
    host.secrets={
        'gemini':{'keys':_records('GEM')},
        'openai':{'model':'gpt-6.1-sol','keys':_records('OAI')},
        'local':{'model':'local-model','endpoint':'http://localhost:11434/v1/chat/completions'},
        'extraction_engines':{'terms':'local','speech':'local','model':''},
        'refinement_editing':{'provider':'openai','model':'gpt-6.1-sol'},
    }
    host._selected_model_id=lambda:'gemini-3.1-flash-lite'
    host._engine_order=lambda:['gemini','local']
    dialog=RefinementRunDialog(host)
    try:
        assert not dialog.findChildren(__import__('PySide6.QtWidgets',fromlist=['QComboBox']).QComboBox)
        assert not dialog._routes_ready
        assert not dialog.start.isEnabled()
        assert '로컬 LLM' in dialog.analysis_info.text()
        assert '재정비에 사용 불가' in dialog.editing_info.text()
        assert '모두 로컬 LLM' in dialog._route_error
        before=dict(host.secrets)
        dialog.reject()
        assert host.secrets==before, 'closing without starting changed saved configuration'
    finally:
        dialog.close(); host.close()
