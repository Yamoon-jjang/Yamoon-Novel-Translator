"""이전 작업 이어 열기 (v2.6.8).

A translation or a refinement that did not reach its end (stopped, paused, the window
closed, the PC switched off) leaves one small note here. At the next start the main window
asks whether to open that work again, with the settings it ran with; a finished job leaves
nothing, so nothing is asked after a run that ended normally.

Two slots, each holding the latest job of its kind:
  translation — one file ('single') or a folder / several dropped files ('folder'),
                with the folder's settings and the 재정비 자동 시작 choice;
  refinement  — a 한국어 소설 재정비 run started in the refinement window (by hand, or by
                재정비 자동 시작 after a single file; a folder run's refinements belong to the
                folder note).
Opening only shows the work as it was: nothing starts by itself, and the saved translation /
refinement records do the rest ([번역 시작 / 계속] / [재정비 시작] go on where they stopped).
The note is never needed for that; it only saves finding the file or folder again.
"""
from __future__ import annotations

import time
from pathlib import Path

from .config import DATA_DIR, load_json, save_json

FILE_NAME = 'unfinished_work.json'
SLOTS = ('translation', 'refinement')


def _path() -> Path:
    return DATA_DIR / FILE_NAME


def _read() -> dict:
    try:
        data = load_json(_path())
    except Exception:
        return {}
    return {k: dict(v) for k, v in data.items() if k in SLOTS and isinstance(v, dict)}


def _write(data: dict) -> None:
    try:
        if not data:
            try:
                _path().unlink()
            except FileNotFoundError:
                pass
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        save_json(_path(), data)
    except Exception:
        pass        # a note that cannot be written only means no question next time


def get(slot: str) -> dict | None:
    return _read().get(slot)


def mark(slot: str, entry: dict) -> None:
    """This job started: it is the slot's unfinished job until it ends."""
    data = _read()
    data[slot] = {**entry, 'saved_at': time.strftime('%Y-%m-%d %H:%M')}
    _write(data)


def update(slot: str, match: dict, **fields) -> None:
    """Progress of the job that holds the slot (nothing when another job holds it)."""
    data = _read()
    entry = data.get(slot)
    if not entry or any(_same(entry.get(k), v) is False for k, v in match.items()):
        return
    if all(entry.get(k) == v for k, v in fields.items()):
        return
    entry.update(fields)
    _write(data)


def clear(slot: str, match: dict | None = None) -> None:
    """The job ended (or the user chose not to open it): the slot is emptied, but only
    when it still holds that job."""
    data = _read()
    entry = data.get(slot)
    if entry is None:
        return
    if match and any(_same(entry.get(k), v) is False for k, v in match.items()):
        return
    data.pop(slot, None)
    _write(data)


def _same(a, b) -> bool:
    if isinstance(a, str) and isinstance(b, str) and ('/' in a or '\\' in a or '/' in b or '\\' in b):
        import os
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    return a == b


def still_there(entry: dict) -> bool:
    """The file or folder of the note can still be opened."""
    try:
        kind = entry.get('kind')
        if kind in ('single', 'refine'):
            return Path(str(entry.get('path') or '')).is_file()
        if kind == 'folder':
            if not Path(str(entry.get('root') or '')).is_dir():
                return False
            sel = entry.get('selection')
            return not sel or any(Path(str(p)).is_file() for p in sel)
    except Exception:
        pass
    return False


def pending() -> list[tuple[str, dict]]:
    """Notes to ask about, translation first. A note whose file or folder is gone is dropped."""
    data = _read()
    out = []
    for slot in SLOTS:
        entry = data.get(slot)
        if not entry:
            continue
        if still_there(entry):
            out.append((slot, entry))
        else:
            clear(slot)
    return out


def question_text(entry: dict) -> str:
    kind = entry.get('kind')
    when = f" (마지막 기록 {entry['saved_at']})" if entry.get('saved_at') else ''
    refine = '켬' if entry.get('auto_refine') else '끔'
    if kind == 'single':
        name = Path(str(entry.get('path'))).name
        pct = entry.get('percent')
        progress = f'번역 {pct}%까지 진행 · ' if isinstance(pct, int) and pct >= 0 else ''
        return (f'이전에 번역하던 파일이 끝나지 않은 채 남아 있습니다{when}.\n\n{name}\n'
                f'{progress}끝나면 재정비 자동 시작: {refine}\n\n'
                '이 파일을 그때 설정대로 열까요?\n'
                '열린 뒤 [번역 시작 / 계속]을 누르면 번역한 곳부터 이어집니다.')
    if kind == 'folder':
        sel = entry.get('selection') or []
        what = f'끌어다 놓은 파일 {len(sel):,}개' if sel else '폴더'
        files = int(entry.get('files') or 0)
        nxt = int(entry.get('next') or 0)
        progress = f'파일 {files:,}개 중 {min(nxt + 1, files):,}번째 파일까지 진행 · ' if files else ''
        rel = entry.get('relation_label') or ''
        return (f'이전에 일괄 번역하던 {what}가 끝나지 않은 채 남아 있습니다{when}.\n\n{entry.get("root")}\n'
                f'{progress}{("파일 관계: " + rel + " · ") if rel else ""}끝나면 재정비 자동 시작: {refine}\n\n'
                '그때 목록과 설정대로 열까요?\n'
                '열린 뒤 [번역 시작 / 계속]을 누르면 끝난 파일은 건너뛰고, 하던 파일(번역 · 재정비)부터 이어집니다.')
    name = Path(str(entry.get('path'))).name
    return (f'이전에 재정비하던 TXT가 끝나지 않은 채 남아 있습니다{when}.\n\n{name}\n\n'
            '재정비 창을 이 파일로 열까요?\n'
            '[재정비 시작]을 누르면 다듬던 곳부터 이어집니다 (같은 윤문 모델일 때).')
