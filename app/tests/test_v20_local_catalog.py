"""The shared role dropdowns must receive local models without first visiting the local tab."""
from PySide6.QtWidgets import QApplication
from novel_translator.ui.api_dialog import ApiDialog


def test_local_models_populate_both_roles_without_tab_switch(monkeypatch):
    app=QApplication.instance() or QApplication([])
    from novel_translator.ui import api_dialog
    callbacks=[]
    class FakeThread:
        def __init__(self,endpoint,parent=None): self.endpoint=endpoint
        def start(self): callbacks.append(self)
        def isRunning(self): return False
        class Event:
            def __init__(self): self.fn=[]
            def connect(self,fn): self.fn.append(fn)
            def emit(self,*args):
                for fn in self.fn: fn(*args)
        @property
        def done(self):
            if not hasattr(self,'_done'): self._done=self.Event()
            return self._done
        @property
        def finished(self):
            if not hasattr(self,'_finished'): self._finished=self.Event()
            return self._finished
    monkeypatch.setattr(api_dialog,'LocalModelsThread',FakeThread)
    dialog=ApiDialog([],local_settings={'model':'old:latest','endpoint':'http://localhost:11434/v1/chat/completions'},
        extraction_engines={'terms':'local','speech':'local','model':'old:latest'},
        refinement_editing={'provider':'local','model':'old:latest'})
    try:
        dialog.show(); app.processEvents()
        assert len(callbacks)==1
        assert dialog.tabs.currentIndex()==0, 'Gemini stays selected; local tab is never clicked'
        callbacks[0].done.emit(['old:latest','new:latest'],'')
        for combo in (dialog.analysis_model,dialog.editing_model):
            assert combo.findData('new:latest')>=0
            assert combo.currentData()=='old:latest'
        assert dialog.local_model.text()=='old:latest'
        dialog.tabs.setCurrentIndex(2);app.processEvents()
        assert len(callbacks)==1, 'no duplicate request after visiting local tab'
    finally: dialog.close()
