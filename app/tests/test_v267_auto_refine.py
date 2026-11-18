"""v2.6.7: 번역이 끝나면 한국어 소설 재정비 자동 시작.

- One app.json option shown in two places: the box at the right of the API state line (it can be
  changed while a translation runs) and 고급 설정. Off on a new install and after 설정 초기화.
- Usable with a refinement-ready (local LLM) API setup, for a single file and a folder run: in a
  folder each file is translated, then refined (its window starts and closes by itself), then
  the next file starts; the shutdown question comes once, after the last file's refinement.
- A single file that finished (after its automatic retries) opens the refinement window with the
  .ko.txt result, which starts by itself: no completion window, no shutdown question then; the
  shutdown question comes after the refinement result is saved (the window's own hook).
- An unfinished result (.ko.partial.txt) is refined too (v2.6.7 보완): the finished translation
  later takes those edits (test_v267_partial_carry.py). A result that cannot be refined (too
  large, engines) is skipped with one log line; the notice then closes by itself.
- In the refinement window an automatic start never asks: the long-run estimate is skipped,
  a retry of preserved paragraphs is accepted, and anything that could lose earlier work
  (an existing result, an earlier run with another model) skips the run and says why.
"""
from __future__ import annotations

import json

import pytest

import novel_translator.ui.main_window as ui
import novel_translator.ui.shutdown_dialog as sd
from test_alpha17 import _window, _wait, qt_app, sandbox  # noqa: F401 (fixtures)
from test_v15_review import _scripted_worker_class, _single_file
from test_v241_folder_auto_retry import _folder

ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]
LOCAL = {
    'local': {'model': 'base:latest', 'endpoint': 'http://localhost:11434/v1/chat/completions'},
    'extraction_engines': {'terms': 'local', 'speech': 'local', 'model': 'analysis:latest'},
    'refinement_editing': {'provider': 'local', 'model': 'editor:latest'},
}


def _cfg(cfg_dir):
    return json.loads((cfg_dir/'app.json').read_text(encoding='utf-8'))


def _set(cfg_dir, **values):
    cfg = _cfg(cfg_dir); cfg.update(values)
    (cfg_dir/'app.json').write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')


@pytest.fixture
def env(sandbox, monkeypatch):
    """Sandbox config for both option readers; refinement window and shutdown recorded."""
    root, cfg = sandbox
    monkeypatch.setattr(sd, 'CONFIG_DIR', cfg)
    import novel_translator.ui.refinement_run_dialog as rd
    rec = {'asked': [], 'answer': False, 'requested': 0, 'refine': [], 'timed': [], 'infos': [], 'outcome': {}}
    monkeypatch.setattr(ui, 'ask_shutdown', lambda parent, title, summary: rec['asked'].append(title) or rec['answer'])
    monkeypatch.setattr(ui, 'request_system_shutdown', lambda: rec.__setitem__('requested', rec['requested']+1) or True)
    monkeypatch.setattr(ui, 'show_timed_information', lambda parent, title, text, *a: rec['timed'].append((title, text)))
    class FakeDialog:
        def __init__(self, host, auto_source=None, auto_note='', batch=False):
            rec['refine'].append((auto_source, auto_note, host._job_busy()) if not batch else
                                 (auto_source, auto_note, 'batch', host.worker.isRunning()))
            name = auto_source.name if auto_source else ''
            self.auto_outcome = rec['outcome'].get(name, ('completed', ''))
        def exec(self): return 0
    monkeypatch.setattr(rd, 'RefinementRunDialog', FakeDialog)
    return root, cfg, rec


def _local_window(monkeypatch, rec):
    w = _window(monkeypatch)
    monkeypatch.setattr(ui.QMessageBox, 'information', lambda *a, **k: rec['infos'].append(a[1]))
    w.secrets = dict(w.secrets, **json.loads(json.dumps(LOCAL)))
    w._update_ready_state()
    return w


def _run(qt_app, monkeypatch, root, rec, script, on=True):
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _scripted_worker_class(started, script))
    w = _local_window(monkeypatch, rec)
    w._open_txt_path(str(_single_file(root)))
    w._update_ready_state()
    w.auto_refine.setChecked(on)
    w.start_translation()
    return w, started


