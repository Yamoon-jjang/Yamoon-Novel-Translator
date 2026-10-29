"""v2.6: PC shutdown question once a long job has finished.

- 고급 설정 option, off on a new install and after 설정 초기화; once on it stays on.
- Asked only when the whole job is over: a single file after its automatic retries, a folder
  after its last file, a Korean refinement that completed. Never for a stopped or failed job.
- [예] is the default and nobody answering within 60 seconds means yes; [아니오] keeps the PC
  on and the usual completion message follows.
- On yes the app closes the normal way (closeEvent saves) and only then asks Windows.
- The 재정비 window keeps the PC awake while it runs (it had no sleep prevention before).
"""
from __future__ import annotations

import json
import os

import pytest
from PySide6.QtWidgets import QDialog

import novel_translator.ui.main_window as ui
import novel_translator.ui.shutdown_dialog as sd
import novel_translator.power as power
from test_alpha17 import _window, _fake_worker_class, _wait, qt_app, sandbox  # noqa: F401 (fixtures)
from test_v15_review import _scripted_worker_class, _single_file
from test_v241_folder_auto_retry import _per_file_worker, _folder


ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]


def _set_option(cfg_dir, value):
    path = cfg_dir/'app.json'
    cfg = json.loads(path.read_text(encoding='utf-8'))
    if value is None:
        cfg.pop(sd.OPTION_KEY, None)
    else:
        cfg[sd.OPTION_KEY] = value
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')


@pytest.fixture
def shut(sandbox, monkeypatch):
    """The option read from the sandbox config; the question and the Windows call recorded."""
    root, cfg = sandbox
    monkeypatch.setattr(sd, 'CONFIG_DIR', cfg)
    rec = {'asked': [], 'answer': True, 'requested': 0}
    def ask(parent, title, summary):
        rec['asked'].append((title, summary)); return rec['answer']
    monkeypatch.setattr(ui, 'ask_shutdown', ask)
    def request():
        rec['requested'] += 1; return True
    monkeypatch.setattr(ui, 'request_system_shutdown', request)
    return root, cfg, rec


# ---------------------------------------------------------------- the option
def test_option_is_off_on_a_new_install_and_after_reset():
    for name in ('app.json', 'defaults.json'):
        cfg = json.loads((ROOT/'config'/name).read_text(encoding='utf-8'))
        assert cfg.get(sd.OPTION_KEY) is False, name
        assert cfg.get('prevent_sleep_while_running') is True, name   # unchanged


def test_option_reads_only_an_explicit_true(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, 'CONFIG_DIR', tmp_path)
    assert sd.shutdown_offer_enabled() is False                       # no app.json at all
    (tmp_path/'app.json').write_text('{}', encoding='utf-8')
    for value, want in ((None, False), (False, False), (True, True), ('true', False), (1, False)):
        _set_option(tmp_path, value)
        assert sd.shutdown_offer_enabled() is want, value
    (tmp_path/'app.json').write_text('{broken', encoding='utf-8')
    assert sd.shutdown_offer_enabled() is False


def test_runtime_audit_flags_a_non_boolean_option(tmp_path, monkeypatch):
    import shutil
    import novel_translator.runtime_audit as ra
    cfg = tmp_path/'config'; shutil.copytree(ROOT/'config', cfg)
    monkeypatch.setattr(ra, 'CONFIG_DIR', cfg)
    assert not [i for i in ra.validate_resources() if 'confirm_shutdown' in i]
    _set_option(cfg, 'yes')
    assert [i for i in ra.validate_resources() if 'confirm_shutdown_after_finish' in i]


def test_advanced_settings_turn_it_on_and_it_stays_on(qt_app, shut):
    import novel_translator.ui.advanced_dialog as advanced
    root, cfg, rec = shut
    d = advanced.AdvancedSettingsDialog()
    try:
        assert d.confirm_shutdown.isChecked() is False
        assert '번역·재정비' in d.prevent_sleep.text() and '번역·재정비' in d.confirm_shutdown.text()
        d.confirm_shutdown.setChecked(True); d._save()
    finally:
        d.close()
    assert json.loads((cfg/'app.json').read_text(encoding='utf-8'))[sd.OPTION_KEY] is True
    assert sd.shutdown_offer_enabled() is True
    again = advanced.AdvancedSettingsDialog()                          # reopened (= restarted app)
    try:
        assert again.confirm_shutdown.isChecked() is True
        assert again.prevent_sleep.isChecked() is True                 # the other option untouched
    finally:
        again.close()


def test_settings_reset_turns_it_off(qt_app, shut, monkeypatch):
    root, cfg, rec = shut
    _set_option(cfg, True)
    w = _window(monkeypatch)
    try:
        w._reset_settings()
        assert sd.shutdown_offer_enabled() is False
    finally:
        w.close()


