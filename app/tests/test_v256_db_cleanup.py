"""v2.5.6: 번역 DB 정리 — 90일 넘게 손대지 않은 작업만 지우고, 최근 작업·이어 번역은 그대로."""
import sqlite3
from novel_translator.storage import Database


def _project(db, name, days_ago, chunks=3):
    pid = db.create_project(name, 'zh', 'zh-ko', 'sha-' + name, 'fp')
    for i in range(chunks):
        db.upsert_chunk(pid, i, f'<P{i+1:06d}>\n原文{i}')
        db.set_chunk_status(pid, i, 'COMPLETED', translation=f'<P{i+1:06d}>\n번역{i}')
    with sqlite3.connect(db.path) as con:
        con.execute("UPDATE projects SET updated_at=datetime('now', ?), created_at=datetime('now', ?) WHERE id=?", (f'-{days_ago} days', f'-{days_ago} days', pid))
        con.execute("UPDATE chunks SET updated_at=datetime('now', ?) WHERE project_id=?", (f'-{days_ago} days', pid))
    return pid


def _ids(db):
    with sqlite3.connect(db.path) as con:
        return {r[0] for r in con.execute('SELECT source_path FROM projects')}


def test_only_jobs_untouched_for_the_period_are_removed(tmp_path):
    db = Database(tmp_path / 't.db')
    _project(db, 'old', 120); _project(db, 'recent', 10); edge = _project(db, 'edge', 89)
    # an old job one chunk of which was translated yesterday is in use
    busy = _project(db, 'busy', 200)
    with sqlite3.connect(db.path) as con:
        con.execute("UPDATE chunks SET updated_at=datetime('now','-1 days') WHERE project_id=? AND chunk_index=0", (busy,))
    assert db.remove_old_projects(90) == (1, 3)
    assert _ids(db) == {'recent', 'edge', 'busy'}
    assert db.completed_translations(edge), 'kept jobs keep their translations'


def test_starting_an_old_job_again_keeps_it(tmp_path):
    db = Database(tmp_path / 't.db')
    _project(db, 'paused', 150)
    pid = db.get_or_create_project('paused', 'zh', 'zh-ko', 'sha-paused', 'fp')   # the worker resumes it
    assert db.remove_old_projects(90) == (0, 0) and _ids(db) == {'paused'}
    assert len(db.completed_translations(pid)) == 3


def test_zero_keeps_everything_and_orphans_go(tmp_path):
    db = Database(tmp_path / 't.db')
    _project(db, 'old', 400)
    assert db.remove_old_projects(0) == (0, 0) and _ids(db) == {'old'}
    with sqlite3.connect(db.path) as con:
        con.execute('PRAGMA foreign_keys=OFF')
        con.execute("INSERT INTO chunks(project_id,chunk_index,source_text) VALUES(999,0,'x')")
    jobs, chunks = db.remove_old_projects(90)
    assert jobs == 1 and chunks == 4 and _ids(db) == set()


def test_compact_shrinks_after_a_big_removal(tmp_path):
    db = Database(tmp_path / 't.db')
    pid = db.create_project('big', 'zh', 'zh-ko', 'sha', 'fp')
    db.upsert_chunks(pid, [type('C', (), {'index': i, 'source_text': '原文' * 400})() for i in range(400)])
    with sqlite3.connect(db.path) as con:
        con.execute("UPDATE projects SET updated_at=datetime('now','-100 days')")
        con.execute("UPDATE chunks SET updated_at=datetime('now','-100 days')")
    con = sqlite3.connect(db.path); con.execute('PRAGMA wal_checkpoint(TRUNCATE)'); con.close()
    before = db.path.stat().st_size
    assert db.remove_old_projects(90)[0] == 1
    assert db.compact() and db.path.stat().st_size < before / 2
    assert not db.compact(), 'nothing left to free'
    Database(tmp_path / 't.db')   # still opens and checks fine


def test_closing_the_window_cleans_and_logs(qt_app, sandbox, monkeypatch):
    from novel_translator.ui import main_window as ui
    from test_alpha17 import _window
    root, _ = sandbox
    db = Database(ui.DATA_DIR / 'translations.db'); _project(db, 'old', 200); _project(db, 'new', 1)
    w = _window(monkeypatch)
    w.close()
    assert _ids(db) == {'new'}
    from novel_translator.config import LOG_DIR
    log = (LOG_DIR / 'db_cleanup.log').read_text(encoding='utf-8')
    assert '번역 작업 1개' in log


from test_alpha17 import qt_app, sandbox  # noqa: E402,F401 (pytest fixtures)


def test_window_is_hidden_before_the_cleanup_runs(qt_app, sandbox, monkeypatch):
    """v2.5.7: shrinking a large record can take seconds; the window must not look frozen."""
    from test_alpha17 import _window
    w = _window(monkeypatch)
    w.show()
    seen = []
    monkeypatch.setattr(w, '_clean_translation_db', lambda: seen.append(w.isVisible()))
    w.close()
    assert seen == [False]