# ---------------------------------------------------------------- the option
def test_off_on_a_new_install_and_audited():
    for name in ('app.json', 'defaults.json'):
        assert json.loads((ROOT/'config'/name).read_text(encoding='utf-8')).get(sd.AUTO_REFINE_KEY) is False, name


def test_runtime_audit_flags_a_non_boolean(tmp_path, monkeypatch):
    import shutil
    import novel_translator.runtime_audit as ra
    cfg = tmp_path/'config'; shutil.copytree(ROOT/'config', cfg)
    monkeypatch.setattr(ra, 'CONFIG_DIR', cfg)
    assert not [i for i in ra.validate_resources() if 'auto_refine' in i]
    _set(cfg, auto_refine_after_translation='yes')
    assert [i for i in ra.validate_resources() if 'auto_refine_after_translation' in i]


def test_box_sits_at_the_right_of_the_api_state_line(qt_app, env, monkeypatch):
    root, cfg, rec = env
    w = _local_window(monkeypatch, rec)
    try:
        row = w.api_runtime_label.parentWidget().layout()
        found = None
        for i in range(row.count()):
            lay = row.itemAt(i).layout()
            if lay is not None and lay.indexOf(w.api_runtime_label) >= 0:
                found = lay
        assert found is not None
        assert found.indexOf(w.api_runtime_label) == 0 and found.indexOf(w.auto_refine) == found.count()-1
        assert w.auto_refine.isChecked() is False and w.auto_refine.isEnabled()
    finally:
        w.close()


def test_box_and_advanced_settings_share_one_option(qt_app, env, monkeypatch):
    import novel_translator.ui.advanced_dialog as advanced
    root, cfg, rec = env
    w = _local_window(monkeypatch, rec)
    try:
        w.auto_refine.setChecked(True)
        assert _cfg(cfg)[sd.AUTO_REFINE_KEY] is True and sd.auto_refine_enabled()
        d = advanced.AdvancedSettingsDialog()
        try:
            assert d.auto_refine.isChecked() is True
            d.auto_refine.setChecked(False); d._save()
        finally:
            d.close()
        assert _cfg(cfg)[sd.AUTO_REFINE_KEY] is False
        assert d.confirm_shutdown.isChecked() is False                   # the other options untouched
        w._sync_auto_refine_box(); assert w.auto_refine.isChecked() is False
        w.auto_refine.setChecked(True)
        w._reset_settings()
        assert w.auto_refine.isChecked() is False and sd.auto_refine_enabled() is False
    finally:
        w.close()


def test_box_stays_usable_while_a_job_runs(qt_app, env, monkeypatch):
    root, cfg, rec = env
    w = _local_window(monkeypatch, rec)
    try:
        w._set_running_ui(True)
        assert not w.advanced_btn.isEnabled() and w.auto_refine.isEnabled()
        w.auto_refine.setChecked(True)
        assert '재정비 자동 시작 · 켬' in w.error_view.toPlainText()
        w._set_running_ui(False)
    finally:
        w.close()


def test_folder_mode_keeps_it_usable(qt_app, env, monkeypatch):
    root, cfg, rec = env
    _set(cfg, auto_refine_after_translation=True)
    w = _local_window(monkeypatch, rec)
    try:
        books = _folder(root, ('1.txt', '2.txt'))
        w._prepare_new_input(); w.input_mode = 'folder'; w.batch_root = books
        w._apply_profile('zh-ko'); w._scan_batch_files(); w._update_ready_state()
        assert w.auto_refine.isEnabled() and w.auto_refine.isChecked()
        assert '폴더(연속) 번역이면' in w.auto_refine.toolTip()
        assert w._auto_refine_wanted() is True
    finally:
        w.close()


# ---------------------------------------------------------------- folder: translate → refine → next file
def _run_folder(qt_app, monkeypatch, root, rec, scripts, names=('1.txt', '2.txt', '3.txt'), on=True):
    from test_v241_folder_auto_retry import _per_file_worker
    books = _folder(root, names); started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _per_file_worker(started, scripts))
    order = []
    orig = ui.MainWindow._start_next_batch_file
    def spy(self, *a, **k):
        if self.batch_index < len(self.batch_files): order.append(('translate', self.batch_files[self.batch_index].name))
        return orig(self, *a, **k)
    monkeypatch.setattr(ui.MainWindow, '_start_next_batch_file', spy)
    w = _local_window(monkeypatch, rec)
    w._prepare_new_input(); w.input_mode = 'folder'; w.batch_root = books
    w._apply_profile('zh-ko'); w._scan_batch_files(); w._update_ready_state()
    w.auto_refine.setChecked(on)
    w.start_translation()
    return w, started, order