# ---------------------------------------------------------------- the countdown window
def test_countdown_window_defaults_to_yes_and_times_out_to_yes(qt_app):
    d = sd.ShutdownCountdownDialog(None, '번역 완료', '번역 결과를 저장했습니다.', seconds=3)
    try:
        assert d.yes.isDefault() and not d.no.isDefault() and not d.no.autoDefault()
        assert '3초 후 PC를 종료할까요?' == d.question.text()
        d.show(); qt_app.processEvents()
        d._tick(); assert d.question.text() == '2초 후 PC를 종료할까요?'
        d._tick(); d._tick()                                           # nobody answered
        assert d.result() == QDialog.Accepted and not d._timer.isActive()
    finally:
        d.close()


def test_countdown_window_no_and_escape_keep_the_pc_on(qt_app):
    for act in ('no', 'escape', 'close'):
        d = sd.ShutdownCountdownDialog(None, '번역 완료', '요약')
        d.show(); qt_app.processEvents()
        {'no': d.no.click, 'escape': d.reject, 'close': d.close}[act]()
        qt_app.processEvents()
        assert d.result() == QDialog.Rejected and not d._timer.isActive(), act


def test_shutdown_request_never_runs_in_tests_or_off_windows(monkeypatch):
    called = []
    monkeypatch.setattr(__import__('subprocess'), 'Popen', lambda *a, **k: called.append(a))
    assert os.environ.get('NOVEL_TRANSLATOR_NO_SHUTDOWN')               # conftest guard
    assert power.request_system_shutdown() is False and not called


def test_shutdown_command_is_plain_shutdown_without_force(monkeypatch):
    """/t 0 on purpose: a timeout above 0 makes Windows force programs closed (/f)."""
    import subprocess
    called = []
    monkeypatch.delenv('NOVEL_TRANSLATOR_NO_SHUTDOWN', raising=False)
    monkeypatch.setattr(power.os, 'name', 'nt')
    monkeypatch.setenv('SystemRoot', r'C:\Windows')
    monkeypatch.setattr(subprocess, 'Popen', lambda args, **k: called.append(args))
    assert power.request_system_shutdown() is True
    assert called and called[0][1:] == ['/s', '/t', '0'] and called[0][0].endswith('shutdown.exe')
    assert '/f' not in called[0]


# ---------------------------------------------------------------- translation
def _run_single(qt_app, monkeypatch, root, script):
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _scripted_worker_class(started, script))
    w = _window(monkeypatch); infos = []
    monkeypatch.setattr(ui.QMessageBox, 'information', lambda *a, **k: infos.append(a[1]))
    w._open_txt_path(str(_single_file(root))); w.start_translation()
    return w, started, infos


def test_single_file_asks_once_after_the_retries_and_shuts_down(qt_app, shut, monkeypatch):
    root, cfg, rec = shut
    _set_option(cfg, True)
    w, started, infos = _run_single(qt_app, monkeypatch, root, [('partial', 2, 0), ('completed', 0, 0)])
    closed = []
    monkeypatch.setattr(w, '_save_window_state', lambda: closed.append('saved'))
    assert _wait(qt_app, lambda: rec['requested'] == 1), (rec, infos, w.error_view.toPlainText()[-600:])
    assert len(started) == 2                                          # the retry ran first
    assert [t for t, _ in rec['asked']] == ['번역 완료']               # asked once, at the very end
    assert '걸린 시간' in rec['asked'][0][1]
    assert infos == []                                                # no second message to block
    assert closed == ['saved'] and not w.isVisible()                  # app closed the normal way first
    log = w.error_view.toPlainText()
    assert 'PC 종료 확인 · 60초' in log and 'PC 종료 진행' in log


def test_single_file_no_keeps_the_pc_on_and_shows_the_usual_message(qt_app, shut, monkeypatch):
    root, cfg, rec = shut
    _set_option(cfg, True); rec['answer'] = False
    w, started, infos = _run_single(qt_app, monkeypatch, root, [('partial', 2, 0), ('partial', 2, 0)])
    try:
        assert _wait(qt_app, lambda: infos)
        for _ in range(20): qt_app.processEvents()
        assert [t for t, _ in rec['asked']] == ['번역 완료 · 일부 남음'] and infos == ['번역 완료 · 일부 남음']
        assert rec['requested'] == 0 and w.isVisible() is not None
        assert 'PC 종료 취소' in w.error_view.toPlainText()
    finally:
        w.close()
    assert rec['requested'] == 0                                      # a later close never shuts down


