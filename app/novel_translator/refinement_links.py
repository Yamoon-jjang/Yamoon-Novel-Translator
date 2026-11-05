"""Link a translated Korean TXT back to the speech table and glossary that produced it.

Refinement edits a Korean TXT. When that TXT is a result of this app, the speech
levels and confirmed names used while translating are far more reliable than a fresh
sample analysis, so they become the refinement reference table. The link is a small
record kept in the data folder, found by the text itself (survives a rename or move)
and, failing that, by the result path (survives a light hand edit).
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
import time

from .config import DATA_DIR, load_json, save_json

FILENAME = 'refinement_links.json'
MAX_LINKS = 400
MAX_TERMS = 120
GUIDANCE_MAX = 3000


def text_digest(text: str) -> str:
    """Same book text, whatever its line endings (a TXT saved again as CRLF/LF still matches)."""
    normalized = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    return sha256(normalized.encode('utf-8', 'surrogatepass')).hexdigest()


def _path_key(path) -> str:
    try:
        return str(Path(path).resolve()).casefold()
    except OSError:
        return str(path).casefold()


def _store(root: Path | None) -> Path:
    return (root or DATA_DIR) / FILENAME


def _load(root: Path | None) -> list[dict]:
    p = _store(root)
    if not p.exists():
        return []
    try:
        rows = load_json(p).get('links')
    except Exception:
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def record_translation(result_path, text: str, speech_rows, terms: dict | None,
                       source_name: str = '', root: Path | None = None) -> bool:
    """Remember what this result was translated with. Never raises: refinement simply
    falls back to its own sample analysis when nothing was recorded."""
    try:
        from .speech_levels import clean_rows
        rows = clean_rows(speech_rows)
        names = []
        for value in (terms or {}).values():
            v = str(value or '').strip()
            if v and v not in names and len(v) <= 40:
                names.append(v)
        if not rows and not names:
            return False
        entry = {'path': _path_key(result_path), 'sha256': text_digest(text),
                 'source': str(source_name or '')[:200], 'speech': rows,
                 'names': names[:MAX_TERMS], 'saved': int(time.time())}
        links = [r for r in _load(root) if r.get('path') != entry['path'] and r.get('sha256') != entry['sha256']]
        links.insert(0, entry)
        save_json(_store(root), {'schema': 1, 'links': links[:MAX_LINKS]})
        return True
    except Exception:
        return False


def find_reference(path, text: str, root: Path | None = None) -> dict | None:
    """The record for this Korean TXT: same text first, then same result path."""
    links = _load(root)
    digest = text_digest(text)
    for row in links:
        if row.get('sha256') == digest:
            return row
    key = _path_key(path)
    for row in links:
        if row.get('path') == key:
            return row
    return None


def reference_guidance(ref: dict | None) -> str:
    """The reference table text sent with every refinement request."""
    if not ref:
        return ''
    from .speech_levels import clean_rows
    rows = clean_rows(ref.get('speech'))
    names = [str(n) for n in (ref.get('names') or []) if str(n).strip()]
    parts = ['[번역할 때 확정된 기준표 · 이 작품 번역에 실제로 쓰인 값]']
    if rows:
        parts.append('인물 말투(화자 → 상대: 말투):')
        parts += [f"- {r['speaker']} → {r['listener']}: {r['level']}" + (f" ({r['note']})" if r['note'] else '')
                  for r in rows]
    if names:
        parts.append('확정 이름·용어(표기를 바꾸지 말 것): ' + ', '.join(names))
    text = '\n'.join(parts)
    return text[:GUIDANCE_MAX]


def reference_summary(ref: dict | None) -> str:
    if not ref:
        return ''
    from .speech_levels import clean_rows
    n_rows = len(clean_rows(ref.get('speech')))
    n_names = len([n for n in (ref.get('names') or []) if str(n).strip()])
    return f'인물 말투 {n_rows}쌍 · 확정 이름·용어 {n_names}개'


# ---- 기준표 보기 (v2.6.2 보완 3): the same table text shown as the translation window shows
# its 인물 말투 and 용어 (rows and columns), for both a translation record and a sample analysis.
_ARROW = r'(?:→|->|⇒|➔|➡)'
_SPEECH_LINE = re.compile(r'^\s*(?:[-*•·]\s*|\d+[.)]\s*)?(.+?)\s*' + _ARROW + r'\s*(.+?)\s*[:：]\s*(.+?)\s*$')
_NOTE_TAIL = re.compile(r'^(.*?)\s*[(（]([^()（）]*)[)）]\s*$')
_TABLE_SEP = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$')
_NAMES_LINE = re.compile(r'^\s*(확정 )?이름·용어[^:：]*[:：]\s*(.+)$')


def _plain(cell: str) -> str:
    """A cell without markdown emphasis, backticks or list marks."""
    s = re.sub(r'[*_`]+', '', str(cell or '')).strip()
    return re.sub(r'^(?:[-•·]\s+|\d+[.)]\s+)', '', s).strip()


def _split_level(level: str) -> tuple[str, str]:
    level = _plain(level)
    m = _NOTE_TAIL.match(level)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2).strip()
    return level, ''


def guidance_tables(text: str) -> tuple[list[tuple[str, str, str, str]], list[tuple[str, str]]]:
    """(speech rows, term rows) read from a reference table, for display only.

    Speech rows are (화자, 상대, 말투, 관계·메모) from lines such as "A → B: 반말 (note)"
    or table rows with an arrow cell or 화자/상대 columns. Term rows are (항목, 내용) from the
    other table rows and "key: value" lines; a translation record's confirmed names become
    one row each. Nothing here changes what a run sends: the text itself stays the table."""
    speech, terms, seen, seen_terms = [], [], set(), set()

    def add_speech(sp, li, level, note=''):
        sp, li = _plain(sp), _plain(li)
        level, tail = _split_level(level)
        note = _plain(note) or tail
        if not sp or not li or not level or sp == li or (sp, li) in seen:
            return False
        if len(sp) > 30 or len(li) > 30 or len(level) > 30:
            return False
        seen.add((sp, li)); speech.append((sp, li, level, note))
        return True

    def add_term(key, value=''):
        key, value = _plain(key), _plain(value)
        if not key or (key, value) in seen_terms:
            return
        seen_terms.add((key, value)); terms.append((key, value))

    header = None          # columns of the markdown table being read
    lines = str(text or '').splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            header = None
            continue
        if _TABLE_SEP.match(line):
            continue
        m = _NAMES_LINE.match(line)
        if m:
            # 확정 이름·용어 (a translation record) or 이름·용어 with kinds (a sample analysis: 시즈코(인물))
            for name in m.group(2).split(','):
                kind = _NOTE_TAIL.match(name.strip())
                if kind and kind.group(1).strip():
                    add_term(kind.group(1), kind.group(2))
                else:
                    add_term(name, '확정 표기' if m.group(1) else '')
            continue
        if line.startswith('|') or line.count('|') >= 2:
            cells = [_plain(c) for c in line.strip('|').split('|')]
            following = lines[i + 1].strip() if i + 1 < len(lines) else ''
            if _TABLE_SEP.match(following):       # the header row of a table
                header = cells
                continue
            cols = [c.replace(' ', '') for c in (header or [])]
            if '화자' in cols and '상대' in cols:
                get = lambda name: cells[cols.index(name)] if cols.index(name) < len(cells) else ''
                level_col = next((c for c in cols if c in ('말투', '말투(존댓말/반말)', '존댓말/반말', '높임')), '')
                if level_col:
                    rest = [c for j, c in enumerate(cells) if j < len(cols) and cols[j] not in ('화자', '상대', level_col) and c]
                    add_speech(get('화자'), get('상대'), get(level_col), ' · '.join(rest))
                    continue
            arrow = next((j for j, c in enumerate(cells) if re.search(_ARROW, c)), None)
            if arrow is not None:
                parts = re.split(_ARROW, cells[arrow], maxsplit=1)
                after = [c for c in cells[arrow + 1:] if c]
                if len(parts) == 2:
                    pair = re.split(r'[:：]', parts[1], maxsplit=1)
                    if len(pair) == 2 and add_speech(parts[0], pair[0], pair[1], ' · '.join(after)):
                        continue
                    if after and add_speech(parts[0], parts[1], after[0], ' · '.join(after[1:])):
                        continue
            cells = [c for c in cells if c and c not in ('-', '—', '–')]
            if cells:
                add_term(cells[0], ' · '.join(cells[1:]))
            continue
        header = None
        if (line.startswith('#') or (line.startswith('[') and line.endswith(']'))
                or re.fullmatch(r'\*\*[^*]+\*\*[:：]?', line)):
            continue        # a heading or the "[번역할 때 확정된 기준표 …]" line
        if line.endswith((':', '：')):
            continue        # a section title such as "인물 말투(화자 → 상대: 말투):"
        m = _SPEECH_LINE.match(line)
        if m and add_speech(m.group(1), m.group(2), m.group(3)):
            continue
        key, sep, value = _plain(line).partition(':')
        if not sep:
            key, sep, value = _plain(line).partition('：')
        if sep and key.strip() and len(key) <= 40:
            add_term(key, value)
        else:
            add_term(line)
    return speech, terms


def guidance_counts(text: str) -> str:
    """'인물 말투 8쌍 · 이름·용어 12개' for the log and the window."""
    speech, terms = guidance_tables(text)
    return f'인물 말투 {len(speech)}쌍 · 이름·용어 {len(terms)}개'