def test_folder_refines_each_file_before_the_next_one(qt_app, env, monkeypatch):
    root, cfg, rec = env
    _set(cfg, confirm_shutdown_after_finish=True)
    w, started, order = _run_folder(qt_app, monkeypatch, root, rec, {
        '1.txt': [('partial', 2, 0), ('completed', 0, 0)], '2.txt': [('completed', 0, 0)], '3.txt': [('completed', 0, 0)]})
    try:
        assert _wait(qt_app, lambda: rec['asked'] or rec['infos']), (rec, w.error_view.toPlainText()[-800:])
        assert [p.name for p, *_ in rec['refine']] == ['1.ko.txt', '2.ko.txt', '3.ko.txt']
        assert all(r[2] == 'batch' and r[3] is False for r in rec['refine'])   # the worker had ended
        assert [s.source_path.name for s in started] == ['1.txt', '1.txt', '2.txt', '3.txt']   # retry first
        assert rec['asked'] == ['폴더 번역 완료']                              # once, after the last refinement
        summary = (w.last_result_path).read_text(encoding='utf-8')
        assert '파일별 한국어 소설 재정비' in summary and summary.count('\t완료') >= 3
        log = w.error_view.toPlainText()
        assert log.count('재정비 완료 · ') == 3 and '(끝나면 다음 파일로)' in log
    finally:
        w.close()


def test_folder_refines_an_unfinished_file_too_and_goes_on(qt_app, env, monkeypatch):
    root, cfg, rec = env
    w, started, order = _run_folder(qt_app, monkeypatch, root, rec, {
        '1.txt': [('partial', 2, 0), ('partial', 2, 0)], '2.txt': [('completed', 0, 0)]}, names=('1.txt', '2.txt'))
    try:
        assert _wait(qt_app, lambda: rec['timed'] or rec['infos']), rec
        assert [p.name for p, *_ in rec['refine']] == ['1.ko.partial.txt', '2.ko.txt']
        assert [s.source_path.name for s in started] == ['1.txt', '1.txt', '2.txt']   # retried first
        assert '재정비 건너뜀' not in w.error_view.toPlainText()
        assert rec['infos'] == [] and rec['timed'][0][0] == '폴더 번역 완료 · 일부 남음'   # closes by itself
    finally:
        w.close()


def test_folder_stop_in_the_refinement_window_ends_the_folder_run(qt_app, env, monkeypatch):
    root, cfg, rec = env
    _set(cfg, confirm_shutdown_after_finish=True)
    rec['outcome']['1.ko.txt'] = ('stopped', '')
    w, started, order = _run_folder(qt_app, monkeypatch, root, rec, {
        '1.txt': [('completed', 0, 0)], '2.txt': [('completed', 0, 0)]}, names=('1.txt', '2.txt'))
    try:
        assert _wait(qt_app, lambda: '다음 파일을 시작하기 전에 멈춤' in w.error_view.toPlainText()), w.error_view.toPlainText()[-600:]
        for _ in range(30): qt_app.processEvents()
        assert [s.source_path.name for s in started] == ['1.txt'] and rec['asked'] == []
        assert '재정비 창에서 중지 · 폴더 번역도 여기서 멈춥니다' in w.error_view.toPlainText()
        assert not w._job_busy() and w.start.text() == '번역 시작 / 계속'
    finally:
        w.close()


