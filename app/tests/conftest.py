"""Shared test setup: the local model list cache of the API settings window goes to a
temporary folder, so one test's list never pre-fills another test's dialog."""
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
    yield
