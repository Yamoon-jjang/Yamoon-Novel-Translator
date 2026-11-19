"""v2.6.8: the PC is never let go between two parts of one unattended job.

2026-11-18 22:39:04: a folder run with 재정비 자동 시작 finished file 4's refinement; the
refinement window released the sleep request when its run ended and closed 1.5 seconds later,
when the folder run would have taken it again. Windows (idle time long passed) slept within that
second, "System Idle", and the run waited until 06:44. Every hand-over now keeps the request:
  folder run  — the window of a folder file never releases it (the folder run holds it);
  single file — translation end → retry round / 재정비 자동 시작: kept through the hand-over;
and the PC is released as before when the job really ends, is stopped or paused.
"""
from __future__ import annotations

import pytest

import novel_translator.ui.main_window as ui
from test_alpha17 import _wait, qt_app, sandbox  # noqa: F401 (fixtures)
from test_v15_review import _scripted_worker_class, _single_file
from test_v241_folder_auto_retry import _folder, _per_file_worker
from test_v267_auto_refine import _local_window, env  # noqa: F401 (fixture)


def _record(monkeypatch, rec):
    events = []
    monkeypatch.setattr(ui, 'set_translation_awake', lambda on: events.append(('awake', bool(on))) or True)
    import novel_translator.ui.refinement_run_dialog as rd
    Fake = rd.RefinementRunDialog
    class Logged(Fake):
        def __init__(self, host, **k):
            events.append(('refine', str(k.get('auto_source').name)))
            super().__init__(host, **k)
    monkeypatch.setattr(rd, 'RefinementRunDialog', Logged)
    return events


def _no_release_between(events, start, end):
    """No ('awake', False) between the first index of start and the following end event."""
    i = events.index(start)
    j = next(k for k in range(i, len(events)) if events[k] == end)
    return ('awake', False) not in events[i:j]


def _idle(qt_app, w):
    _wait(qt_app, lambda: not (w.worker and w.worker.isRunning()) and not w._job_busy())
    for _ in range(30): qt_app.processEvents()


def test_single_file_keeps_the_pc_awake_into_the_refinement(qt_app, env, monkeypatch):
    root, cfg, rec = env
    events = _record(monkeypatch, rec)
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _scripted_worker_class(started, [('completed', 0, 0)]))
    w = _local_window(monkeypatch, rec)
    try:
        w._open_txt_path(str(_single_file(root))); w.auto_refine.setChecked(True)
        w.start_translation()
        assert _wait(qt_app, lambda: rec['refine']), events
        _idle(qt_app, w)
        first_on = events.index(('awake', True))
        assert ('awake', False) not in events[first_on:events.index(('refine', 'book.ko.txt'))], events
        assert events[-1] == ('awake', False)          # released once the window is done
    finally:
        w.close()


def test_single_file_without_the_option_releases_as_before(qt_app, env, monkeypatch):
    root, cfg, rec = env
    events = _record(monkeypatch, rec)
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _scripted_worker_class(started, [('completed', 0, 0)]))
    w = _local_window(monkeypatch, rec)
    try:
        w._open_txt_path(str(_single_file(root))); w.auto_refine.setChecked(False)
        w.start_translation()
        assert _wait(qt_app, lambda: rec['infos'] or rec['timed'])
        _idle(qt_app, w)
        assert not rec['refine'] and events[-1] == ('awake', False)
    finally:
        w.close()


def test_a_result_that_cannot_be_refined_releases_the_pc(qt_app, env, monkeypatch):
    root, cfg, rec = env
    events = _record(monkeypatch, rec)
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _scripted_worker_class(started, [('completed', 0, 0)]))
    monkeypatch.setattr(ui.MainWindow, '_auto_refine_target', lambda self: (None, '시험: 재정비할 수 없음'))
    w = _local_window(monkeypatch, rec)
    try:
        w._open_txt_path(str(_single_file(root))); w.auto_refine.setChecked(True)
        w.start_translation()
        assert _wait(qt_app, lambda: '재정비 자동 시작 건너뜀' in w.error_view.toPlainText())
        _idle(qt_app, w)
        assert not rec['refine'] and events[-1] == ('awake', False)
    finally:
        w.close()


def test_a_retry_round_keeps_the_pc_awake(qt_app, env, monkeypatch):
    root, cfg, rec = env
    events = _record(monkeypatch, rec)
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _scripted_worker_class(started, [('partial', 2, 0), ('completed', 0, 0)]))
    w = _local_window(monkeypatch, rec)
    try:
        w._open_txt_path(str(_single_file(root))); w.auto_refine.setChecked(False)
        w.start_translation()
        assert _wait(qt_app, lambda: len(started) >= 2 and (rec['infos'] or rec['timed'])), (started, events)
        _idle(qt_app, w)
        first_on = events.index(('awake', True))
        last_off = len(events) - 1 - events[::-1].index(('awake', False))
        assert ('awake', False) not in events[first_on:last_off], events   # only the final release
        assert events[-1] == ('awake', False)
    finally:
        w.close()