def test_option_off_never_asks(qt_app, shut, monkeypatch):
    root, cfg, rec = shut
    w, started, infos = _run_single(qt_app, monkeypatch, root, [('completed', 0, 0)])
    try:
        assert _wait(qt_app, lambda: infos == ['번역 완료'])
        assert rec['asked'] == [] and 'PC 종료' not in w.error_view.toPlainText()
    finally:
        w.close()
    assert rec['requested'] == 0


def test_failed_or_stopped_runs_never_ask(qt_app, shut, monkeypatch):
    root, cfg, rec = shut
    _set_option(cfg, True)
    from PySide6.QtCore import QThread
    started = []
    Base = _fake_worker_class(started)
    class Failing(Base):
        def run(self):
            self.failed.emit('설정 오류 · 시험')
    monkeypatch.setattr(ui, 'TranslationWorker', Failing)
    w = _window(monkeypatch)
    warns = []
    monkeypatch.setattr(ui.QMessageBox, 'warning', lambda *a, **k: warns.append(a[1]))
    monkeypatch.setattr(ui.QMessageBox, 'information', lambda *a, **k: None)
    try:
        w._open_txt_path(str(_single_file(root))); w.start_translation()
        assert _wait(qt_app, lambda: warns and not (w.worker and w.worker.isRunning()))
        for _ in range(30): qt_app.processEvents()
        assert rec['asked'] == []
        # a user stop ends in the same branch (_stop_batch): no question either
        w._stop_batch = True; w._completion_message = '번역 완료'
        w._worker_thread_finished()
        assert rec['asked'] == []
    finally:
        w.close()
    assert rec['requested'] == 0


def test_folder_asks_once_after_the_last_file(qt_app, shut, monkeypatch):
    root, cfg, rec = shut
    _set_option(cfg, True); rec['answer'] = False
    books = _folder(root, ('1.txt', '2.txt', '3.txt')); started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _per_file_worker(started, {
        '1.txt': [('partial', 2, 0), ('completed', 0, 0)], '2.txt': [('completed', 0, 0)], '3.txt': [('completed', 0, 0)]}))
    w = _window(monkeypatch); infos = []
    monkeypatch.setattr(ui.QMessageBox, 'information', lambda *a, **k: infos.append(a[1]))
    try:
        w._prepare_new_input(); w.input_mode = 'folder'; w.batch_root = books
        w._apply_profile('zh-ko'); w._scan_batch_files(); w._update_ready_state()
        w.start_translation()
        assert _wait(qt_app, lambda: infos), (rec, infos)
        assert [s.source_path.name for s in started] == ['1.txt', '1.txt', '2.txt', '3.txt']
        assert [t for t, _ in rec['asked']] == ['폴더 번역 완료'] and infos == ['폴더 번역 완료']
        assert '번역_요약.txt' in rec['asked'][0][1]                     # written before the question
        assert w.last_result_path.name == '번역_요약.txt'
    finally:
        w.close()
    assert rec['requested'] == 0


def test_folder_yes_shuts_down_after_the_summary_is_written(qt_app, shut, monkeypatch):
    root, cfg, rec = shut
    _set_option(cfg, True)
    books = _folder(root, ('1.txt', '2.txt')); started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _per_file_worker(started, {
        '1.txt': [('completed', 0, 0)], '2.txt': [('partial', 1, 0), ('partial', 1, 0)]}))
    w = _window(monkeypatch); infos = []
    monkeypatch.setattr(ui.QMessageBox, 'information', lambda *a, **k: infos.append(a[1]))
    w._prepare_new_input(); w.input_mode = 'folder'; w.batch_root = books
    w._apply_profile('zh-ko'); w._scan_batch_files(); w._update_ready_state()
    w.start_translation()
    assert _wait(qt_app, lambda: rec['requested'] == 1), (rec, infos)
    assert [t for t, _ in rec['asked']] == ['폴더 번역 완료 · 일부 남음'] and infos == []
    assert w.last_result_path.name == '번역_요약.txt' and w.last_result_path.is_file()   # written before
    assert not w.isVisible()


# ---------------------------------------------------------------- 재정비
class _Host:
    """A refinement host with the main window's two shutdown hooks recorded."""
    def __new__(cls, answer):
        from test_v20_refinement_api_integration import Host
        class H(Host):
            def __init__(self):
                super().__init__(); self.asked = []; self.shut = 0; self.answer = answer
            def _offer_shutdown(self, title, summary, parent=None, log=True):
                self.asked.append((title, summary, parent, log)); return self.answer
            def _shutdown_now(self):
                self.shut += 1
        return H()


