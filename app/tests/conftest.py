"""Shared test setup: the local model list cache of the API settings window goes to a
temporary folder, so one test's list never pre-fills another test's dialog."""
import gc
import os
import pytest

# No test may ever turn the PC (or a Windows build runner) off: the shutdown request of
# v2.6 is a no-op while this is set, whatever a test does.
os.environ['NOVEL_TRANSLATOR_NO_SHUTDOWN'] = '1'


@pytest.fixture(autouse=True)
def _isolated_local_models_cache(tmp_path, monkeypatch):
    try:
        from novel_translator.ui import api_dialog
    except Exception:          # a test run without Qt still works
        yield; return
    monkeypatch.setattr(api_dialog, 'LOCAL_MODELS_CACHE_DIR', tmp_path)
    try:
        # The refinement window remembers its size and place (v2.6.2): one test's window
        # never sets the size another test's window opens with.
        from novel_translator.ui import refinement_run_dialog
        monkeypatch.setattr(refinement_run_dialog, 'DATA_DIR', tmp_path / 'window-state')
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _collect_qt_garbage_on_the_main_thread():
    """Free each test's leftover Qt objects here, on the main thread. Left to Python's cyclic
    garbage collector they may be freed later on whatever thread happens to run it (a later
    test's provider thread), and PySide 6.12 then crashes the whole test run when it hands
    the deletion back to the main thread (Shiboken::BindingManager::runDeletionInMainThread)."""
    yield
    gc.collect()
