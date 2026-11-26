from __future__ import annotations
import hashlib
import re
import sqlite3
from pathlib import Path
from contextlib import contextmanager
import time

# v2.7.0: the whole-file check (PRAGMA quick_check) no longer runs when the database is first
# opened in a session. It read every page of the file, so the first book opened after a start
# waited for the whole translation record (hundreds of MB) to come off the disk. SQLite still
# reports a damaged page the moment it is read or written ("database disk image is
# malformed"); such an error is noted here, confirmed with the full check and repaired by
# recover_database() once no job runs. A full check also runs in the background now and then.

# Paths (resolved, as str) where a damage-looking SQLite error was met in this session.
_DAMAGE_SEEN: dict[str, str] = {}

_DAMAGE_WORDS = ('malformed', 'not a database', 'file is encrypted', 'disk image', 'database corrupt')


def is_damage_error(exc: BaseException) -> bool:
    """True for the SQLite errors a damaged file gives (not for 'locked', 'busy', 'no such table'…)."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    text = str(exc).lower()
    return any(w in text for w in _DAMAGE_WORDS)


def _key(path: Path) -> str:
    try:
        return str(Path(path).resolve())
    except Exception:
        return str(path)


def note_damage(path: Path, message: str) -> None:
    _DAMAGE_SEEN.setdefault(_key(path), str(message)[:300])


def damage_noticed(path: Path) -> str:
    """The error text when a damage-looking error was met for this database in this session."""
    return _DAMAGE_SEEN.get(_key(path), '')


def clear_damage_notice(path: Path) -> None:
    _DAMAGE_SEEN.pop(_key(path), None)


def check_integrity(path: Path, interrupt_holder: list | None = None) -> tuple[bool, str]:
    """Full check of the whole file (reads every page: seconds for a large file).
    Read-only, so it can run while a translation writes (WAL). (True,'ok') when sound,
    (False, what is wrong) when damaged. Other errors (locked, interrupted, missing file) raise."""
    path = Path(path)
    con = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True, timeout=30)
    if interrupt_holder is not None:
        interrupt_holder.append(con)
    try:
        con.execute('PRAGMA busy_timeout=30000')
        rows = [str(r[0]) for r in con.execute('PRAGMA quick_check(20)').fetchall()]
    except sqlite3.DatabaseError as e:
        if is_damage_error(e):
            return False, str(e)
        raise
    finally:
        con.close()
    if rows == ['ok']:
        return True, 'ok'
    return False, '\n'.join(rows)


_SALVAGE_TABLES = ('projects', 'terms', 'chunks')


def _salvage_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> tuple[int, int]:
    """Copy the readable rows of one table, walking by rowid so one damaged page loses only
    the rows on it. (copied, lost-ranges)."""
    cols = [r[1] for r in dst.execute(f'PRAGMA table_info({table})').fetchall()]
    have = set()
    try:
        have = {r[1] for r in src.execute(f'PRAGMA table_info({table})').fetchall()}
    except sqlite3.DatabaseError:
        return 0, 1
    use = [c for c in cols if c in have]
    if 'id' not in use:
        return 0, 1
    sel = ','.join(use); marks = ','.join('?' * len(use))
    try:
        hi = src.execute(f'SELECT max(id) FROM {table}').fetchone()[0] or 0
    except sqlite3.DatabaseError:
        return 0, 1          # the table itself cannot be walked
    copied = lost = 0
    step = 500; start = 0
    while start <= hi:
        end = start + step
        try:
            rows = src.execute(f'SELECT {sel} FROM {table} WHERE id>=? AND id<? ORDER BY id', (start, end)).fetchall()
        except sqlite3.DatabaseError:
            rows = []
            for i in range(start, end):
                try:
                    r = src.execute(f'SELECT {sel} FROM {table} WHERE id=?', (i,)).fetchone()
                except sqlite3.DatabaseError:
                    lost += 1; continue
                if r is not None:
                    rows.append(r)
        if rows:
            dst.executemany(f'INSERT OR IGNORE INTO {table}({sel}) VALUES({marks})', [tuple(r) for r in rows])
            copied += len(rows)
        start = end
    return copied, lost


def _rename_when_free(src: Path, dst: Path, tries: int = 20) -> None:
    """Windows refuses to rename a file some connection still holds. A connection closed while
    a cursor of a failed query is still alive (kept by an exception's traceback) stays open
    until that cursor is freed, so free such leftovers first and wait a little (a virus
    scanner may also hold the file for a moment)."""
    import gc
    for n in range(tries):
        try:
            src.replace(dst); return
        except PermissionError:
            if n == tries - 1:
                raise
            gc.collect(); time.sleep(0.25)


def recover_database(path: Path) -> dict:
    """Repair a damaged translation database. Run only when no job uses it.

    1. Confirm with the full check: a sound file is left as it is (false alarm).
    2. Keep the damaged file (and its -wal/-shm) as translations.db.corrupt_<time>.bak.
    3. Build a new database and copy every readable job, glossary and translated chunk.
    Returns {'damaged': bool, 'detail', 'backup', 'projects', 'terms', 'chunks', 'lost'}."""
    path = Path(path)
    result = {'damaged': False, 'detail': '', 'backup': '', 'projects': 0, 'terms': 0, 'chunks': 0, 'lost': 0}
    if not path.exists():
        clear_damage_notice(path); return result
    try:
        ok, detail = check_integrity(path)
    except sqlite3.DatabaseError as e:
        ok, detail = (False, str(e)) if is_damage_error(e) else (True, '')
    if ok:
        clear_damage_notice(path); return result
    result['damaged'] = True; result['detail'] = detail
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = path.with_name(f'{path.name}.corrupt_{stamp}.bak')
    n = 1
    while backup.exists():
        backup = path.with_name(f'{path.name}.corrupt_{stamp}_{n}.bak'); n += 1
    _rename_when_free(path, backup)
    for side in ('-wal', '-shm'):
        extra = Path(str(path) + side)
        if extra.exists():
            try: extra.replace(Path(str(backup) + side))
            except OSError: pass
    result['backup'] = str(backup)
    fresh = Database(path)          # empty database with the current schema
    try:
        src = sqlite3.connect(f'{backup.resolve().as_uri()}?mode=ro', uri=True, timeout=30)
    except sqlite3.DatabaseError:
        src = None
    if src is not None:
        try:
            with fresh.connect() as dst:
                dst.execute('PRAGMA foreign_keys=OFF')
                for table in _SALVAGE_TABLES:
                    copied, lost = _salvage_table(src, dst, table)
                    result[table] = copied; result['lost'] += lost
                # Rows whose job did not survive are of no use.
                dst.execute('DELETE FROM chunks WHERE project_id NOT IN (SELECT id FROM projects)')
                dst.execute('DELETE FROM terms WHERE project_id NOT IN (SELECT id FROM projects)')
                result['chunks'] = int(dst.execute('SELECT COUNT(*) FROM chunks').fetchone()[0])
                result['terms'] = int(dst.execute('SELECT COUNT(*) FROM terms').fetchone()[0])
                dst.execute("UPDATE chunks SET status='PENDING', error_type=NULL, error_message=NULL WHERE status IN ('REQUESTING','RECEIVED','VALIDATING')")
        finally:
            src.close()
    clear_damage_notice(path)
    return result

# Bumped when a one-time data upgrade is added to Database.__init__.
DATA_VERSION = 1

SCHEMA = '''
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS projects (
 id INTEGER PRIMARY KEY,
 source_path TEXT NOT NULL,
 source_language TEXT NOT NULL,
 profile_id TEXT NOT NULL,
 source_sha256 TEXT NOT NULL,
 job_fingerprint TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS chunks (
 id INTEGER PRIMARY KEY,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 chunk_index INTEGER NOT NULL,
 source_text TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING',
 translation TEXT,
 provider TEXT,
 model TEXT,
 error_type TEXT,
 error_message TEXT,
 retry_count INTEGER NOT NULL DEFAULT 0,
 fallback_used INTEGER NOT NULL DEFAULT 0,
 prompt_version TEXT,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(project_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS terms (
 id INTEGER PRIMARY KEY,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 source_term TEXT NOT NULL,
 target_term TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'CONFIRMED',
 UNIQUE(project_id, source_term)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY,
 project_id INTEGER,
 chunk_index INTEGER,
 level TEXT NOT NULL,
 event_type TEXT NOT NULL,
 message TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
'''

def _sha(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8','surrogatepass')).hexdigest()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_rename_skipped = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)
            # Migrate old alpha databases before creating indexes that reference new columns.
            pcols = {r['name'] for r in con.execute('PRAGMA table_info(projects)').fetchall()}
            if 'job_fingerprint' not in pcols:
                con.execute("ALTER TABLE projects ADD COLUMN job_fingerprint TEXT NOT NULL DEFAULT ''")
            if 'settings_key' not in pcols:
                # Translation-memory key: same profile/prompt/terms/memory/options.
                con.execute("ALTER TABLE projects ADD COLUMN settings_key TEXT NOT NULL DEFAULT ''")
            if 'identity_json' not in pcols:
                # What the job identity was made of (hashes per setting), so a later run can
                # say which setting changed. Older projects have none.
                con.execute("ALTER TABLE projects ADD COLUMN identity_json TEXT NOT NULL DEFAULT ''")
            ccols = {r['name'] for r in con.execute('PRAGMA table_info(chunks)').fetchall()}
            migrations = {
                'provider': "ALTER TABLE chunks ADD COLUMN provider TEXT",
                'model': "ALTER TABLE chunks ADD COLUMN model TEXT",
                'error_type': "ALTER TABLE chunks ADD COLUMN error_type TEXT",
                'error_message': "ALTER TABLE chunks ADD COLUMN error_message TEXT",
                'retry_count': "ALTER TABLE chunks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
                'fallback_used': "ALTER TABLE chunks ADD COLUMN fallback_used INTEGER NOT NULL DEFAULT 0",
                'prompt_version': "ALTER TABLE chunks ADD COLUMN prompt_version TEXT",
                'updated_at': "ALTER TABLE chunks ADD COLUMN updated_at TEXT",
                'source_sha': "ALTER TABLE chunks ADD COLUMN source_sha TEXT",
                # Paragraph IDs a PARTIAL chunk kept as source (they may be half Korean when
                # only one sentence of a paragraph failed): re-sent on the next run.
                'kept_pids': "ALTER TABLE chunks ADD COLUMN kept_pids TEXT",
            }
            for col,sql in migrations.items():
                if col not in ccols: con.execute(sql)
            # One-time data upgrades scan every row, so a large database (several GB) would
            # pay for them on every open. They run once and are recorded in user_version.
            if int(con.execute('PRAGMA user_version').fetchone()[0] or 0) < DATA_VERSION:
                con.execute("UPDATE chunks SET updated_at=CURRENT_TIMESTAMP WHERE updated_at IS NULL")
                # Backfill of the chunk source hash used by the translation memory.
                missing=con.execute("SELECT id,source_text FROM chunks WHERE source_sha IS NULL").fetchall()
                if missing:
                    con.executemany("UPDATE chunks SET source_sha=? WHERE id=?",[(_sha(r['source_text']),r['id']) for r in missing])
                con.execute(f'PRAGMA user_version={DATA_VERSION}')
            con.execute('CREATE INDEX IF NOT EXISTS idx_chunks_memory ON chunks(source_sha, status)')
            con.execute('CREATE INDEX IF NOT EXISTS idx_projects_resume ON projects(source_sha256, profile_id, job_fingerprint)')
            # Tiny partial index: only in-flight chunks are in it, so the crash recovery below
            # never scans the whole table.
            con.execute("CREATE INDEX IF NOT EXISTS idx_chunks_inflight ON chunks(status) WHERE status IN ('REQUESTING','RECEIVED','VALIDATING')")
            # (No whole-file check here since v2.7.0: see the note at the top of this file.)
            con.execute("UPDATE chunks SET status='PENDING', error_type=NULL, error_message=NULL WHERE status IN ('REQUESTING','RECEIVED','VALIDATING')")

    @contextmanager
    def connect(self):
        try:
            con = sqlite3.connect(self.path, timeout=30)
            con.execute('PRAGMA busy_timeout=30000')
        except sqlite3.DatabaseError as e:
            if is_damage_error(e): note_damage(self.path, e)
            raise
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA foreign_keys=ON')
            yield con
            con.commit()
        except sqlite3.DatabaseError as e:
            # A damaged page shows up as soon as it is read or written: remember it so the
            # program confirms and repairs the file once no job runs (v2.7.0).
            if is_damage_error(e): note_damage(self.path, e)
            raise
        finally:
            con.close()

    def create_project(self, source_path: str, source_language: str, profile_id: str, source_sha256: str, job_fingerprint: str = '') -> int:
        with self.connect() as con:
            cur = con.execute('INSERT INTO projects(source_path,source_language,profile_id,source_sha256,job_fingerprint) VALUES(?,?,?,?,?)',
                              (source_path, source_language, profile_id, source_sha256, job_fingerprint))
            return int(cur.lastrowid)

    def upsert_chunk(self, project_id: int, chunk_index: int, source_text: str) -> None:
        with self.connect() as con:
            row = con.execute('SELECT source_text,status FROM chunks WHERE project_id=? AND chunk_index=?', (project_id, chunk_index)).fetchone()
            if row is None:
                con.execute('INSERT INTO chunks(project_id,chunk_index,source_text,source_sha) VALUES(?,?,?,?)', (project_id, chunk_index, source_text, _sha(source_text)))
            elif row['source_text'] != source_text:
                # Never reuse a completed translation for changed chunk boundaries/source.
                con.execute("UPDATE chunks SET source_text=?,source_sha=?,status='PENDING',translation=NULL,error_type=NULL,error_message=NULL,retry_count=0,updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND chunk_index=?",
                            (source_text, _sha(source_text), project_id, chunk_index))

    def upsert_chunks(self, project_id: int, chunks) -> None:
        """upsert_chunk for every chunk of a job in ONE transaction (a book of thousands of
        chunks used to open and commit the database once per chunk before translating)."""
        with self.connect() as con:
            rows = {int(r['chunk_index']): r['source_text'] for r in
                    con.execute('SELECT chunk_index,source_text FROM chunks WHERE project_id=?', (project_id,)).fetchall()}
            for c in chunks:
                old = rows.get(int(c.index), None)
                if int(c.index) not in rows:
                    con.execute('INSERT INTO chunks(project_id,chunk_index,source_text,source_sha) VALUES(?,?,?,?)',
                                (project_id, c.index, c.source_text, _sha(c.source_text)))
                elif old != c.source_text:
                    # Never reuse a completed translation for changed chunk boundaries/source.
                    con.execute("UPDATE chunks SET source_text=?,source_sha=?,status='PENDING',translation=NULL,error_type=NULL,error_message=NULL,retry_count=0,updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND chunk_index=?",
                                (c.source_text, _sha(c.source_text), project_id, c.index))

    def prune_chunks(self, project_id: int, keep_indices) -> int:
        """Remove chunk rows the current chunk layout no longer has (a continued job whose
        chunking changed). Their paragraphs were reused through the paragraph memory."""
        keep = {int(i) for i in keep_indices}
        with self.connect() as con:
            stale = [int(r['chunk_index']) for r in con.execute('SELECT chunk_index FROM chunks WHERE project_id=?', (project_id,)).fetchall()
                     if int(r['chunk_index']) not in keep]
            if stale:
                con.executemany('DELETE FROM chunks WHERE project_id=? AND chunk_index=?', [(project_id, i) for i in stale])
            return len(stale)

    def cached_translations(self, profile_id: str, settings_key: str, chunks, exclude_project: int | None = None) -> dict[int, str]:
        """cached_translation for many chunks with one connection: {chunk index: translation}."""
        out: dict[int, str] = {}
        if not settings_key:
            return out
        with self.connect() as con:
            for c in chunks:
                row = con.execute('''SELECT c.translation, c.source_text FROM chunks c JOIN projects p ON p.id=c.project_id
                                     WHERE c.source_sha=? AND c.status='COMPLETED' AND p.profile_id=? AND p.settings_key=?
                                           AND c.project_id<>? AND c.translation IS NOT NULL
                                     ORDER BY c.updated_at DESC LIMIT 1''',
                                  (_sha(c.source_text), profile_id, settings_key, exclude_project if exclude_project is not None else -1)).fetchone()
                if row and row['source_text'] == c.source_text:
                    out[int(c.index)] = row['translation']
        return out

    def progress_counts(self, project_id: int) -> tuple[int, int]:
        """(chunks with a saved translation, all chunks) of a job."""
        with self.connect() as con:
            row = con.execute("SELECT SUM(CASE WHEN status IN ('COMPLETED','PARTIAL') AND translation IS NOT NULL THEN 1 ELSE 0 END) AS done, COUNT(*) AS n FROM chunks WHERE project_id=?",
                              (project_id,)).fetchone()
            return int(row['done'] or 0), int(row['n'] or 0)

    def set_identity(self, project_id: int, identity_json: str) -> None:
        with self.connect() as con:
            con.execute('UPDATE projects SET identity_json=? WHERE id=?', (identity_json or '', project_id))

    @staticmethod
    def kept_pid_set(row) -> set[int]:
        """Paragraphs a PARTIAL chunk kept as source (older rows: the IDs in its message)."""
        try:
            raw = row['kept_pids']
        except (IndexError, KeyError):
            raw = None
        if raw:
            return {int(x) for x in str(raw).split(',') if x.strip().isdigit()}
        try:
            status, msg = row['status'] if 'status' in row.keys() else 'PARTIAL', row['error_message'] or ''
        except (IndexError, KeyError):
            return set()
        if status != 'PARTIAL':
            return set()
        head = msg.split(') · ', 1)[0]
        return {int(x) for x in re.findall(r'P(\d{6})', head)}

    def set_chunk_status(self, project_id: int, chunk_index: int, status: str, **fields) -> None:
        sql, vals = self._status_update(project_id, chunk_index, status, fields)
        with self.connect() as con:
            con.execute(sql, vals)

    def set_chunk_statuses(self, project_id: int, updates) -> None:
        """set_chunk_status for many chunks in one transaction: [(chunk_index, status, fields)]."""
        with self.connect() as con:
            for chunk_index, status, fields in updates:
                sql, vals = self._status_update(project_id, chunk_index, status, dict(fields))
                con.execute(sql, vals)

    @staticmethod
    def _status_update(project_id: int, chunk_index: int, status: str, fields: dict):
        allowed = {'translation','provider','model','error_type','error_message','retry_count','fallback_used','prompt_version','kept_pids'}
        if status == 'COMPLETED':
            # Keep source/translation for exact-match reuse, but remove stale
            # interruption and error metadata from successfully finished chunks.
            fields = dict(fields, kept_pids=None, error_type=None, error_message=None)
        elif status != 'PARTIAL':
            fields = dict(fields, kept_pids=None)   # only a PARTIAL chunk has kept paragraphs
        pairs = [('status', status)] + [(k, v) for k, v in fields.items() if k in allowed]
        sql = 'UPDATE chunks SET ' + ', '.join(f'{k}=?' for k, _ in pairs) + ', updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND chunk_index=?'
        vals = [v for _, v in pairs] + [project_id, chunk_index]
        return sql, vals


    def reset_chunks_from(self, project_id: int, chunk_index: int) -> None:
        """Invalidate a translation suffix after the first incomplete chunk.

        Later completed chunks may have been translated with stale/missing previous context.
        Keeping only the fully completed prefix preserves contextual integrity on resume.
        """
        with self.connect() as con:
            con.execute("""UPDATE chunks
                           SET status='PENDING', translation=NULL, error_type=NULL, error_message=NULL,
                               retry_count=0, fallback_used=0, updated_at=CURRENT_TIMESTAMP
                           WHERE project_id=? AND chunk_index>=?""", (project_id, chunk_index))

    def pending_chunks(self, project_id: int):
        with self.connect() as con:
            return con.execute("SELECT * FROM chunks WHERE project_id=? AND status!='COMPLETED' ORDER BY chunk_index", (project_id,)).fetchall()

    def completed_translations(self, project_id: int):
        with self.connect() as con:
            return con.execute("SELECT chunk_index,translation FROM chunks WHERE project_id=? AND status='COMPLETED' ORDER BY chunk_index", (project_id,)).fetchall()

    def partial_translations(self, project_id: int):
        with self.connect() as con:
            return con.execute("SELECT chunk_index,translation,kept_pids,error_message FROM chunks WHERE project_id=? AND status='PARTIAL' AND translation IS NOT NULL ORDER BY chunk_index", (project_id,)).fetchall()

    def has_progress_for_source(self, source_sha256: str, profile_id: str) -> bool:
        """True when any job on this exact source text already has translated chunks."""
        with self.connect() as con:
            row=con.execute("SELECT 1 FROM projects p JOIN chunks c ON c.project_id=p.id WHERE p.source_sha256=? AND p.profile_id=? AND c.status IN ('COMPLETED','PARTIAL') LIMIT 1",
                            (source_sha256, profile_id)).fetchone()
            return bool(row)

    def translated_rows_for_source(self, source_sha256: str, profile_id: str, settings_key: str):
        """Translated chunks of every job on this exact source with the same translation
        settings (glossary, memo, options, prompt), newest job last."""
        with self.connect() as con:
            return con.execute("""SELECT c.source_text,c.translation,c.status,c.kept_pids,c.error_message FROM chunks c JOIN projects p ON p.id=c.project_id
                                   WHERE p.source_sha256=? AND p.profile_id=? AND p.settings_key=? AND c.status IN ('COMPLETED','PARTIAL')
                                   AND c.translation IS NOT NULL ORDER BY p.id, c.chunk_index""",
                               (source_sha256, profile_id, settings_key)).fetchall()

    def all_chunks(self, project_id: int):
        with self.connect() as con:
            return con.execute('SELECT * FROM chunks WHERE project_id=? ORDER BY chunk_index', (project_id,)).fetchall()

    def find_project(self, source_path: str, source_language: str, profile_id: str, source_sha256: str, job_fingerprint: str = ''):
        with self.connect() as con:
            row = con.execute('''SELECT * FROM projects WHERE source_sha256=? AND source_language=? AND profile_id=? AND job_fingerprint=? ORDER BY id DESC LIMIT 1''',
                              (source_sha256, source_language, profile_id, job_fingerprint)).fetchone()
            if row and row['source_path'] != source_path:
                con.execute('UPDATE projects SET source_path=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (source_path, row['id']))
            return row

    def get_or_create_project(self, source_path: str, source_language: str, profile_id: str, source_sha256: str, job_fingerprint: str = '', settings_key: str = '') -> int:
        row = self.find_project(source_path, source_language, profile_id, source_sha256, job_fingerprint)
        pid = int(row['id']) if row else self.create_project(source_path, source_language, profile_id, source_sha256, job_fingerprint)
        with self.connect() as con:
            # The job is being worked on again: it counts as recent for the clean-up below.
            if settings_key:
                con.execute('UPDATE projects SET settings_key=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', (settings_key, pid))
            else:
                con.execute('UPDATE projects SET updated_at=CURRENT_TIMESTAMP WHERE id=?', (pid,))
        return pid

    # ------------------------------------------------------------ clean-up (v2.5.6)
    def remove_old_projects(self, keep_days: int) -> tuple[int, int]:
        """Delete the jobs nobody has worked on for `keep_days` days (no chunk translated,
        retried or resumed, and the job not started again since). Returns (jobs, chunks)
        removed. A job in use is never this old; keep_days <= 0 keeps everything."""
        if int(keep_days or 0) <= 0:
            return 0, 0
        cutoff = f'-{int(keep_days)} days'
        with self.connect() as con:
            old = [int(r['id']) for r in con.execute(
                """SELECT p.id FROM projects p LEFT JOIN chunks c ON c.project_id=p.id
                   GROUP BY p.id
                   HAVING MAX(p.updated_at, COALESCE(MAX(c.updated_at), p.updated_at)) < datetime('now', ?)""",
                (cutoff,)).fetchall()]
            chunks = 0
            for i in range(0, len(old), 500):
                part = old[i:i + 500]; marks = ','.join('?' * len(part))
                chunks += int(con.execute(f'SELECT COUNT(*) FROM chunks WHERE project_id IN ({marks})', part).fetchone()[0])
                con.execute(f'DELETE FROM chunks WHERE project_id IN ({marks})', part)
                con.execute(f'DELETE FROM terms WHERE project_id IN ({marks})', part)
                con.execute(f'DELETE FROM projects WHERE id IN ({marks})', part)
            # Rows left behind by very old builds whose job is already gone.
            chunks += con.execute('DELETE FROM chunks WHERE project_id NOT IN (SELECT id FROM projects)').rowcount or 0
            con.execute('DELETE FROM terms WHERE project_id NOT IN (SELECT id FROM projects)')
            con.execute("DELETE FROM events WHERE created_at < datetime('now', ?)", (cutoff,))
        return len(old), chunks

    def compact(self, max_bytes: int = 1_000_000_000, min_free_share: float = 0.25) -> bool:
        """Shrink the file (VACUUM) when at least a quarter of it is free space left by
        deleted rows. Skipped for a very large file (it would take long). True when done."""
        try:
            if self.path.stat().st_size > max_bytes:
                return False
            with self.connect() as con:
                pages = int(con.execute('PRAGMA page_count').fetchone()[0] or 0)
                free = int(con.execute('PRAGMA freelist_count').fetchone()[0] or 0)
            if not pages or free / pages < min_free_share:
                return False
            con = sqlite3.connect(self.path, timeout=30)
            try:
                con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                con.execute('VACUUM')
                # In WAL mode the rewritten pages land in the -wal file first: move them into
                # the database file so it really becomes smaller.
                con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            finally:
                con.close()
            return True
        except Exception:
            return False

    def replace_terms(self, project_id: int, terms: dict[str, str]) -> None:
        with self.connect() as con:
            con.execute('DELETE FROM terms WHERE project_id=?', (project_id,))
            con.executemany('INSERT INTO terms(project_id,source_term,target_term,status) VALUES(?,?,?,?)',
                            [(project_id, a, b, 'CONFIRMED') for a, b in terms.items()])

    def latest_project_for_source(self, source_sha256: str, source_language: str, profile_id: str):
        with self.connect() as con:
            return con.execute('SELECT * FROM projects WHERE source_sha256=? AND source_language=? AND profile_id=? ORDER BY id DESC LIMIT 1',
                               (source_sha256, source_language, profile_id)).fetchone()

    def best_project_for_source(self, source_sha256: str, source_language: str, profile_id: str, exclude_project: int | None = None):
        """The job on this exact source with the most saved translation (newest on a tie),
        with its count as 'done'; None when no job has any."""
        with self.connect() as con:
            return con.execute("""SELECT p.*, SUM(CASE WHEN c.status IN ('COMPLETED','PARTIAL') AND c.translation IS NOT NULL THEN 1 ELSE 0 END) AS done
                                   FROM projects p JOIN chunks c ON c.project_id=p.id
                                   WHERE p.source_sha256=? AND p.source_language=? AND p.profile_id=? AND p.id<>?
                                   GROUP BY p.id HAVING done>0 ORDER BY done DESC, p.id DESC LIMIT 1""",
                               (source_sha256, source_language, profile_id, exclude_project if exclude_project is not None else -1)).fetchone()

    def project_terms(self, project_id: int) -> dict[str, str]:
        with self.connect() as con:
            rows = con.execute('SELECT source_term,target_term FROM terms WHERE project_id=? ORDER BY id', (project_id,)).fetchall()
            return {r['source_term']: r['target_term'] for r in rows}

    def rename_in_translations(self, project_id: int, renames: dict[str, str]) -> int:
        """Apply a changed name rendering to every stored translation of the project and
        move the project to its new identity in the same transaction."""
        from .name_rename import apply_counted
        total = 0
        self.last_rename_skipped = 0      # occurrences left alone as part of a longer word (하늘색)
        with self.connect() as con:
            rows = con.execute('SELECT chunk_index, translation FROM chunks WHERE project_id=? AND translation IS NOT NULL', (project_id,)).fetchall()
            for r in rows:
                new, n, skipped = apply_counted(r['translation'], renames)
                self.last_rename_skipped += skipped
                if n:
                    total += n
                    con.execute('UPDATE chunks SET translation=? WHERE project_id=? AND chunk_index=?', (new, project_id, r['chunk_index']))
        return total

    def rekey_project(self, project_id: int, job_fingerprint: str, settings_key: str) -> None:
        with self.connect() as con:
            con.execute('UPDATE projects SET job_fingerprint=?, settings_key=? WHERE id=?', (job_fingerprint, settings_key, project_id))

    def latest_terms_for_source(self, source_sha256: str, profile_id: str) -> dict[str, str]:
        with self.connect() as con:
            row = con.execute('SELECT id FROM projects WHERE source_sha256=? AND profile_id=? ORDER BY id DESC LIMIT 1',
                              (source_sha256, profile_id)).fetchone()
            if not row:
                return {}
            rows = con.execute('SELECT source_term,target_term FROM terms WHERE project_id=? ORDER BY id', (row['id'],)).fetchall()
            return {r['source_term']: r['target_term'] for r in rows}