def test_folder_skipped_or_failed_refinement_goes_on(qt_app, env, monkeypatch):
    root, cfg, rec = env
    rec['outcome']['1.ko.txt'] = ('skipped', '같은 이름의 재정비 결과 파일이 이미 있어 덮어쓰지 않습니다.')
    rec['outcome']['2.ko.txt'] = ('failed', '서버 응답 없음')
    w, started, order = _run_folder(qt_app, monkeypatch, root, rec, {
        '1.txt': [('completed', 0, 0)], '2.txt': [('completed', 0, 0)], '3.txt': [('completed', 0, 0)]})
    try:
        assert _wait(qt_app, lambda: rec['timed'] or rec['infos'])
        assert [s.source_path.name for s in started] == ['1.txt', '2.txt', '3.txt']
        summary = w.last_result_path.read_text(encoding='utf-8')
        assert '건너뜀 · 같은 이름의 재정비 결과' in summary and '오류 · 서버 응답 없음' in summary
    finally:
        w.close()


def test_folder_off_never_refines(qt_app, env, monkeypatch):
    root, cfg, rec = env
    w, started, order = _run_folder(qt_app, monkeypatch, root, rec, {'1.txt': [('completed', 0, 0)]}, names=('1.txt',), on=False)
    try:
        assert _wait(qt_app, lambda: rec['infos'])
        assert rec['refine'] == [] and rec['infos'] == ['폴더 번역 완료']
        assert '재정비' not in w.last_result_path.read_text(encoding='utf-8')
    finally:
        w.close()


def test_api_engines_that_cannot_refine_grey_it_with_the_reason(qt_app, env, monkeypatch):
    root, cfg, rec = env
    w = _window(monkeypatch)                                           # Gemini only
    try:
        w._update_ready_state()
        assert not w.auto_refine.isEnabled() and '로컬 LLM만' in w.auto_refine.toolTip()
    finally:
        w.close()


# ---------------------------------------------------------------- translation → refinement
def test_finished_translation_starts_the_refinement_without_windows(qt_app, env, monkeypatch):
    root, cfg, rec = env
    _set(cfg, confirm_shutdown_after_finish=True)                       # asked later, by the 재정비 window
    monkeypatch.setattr(sd, 'CONFIG_DIR', cfg)
    w, started = _run(qt_app, monkeypatch, root, rec, [('partial', 2, 0), ('completed', 0, 0)])
    try:
        assert _wait(qt_app, lambda: rec['refine']), (rec, w.error_view.toPlainText()[-500:])
        assert len(started) == 2                                        # the automatic retry ran first
        path, note, busy = rec['refine'][0]
        assert path.name == 'book.ko.txt' and '번역 걸린 시간' in note and busy is False
        assert rec['infos'] == [] and rec['timed'] == [] and rec['asked'] == []
        log = w.error_view.toPlainText()
        assert '번역 완료 → 한국어 소설 재정비를 자동으로 시작합니다 · book.ko.txt' in log
        assert log.count('작업 끝 · 걸린 시간') == 1
    finally:
        w.close()


def test_off_keeps_the_usual_completion(qt_app, env, monkeypatch):
    root, cfg, rec = env
    w, started = _run(qt_app, monkeypatch, root, rec, [('completed', 0, 0)], on=False)
    try:
        assert _wait(qt_app, lambda: rec['infos'])
        assert rec['infos'] == ['번역 완료'] and rec['refine'] == [] and rec['timed'] == []
    finally:
        w.close()


def test_turned_on_during_the_run_is_used_at_the_end(qt_app, env, monkeypatch):
    root, cfg, rec = env
    w, started = _run(qt_app, monkeypatch, root, rec, [('partial', 2, 0), ('completed', 0, 0)], on=False)
    try:
        w.auto_refine.setChecked(True)                                  # changed mid-run
        assert _wait(qt_app, lambda: rec['refine'] or rec['infos'])
        assert rec['refine'] and rec['infos'] == []
    finally:
        w.close()


def test_unfinished_result_is_refined_after_the_retries(qt_app, env, monkeypatch):
    root, cfg, rec = env
    _set(cfg, confirm_shutdown_after_finish=True)                       # asked by the 재정비 window later
    w, started = _run(qt_app, monkeypatch, root, rec, [('partial', 2, 0), ('partial', 2, 0)])
    try:
        assert _wait(qt_app, lambda: rec['refine']), rec
        assert len(started) == 2                                        # the automatic retry ran first
        assert rec['refine'][0][0].name == 'book.ko.partial.txt'
        assert rec['infos'] == [] and rec['timed'] == [] and rec['asked'] == []
        log = w.error_view.toPlainText()
        assert '미번역 남음 · ' in log and '새로 번역된 문단만 다듬음' in log
        assert '번역 완료 → 한국어 소설 재정비를 자동으로 시작합니다 · book.ko.partial.txt' in log
    finally:
        w.close()


