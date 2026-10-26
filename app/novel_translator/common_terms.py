"""내 공용 용어집: glossary entries kept across books, one list per source language.

Every finished book adds its confirmed glossary entries that are not already there
(the first rendering wins; nothing is overwritten). A new book then
  * takes the entries that occur in its text into its own glossary when the
    names/terms are extracted (a sequel keeps the same names) — two-character
    sources only when they occur 5+ times, so another book's short name does not
    slip into a place where the same two characters are an ordinary word — and
  * gets the remaining entries that occur in a chunk as reference hints.
Suspicious entries (Chinese characters left in the Korean, plain common words,
one-character sources) are never stored.
"""
from __future__ import annotations
import json, os, re
from pathlib import Path

FILE = 'common_terms.json'
MAX_PER_LANG = 5000
PULL_MIN = 2          # times a source must occur in a new book to be taken into its glossary
PULL_MIN_SHORT = 5    # ... when the source is only two characters long


def _path(base) -> Path:
    if base is None:
        from .config import DATA_DIR
        base = DATA_DIR
    return Path(base) / FILE


def load_all(base=None) -> dict:
    try:
        data = json.loads(_path(base).read_text(encoding='utf-8'))
        return {k: dict(v) for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
    except Exception:
        return {}


def load(lang: str, base=None) -> dict[str, str]:
    return {str(a): str(b) for a, b in load_all(base).get(lang, {}).items() if str(a).strip() and str(b).strip()}


def _save_all(data: dict, base=None) -> None:
    p = _path(base); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def acceptable(source: str, korean: str) -> bool:
    from .term_extraction import valid_rendering, split_address, is_address_term, is_not_name
    s = str(source or '').strip(); k = str(korean or '').strip()
    return (len(s) >= 2 and bool(k) and len(k) <= 30 and s != k
            and not is_not_name(s)               # 公司 / He's / My God / Son: not for every book
            and valid_rendering(s, k) and bool(re.search(r'[가-힣]', k))
            and split_address(s, k) is None      # 丹丹姐 = 단단 언니 depends on the speaker
            and not is_address_term(s))          # 老姨 = 노이 is an address, not a name


def usable(entries: dict[str, str]) -> dict[str, str]:
    """Entries a book may use. An older shared entry that fixes a speaker-dependent address
    (丹丹姐 = 단단 언니) is left out; the file itself is not changed."""
    from .term_extraction import split_address, is_address_term, is_never_term
    # He's / My God stored before v2.5.4 are never a term; a doubtful word (Son, God) is kept,
    # since the user may have put it there on purpose for a name.
    return {a: b for a, b in (entries or {}).items() if split_address(a, b) is None and not is_address_term(a)
            and not is_never_term(a)}


def new_candidates(lang: str, terms: dict, base=None) -> dict[str, str]:
    """Entries of `terms` that add() would store if there were room."""
    cur = load_all(base).get(lang, {})
    out = {}
    for a, b in (terms or {}).items():
        a = str(a).strip(); b = str(b).strip()
        if a not in cur and a not in out and acceptable(a, b):
            out[a] = b
    return out


def hits(source: str, common: dict, current: dict) -> dict:
    """Shared entries that occur in this text (2+ times; 5+ for two characters) and are not
    in the book's glossary yet. They are only CANDIDATES for the extraction."""
    if not source or not common:
        return {}
    return {a: b for a, b in present_in(source, common).items() if a not in current}


def add(lang: str, terms: dict, base=None) -> list[tuple[str, str]]:
    """Add new entries (existing sources are kept as they are). Returns what was added."""
    data = load_all(base); cur = data.setdefault(lang, {})
    added = []
    for a, b in (terms or {}).items():
        a = str(a).strip(); b = str(b).strip()
        if a in cur or not acceptable(a, b) or len(cur) >= MAX_PER_LANG:
            continue
        cur[a] = b; added.append((a, b))
    if added:
        _save_all(data, base)
    return added


def replace(lang: str, terms: dict, base=None) -> None:
    """Save the list as edited in the dialog."""
    data = load_all(base)
    data[lang] = {str(a).strip(): str(b).strip() for a, b in (terms or {}).items() if str(a).strip() and str(b).strip()}
    _save_all(data, base)


def present_in(text: str, entries: dict[str, str], min_count: int = PULL_MIN,
               short_min: int = PULL_MIN_SHORT) -> dict[str, str]:
    """Entries whose source occurs often enough in the text: `min_count` times, or
    `short_min` times for a two-character (or shorter) source."""
    out = {}
    for a, b in entries.items():
        if not a:
            continue
        need = max(min_count, short_min) if len(a) <= 2 else min_count
        if text.count(a) >= need:
            out[a] = b
    return out
