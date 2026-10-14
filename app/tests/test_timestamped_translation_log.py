"""Timestamp presentation and monotonic chunk durations are additive."""
from pathlib import Path
from novel_translator.ui.worker import TranslationWorker


def test_chunk_duration_returns_pause_adjusted_elapsed():
    # Construct only the fields exercised by the pure timing helper; no Qt thread start.
    from types import SimpleNamespace
    worker = SimpleNamespace()
    worker._paused_seconds = 3.0
    worker._chunk_paused_base = 1.0
    worker._chunk_requests = 1
    worker._timing = {'chunks': 0, 'seconds': 0.0,
                      'recovered_chunks': 0, 'recovered_seconds': 0.0}
    import time
    elapsed = TranslationWorker._note_chunk_time(worker, time.monotonic() - 8.0)
    assert 5.0 <= elapsed <= 7.0
    assert worker._timing['chunks'] == 1
    assert worker._timing['recovered_chunks'] == 0


def test_all_activity_entries_get_one_display_timestamp():
    main = (Path(__file__).resolve().parents[1] / 'novel_translator' /
            'ui' / 'main_window.py').read_text(encoding='utf-8')
    worker = (Path(__file__).resolve().parents[1] / 'novel_translator' /
              'ui' / 'worker.py').read_text(encoding='utf-8')
    assert "self.error_view.appendPlainText(f'[{time.strftime(" in main
    assert '"%H:%M:%S"' in main
    assert ' · {elapsed:.1f}초' in worker
    assert "elapsed=self._note_chunk_time(chunk_started)" in worker
