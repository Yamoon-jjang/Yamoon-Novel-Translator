"""v2.6.1 flow fixes found by the 2026-11-02 flow review.

- Korean refinement never starts while a translation job is still alive (running, analysing,
  waiting to resume, between folder files): one local LLM, one sleep/shutdown setting.
- A job paused by hand lets the PC sleep; [다시 진행] keeps it awake again.
- A stopped refinement continues after the TXT was edited or saved with other line endings:
  unchanged paragraphs keep their edit, only changed ones are edited again.
- Refinement resume files and 원문 창 copies nobody used for the keep period are removed.
- The refinement window: no unfinished translation or too-large TXT pre-filled, a too-large
  TXT is said at once, an unfinished one is confirmed (default No), results never get '.ko' twice,
  a refined TXT keeps the translation's reference table.
- An edited opened file's result goes beside that file; [처음부터 다시] defaults to [아니오].
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import novel_translator.ui.main_window as ui
from novel_translator.refinement_runtime import run_refinement, prune_refinement_jobs, peek_guidance
from novel_translator.refinement_links import record_translation, find_reference
from test_alpha17 import _window, qt_app, sandbox  # noqa: F401 (fixtures)
from test_refinement_reference_and_safety import Editor, write


# ---------------------------------------------------------------- no refinement during a job
@pytest.fixture
def opened(monkeypatch):
    import novel_translator.ui.refinement_run_dialog as rd
    shown = []
    class Fake:
        def __init__(self, host): shown.append(host)
        def exec(self): return 0
    monkeypatch.setattr(rd, 'RefinementRunDialog', Fake)
    return shown


def test_refinement_waits_for_every_stage_of_a_translation_job(qt_app, sandbox, monkeypatch, opened):
    infos = []
    w = _window(monkeypatch)
    monkeypatch.setattr(ui.QMessageBox, 'information', lambda *a, **k: infos.append(a[1]))
    try:
        w.open_refinement_run()
        assert len(opened) == 1 and not infos                        # idle: opens as before
        w._job_active = True                                         # between folder files / retries
        w.open_refinement_run()
        w._job_active = False
        w._resume_timer.start()                                      # waiting to resume after a pause
        w.open_refinement_run()
        w._resume_timer.stop()
        class Busy:
            def isRunning(self): return True
        w._term_thread = Busy()                                      # names / speech analysis
        w.open_refinement_run()
        w._term_thread = None
        assert len(opened) == 1 and infos == ['번역 작업 중'] * 3
        w.open_refinement_run()
        assert len(opened) == 2
    finally:
        w.close()


def test_job_running_flag_follows_the_running_ui(qt_app, sandbox, monkeypatch):
    w = _window(monkeypatch)
    try:
        assert not w._job_busy()
        w._set_running_ui(True); assert w._job_busy()
        w._set_running_ui(False); assert not w._job_busy()
    finally:
        w.close()


def test_a_hand_paused_job_lets_the_pc_sleep(qt_app, sandbox, monkeypatch):
    awake = []
    monkeypatch.setattr(ui, 'set_translation_awake', lambda on: awake.append(bool(on)) or True)
    w = _window(monkeypatch)
    try:
        class Running:
            def isRunning(self): return True
            def pause(self): pass
            def resume(self): pass
        w.worker = Running()
        awake.clear()
        w._toggle_pause(); assert awake == [False]                   # paused: sleep allowed
        w._toggle_pause(); assert awake == [False, True]             # resumed: kept awake again
        w.worker = None
    finally:
        w.close()


# ---------------------------------------------------------------- refinement resume after edits
PARAS = [f'어색한 문장 {i}번입니다. 그는 어색한 표정으로 웃었다.' for i in range(1, 41)]


def _stop_after(ed, n):
    return lambda: ed.calls.count('edit') >= n


def _first_run(tmp_path, text, model='m'):
    src = tmp_path / '책.ko.txt'; src.write_bytes(text.encode('utf-8'))
    ed = Editor()
    res = run_refinement(src, tmp_path / 'out.txt', ed, model, 'k', checkpoint_dir=tmp_path / 'cp',
                         links_root=tmp_path, max_items=5, stop=_stop_after(ed, 3))
    assert res['status'] == 'stopped' and res['done'] == 15
    return src


def _second_run(tmp_path, src, model='m'):
    ed, events = Editor(), []
    res = run_refinement(src, tmp_path / 'out.txt', ed, model, 'k', checkpoint_dir=tmp_path / 'cp',
                         links_root=tmp_path, max_items=5, event=events.append)
    return res, ed, events


def test_unchanged_paragraphs_keep_their_edit_after_a_small_change(tmp_path):
    src = _first_run(tmp_path, '\r\n\r\n'.join(PARAS))
    edited = list(PARAS); edited[2] = '어색한 문장 3번을 고쳤습니다.'          # inside the finished part
    src.write_bytes('\n\n'.join(edited).encode('utf-8'))                  # and LF instead of CRLF
    res, ed, events = _second_run(tmp_path, src)
    assert res['status'] == 'completed'
    assert any('그대로인 14문단' in e for e in events), events
    # 14 kept + 26 edited now; the requests cover only the changed and unfinished paragraphs.
    assert ed.calls.count('edit') == 6, ed.calls                                           # P3 alone + 25 left in 5s
    assert len(list((tmp_path / 'cp').glob('*.json'))) == 0              # finished: no file left
    out = (tmp_path / 'out.txt').read_text(encoding='utf-8-sig')
    assert '어색한' not in out


def test_a_line_added_at_the_start_moves_every_kept_edit_with_its_paragraph(tmp_path):
    src = _first_run(tmp_path, '\n\n'.join(PARAS))
    src.write_bytes('\n\n'.join(['새로 넣은 첫 줄입니다.'] + PARAS).encode('utf-8'))
    res, ed, events = _second_run(tmp_path, src)
    assert any('그대로인 15문단' in e for e in events), events
    assert res['status'] == 'completed'


def test_repeated_short_lines_never_take_another_place_s_edit(tmp_path):
    paras = []
    for i in range(1, 21):
        paras += [f'어색한 장면 {i}.', '……']
    src = _first_run(tmp_path, '\n\n'.join(paras))
    # The first half is removed: the kept '……' lines are the ones of the second half only.
    src.write_bytes('\n\n'.join(paras[30:]).encode('utf-8'))
    res, ed, events = _second_run(tmp_path, src)
    assert not any('그대로인' in e for e in events), events               # nothing finished survives
    assert res['status'] == 'completed'


def test_another_editing_model_starts_over_and_keeps_the_old_file(tmp_path):
    src = _first_run(tmp_path, '\n\n'.join(PARAS))
    src.write_bytes(('\n\n'.join(PARAS) + '\n\n추가 문단.').encode('utf-8'))
    ed, events = Editor(), []
    res = run_refinement(src, tmp_path / 'out2.txt', ed, 'other', 'k', checkpoint_dir=tmp_path / 'cp',
                         links_root=tmp_path, max_items=5, event=events.append,
                         stop=_stop_after(ed, 1))
    assert not any('그대로인' in e for e in events)
    assert len(list((tmp_path / 'cp').glob('*.json'))) == 2              # the old run is not deleted


def test_a_resume_of_the_same_text_is_unchanged(tmp_path):
    src = _first_run(tmp_path, '\n\n'.join(PARAS))
    res, ed, events = _second_run(tmp_path, src)
    assert any('건너뛰고 이어서' in e for e in events) and not any('그대로인' in e for e in events)
    assert ed.calls.count('edit') == 5


def test_sample_analysis_of_an_edited_txt_is_shown_and_reused(tmp_path):
    # v2.6.2 보완 3: the analysis finds names in the book (a book with no names has no table)
    named = [f'어색한 은지의 문장 {i}번입니다. 민수가 어색한 표정으로 웃었다.' for i in range(1, 41)]
    src = _first_run(tmp_path, '\n\n'.join(named))
    where, _, table = peek_guidance(src, checkpoint_dir=tmp_path / 'cp', links_root=tmp_path)
    assert where == 'analysis' and table
    src.write_bytes(('\n\n'.join(named) + '\n\n추가.').encode('utf-8'))
    where2, _, table2 = peek_guidance(src, checkpoint_dir=tmp_path / 'cp', links_root=tmp_path)
    assert where2 == 'analysis' and table2 == table
    res, ed, _ = _second_run(tmp_path, src)
    assert 'analysis' not in ed.calls


# ---------------------------------------------------------------- old records go
def test_resume_files_unused_for_the_keep_period_are_removed(tmp_path):
    folder = tmp_path / 'jobs'; folder.mkdir()
    old, new = folder / 'text-old.json', folder / 'text-new.json'
    old.write_text('{}', encoding='utf-8'); new.write_text('{}', encoding='utf-8')
    past = time.time() - 91 * 86400
    os.utime(old, (past, past))
    assert prune_refinement_jobs(90, folder) == 1
    assert not old.exists() and new.exists()
    assert prune_refinement_jobs(0, folder) == 0 and new.exists()        # 0 keeps everything


def test_closing_removes_old_resume_files_and_paste_copies(qt_app, sandbox, monkeypatch):
    import novel_translator.refinement_runtime as rr
    root, _ = sandbox
    jobs = root / 'jobs'; jobs.mkdir()
    monkeypatch.setattr(rr, 'DATA_DIR', root / 'rdata')
    (root / 'rdata' / 'refinement_jobs').mkdir(parents=True)
    old_job = root / 'rdata' / 'refinement_jobs' / 'text-a.json'; old_job.write_text('{}', encoding='utf-8')
    paste = root / 'data' / 'paste'; paste.mkdir(parents=True)
    old_paste, new_paste = paste / '붙여넣기_1.txt', paste / '붙여넣기_2.txt'
    old_paste.write_text('a', encoding='utf-8'); new_paste.write_text('b', encoding='utf-8')
    past = time.time() - 120 * 86400
    for f in (old_job, old_paste):
        os.utime(f, (past, past))
    w = _window(monkeypatch)
    try:
        w._clean_side_records(90)
    finally:
        w.close()
    assert not old_job.exists() and not old_paste.exists() and new_paste.exists()


# ---------------------------------------------------------------- refinement window
def _host(root, last=None, source=None):
    from PySide6.QtWidgets import QWidget
    host = QWidget(); host.secrets = {}; host.source_path = source; host.last_result_path = last
    host._selected_model_id = lambda: ''
    host.custom_output_dir = None
    return host


def test_window_never_prefills_an_unfinished_translation(qt_app, sandbox):
    from novel_translator.ui.refinement_run_dialog import RefinementRunDialog
    root, _ = sandbox
    part = root / 'book.ko.partial.txt'; part.write_text('그는 창밖을 보았다.\n' * 20, encoding='utf-8')
    d = RefinementRunDialog(_host(root, last=part))
    assert d.source is None
    full = root / 'book.ko.txt'; full.write_text('그는 창밖을 보았다.\n' * 20, encoding='utf-8')
    d2 = RefinementRunDialog(_host(root, last=full))
    assert d2.source == full


def test_an_unfinished_translation_is_confirmed_with_no_as_default(qt_app, sandbox, monkeypatch):
    import novel_translator.ui.refinement_run_dialog as rd
    root, _ = sandbox
    part = root / 'book.ko.partial.txt'; part.write_text('그는 창밖을 보았다.\n' * 20, encoding='utf-8')
    d = rd.RefinementRunDialog(_host(root))
    d._set_source(part)
    assert '미완료 번역 결과' in d.status.text()
    asked = []
    class Box:
        Question = rd.QMessageBox.Question; Yes = rd.QMessageBox.Yes; No = rd.QMessageBox.No
        def __init__(self, *a): asked.append(a[1])
        def setDefaultButton(self, b): asked.append(b)
        def exec(self): return rd.QMessageBox.No
    monkeypatch.setattr(rd, 'QMessageBox', Box)
    started = []
    monkeypatch.setattr(rd, 'RefinementWorker', lambda *a, **k: started.append(a))
    d._start()
    assert asked == ['미완료 번역 결과', rd.QMessageBox.No if hasattr(rd.QMessageBox, 'No') else Box.No] and not started


def test_a_too_large_txt_is_said_at_once(qt_app, sandbox, monkeypatch):
    import novel_translator.ui.refinement_run_dialog as rd
    root, _ = sandbox
    monkeypatch.setattr(rd, 'MAX_REFINEMENT_BYTES', 100)
    big = root / 'big.ko.txt'; big.write_text('그는 창밖을 보았다.\n' * 20, encoding='utf-8')
    d = rd.RefinementRunDialog(_host(root, last=big))
    assert d.source is None                                               # not pre-filled
    d._routes_ready = True
    d._set_source(big)
    assert rd.MAX_REFINEMENT_LABEL + ' 미만' in d.status.text() and not d.start.isEnabled()
    small = root / 's.ko.txt'; small.write_text('그는 웃었다.', encoding='utf-8')
    d._set_source(small)
    assert rd.MAX_REFINEMENT_LABEL not in d.status.text() and d.start.isEnabled()          # nothing added for a small TXT


@pytest.mark.parametrize('name,out', [
    ('book.txt', 'book.ko.refined.txt'),
    ('book.ko.txt', 'book.ko.refined.txt'),
    ('book.ko.partial.txt', 'book.ko.partial.refined.txt'),
    ('book.ko.refined.txt', 'book.ko.refined.refined.txt'),
])
def test_refined_names_never_get_ko_twice(tmp_path, name, out):
    from novel_translator.ui.refinement_run_dialog import refined_output_path
    assert refined_output_path(tmp_path / name).name == out


def test_a_refined_txt_keeps_the_translation_reference(tmp_path):
    src = write(tmp_path, 'novel.ko.txt', '어색한 은지의 문장입니다.\n\n어색한 민수의 문장입니다.')
    record_translation(src, src.read_text(encoding='utf-8'),
                       [{'speaker': '은지', 'listener': '민수', 'level': '해요체'}], {'恩智': '은지'}, root=tmp_path)
    out = tmp_path / 'novel.ko.refined.txt'
    res = run_refinement(src, out, Editor(), 'm', 'k', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path)
    assert res['guidance_source'] == 'translation'
    from novel_translator.textio import read_txt
    text, _ = read_txt(out, 'ko')
    ref = find_reference(tmp_path / 'copy-elsewhere.txt', text, tmp_path)   # found by its text
    assert ref and ref['speech'][0]['speaker'] == '은지' and '은지' in ref['names']


def test_a_sample_analysed_refinement_records_nothing(tmp_path):
    src = write(tmp_path, 'other.txt', '어색한 문장입니다.\n\n어색한 대사입니다.')
    run_refinement(src, tmp_path / 'other.ko.refined.txt', Editor(), 'm', 'k',
                   checkpoint_dir=tmp_path / 'cp', links_root=tmp_path)
    assert not (tmp_path / 'refinement_links.json').exists()


# ---------------------------------------------------------------- main window
def test_an_edited_opened_file_is_saved_beside_it(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    books = root / 'books'; books.mkdir()
    src = books / '책.txt'; src.write_text('第一章\n\n他静静地看着窗外的雨。', encoding='utf-8')
    w = _window(monkeypatch)
    try:
        w._open_txt_path(str(src))
        w.preview.setPlainText('第一章\n\n他静静地看着窗外的雨。\n\n她笑了。')
        assert w._use_preview_as_source()
        assert w.source_path.name.startswith('책_수정_')
        assert w._output_dir_for(w.source_path) == books
        out = root / 'myout'; out.mkdir(); w.custom_output_dir = out
        assert w._output_dir_for(w.source_path) == out                      # a chosen folder still wins
        w.custom_output_dir = None
        w.preview.setPlainText('第一章\n\n他静静地看着窗外的雨。\n\n她笑了。\n\n又一行。')
        assert w._use_preview_as_source()                                  # an edit of the edit
        assert w._output_dir_for(w.source_path) == books
    finally:
        w.close()
    w = _window(monkeypatch)
    try:
        w.preview.setPlainText('完全新的文字。她笑了。')
        assert w._use_preview_as_source()
        assert w._output_dir_for(w.source_path) == w._documents_output_dir()  # new pasted text: as before
    finally:
        w.close()


def test_start_over_defaults_to_no(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    src = root / '책.txt'; src.write_text('第一章\n\n他静静地看着窗外的雨。', encoding='utf-8')
    asked = []
    w = _window(monkeypatch)
    try:
        w._open_txt_path(str(src))
        monkeypatch.setattr(ui.QMessageBox, 'question', lambda *a: asked.append(a) or ui.QMessageBox.No)
        started = []
        monkeypatch.setattr(w, 'start_translation', lambda force=False: started.append(force))
        w._restart_translation()
        assert asked and asked[0][1] == '처음부터 다시 번역' and asked[0][-1] == ui.QMessageBox.No
        assert not started
    finally:
        w.close()


# ---------------------------------------------------------------- reference table after a stopped run
def test_a_translation_record_replaces_a_kept_sample_analysis(tmp_path):
    src = write(tmp_path, 'novel.ko.txt', '\n\n'.join(f'어색한 은지의 문장 {i}입니다.' for i in range(30)))
    ed = Editor()
    run_refinement(src, tmp_path / 'r.txt', ed, 'm', 'k', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path,
                   max_items=5, stop=lambda: ed.calls.count('edit') >= 1)
    assert 'analysis' in ed.calls                                         # no record then: sample analysis
    record_translation(src, src.read_text(encoding='utf-8'),
                       [{'speaker': '은지', 'listener': '민수', 'level': '해요체'}], {'恩智': '은지'}, root=tmp_path)
    assert peek_guidance(src, checkpoint_dir=tmp_path / 'cp', links_root=tmp_path)[0] == 'translation'
    ed2, events = Editor(), []
    res = run_refinement(src, tmp_path / 'r.txt', ed2, 'm', 'k', checkpoint_dir=tmp_path / 'cp',
                         links_root=tmp_path, max_items=5, event=events.append)
    assert res['guidance_source'] == 'translation' and '은지 → 민수: 해요체' in ed2.last_user
    assert 'analysis' not in ed2.calls and ed2.calls.count('edit') == 5    # finished group kept, no new analysis
    assert any('샘플 분석 대신 번역 기준표' in e for e in events)


def test_a_kept_translation_table_follows_a_redone_translation(tmp_path):
    src = write(tmp_path, 'novel.ko.txt', '\n\n'.join(f'어색한 은지의 문장 {i}입니다.' for i in range(30)))
    record_translation(src, src.read_text(encoding='utf-8'),
                       [{'speaker': '은지', 'listener': '민수', 'level': '해요체'}], {'恩智': '은지'}, root=tmp_path)
    ed = Editor()
    run_refinement(src, tmp_path / 'r.txt', ed, 'm', 'k', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path,
                   max_items=5, stop=lambda: ed.calls.count('edit') >= 1)
    record_translation(src, src.read_text(encoding='utf-8'),
                       [{'speaker': '은지', 'listener': '민수', 'level': '반말'}], {'恩智': '은지'}, root=tmp_path)
    ed2, events = Editor(), []
    run_refinement(src, tmp_path / 'r.txt', ed2, 'm', 'k', checkpoint_dir=tmp_path / 'cp',
                   links_root=tmp_path, max_items=5, event=events.append)
    assert '은지 → 민수: 반말' in ed2.last_user and ed2.calls.count('edit') == 5


def test_a_resume_with_the_same_translation_table_is_unchanged(tmp_path):
    src = write(tmp_path, 'novel.ko.txt', '\n\n'.join(f'어색한 은지의 문장 {i}입니다.' for i in range(30)))
    record_translation(src, src.read_text(encoding='utf-8'),
                       [{'speaker': '은지', 'listener': '민수', 'level': '해요체'}], {'恩智': '은지'}, root=tmp_path)
    ed = Editor()
    run_refinement(src, tmp_path / 'r.txt', ed, 'm', 'k', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path,
                   max_items=5, stop=lambda: ed.calls.count('edit') >= 1)
    ed2, events = Editor(), []
    run_refinement(src, tmp_path / 'r.txt', ed2, 'm', 'k', checkpoint_dir=tmp_path / 'cp',
                   links_root=tmp_path, max_items=5, event=events.append)
    assert any('이전 실행에서 이어 씀' in e for e in events)                 # as before