def test_a_skipped_refinement_still_asks_about_the_pc_at_the_translation_end(qt_app, env, monkeypatch):
    import novel_translator.refinement_runtime as rr
    root, cfg, rec = env
    _set(cfg, confirm_shutdown_after_finish=True); rec['answer'] = False
    monkeypatch.setattr(rr, 'MAX_REFINEMENT_BYTES', 1)
    w, started = _run(qt_app, monkeypatch, root, rec, [('partial', 2, 0), ('partial', 2, 0)])
    try:
        assert _wait(qt_app, lambda: rec['asked'] and rec['timed'])
        assert rec['asked'] == ['번역 완료 · 일부 남음'] and rec['refine'] == []
    finally:
        w.close()


def test_refinement_limits_skip_with_the_reason(qt_app, env, monkeypatch):
    import novel_translator.refinement_runtime as rr
    root, cfg, rec = env
    monkeypatch.setattr(rr, 'MAX_REFINEMENT_BYTES', 1)                 # any result is too large
    w, started = _run(qt_app, monkeypatch, root, rec, [('completed', 0, 0)])
    try:
        assert _wait(qt_app, lambda: rec['timed'])
        assert rec['refine'] == [] and '이상이라 재정비할 수 없습니다' in rec['timed'][0][1]
    finally:
        w.close()


# ---------------------------------------------------------------- the refinement window started by itself
def _auto_dialog(monkeypatch, tmp_path, host, prepare=None):
    import novel_translator.ui.refinement_run_dialog as rd
    from PySide6.QtCore import QThread, Signal
    monkeypatch.setattr(rd, 'set_translation_awake', lambda on: True)
    boxes = []
    for name in ('information', 'warning', 'question'):
        monkeypatch.setattr(rd.QMessageBox, name, lambda *a, n=name, **k: boxes.append(n) or rd.QMessageBox.No)
    monkeypatch.setattr(rd.QMessageBox, 'exec', lambda self: boxes.append('exec') or rd.QMessageBox.No)
    workers = []
    class FakeWorker(QThread):
        progressed = Signal(int, int, int); outcome = Signal(object); failed = Signal(str); activity = Signal(str)
        def __init__(self, *a, **k): super().__init__(); workers.append(k)
        def stop(self): pass
        def run(self): self.outcome.emit({'status': 'stopped', 'seconds': 1})
    monkeypatch.setattr(rd, 'RefinementWorker', FakeWorker)
    monkeypatch.setattr(rd, 'estimate_run', lambda *a, **k: {'requests_left': 10**6, 'paragraphs': 10**6, 'done': 0})
    src = tmp_path/'소설.ko.txt'; src.write_text('어색한 첫 문단입니다.\n\n"어색한 대사입니다."\n', encoding='utf-8')
    if prepare: prepare(rd, src)
    d = rd.RefinementRunDialog(host, auto_source=src, auto_note='번역 걸린 시간 1분')
    return d, boxes, workers


def test_auto_window_starts_without_any_question(qt_app, monkeypatch, tmp_path):
    from test_v26_shutdown_prompt import _Host
    host = _Host(False)
    d, boxes, workers = _auto_dialog(monkeypatch, tmp_path, host)
    try:
        d.show()
        assert _wait(qt_app, lambda: workers)
        assert boxes == [] and d.source.name == '소설.ko.txt'          # the long-run estimate was not asked
        assert '자동 시작' in d.log_view.toPlainText() and '번역 걸린 시간 1분' in d.log_view.toPlainText()
        assert d._auto_run is False                                     # a later click asks as before
        _wait(qt_app, lambda: d.worker is None)
    finally:
        d.close()