def _refine_dialog(monkeypatch, tmp_path, host, outcome):
    import novel_translator.ui.refinement_run_dialog as rd
    from PySide6.QtCore import QThread, Signal
    awake = []
    monkeypatch.setattr(rd, 'set_translation_awake', lambda on: awake.append(bool(on)) or True)
    class FakeWorker(QThread):
        progressed = Signal(int, int, int); outcome = Signal(object); failed = Signal(str); activity = Signal(str)
        def __init__(self, *a, **k): super().__init__()
        def stop(self): pass
        def run(self):
            if outcome == 'failed': self.failed.emit('오류 · 시험')
            else: self.outcome.emit(outcome)
    monkeypatch.setattr(rd, 'RefinementWorker', FakeWorker)
    src = tmp_path/'소설.ko.txt'; src.write_text('어색한 첫 문단입니다.\n\n"어색한 대사입니다."\n', encoding='utf-8')
    d = rd.RefinementRunDialog(host)
    d._set_source(src)
    return d, awake


def _done(out, status='completed'):
    return {'status': status, 'done': 2, 'total': 2, 'changed': 2, 'identical': 0, 'preserved': 0,
            'seconds': 3723, 'output': str(out), 'report': str(out)+'.review.txt',
            'guidance_source': 'analysis', 'guidance': '표'}


def test_refinement_keeps_the_pc_awake_and_asks_after_a_completed_run(qt_app, monkeypatch, tmp_path):
    host = _Host(True)
    d, awake = _refine_dialog(monkeypatch, tmp_path, host, _done(tmp_path/'소설.ko.refined.txt'))
    try:
        d.show(); d._start()
        assert awake == [True]                                          # on while it runs
        assert _wait(qt_app, lambda: host.shut == 1), (host.asked, awake)
        assert awake == [True, False]                                   # released when it ends
        title, summary, parent, log = host.asked[0]
        assert title == '재정비 완료' and parent is d and log is False
        assert '1시간 02분 03초' in summary and '소설.ko.refined.txt' in summary
        assert d.result() == QDialog.Accepted                           # the window closed first
    finally:
        d.close()


def test_refinement_no_keeps_the_window_and_the_pc(qt_app, monkeypatch, tmp_path):
    host = _Host(False)
    d, awake = _refine_dialog(monkeypatch, tmp_path, host, _done(tmp_path/'소설.ko.refined.txt'))
    try:
        d.show(); d._start()
        assert _wait(qt_app, lambda: host.asked)
        for _ in range(30): qt_app.processEvents()
        assert host.shut == 0 and d.isVisible() and d.done_badge.isVisible()
        assert awake == [True, False]
    finally:
        d.close()


@pytest.mark.parametrize('outcome', ['stopped', 'failed'])
def test_refinement_stopped_or_failed_never_asks(qt_app, monkeypatch, tmp_path, outcome):
    host = _Host(True)
    result = _done(tmp_path/'소설.ko.refined.txt', 'stopped') if outcome == 'stopped' else 'failed'
    d, awake = _refine_dialog(monkeypatch, tmp_path, host, result)
    try:
        d.show(); d._start()
        assert _wait(qt_app, lambda: d.worker is None)
        for _ in range(30): qt_app.processEvents()
        assert host.asked == [] and host.shut == 0 and awake == [True, False]
    finally:
        d.close()


def test_refinement_follows_the_sleep_option(qt_app, monkeypatch, tmp_path):
    import novel_translator.ui.refinement_run_dialog as rd
    cfg = tmp_path/'cfg'; cfg.mkdir()
    (cfg/'app.json').write_text(json.dumps({'prevent_sleep_while_running': False}), encoding='utf-8')
    monkeypatch.setattr(rd, 'CONFIG_DIR', cfg)
    host = _Host(False)
    d, awake = _refine_dialog(monkeypatch, tmp_path, host, _done(tmp_path/'소설.ko.refined.txt'))
    try:
        d.show(); d._start()
        assert _wait(qt_app, lambda: d.worker is None)
        assert awake == [False, False]
    finally:
        d.close()


def test_main_window_offer_closes_the_app_then_shuts_down(qt_app, shut, monkeypatch):
    """The 재정비 path: the main window's own hook, called with the dialog as parent."""
    root, cfg, rec = shut
    _set_option(cfg, True)
    w = _window(monkeypatch)
    order = []
    monkeypatch.setattr(ui, 'request_system_shutdown', lambda: order.append('shutdown') or True)
    monkeypatch.setattr(w, '_clean_translation_db', lambda: order.append('db cleanup'))
    try:
        parent = object()
        assert w._offer_shutdown('재정비 완료', '요약', parent=parent, log=False) is True
        for _ in range(10): qt_app.processEvents()
        assert order == []                                              # the dialog closes first
        w._shutdown_now()
        assert order == ['db cleanup', 'shutdown'] and not w.isVisible()
    finally:
        w.close()
