"""The reader's own translation guidelines, one text per language profile.

Kept in the user data folder (not the program folder), so they survive updates.
They are added to every request as style wishes; the fixed rules still come first.
"""
from __future__ import annotations
import json
from .config import DATA_DIR

GUIDELINES_FILE = 'user_guidelines.json'
MAX_CHARS = 3000


def _path(base=None):
    return (base or DATA_DIR) / GUIDELINES_FILE


def load_all(base=None) -> dict[str, str]:
    try:
        data = json.loads(_path(base).read_text(encoding='utf-8'))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_user_guidelines(profile_id: str, base=None) -> str:
    return (load_all(base).get(profile_id) or '').strip()[:MAX_CHARS]


def save_user_guidelines(profile_id: str, text: str, base=None) -> None:
    data = load_all(base)
    text = (text or '').strip()[:MAX_CHARS]
    if text:
        data[profile_id] = text
    else:
        data.pop(profile_id, None)
    path = _path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)