def test_auto_window_retries_only_preserved_paragraphs(qt_app, monkeypatch, tmp_path):
    from test_v26_shutdown_prompt import _Host
    host = _Host(False)
    def prepare(rd, src):
        rd.refined_output_path(src).write_text('이전 결과', encoding='utf-8')
        monkeypatch.setattr(rd, 'retry_status', lambda *a, **k: 3)
    d, boxes, workers = _auto_dialog(monkeypatch, tmp_path, host, prepare)
    try:
        d.show()
        assert _wait(qt_app, lambda: workers)
        assert boxes == [] and workers[0].get('update_existing') is True
        _wait(qt_app, lambda: d.worker is None)
    finally:
        d.close()


@pytest.mark.parametrize('case', ['existing_result', 'other_model'])
def test_auto_window_never_overwrites_and_says_why(qt_app, monkeypatch, tmp_path, case):
    from test_v26_shutdown_prompt import _Host
    host = _Host(False)
    host.issues = []; host.on_worker_issue = host.issues.append
    def prepare(rd, src):
        if case == 'existing_result':
            rd.refined_output_path(src).write_text('이전 결과', encoding='utf-8')
            monkeypatch.setattr(rd, 'retry_status', lambda *a, **k: 0)
        else:
            monkeypatch.setattr(rd, 'resume_conflict', lambda *a, **k: (120, 'old:latest'))
    d, boxes, workers = _auto_dialog(monkeypatch, tmp_path, host, prepare)
    try:
        d.show()
        assert _wait(qt_app, lambda: host.asked)
        assert boxes == [] and workers == []
        assert '재정비 자동 시작 건너뜀' in d.status.text() and '건너뜀' in d.log_view.toPlainText()
        assert host.issues and host.issues[0].startswith('재정비 자동 시작 건너뜀')
        title, summary, parent, log = host.asked[0]
        assert title == '번역 완료 · 재정비 건너뜀' and parent is d and '번역은 완료했지만' in summary
    finally:
        d.close()


def test_manual_window_still_asks_as_before(qt_app, monkeypatch, tmp_path):
    from test_v26_shutdown_prompt import _Host
    import novel_translator.ui.refinement_run_dialog as rd
    host = _Host(False)
    d, boxes, workers = _auto_dialog(monkeypatch, tmp_path, host)
    try:
        d.show(); assert _wait(qt_app, lambda: workers); _wait(qt_app, lambda: d.worker is None)
        boxes.clear(); workers.clear()
        d._start()                                                      # the user's own click
        assert boxes == ['exec'] and workers == []                      # the long-run question, answered No
    finally:
        d.close()


def test_batch_window_closes_by_itself_without_the_shutdown_question(qt_app, monkeypatch, tmp_path):
    from test_v26_shutdown_prompt import _Host
    import novel_translator.ui.refinement_run_dialog as rd
    from PySide6.QtCore import QThread, Signal
    from PySide6.QtWidgets import QDialog
    host = _Host(True)
    monkeypatch.setattr(rd, 'set_translation_awake', lambda on: True)
    monkeypatch.setattr(rd.RefinementRunDialog, 'BATCH_CLOSE_MS', 10)
    out = tmp_path/'sub'/'소설.ko.refined.txt'
    class FakeWorker(QThread):
        progressed = Signal(int, int, int); outcome = Signal(object); failed = Signal(str); activity = Signal(str)
        def __init__(self, source, output, *a, **k): super().__init__(); self.output = output
        def stop(self): pass
        def run(self):
            self.outcome.emit({'status': 'completed', 'done': 1, 'total': 1, 'changed': 1, 'identical': 0, 'preserved': 0,
                               'seconds': 5, 'output': str(self.output), 'report': str(self.output)+'.review.txt'})
    monkeypatch.setattr(rd, 'RefinementWorker', FakeWorker)
    (tmp_path/'sub').mkdir()
    src = tmp_path/'sub'/'소설.ko.txt'; src.write_text('어색한 첫 문단입니다.\n\n"어색한 대사입니다."\n', encoding='utf-8')
    host.custom_output_dir = tmp_path                                   # the folder run's root
    d = rd.RefinementRunDialog(host, auto_source=src, batch=True)
    try:
        assert d.output == out                                          # beside its translation (subfolder kept)
        d.show()
        assert _wait(qt_app, lambda: d.result() == QDialog.Accepted and d.auto_outcome[0])
        assert d.auto_outcome == ('completed', '') and host.asked == [] and host.shut == 0
    finally:
        d.close()