def test_a_retry_that_does_not_start_lets_the_pc_go(qt_app, env, monkeypatch):
    root, cfg, rec = env
    events = _record(monkeypatch, rec)
    w = _local_window(monkeypatch, rec)
    try:
        monkeypatch.setattr(w, 'start_translation', lambda: None)
        w._apply_sleep_prevention(True)
        w._start_retry_round()
        assert events[-1] == ('awake', False)
    finally:
        w.close()


def test_folder_run_never_lets_the_pc_go_between_files_and_refinements(qt_app, env, monkeypatch):
    root, cfg, rec = env
    events = _record(monkeypatch, rec)
    books = _folder(root, ('1.txt', '2.txt', '3.txt'))
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _per_file_worker(started, {n: [('completed', 0, 0)] for n in ('1.txt', '2.txt', '3.txt')}))
    w = _local_window(monkeypatch, rec)
    try:
        w._open_folder_path(str(books)); w._update_ready_state(); w.auto_refine.setChecked(True)
        w.start_translation()
        assert _wait(qt_app, lambda: w.last_result_path is not None and w.last_result_path.name == '번역_요약.txt')
        _idle(qt_app, w)
        assert [n for k, n in events if k == 'refine'] == ['1.ko.txt', '2.ko.txt', '3.ko.txt']
        first_on = events.index(('awake', True))
        assert ('awake', False) not in events[first_on:events.index(('refine', '3.ko.txt'))], events
        assert events[-1] == ('awake', False)          # the folder run ended
    finally:
        w.close()


@pytest.mark.parametrize('status', ['completed', 'skipped', 'failed'])
def test_the_folder_files_refinement_window_never_releases(qt_app, monkeypatch, tmp_path, status):
    from test_v26_shutdown_prompt import _Host, _refine_dialog, _done
    d, awake = _refine_dialog(monkeypatch, tmp_path, _Host(False), _done(tmp_path/'소설.ko.refined.txt', status))
    try:
        d._batch_auto = True
        d.show(); d._start()
        assert _wait(qt_app, lambda: d.worker is None)
        assert awake == [True]                          # held; the folder run goes on holding it
    finally:
        d.close()


def test_a_refinement_by_hand_still_releases_when_it_ends(qt_app, monkeypatch, tmp_path):
    from test_v26_shutdown_prompt import _Host, _refine_dialog, _done
    d, awake = _refine_dialog(monkeypatch, tmp_path, _Host(False), _done(tmp_path/'소설.ko.refined.txt', 'stopped'))
    try:
        d.show(); d._start()
        assert _wait(qt_app, lambda: d.worker is None)
        assert awake == [True, False]
    finally:
        d.close()


# ---------------------------------------------------------------- names / speech analysis
def test_analysis_holds_the_pc_and_lets_it_go_only_when_nothing_follows(qt_app, env, monkeypatch):
    root, cfg, rec = env
    events = _record(monkeypatch, rec)
    w = _local_window(monkeypatch, rec)
    try:
        w._analysis_is_auto = False
        w._set_analysis_busy(True, '용어 검출 중…')
        assert events[-1] == ('awake', True)
        w._set_analysis_busy(False)
        assert events[-1] == ('awake', True)                      # not let go at once
        assert _wait(qt_app, lambda: events[-1] == ('awake', False), 5)   # nothing followed
    finally:
        w.close()


def test_analysis_followed_by_the_translation_never_lets_go(qt_app, env, monkeypatch):
    root, cfg, rec = env
    events = _record(monkeypatch, rec)
    w = _local_window(monkeypatch, rec)
    try:
        w._analysis_is_auto = True
        w._set_analysis_busy(True, '용어 검출 중…')
        w._set_analysis_busy(False)
        w._job_active = True                                       # the translation started
        for _ in range(40):
            qt_app.processEvents(); import time; time.sleep(0.05)
        assert ('awake', False) not in events
    finally:
        w._job_active = False
        w.close()


def test_an_automatic_step_waiting_on_a_closing_window_keeps_the_pc(qt_app, env, monkeypatch):
    from PySide6.QtWidgets import QApplication, QDialog
    root, cfg, rec = env
    events = _record(monkeypatch, rec)
    w = _local_window(monkeypatch, rec)
    try:
        modal = [QDialog(w)]
        monkeypatch.setattr(QApplication, 'activeModalWidget', staticmethod(lambda: modal[0]))
        w._apply_sleep_prevention(True)
        w._release_awake_if_idle(True)
        assert events[-1] == ('awake', True)                       # the collision window is open
        modal[0] = None
        assert _wait(qt_app, lambda: events[-1] == ('awake', False), 5)
    finally:
        w.close()


def test_a_refinement_window_from_the_main_window_is_never_released_under_it(qt_app, env, monkeypatch):
    root, cfg, rec = env
    events = _record(monkeypatch, rec)
    w = _local_window(monkeypatch, rec)
    try:
        class Dlg:
            def exec(self_):
                w._release_awake_if_idle(False)                    # a late check while it runs
                return 0
        w._apply_sleep_prevention(True)
        w._run_refinement_window(Dlg())
        assert ('awake', False) not in events and w._refining is False
    finally:
        w.close()
