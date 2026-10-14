"""v2.1: one app-wide output folder for every result, no .ko.failed.txt beside results,
database startup cost independent of its size, refinement resume follows the text."""
from __future__ import annotations
import sqlite3
from pathlib import Path

import pytest

import novel_translator.ui.main_window as ui
from test_alpha17 import qt_app, sandbox, _window, _fake_worker_class, _wait  # noqa: F401,F811

ZH = '林雨推开门，看见苏晴坐在窗边。她没有回头，声音很轻。\n'


def _book(folder: Path, name: str, lines: int = 30) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text(ZH * lines, encoding='utf-8')
    return p


def _choose(w, monkeypatch, folder: Path):
    monkeypatch.setattr(ui.QFileDialog, 'getExistingDirectory', lambda *a, **k: str(folder))
    w.choose_output_dir()


# ---------------------------------------------------------------- app-wide output folder

def test_output_folder_applies_to_every_book_whatever_the_order(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    out = root / 'myout'; out.mkdir()
    _choose(w, monkeypatch, out)                       # chosen before any file is open
    a = _book(root / 'books', 'a.txt'); b = _book(root / 'other', 'b.txt', 31)
    w._open_txt_path(str(a)); assert w._output_dir_for(a) == out
    w._open_txt_path(str(b)); assert w._output_dir_for(b) == out
    assert '모든 결과에 적용' in w.output_label.text()


def test_output_folder_survives_a_restart_and_reset_returns_to_the_source_folder(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    out = root / 'myout'; out.mkdir()
    _choose(w, monkeypatch, out)
    w2 = _window(monkeypatch)                          # a new window = the app started again
    a = _book(root / 'books', 'a.txt')
    w2._open_txt_path(str(a)); assert w2._output_dir_for(a) == out
    w2.reset_output_dir()
    assert w2._output_dir_for(a) == a.parent
    assert _window(monkeypatch).custom_output_dir is None


def test_resetting_one_books_settings_keeps_the_output_folder(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    out = root / 'myout'; out.mkdir()
    _choose(w, monkeypatch, out)
    a = _book(root / 'books', 'a.txt'); w._open_txt_path(str(a))
    w._prepare_new_input()
    assert w.custom_output_dir == out


def test_translation_and_folder_results_go_to_the_output_folder(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _fake_worker_class(started))
    w = _window(monkeypatch)
    out = root / 'myout'; out.mkdir()
    _choose(w, monkeypatch, out)
    a = _book(root / 'books', 'a.txt')
    w._open_txt_path(str(a)); w.start_translation()
    assert _wait(qt_app, lambda: started and not (w.worker and w.worker.isRunning()) and w.last_result_path, 20)
    assert started[-1].out_dir == out and (out / 'a.ko.txt').exists()
    src = root / 'folder'; _book(src, '1.txt'); _book(src / 'sub', '2.txt', 31)
    started.clear(); w._open_folder_path(str(src)); w.include_sub.setChecked(True); w._scan_batch_files()
    assert len(w.batch_files) == 2
    w.start_translation()
    assert _wait(qt_app, lambda: len(started) >= 2 and not (w.worker and w.worker.isRunning()), 30)
    assert sorted(str(s.out_dir) for s in started) == sorted([str(out), str(out / 'sub')])   # structure kept


def test_pasted_text_without_an_output_folder_goes_to_documents(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    monkeypatch.setattr(ui.MainWindow, '_documents_output_dir', staticmethod(lambda: root / 'Documents' / '야문 Novel Translator'))
    w = _window(monkeypatch)
    w.preview.setPlainText(ZH * 5)
    assert w._use_preview_as_source()
    assert w._output_dir_for(w.source_path) == root / 'Documents' / '야문 Novel Translator'
    out = root / 'myout'; out.mkdir(); _choose(w, monkeypatch, out)
    assert w._output_dir_for(w.source_path) == out


def test_safe_folder_is_used_for_one_run_only(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    w = _window(monkeypatch)
    out = root / 'myout'; out.mkdir()
    _choose(w, monkeypatch, out)
    a = _book(root / 'books', 'a.txt'); w._open_txt_path(str(a))
    monkeypatch.setattr(ui, 'directory_writable', lambda p: Path(p) != out)
    assert w._preflight_output()
    assert w._output_dir_for(a) == ui.DATA_DIR / 'exports'
    assert w.custom_output_dir == out                       # the user's choice is kept
    b = _book(root / 'books', 'b.txt', 31); w._open_txt_path(str(b))
    assert w._output_dir_for(b) == out                      # next book: the chosen folder again


def test_refinement_result_goes_to_the_output_folder(qt_app, sandbox, monkeypatch):
    from PySide6.QtWidgets import QWidget
    from novel_translator.ui.refinement_run_dialog import RefinementRunDialog
    root, _ = sandbox
    host = QWidget(); host.secrets = {}; host.source_path = None; host.last_result_path = None
    host._selected_model_id = lambda: ''
    host.custom_output_dir = root / 'myout'
    d = RefinementRunDialog(host)
    ko = root / 'books'; ko.mkdir(); src = ko / 'book.ko.txt'; src.write_text('그는 창밖을 보았다.\n' * 20, encoding='utf-8')
    d._set_source(src)
    assert d.output == root / 'myout' / 'book.ko.refined.txt'
    host.custom_output_dir = None
    d._set_source(src)
    assert d.output == ko / 'book.ko.refined.txt'


# ---------------------------------------------------------------- no .ko.failed.txt

def test_a_stopped_or_partial_book_leaves_only_the_partial_file(tmp_path):
    from novel_translator.chunking import split_paragraphs, make_chunks
    from novel_translator.rendering import render_snapshot
    text = '\n\n'.join(['第一段原文。', '第二段原文。', '第三段原文。'])
    paras = split_paragraphs(text)
    chunks = make_chunks(paras, 'zh', target_tokens=1, min_tokens=1, max_tokens=3)
    source = tmp_path / 'book.txt'; source.write_text(text, encoding='utf-8')
    (tmp_path / 'book.ko.failed.txt').write_text('old list', encoding='utf-8')     # left by an earlier build
    rows = [{'chunk_index': c.index, 'source_text': c.source_text, 'status': 'COMPLETED' if c.index == 0 else 'PENDING',
             'translation': '\n\n'.join(f'<P{p.pid:06d}>\n번역{p.pid}' for p in c.paragraphs) if c.index == 0 else None,
             'error_type': None, 'error_message': None} for c in chunks]
    out, failed, report, _ = render_snapshot(chunks, rows, source, tmp_path, tmp_path / 'fallback')
    assert out.name == 'book.ko.partial.txt' and failed and '청크 2' in report
    assert sorted(p.name for p in tmp_path.glob('book.ko*')) == ['book.ko.partial.txt']


# ---------------------------------------------------------------- database

OLD_SCHEMA = """
CREATE TABLE projects (id INTEGER PRIMARY KEY, source_path TEXT NOT NULL, source_language TEXT NOT NULL,
  profile_id TEXT NOT NULL, source_sha256 TEXT NOT NULL);
CREATE TABLE chunks (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, chunk_index INTEGER NOT NULL,
  source_text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', translation TEXT, UNIQUE(project_id, chunk_index));
INSERT INTO projects(source_path,source_language,profile_id,source_sha256) VALUES('a','zh','zh-ko','s');
INSERT INTO chunks(project_id,chunk_index,source_text) VALUES(1,0,'原文');
"""


def _old_db(path):
    with sqlite3.connect(path) as con:
        con.executescript(OLD_SCHEMA)


def test_old_database_is_upgraded_once_and_not_rescanned_on_every_open(tmp_path):
    import novel_translator.storage as st
    path = tmp_path / 'old.db'
    _old_db(path)
    st.Database(path)
    with sqlite3.connect(path) as con:
        row = con.execute('SELECT updated_at, source_sha FROM chunks').fetchone()
        assert row[0] is not None and row[1]                        # upgraded
        assert con.execute('PRAGMA user_version').fetchone()[0] == st.DATA_VERSION
        con.execute("INSERT INTO chunks(project_id,chunk_index,source_text,updated_at) VALUES(1,1,'原文二',NULL)")
    st.Database(path)
    with sqlite3.connect(path) as con:      # already upgraded: rows are not rewritten on every open
        assert con.execute('SELECT updated_at FROM chunks WHERE chunk_index=1').fetchone()[0] is None


def test_crash_recovery_uses_a_small_index(tmp_path):
    import novel_translator.storage as st
    path = tmp_path / 't.db'
    db = st.Database(path)
    with sqlite3.connect(path) as con:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert 'idx_chunks_inflight' in names
        plan = ' '.join(str(r) for r in con.execute(
            "EXPLAIN QUERY PLAN UPDATE chunks SET status='PENDING' WHERE status IN ('REQUESTING','RECEIVED','VALIDATING')"))
        assert 'idx_chunks_inflight' in plan
    pid = db.create_project('a', 'zh', 'zh-ko', 'sha'); db.upsert_chunk(pid, 0, 'x')
    db.set_chunk_status(pid, 0, 'REQUESTING')
    assert st.Database(path).all_chunks(pid)[0]['status'] == 'PENDING'


def test_refinement_resumes_after_the_file_is_renamed(tmp_path):
    from novel_translator.refinement_runtime import run_refinement
    from test_v20_local_paths import EditLocal
    a = tmp_path / 'old_name.ko.txt'
    a.write_text('\n'.join(f'{i}번째 문단입니다.' for i in range(40)), encoding='utf-8')
    calls = {'n': 0}

    class Counting(EditLocal):
        def translate(self, req, key):
            calls['n'] += 1
            return super().translate(req, key)
    first = run_refinement(a, tmp_path / 'r1.txt', Counting(), 'm', 'k', checkpoint_dir=tmp_path / 'cp',
                           max_chars=60, stop=lambda: calls['n'] >= 3)
    assert first['status'] == 'stopped' and first['done'] > 0
    b = a.rename(tmp_path / 'new_name.ko.txt')
    done_before = first['done']
    second = run_refinement(b, tmp_path / 'r2.txt', Counting(), 'm', 'k', checkpoint_dir=tmp_path / 'cp', max_chars=60)
    assert second['status'] == 'completed'
    assert calls['n'] < 3 + (40 // 2) + 5 and done_before > 0        # continued, did not start over


def test_refinement_continues_a_run_saved_by_an_earlier_build(tmp_path):
    from novel_translator.refinement_runtime import run_refinement, checkpoint_path, content_checkpoint_path
    from novel_translator.config import save_json, load_json
    from test_v20_local_paths import EditLocal
    src = tmp_path / 'book.ko.txt'; src.write_text('첫 문단입니다.\n둘째 문단입니다.', encoding='utf-8')
    legacy = checkpoint_path(src, tmp_path / 'cp')
    save_json(legacy, {'schema': 1, 'identity': 'other', 'completed': {}, 'issues': {}})
    run_refinement(src, tmp_path / 'out.txt', EditLocal(), 'm', 'k', checkpoint_dir=tmp_path / 'cp')
    assert not legacy.exists()