def test_batch_window_user_stop_is_reported(qt_app, monkeypatch, tmp_path):
    from test_v26_shutdown_prompt import _Host
    import novel_translator.ui.refinement_run_dialog as rd
    host = _Host(True)
    monkeypatch.setattr(rd.RefinementRunDialog, 'BATCH_CLOSE_MS', 10)
    d, boxes, workers = _auto_dialog(monkeypatch, tmp_path, host)      # its fake run ends 'stopped'
    d._batch_auto = True
    try:
        d.show()
        assert _wait(qt_app, lambda: d.auto_outcome[0])
        assert d.auto_outcome[0] == 'stopped' and host.asked == [] and boxes == []
    finally:
        d.close()


def test_auto_window_closed_before_its_start_never_starts(qt_app, monkeypatch, tmp_path):
    from test_v26_shutdown_prompt import _Host
    host = _Host(False)
    d, boxes, workers = _auto_dialog(monkeypatch, tmp_path, host)
    try:
        d.show(); qt_app.processEvents(); d.close()                     # closed within the 0.3 s
        import time
        t = time.time()
        while time.time() - t < 0.6: qt_app.processEvents()
        assert workers == [] and boxes == []
    finally:
        d.close()


def test_batch_skip_is_logged_once_by_the_folder_run(qt_app, monkeypatch, tmp_path):
    from test_v26_shutdown_prompt import _Host
    import novel_translator.ui.refinement_run_dialog as rd
    host = _Host(False); host.issues = []; host.on_worker_issue = host.issues.append
    monkeypatch.setattr(rd.RefinementRunDialog, 'BATCH_CLOSE_MS', 10)
    def prepare(rd_, src):
        monkeypatch.setattr(rd_, 'resume_conflict', lambda *a, **k: (5, 'old:latest'))
    d, boxes, workers = _auto_dialog(monkeypatch, tmp_path, host, prepare)
    d._batch_auto = True
    try:
        d.show()
        assert _wait(qt_app, lambda: d.auto_outcome[0])
        assert d.auto_outcome[0] == 'skipped' and '중지한 기록' in d.auto_outcome[1]
        assert host.issues == [] and host.asked == [] and workers == []
    finally:
        d.close()


def test_auto_window_refines_an_unfinished_result_without_a_question(qt_app, monkeypatch, tmp_path):
    from test_v26_shutdown_prompt import _Host
    host = _Host(False)
    def prepare(rd, src):
        pass
    d, boxes, workers = _auto_dialog(monkeypatch, tmp_path, host, prepare)
    part = tmp_path/'소설.ko.partial.txt'; part.write_text('어색한 첫 문단입니다.\n\n他静静地看着窗外的雨。\n', encoding='utf-8')
    d._set_source(part)
    try:
        d.show()
        assert _wait(qt_app, lambda: workers)
        assert boxes == [] and '새로 번역된 문단만 다듬음' in d.log_view.toPlainText()
        assert d.output.name == '소설.ko.partial.refined.txt'
        _wait(qt_app, lambda: d.worker is None)
    finally:
        d.close()


def test_manual_unfinished_question_says_the_edits_are_kept(qt_app, monkeypatch, tmp_path):
    from test_v26_shutdown_prompt import _Host
    import novel_translator.ui.refinement_run_dialog as rd
    host = _Host(False)
    d, boxes, workers = _auto_dialog(monkeypatch, tmp_path, host)
    try:
        d.show(); assert _wait(qt_app, lambda: workers); _wait(qt_app, lambda: d.worker is None)
        texts = []
        monkeypatch.setattr(rd.QMessageBox, 'exec', lambda self: texts.append((self.text(), self.defaultButton() is self.button(rd.QMessageBox.No))) or rd.QMessageBox.No)
        part = tmp_path/'소설.ko.partial.txt'; part.write_text('어색한 첫 문단입니다.\n', encoding='utf-8')
        d._set_source(part); workers.clear()
        d._start()
        assert workers == [] and len(texts) == 1
        text, default_no = texts[0]
        assert '새로 번역된 문단만 다듬습니다' in text and default_no          # still asked, [아니오] default
        assert '새로 번역된 문단만 다듬습니다' in d.status.text()
    finally:
        d.close()
