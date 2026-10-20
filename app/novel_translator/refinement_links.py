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
