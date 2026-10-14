"""Two-group model pickers and immediate list replacement on provider change."""
from novel_translator.ui.refinement_engine_dialog import configured_models,RefinementEngineDialog

def test_catalog_models():
    assert any(x[0]=='gemini-3.1-flash-lite' for x in configured_models('gemini'))
    assert any(x[0]=='gpt-6.1-sol' for x in configured_models('openai'))
    assert configured_models('local')==[]

def test_two_group_combo_and_provider_switch(tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication
    app=QApplication.instance() or QApplication([])
    import novel_translator.ui.refinement_engine_dialog as mod
    from novel_translator.refinement_engines import load_routes
    routes=load_routes(tmp_path/'missing.json')
    routes['analysis']={'provider':'gemini','model':'gemini-3.1-flash-lite'}
    routes['editing']={'provider':'openai','model':'gpt-6.1-sol'}
    monkeypatch.setattr(mod,'load_routes',lambda:routes)
    captured=[]
    monkeypatch.setattr(mod,'save_routes',lambda x:captured.append(x))
    d=RefinementEngineDialog()
    try:
        assert len(d.rows)==2
        assert d.rows['analysis'][1].currentData()=='gemini-3.1-flash-lite'
        assert d.rows['editing'][1].currentData()=='gpt-6.1-sol'
        provider,combo=d.rows['editing']
        provider.setCurrentIndex(provider.findData('gemini'))
        assert combo.findData('gpt-6.1-sol')<0
        assert combo.findData('gemini-3.1-flash-lite')>=0
        combo.setCurrentIndex(combo.findData('gemini-3.1-flash-lite'))
        d._save()
        assert captured[0]['editing']=={'provider':'gemini','model':'gemini-3.1-flash-lite'}
    finally:
        d.close()
