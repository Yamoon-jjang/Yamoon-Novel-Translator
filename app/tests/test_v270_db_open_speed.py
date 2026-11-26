"""v2.7.0: opening a book no longer reads the whole translation DB.

Before, the first Database() of a session ran PRAGMA quick_check, which reads every page of
translations.db: with a record of hundreds of MB the first book opened after a start waited
5~10 s on a cold disk. Now damage is noticed when SQLite meets it, confirmed with the full
check and repaired (damaged file kept, readable rows copied to a new DB)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import novel_translator.storage as st
from novel_translator.storage import Database


def _fill(path: Path, projects=3, chunks=40):
    db = Database(path)
    with db.connect() as con:
        for n in range(projects):
            pid = con.execute("INSERT INTO projects(source_path,source_language,profile_id,source_sha256) VALUES(?,?,?,?)",
                              (f'book{n}.txt', 'zh', 'zh-ko', f'{n:064x}')).lastrowid
            con.executemany("INSERT INTO chunks(project_id,chunk_index,source_text,translation,status,source_sha) VALUES(?,?,?,?,'COMPLETED',?)",
                            [(pid, i, f'原文{n}-{i}' * 50, f'번역{n}-{i}' * 50, f's{n}-{i}') for i in range(chunks)])
            con.execute("INSERT INTO terms(project_id,source_term,target_term) VALUES(?,?,?)", (pid, f'林{n}', f'임{n}'))
    return db


def test_open_never_runs_the_whole_file_check(tmp_path, monkeypatch):
    path = tmp_path / 'translations.db'
    _fill(path)
    seen = []

    class Spy(sqlite3.Connection):
        def execute(self, sql, *a):
            seen.append(str(sql))
            return super().execute(sql, *a)

    real = sqlite3.connect
    monkeypatch.setattr(st.sqlite3, 'connect', lambda *a, **k: real(*a, factory=Spy, **k))
    db = Database(path)
    assert db.latest_terms_for_source(f'{0:064x}', 'zh-ko') == {'林0': '임0'}
    assert db.has_progress_for_source(f'{1:064x}', 'zh-ko')
    assert not any('quick_check' in s.lower() or 'integrity_check' in s.lower() for s in seen), seen


def test_check_integrity_sound_file(tmp_path):
    path = tmp_path / 'translations.db'
    _fill(path)
    assert st.check_integrity(path) == (True, 'ok')


def _damage(path: Path):
    """Overwrite pages in the middle of the file (the chunk table's data)."""
    with sqlite3.connect(path) as con:
        con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        page = con.execute('PRAGMA page_size').fetchone()[0]
        pages = con.execute('PRAGMA page_count').fetchone()[0]
    data = bytearray(path.read_bytes())
    for p in range(pages // 2, pages // 2 + 3):
        data[p * page:(p + 1) * page] = b'\xa5' * page
    path.write_bytes(bytes(data))


def test_damage_is_noticed_and_recovered(tmp_path):
    path = tmp_path / 'translations.db'
    _fill(path, projects=4, chunks=300)
    _damage(path)
    st.clear_damage_notice(path)
    db = Database(path)
    failed = False
    try:
        with db.connect() as con:
            con.execute('SELECT group_concat(translation) FROM chunks').fetchall()
    except sqlite3.DatabaseError:
        failed = True        # (the error and its traceback go here, as in the program)
    assert failed and st.damage_noticed(path)
    ok, detail = st.check_integrity(path)
    assert not ok and detail

    res = st.recover_database(path)
    assert res['damaged']
    backup = Path(res['backup'])
    assert backup.exists() and backup.name.startswith('translations.db.corrupt_')
    assert not st.damage_noticed(path)
    assert st.check_integrity(path) == (True, 'ok')
    # The readable jobs, glossaries and most chunks survive; the new DB works for a job.
    assert res['projects'] == 4 and res['terms'] == 4
    assert 0 < res['chunks'] < 1200
    new = Database(path)
    assert new.latest_terms_for_source(f'{0:064x}', 'zh-ko') == {'林0': '임0'}
    pid = new.get_or_create_project('book9.txt', 'zh', 'zh-ko', 'f' * 64)
    new.upsert_chunk(pid, 0, '新')
    assert new.pending_chunks(pid)


def test_false_alarm_leaves_the_file_alone(tmp_path):
    path = tmp_path / 'translations.db'
    _fill(path)
    st.note_damage(path, 'database disk image is malformed')
    before = path.read_bytes()
    res = st.recover_database(path)
    assert not res['damaged'] and not res['backup']
    assert not st.damage_noticed(path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('*.corrupt_*'))


def test_only_damage_errors_are_noted():
    assert st.is_damage_error(sqlite3.DatabaseError('database disk image is malformed'))
    assert st.is_damage_error(sqlite3.DatabaseError('file is not a database'))
    assert not st.is_damage_error(sqlite3.OperationalError('database is locked'))
    assert not st.is_damage_error(sqlite3.OperationalError('no such table: chunks'))
    assert not st.is_damage_error(ValueError('malformed'))


def test_not_a_database_file_is_recovered_to_an_empty_db(tmp_path):
    path = tmp_path / 'translations.db'
    path.write_bytes(b'this is not sqlite' * 500)
    with pytest.raises(sqlite3.DatabaseError):
        Database(path)
    assert st.damage_noticed(path)
    res = st.recover_database(path)
    assert res['damaged'] and Path(res['backup']).exists()
    assert st.check_integrity(path) == (True, 'ok')
    assert Database(path).latest_terms_for_source('0' * 64, 'zh-ko') == {}


def test_rename_waits_for_a_held_file(tmp_path, monkeypatch):
    """Windows: the damaged file may be held for a moment (a closed connection whose failed
    cursor is not freed yet, a virus scanner): the rename is retried, not given up at once."""
    src = tmp_path / 'a.db'; src.write_bytes(b'x'); dst = tmp_path / 'b.bak'
    calls = {'n': 0}
    real = Path.replace

    def flaky(self, target):
        calls['n'] += 1
        if calls['n'] < 3:
            raise PermissionError(32, 'in use')
        return real(self, target)

    monkeypatch.setattr(Path, 'replace', flaky)
    monkeypatch.setattr(st.time, 'sleep', lambda s: None)
    st._rename_when_free(src, dst)
    assert calls['n'] == 3 and dst.exists() and not src.exists()


def test_check_reads_while_a_writer_is_open(tmp_path):
    """The background check is read-only (WAL): a translation writing meanwhile is not blocked."""
    path = tmp_path / 'translations.db'
    db = _fill(path)
    with db.connect() as con:
        con.execute("UPDATE chunks SET translation='x' WHERE id=1")
        assert st.check_integrity(path) == (True, 'ok')


# ---------------------------------------------------------------- the program window
import json  # noqa: E402

import novel_translator.ui.main_window as ui  # noqa: E402
from test_alpha17 import _window, _wait, qt_app, sandbox  # noqa: E402,F401 (fixtures)


def test_window_repairs_a_noticed_damage_only_when_idle(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    data = root / 'data'; data.mkdir(exist_ok=True)
    path = data / 'translations.db'
    _fill(path, projects=2, chunks=300)
    _damage(path)
    w = _window(monkeypatch)
    try:
        st.note_damage(path, 'database disk image is malformed')
        w._job_active = True                      # a job is alive: never touch the DB file
        w._db_damage_tick()
        assert st.damage_noticed(path) and not list(data.glob('*.corrupt_*'))
        w._job_active = False
        w._db_damage_tick()
        assert not st.damage_noticed(path)
        assert list(data.glob('translations.db.corrupt_*.bak'))
        assert st.check_integrity(path) == (True, 'ok')
        assert '번역 기록 DB 복구' in w.error_view.toPlainText()
    finally:
        st.clear_damage_notice(path); w.close()


def test_noticed_damage_is_repaired_before_the_next_worker(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    order = []
    monkeypatch.setattr(ui.MainWindow, '_repair_translation_db', lambda self: order.append('repair'))
    monkeypatch.setattr(ui.MainWindow, '_make_worker', lambda self, *a: (order.append('worker'), (_ for _ in ()).throw(RuntimeError('stop')))[1])
    w = _window(monkeypatch)
    path = root / 'data' / 'translations.db'
    try:
        st.note_damage(path, 'database disk image is malformed')
        with pytest.raises(RuntimeError):
            w._start_one_worker(root / 'a.txt', b'x', 'x', [], [], 'm')
        assert order == ['repair', 'worker']
    finally:
        st.clear_damage_notice(path); w.close()


def test_background_check_about_once_a_week(qt_app, sandbox, monkeypatch):
    root, cfg = sandbox
    data = root / 'data'; data.mkdir(exist_ok=True)
    path = data / 'translations.db'
    _fill(path)
    w = _window(monkeypatch)
    try:
        app_cfg = json.loads((cfg / 'app.json').read_text(encoding='utf-8'))
        app_cfg['db_checked_at'] = int(__import__('time').time())
        (cfg / 'app.json').write_text(json.dumps(app_cfg), encoding='utf-8')
        w.start_background_db_check()
        assert w._db_check_thread is None                   # checked this week: nothing runs
        app_cfg['db_checked_at'] = 0
        (cfg / 'app.json').write_text(json.dumps(app_cfg), encoding='utf-8')
        w.start_background_db_check()
        assert w._db_check_thread is not None
        assert _wait(qt_app, lambda: not w._db_check_thread.isRunning())
        qt_app.processEvents()
        assert json.loads((cfg / 'app.json').read_text(encoding='utf-8'))['db_checked_at'] > 0
        assert not st.damage_noticed(path)
    finally:
        w._stop_db_check(); w.close()


def test_background_check_notes_damage(qt_app, sandbox, monkeypatch):
    root, cfg = sandbox
    data = root / 'data'; data.mkdir(exist_ok=True)
    path = data / 'translations.db'
    _fill(path, projects=2, chunks=300)
    _damage(path)
    w = _window(monkeypatch)
    try:
        w._job_active = True       # the repair itself waits; only the finding is tested here
        w.start_background_db_check()
        if w._db_check_thread is None:       # job busy: rescheduled; run it directly
            w._job_active = False; w.start_background_db_check(); w._job_active = True
        assert _wait(qt_app, lambda: not w._db_check_thread.isRunning())
        qt_app.processEvents()
        assert st.damage_noticed(path)
        assert 'db_checked_at' not in json.loads((cfg / 'app.json').read_text(encoding='utf-8')) or \
            not json.loads((cfg / 'app.json').read_text(encoding='utf-8')).get('db_checked_at')
    finally:
        st.clear_damage_notice(path); w._stop_db_check(); w.close()


def test_environment_check_reports_the_db(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    data = root / 'data'; data.mkdir(exist_ok=True)
    _fill(data / 'translations.db')
    w = _window(monkeypatch)
    try:
        ok, text = w._db_check_report()
        assert ok and text.startswith('TRANSLATION_DB=PASS')
    finally:
        w.close()


def test_program_start_schedules_the_background_check():
    src = (Path(ui.__file__).resolve().parents[1] / 'app.py').read_text(encoding='utf-8')
    assert 'w.start_background_db_check' in src
