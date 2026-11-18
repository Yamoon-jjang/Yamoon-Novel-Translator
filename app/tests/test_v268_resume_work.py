"""v2.6.8: 이전 작업 이어 열기.

A translation (one file, a folder, several dropped files) or a refinement that did not reach its
end leaves a note; at the next start the program asks, once per note, whether to open that work
again with the settings it ran with (folder list, excluded files, 파일 관계, 하위 폴더, 원문 언어,
재정비 자동 시작). Nothing starts by itself: [번역 시작 / 계속] / [재정비 시작] go on from the
saved records. A job that ended (finished, or finished with parts left) leaves no note, so nothing
is asked after it. 열지 않음 removes the note (the records stay); closing the question with its
X keeps it for the next start.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import novel_translator.ui.main_window as ui
from novel_translator import resume_work
from test_alpha17 import _fake_worker_class, _wait, qt_app, sandbox  # noqa: F401 (fixtures)
from test_v15_review import _single_file
from test_v241_folder_auto_retry import _folder, _per_file_worker
from test_v267_auto_refine import _local_window, env  # noqa: F401 (fixture)


# ---------------------------------------------------------------- the note itself
def test_mark_update_clear_only_touch_the_same_job(tmp_path):
    f = tmp_path/'a.txt'; f.write_text('x', encoding='utf-8')
    resume_work.mark('translation', {'kind': 'single', 'path': str(f), 'percent': -1})
    resume_work.update('translation', {'kind': 'single', 'path': str(tmp_path/'other.txt')}, percent=50)
    assert resume_work.get('translation')['percent'] == -1             # another job: untouched
    resume_work.update('translation', {'kind': 'single', 'path': str(f)}, percent=40)
    assert resume_work.get('translation')['percent'] == 40 and resume_work.get('translation')['saved_at']
    resume_work.clear('translation', {'path': str(tmp_path/'other.txt')})
    assert resume_work.get('translation') is not None                  # not that job's note
    resume_work.mark('refinement', {'kind': 'refine', 'path': str(f), 'output': str(f)})
    assert [s for s, _ in resume_work.pending()] == ['translation', 'refinement']
    resume_work.clear('translation', {'path': str(f)})
    assert resume_work.get('translation') is None and resume_work.get('refinement')
    resume_work.clear('refinement')
    assert not (resume_work.DATA_DIR/resume_work.FILE_NAME).exists()   # nothing left: no file


def test_a_note_whose_file_or_folder_is_gone_is_dropped(tmp_path):
    f = tmp_path/'a.txt'; f.write_text('x', encoding='utf-8')
    resume_work.mark('translation', {'kind': 'folder', 'root': str(tmp_path/'gone')})
    resume_work.mark('refinement', {'kind': 'refine', 'path': str(f)})
    assert [s for s, _ in resume_work.pending()] == ['refinement'] and resume_work.get('translation') is None
    resume_work.mark('translation', {'kind': 'folder', 'root': str(tmp_path), 'selection': [str(tmp_path/'x.txt')]})
    assert [s for s, _ in resume_work.pending()] == ['refinement']    # none of the dropped files left


def test_a_broken_note_file_never_breaks_the_start(tmp_path):
    resume_work.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (resume_work.DATA_DIR/resume_work.FILE_NAME).write_text('{망가진', encoding='utf-8')
    assert resume_work.pending() == []
    resume_work.mark('refinement', {'kind': 'refine', 'path': 'x'})       # written over
    assert resume_work.get('refinement')['path'] == 'x'


def test_question_texts_say_what_will_open():
    s = resume_work.question_text({'kind': 'single', 'path': 'C:/책/소설.txt', 'percent': 63, 'auto_refine': True, 'saved_at': '2026-11-18 12:00'})
    assert '소설.txt' in s and '63%' in s and '재정비 자동 시작: 켬' in s and '[번역 시작 / 계속]' in s and '2026-11-18 12:00' in s
    now = [f'C:/책/{i}.txt' for i in range(1, 13)]
    s = resume_work.question_text({'kind': 'folder', 'root': 'C:/책', 'current': 'C:/책/5.txt', 'relation_label': '한 작품의 연속 파일', 'auto_refine': False},
                                  {'now': now, 'gone': []})
    assert '파일 12개 중 5번째 파일(5.txt)까지 진행' in s and '한 작품의 연속 파일' in s and '재정비 자동 시작: 끔' in s and '끝난 파일은 건너뛰고' in s
    assert '지워졌거나' not in s
    s = resume_work.question_text({'kind': 'folder', 'root': 'C:/책', 'selection': ['a', 'b', 'c']})
    assert '끌어다 놓은 파일 3개' in s
    s = resume_work.question_text({'kind': 'refine', 'path': 'C:/책/소설.ko.txt'})
    assert '재정비하던 TXT' in s and '소설.ko.txt' in s and '[재정비 시작]' in s


# ---------------------------------------------------------------- helpers
def _stopping_worker(started, at=None):
    """Scripted per file: 'stop' = the user stopped it (as the real worker reports a stop)."""
    Base = _fake_worker_class(started)
    class W(Base):
        def run(self):
            self.out_dir.mkdir(parents=True, exist_ok=True)
            name = self.source_path.name
            if at is None or name == at:
                out = self.out_dir/(self.source_path.stem+'.ko.partial.txt'); out.write_text('번역', encoding='utf-8')
                self.progress.emit(3, 10, '번역 중')
                self.result_ready.emit('stopped', str(out), 0); self.paused.emit('사용자 중지', True)
                return
            out = self.out_dir/(self.source_path.stem+'.ko.txt'); out.write_text('번역', encoding='utf-8')
            self.result_ready.emit('completed', str(out), 0); self.finished_ok.emit(str(out))
    return W


def _answer(w, qt_app, label):
    assert _wait(qt_app, lambda: getattr(w, '_resume_box', None) is not None)
    box = w._resume_box
    text = box.text()
    btn = next(b for b in box.buttons() if b.text() == label) if label else None
    if btn is None:
        box.reject()                # the window's X
    else:
        btn.click()
    for _ in range(20): qt_app.processEvents()
    return text


def _idle(qt_app, w):
    _wait(qt_app, lambda: not (w.worker and w.worker.isRunning()) and not w._job_busy())
    for _ in range(20): qt_app.processEvents()


# ---------------------------------------------------------------- one file
def test_a_stopped_file_is_asked_about_and_reopens_with_its_settings(qt_app, env, monkeypatch):
    root, cfg, rec = env
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _stopping_worker(started))
    book = _single_file(root)
    w = _local_window(monkeypatch, rec)
    try:
        w._open_txt_path(str(book)); w.auto_refine.setChecked(True); w.start_translation()
        _idle(qt_app, w)
        note = resume_work.get('translation')
        assert note['kind'] == 'single' and Path(note['path']) == book and note['auto_refine'] is True
        assert note['percent'] == 30 and note['profile'] == w.pm.active.id
    finally:
        w.close()
    # next start: the option was turned off meanwhile; opening brings the run's choice back
    w = _local_window(monkeypatch, rec)
    try:
        w.auto_refine.setChecked(False)
        w.offer_previous_work()
        text = _answer(w, qt_app, '열기')
        assert 'book.txt' in text and '30%' in text and '재정비 자동 시작: 켬' in text
        assert w.input_mode == 'single' and w.source_path == book and w.auto_refine.isChecked()
        assert '이전 작업 열기' in w.error_view.toPlainText() and not w._job_busy()   # nothing started
        assert resume_work.get('translation') is not None             # still unfinished until it ends
    finally:
        w.close()


def test_open_not_removes_the_note_and_x_keeps_it(qt_app, env, monkeypatch):
    root, cfg, rec = env
    book = _single_file(root)
    resume_work.mark('translation', {'kind': 'single', 'path': str(book), 'percent': 10, 'auto_refine': False})
    w = _local_window(monkeypatch, rec)
    try:
        w.offer_previous_work(); _answer(w, qt_app, None)                 # X
        assert resume_work.get('translation') and w.source_path is None
        w.offer_previous_work(); _answer(w, qt_app, '열지 않음')
        assert resume_work.get('translation') is None and w.source_path is None
        w.offer_previous_work()
        for _ in range(20): qt_app.processEvents()
        assert getattr(w, '_resume_box', None) is None                    # nothing more to ask
    finally:
        w.close()


@pytest.mark.parametrize('script', [[('completed', 0, 0)], [('partial', 2, 0)]])
def test_a_file_that_reached_its_end_leaves_no_note(qt_app, env, monkeypatch, script):
    from test_v15_review import _scripted_worker_class
    root, cfg, rec = env
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _scripted_worker_class(started, script))
    w = _local_window(monkeypatch, rec)
    try:
        w._open_txt_path(str(_single_file(root))); w.start_translation()
        assert _wait(qt_app, lambda: rec['infos'] or rec['timed']), rec
        _idle(qt_app, w)
        assert resume_work.get('translation') is None
    finally:
        w.close()


def test_pasted_text_is_not_reopened(qt_app, env, monkeypatch):
    root, cfg, rec = env
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _stopping_worker(started))
    w = _local_window(monkeypatch, rec)
    try:
        w.preview.setPlainText('第一章\n\n他静静地看着窗外的雨。'); w._update_ready_state()
        w.start_translation(); _idle(qt_app, w)
        assert started and w._paste_active and resume_work.get('translation') is None
    finally:
        w.close()


# ---------------------------------------------------------------- folder / several files
def _folder_window(qt_app, monkeypatch, rec, books, started, worker, relation='continuous', exclude=None, on=True):
    monkeypatch.setattr(ui, 'TranslationWorker', worker)
    w = _local_window(monkeypatch, rec)
    w._open_folder_path(str(books))
    w.batch_relation.setCurrentIndex(w.batch_relation.findData(relation))
    if exclude:
        w.batch_excluded.add(str((books/exclude).resolve())); w._scan_batch_files()
    w._update_ready_state(); w.auto_refine.setChecked(on)
    return w


def test_a_stopped_folder_reopens_with_its_list_and_settings_and_goes_on(qt_app, env, monkeypatch):
    root, cfg, rec = env
    books = _folder(root, ('1.txt', '2.txt', '3.txt', '4.txt'))
    started = []
    w = _folder_window(qt_app, monkeypatch, rec, books, started, _stopping_worker(started, at='3.txt'), exclude='2.txt')
    try:
        w.start_translation()
        assert _wait(qt_app, lambda: '사용자 중지' in w.error_view.toPlainText()), w.error_view.toPlainText()[-400:]
        _idle(qt_app, w)
        note = resume_work.get('translation')
        assert note['kind'] == 'folder' and Path(note['root']) == books and note['relation'] == 'continuous'
        assert note['files'] == 3 and note['next'] == 1 and note['auto_refine'] is True
        assert Path(note['current']).name == '3.txt' and [Path(x).name for x in note['names']] == ['1.txt', '3.txt', '4.txt']
        assert note['excluded'] == [str((books/'2.txt').resolve())]
        assert [p for p, *_ in rec['refine']] and rec['refine'][0][0].name == '1.ko.txt'   # 1 was refined
    finally:
        w.close()
    # next start, with other settings showing
    rec['refine'].clear()
    started.clear()
    monkeypatch.setattr(ui, 'TranslationWorker', _per_file_worker(started, {n: [('completed', 0, 0)] for n in ('1.txt', '3.txt', '4.txt')}))
    w = _local_window(monkeypatch, rec)
    try:
        w.auto_refine.setChecked(False)
        w.offer_previous_work()
        text = _answer(w, qt_app, '열기')
        assert '파일 3개 중 2번째 파일(3.txt)까지 진행' in text and '한 작품의 연속 파일' in text
        assert w.input_mode == 'folder' and w.batch_root == books and w.batch_relation.currentData() == 'continuous'
        assert [p.name for p in w.batch_files] == ['1.txt', '3.txt', '4.txt']           # 2.txt still excluded
        assert w.auto_refine.isChecked() and not w._job_busy()
        w.start_translation()
        assert _wait(qt_app, lambda: w.last_result_path is not None and w.last_result_path.name == '번역_요약.txt')
        _idle(qt_app, w)
        assert [p.name for p, *_ in rec['refine']] == ['1.ko.txt', '3.ko.txt', '4.ko.txt']
        assert resume_work.get('translation') is None                                 # the folder reached its end
    finally:
        w.close()


def test_dropped_files_reopen_as_the_same_list(qt_app, env, monkeypatch):
    root, cfg, rec = env
    books = _folder(root, ('1.txt', '2.txt', '3.txt'))
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _stopping_worker(started, at='3.txt'))
    w = _local_window(monkeypatch, rec)
    try:
        w._handle_drop([books/'1.txt', books/'3.txt']); w._update_ready_state()
        w.start_translation()
        assert _wait(qt_app, lambda: '사용자 중지' in w.error_view.toPlainText())
        _idle(qt_app, w)
        assert sorted(Path(p).name for p in resume_work.get('translation')['selection']) == ['1.txt', '3.txt']
    finally:
        w.close()
    w = _local_window(monkeypatch, rec)
    try:
        w.offer_previous_work()
        assert '끌어다 놓은 파일 2개' in _answer(w, qt_app, '열기')
        assert [p.name for p in w.batch_files] == ['1.txt', '3.txt'] and w.batch_selection
        assert not w.include_sub.isEnabled()
    finally:
        w.close()


def test_turning_the_option_while_running_follows_into_the_note(qt_app, env, monkeypatch):
    root, cfg, rec = env
    books = _folder(root, ('1.txt', '2.txt'))
    started = []
    w = _folder_window(qt_app, monkeypatch, rec, books, started, _stopping_worker(started, at='1.txt'), on=False)
    try:
        w._mark_unfinished(); w._set_running_ui(True)
        w.auto_refine.setChecked(True)
        assert resume_work.get('translation')['auto_refine'] is True
        w._set_running_ui(False)
    finally:
        w.close()


# ---------------------------------------------------------------- refinement window
def test_refinement_note_is_left_by_an_unfinished_run_and_removed_by_a_finished_one(qt_app, monkeypatch, tmp_path):
    from test_v26_shutdown_prompt import _Host, _refine_dialog, _done
    out = tmp_path/'소설.ko.refined.txt'
    d, _awake = _refine_dialog(monkeypatch, tmp_path, _Host(False), _done(out, 'stopped'))
    try:
        d.show(); d._start()
        assert _wait(qt_app, lambda: d.worker is None)
        note = resume_work.get('refinement')
        assert note['kind'] == 'refine' and Path(note['path']).name == '소설.ko.txt' and note['output'] == str(d.output)
    finally:
        d.close()
    d, _awake = _refine_dialog(monkeypatch, tmp_path, _Host(False), _done(out))
    try:
        d.show(); d._start()
        assert _wait(qt_app, lambda: d.worker is None)
        assert resume_work.get('refinement') is None
    finally:
        d.close()


def test_a_folder_runs_refinement_leaves_no_note_of_its_own(qt_app, monkeypatch, tmp_path):
    from test_v26_shutdown_prompt import _Host, _refine_dialog, _done
    d, _awake = _refine_dialog(monkeypatch, tmp_path, _Host(False), _done(tmp_path/'x.txt', 'stopped'))
    try:
        d._batch_auto = True
        d.show(); d._start()
        assert _wait(qt_app, lambda: d.worker is None)
        assert resume_work.get('refinement') is None
    finally:
        d.close()


def test_the_refinement_window_opens_on_the_unfinished_txt(qt_app, monkeypatch, tmp_path):
    import novel_translator.ui.refinement_run_dialog as rd
    from test_v20_refinement_api_integration import Host
    src = tmp_path/'책'/'소설.ko.txt'; src.parent.mkdir(); src.write_text('한국어 문단입니다.\n\n또 다른 문단입니다.\n', encoding='utf-8')
    out_dir = tmp_path/'결과'; out_dir.mkdir()
    d = rd.RefinementRunDialog(Host(), preset_source=str(src), preset_output=str(out_dir/'소설.ko.refined.txt'))
    try:
        assert d.source == src and d.output.parent == out_dir and d.worker is None   # selected, not started
    finally:
        d.close()


def test_the_main_window_asks_about_the_refinement_after_the_translation(qt_app, env, monkeypatch):
    import novel_translator.ui.refinement_run_dialog as rd
    root, cfg, rec = env
    book = _single_file(root)
    txt = root/'소설.ko.txt'; txt.write_text('한국어 문단입니다.', encoding='utf-8')
    resume_work.mark('translation', {'kind': 'single', 'path': str(book), 'auto_refine': False})
    resume_work.mark('refinement', {'kind': 'refine', 'path': str(txt), 'output': str(root/'소설.ko.refined.txt')})
    opened = []
    class Fake:
        def __init__(self, host, **k): opened.append(k)
        def exec(self): return 0
    monkeypatch.setattr(rd, 'RefinementRunDialog', Fake)
    w = _local_window(monkeypatch, rec)
    try:
        w.offer_previous_work()
        assert '번역하던 파일' in _answer(w, qt_app, '열지 않음')
        assert '재정비하던 TXT' in _answer(w, qt_app, '열기')
        assert opened == [{'preset_source': str(txt), 'preset_output': str(root/'소설.ko.refined.txt')}]
        assert resume_work.get('translation') is None and resume_work.get('refinement')   # it is still unfinished
    finally:
        w.close()


def test_only_the_real_program_start_asks():
    import inspect
    import novel_translator.app as app
    assert 'offer_previous_work' in inspect.getsource(app.main)
    assert 'offer_previous_work' not in inspect.getsource(app.gui_self_test)
    assert 'offer_previous_work' not in inspect.getsource(ui.MainWindow.__init__)


# ---------------------------------------------------------------- files deleted or moved before the restart
def _folder_note(books, names, current=None, **extra):
    return {'kind': 'folder', 'root': str(books), 'names': [str(books/n) for n in names],
            'current': str(books/current) if current else None, 'files': len(names), 'next': 0,
            'skip_root': str((books/'한국어_번역').resolve()), 'include_sub': False, 'excluded': [],
            'relation': 'independent', 'relation_label': '각 파일 독립 작품', 'auto_refine': True, **extra}


def test_deleted_single_file_is_not_asked_and_says_why(qt_app, env, monkeypatch):
    root, cfg, rec = env
    book = _single_file(root)
    resume_work.mark('translation', {'kind': 'single', 'path': str(book), 'percent': 40})
    book.unlink()
    w = _local_window(monkeypatch, rec)
    try:
        w.offer_previous_work()
        for _ in range(20): qt_app.processEvents()
        assert getattr(w, '_resume_box', None) is None and resume_work.get('translation') is None
        assert '번역하던 파일이 없어졌습니다 (book.txt)' in w.error_view.toPlainText()
        assert '묻지 않았습니다' in w.status.text()
    finally:
        w.close()


def test_folder_left_but_all_its_files_deleted_is_not_asked(qt_app, env, monkeypatch):
    root, cfg, rec = env
    books = _folder(root, ('1.txt', '2.txt'))
    (books/'한국어_번역').mkdir(); (books/'한국어_번역'/'1.ko.txt').write_text('번역', encoding='utf-8')
    resume_work.mark('translation', _folder_note(books, ('1.txt', '2.txt'), '2.txt'))
    for n in ('1.txt', '2.txt'): (books/n).unlink()
    asks, dropped = resume_work.review()
    assert asks == [] and dropped == ['일괄 번역하던 폴더에 원문 파일이 남아 있지 않습니다 (books)']   # results are not sources
    assert resume_work.get('translation') is None


def test_folder_with_some_files_deleted_asks_with_what_is_left(qt_app, env, monkeypatch):
    root, cfg, rec = env
    books = _folder(root, ('1.txt', '2.txt', '3.txt', '4.txt'))
    resume_work.mark('translation', _folder_note(books, ('1.txt', '2.txt', '3.txt', '4.txt'), '3.txt'))
    (books/'2.txt').unlink()
    w = _local_window(monkeypatch, rec)
    try:
        w.offer_previous_work()
        text = _answer(w, qt_app, '열기')
        assert '파일 3개 중 2번째 파일(3.txt)까지 진행' in text and '그때 있던 파일 중 1개는 지워졌거나 옮겨져 건너뜁니다' in text
        assert [p.name for p in w.batch_files] == ['1.txt', '3.txt', '4.txt']
        assert '1개가 지워졌거나 옮겨져 건너뜁니다: 2.txt · 지금 3개' in w.error_view.toPlainText()
    finally:
        w.close()


def test_the_file_being_worked_on_deleted_goes_on_with_the_next(qt_app, env, monkeypatch):
    root, cfg, rec = env
    books = _folder(root, ('1.txt', '2.txt', '3.txt'))
    resume_work.mark('translation', _folder_note(books, ('1.txt', '2.txt', '3.txt'), '2.txt'))
    (books/'2.txt').unlink()
    (slot, entry, info), = resume_work.review()[0]
    text = resume_work.question_text(entry, info)
    assert '파일 2개 · 하던 파일(2.txt)은 없어져 그다음 파일부터' in text


def test_a_new_file_added_to_the_folder_is_simply_in_the_list(qt_app, env, monkeypatch):
    root, cfg, rec = env
    books = _folder(root, ('1.txt', '2.txt'))
    resume_work.mark('translation', _folder_note(books, ('1.txt', '2.txt'), '2.txt'))
    (books/'3.txt').write_text('第一章\n\n新的。', encoding='utf-8')
    (slot, entry, info), = resume_work.review()[0]
    assert [Path(x).name for x in info['now']] == ['1.txt', '2.txt', '3.txt'] and info['gone'] == []
    assert '파일 3개 중 2번째' in resume_work.question_text(entry, info)


def test_excluded_and_dropped_lists_count_only_what_the_run_would_do(qt_app, env, monkeypatch):
    root, cfg, rec = env
    books = _folder(root, ('1.txt', '2.txt', '3.txt'))
    note = _folder_note(books, ('1.txt', '3.txt'), excluded=[str((books/'2.txt').resolve())])
    resume_work.mark('translation', note)
    (slot, entry, info), = resume_work.review()[0]
    assert [Path(x).name for x in info['now']] == ['1.txt', '3.txt']
    resume_work.mark('translation', {**note, 'selection': [str(books/'1.txt'), str(books/'3.txt')], 'names': None, 'excluded': []})
    (books/'3.txt').unlink()
    (slot, entry, info), = resume_work.review()[0]
    text = resume_work.question_text(entry, info)
    assert '끌어다 놓은 파일 2개' in text and '파일 1개' in text and '1개는 지워졌거나' in text


def test_deleted_while_the_question_is_open_opens_nothing(qt_app, env, monkeypatch):
    root, cfg, rec = env
    book = _single_file(root)
    resume_work.mark('translation', {'kind': 'single', 'path': str(book), 'percent': 10})
    w = _local_window(monkeypatch, rec)
    try:
        w.offer_previous_work()
        assert _wait(qt_app, lambda: getattr(w, '_resume_box', None) is not None)
        book.unlink()
        _answer(w, qt_app, '열기')
        assert w.source_path is None and resume_work.get('translation') is None
        assert '열지 못하고 기록만 지웠습니다' in w.error_view.toPlainText()
    finally:
        w.close()


def test_refinement_txt_deleted_but_original_there_offers_the_original(qt_app, env, monkeypatch):
    root, cfg, rec = env
    book = _single_file(root)
    gone = root/'book.ko.txt'
    resume_work.mark('refinement', {'kind': 'refine', 'path': str(gone), 'output': str(root/'book.ko.refined.txt'), 'origin': str(book)})
    w = _local_window(monkeypatch, rec)
    try:
        w.auto_refine.setChecked(False)
        w.offer_previous_work()
        text = _answer(w, qt_app, '열기')
        assert '결과 TXT(book.ko.txt)가 없어졌습니다' in text and '원문: book.txt' in text
        assert w.source_path == book and w.auto_refine.isChecked()          # result made again, then refined
        assert '저장된 번역으로 결과를 다시 만들고' in w.error_view.toPlainText()
    finally:
        w.close()
    w = _local_window(monkeypatch, rec)
    try:
        w.offer_previous_work(); _answer(w, qt_app, '열지 않음')
        assert resume_work.get('refinement') is None
    finally:
        w.close()


def test_refinement_txt_and_original_both_deleted_is_not_asked(tmp_path):
    resume_work.mark('refinement', {'kind': 'refine', 'path': str(tmp_path/'a.ko.txt'), 'origin': str(tmp_path/'a.txt')})
    asks, dropped = resume_work.review()
    assert asks == [] and dropped == ['재정비하던 TXT가 없어졌습니다 (a.ko.txt)'] and resume_work.get('refinement') is None


def test_refinement_by_hand_has_no_original_to_fall_back_to(tmp_path):
    resume_work.mark('refinement', {'kind': 'refine', 'path': str(tmp_path/'a.ko.txt'), 'origin': None})
    assert resume_work.review() == ([], ['재정비하던 TXT가 없어졌습니다 (a.ko.txt)'])


def test_one_question_when_both_notes_point_to_the_same_original(tmp_path):
    book = tmp_path/'a.txt'; book.write_text('x', encoding='utf-8')
    resume_work.mark('translation', {'kind': 'single', 'path': str(book)})
    resume_work.mark('refinement', {'kind': 'refine', 'path': str(tmp_path/'a.ko.txt'), 'origin': str(book)})
    asks, dropped = resume_work.review()
    assert [(s, e['kind']) for s, e, _ in asks] == [('translation', 'single')] and dropped == []


def test_auto_refinement_after_a_single_file_records_its_original(qt_app, monkeypatch, tmp_path):
    from test_v26_shutdown_prompt import _Host, _refine_dialog, _done
    out = tmp_path/'소설.ko.refined.txt'
    host = _Host(False)
    host.input_mode = 'single'; host.source_path = tmp_path/'소설.txt'; host._paste_active = False
    d, _awake = _refine_dialog(monkeypatch, tmp_path, host, _done(out, 'stopped'))
    try:
        d._auto_source = d.source                     # as when the translation started it
        d.show(); d._start()
        assert _wait(qt_app, lambda: d.worker is None)
        assert resume_work.get('refinement')['origin'] == str(tmp_path/'소설.txt')
    finally:
        d.close()
    d, _awake = _refine_dialog(monkeypatch, tmp_path, host, _done(out, 'stopped'))
    try:
        d.show(); d._start()                          # started by hand: no original to fall back to
        assert _wait(qt_app, lambda: d.worker is None)
        assert resume_work.get('refinement')['origin'] is None
    finally:
        d.close()


def test_a_second_work_for_the_main_window_never_replaces_the_one_just_opened(qt_app, env, monkeypatch):
    root, cfg, rec = env
    books = _folder(root, ('1.txt', '2.txt'))
    book = root/'소설.txt'; book.write_text('第一章\n\n他看着。', encoding='utf-8')
    resume_work.mark('translation', _folder_note(books, ('1.txt', '2.txt'), '2.txt'))
    resume_work.mark('refinement', {'kind': 'refine', 'path': str(root/'소설.ko.txt'), 'origin': str(book)})
    w = _local_window(monkeypatch, rec)
    try:
        w.offer_previous_work()
        _answer(w, qt_app, '열기')
        for _ in range(20): qt_app.processEvents()
        assert getattr(w, '_resume_box', None) is None                     # not asked now
        assert w.input_mode == 'folder' and w.batch_root == books         # the folder stays open
        assert '소설.txt 도 끝나지 않았습니다' in w.error_view.toPlainText()
        assert resume_work.get('refinement')                               # asked at the next start
    finally:
        w.close()
    w = _local_window(monkeypatch, rec)
    try:
        resume_work.clear('translation')
        w.offer_previous_work()
        assert '결과 TXT(소설.ko.txt)가 없어졌습니다' in _answer(w, qt_app, '열지 않음')
    finally:
        w.close()


def test_declining_the_first_still_asks_the_second(qt_app, env, monkeypatch):
    root, cfg, rec = env
    books = _folder(root, ('1.txt',))
    book = root/'소설.txt'; book.write_text('第一章', encoding='utf-8')
    resume_work.mark('translation', _folder_note(books, ('1.txt',)))
    resume_work.mark('refinement', {'kind': 'refine', 'path': str(root/'소설.ko.txt'), 'origin': str(book)})
    w = _local_window(monkeypatch, rec)
    try:
        w.offer_previous_work()
        _answer(w, qt_app, '열지 않음')
        assert '원문: 소설.txt' in _answer(w, qt_app, '열기') and w.source_path == book
    finally:
        w.close()
