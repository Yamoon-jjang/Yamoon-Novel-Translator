from __future__ import annotations
import hashlib
import re
import sqlite3
from pathlib import Path
from contextlib import contextmanager

_INTEGRITY_CHECKED: set[str] = set()
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
            key=str(self.path.resolve())
            if key not in _INTEGRITY_CHECKED:
                # Once per session. quick_check finds damaged pages/records like integrity_check
                # but skips the slow cross-check of index contents.
                check = con.execute('PRAGMA quick_check').fetchone()[0]
                if str(check).lower() != 'ok':
                    raise sqlite3.DatabaseError(f'SQLite integrity_check failed: {check}')
                _INTEGRITY_CHECKED.add(key)
            con.execute("UPDATE chunks SET status='PENDING', error_type=NULL, error_message=NULL WHERE status IN ('REQUESTING','RECEIVED','VALIDATING')")

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.execute('PRAGMA busy_timeout=30000')
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        try:
            yield con
            con.commit()
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

    def first_incomplete_index(self, project_id: int) -> int | None:
        with self.connect() as con:
            row=con.execute("SELECT MIN(chunk_index) AS idx FROM chunks WHERE project_id=? AND status!='COMPLETED'", (project_id,)).fetchone()
            return None if row is None or row['idx'] is None else int(row['idx'])

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
        if settings_key:
            with self.connect() as con:
                con.execute('UPDATE projects SET settings_key=? WHERE id=?', (settings_key, pid))
        return pid

    def cached_translation(self, profile_id: str, settings_key: str, source_text: str, exclude_project: int | None = None) -> str | None:
        """Translation memory: a COMPLETED translation of exactly this chunk source,
        made with the same profile and translation settings in another job."""
        if not settings_key:
            return None
        with self.connect() as con:
            row = con.execute('''SELECT c.translation, c.source_text FROM chunks c JOIN projects p ON p.id=c.project_id
                                 WHERE c.source_sha=? AND c.status='COMPLETED' AND p.profile_id=? AND p.settings_key=?
                                       AND c.project_id<>? AND c.translation IS NOT NULL
                                 ORDER BY c.updated_at DESC LIMIT 1''',
                              (_sha(source_text), profile_id, settings_key, exclude_project if exclude_project is not None else -1)).fetchone()
            if row and row['source_text'] == source_text:
                return row['translation']
        return None

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
