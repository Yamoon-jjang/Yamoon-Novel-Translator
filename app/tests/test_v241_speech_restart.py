"""v2.41 fix: an emptied speech table is built again on the next start, and the
narrator message uses the right Korean particle (형으로, 누나로)."""
import json
from novel_translator.ui import main_window as ui
from novel_translator.ui.main_window import _ro
from test_alpha17 import _wait, qt_app, sandbox  # noqa: F401 (pytest fixtures)
from test_v24b_local_speech import _memoir_window

ANSWER = json.dumps({'pairs': [{'speaker': '나', 'listener': '엄마', 'level': '해요체', 'relation': '모자'},
                               {'speaker': '엄마', 'listener': '나', 'level': '반말', 'relation': '모자'}]},
                    ensure_ascii=False)


def test_particle():
    assert [_ro(w) for w in ('형', '누나', '오빠', '언니', '길', '단단')] == ['형으로', '누나로', '오빠로', '언니로', '길로', '단단으로']


def _built(qt_app, w):
    w._generate_speech()
    assert _wait(qt_app, lambda: w._term_thread is None, 10)
    assert w._speech_rows()


def test_deleting_every_row_lets_the_next_start_build_the_table_again(qt_app, sandbox, monkeypatch):
    w, _ = _memoir_window(qt_app, sandbox, monkeypatch, lambda pid, req: ANSWER)
    try:
        w.auto_speech.setChecked(True)
        _built(qt_app, w)
        assert not w._speech_auto_due()                  # a table exists
        w.speech_table.selectAll(); w._delete_speech_rows()
        assert w.speech_table.rowCount() == 0
        assert w._speech_auto_due()                      # the earlier attempt no longer blocks it
    finally:
        w.close()


def test_start_over_forgets_the_attempt_only_when_the_table_is_empty(qt_app, sandbox, monkeypatch):
    w, _ = _memoir_window(qt_app, sandbox, monkeypatch, lambda pid, req: ANSWER)
    try:
        w.auto_speech.setChecked(True)
        _built(qt_app, w)
        w.speech_table.setRowCount(0)                    # emptied without the button
        assert not w._speech_auto_due()                  # this session already tried
        monkeypatch.setattr(w, '_ready_engines', lambda: [])   # stop right after the start bookkeeping
        w.start_translation(False)
        assert not w._speech_auto_due()                  # a plain resume does not ask again
        w.start_translation(True)                        # [처음부터 다시]
        assert w._speech_auto_due()
        # A table that still has rows is never rebuilt behind the user's back.
        _built(qt_app, w)
        w.start_translation(True)
        assert not w._speech_auto_due()
    finally:
        w.close()


def test_narrator_note_reads_naturally():
    from novel_translator.speech_levels import clean_rows

    class Fake:
        speech_table = object(); term_table = object()
        def _speech_rows(self):
            return clean_rows([{'speaker': '나', 'listener': '엄마', 'level': '반말', 'note': '모자'}])
        def _terms(self): return {'单单姐': '단단 언니'}

    note = ui.MainWindow._address_note(Fake())
    assert '哥는 형으로 번역' in note and '누나·형으로 고치고' in note and '형로' not in note
