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


def _is_file(p) -> bool:
    try:
        return bool(p) and Path(str(p)).is_file()
    except Exception:
        return False


def _folder_files_now(entry: dict) -> list[str]:
    """The folder's source files as the run would list them now (same rules as the window:
    the dropped list, or the folder with its 하위 폴더 / result-folder rule, minus 제외)."""
    sel = entry.get('selection')
    if sel:
        now = [str(p) for p in sel if _is_file(p)]
    else:
        from .batch import discover_txt_files
        skip = entry.get('skip_root')
        try:
            now = [str(p) for p in discover_txt_files(Path(str(entry.get('root'))), bool(entry.get('include_sub')),
                                                      Path(str(skip)) if skip else None)]
        except Exception:
            now = []
    excluded = set(entry.get('excluded') or [])
    if excluded:
        def key(x):
            try:
                return str(Path(x).resolve())
            except Exception:
                return x
        now = [x for x in now if key(x) not in excluded]
    return now


def inspect(entry: dict, translation: dict | None = None):
    """What the note can still open, decided once for every case:
      ('ask', entry, info)  — ask about it (entry may be a converted one, see refine);
      ('drop', entry, why)  — nothing left to open: the note goes without a question.
    info carries what the question and the opening report (files now / gone / current file)."""
    kind = entry.get('kind')
    try:
        if kind == 'single':
            if _is_file(entry.get('path')):
                return 'ask', entry, {}
            return 'drop', entry, f"번역하던 파일이 없어졌습니다 ({Path(str(entry.get('path'))).name})"
        if kind == 'refine':
            if _is_file(entry.get('path')):
                return 'ask', entry, {}
            origin = entry.get('origin')
            if _is_file(origin):
                # The translation result it was refining is gone but the original is there:
                # opening the original and [번역 시작 / 계속] makes the result again from the
                # saved translation (no new translating), and 재정비 자동 시작 goes on with it.
                t = translation or {}
                if t.get('kind') == 'single' and _same(t.get('path'), origin):
                    return 'drop', entry, ''      # the translation note already opens that file
                return 'ask', {'kind': 'single', 'path': str(origin), 'profile': entry.get('profile'),
                               'auto_refine': True, 'percent': 100, 'result_gone': Path(str(entry.get('path'))).name}, {}
            return 'drop', entry, f"재정비하던 TXT가 없어졌습니다 ({Path(str(entry.get('path'))).name})"
        if kind == 'folder':
            root = Path(str(entry.get('root') or ''))
            if not root.is_dir():
                return 'drop', entry, f'일괄 번역하던 폴더가 없어졌습니다 ({root.name or root})'
            now = _folder_files_now(entry)
            if not now:
                return 'drop', entry, f'일괄 번역하던 폴더에 원문 파일이 남아 있지 않습니다 ({root.name})'
            now_keys = {_norm(x) for x in now}
            then = list(entry.get('names') or entry.get('selection') or [])
            gone = [x for x in then if _norm(x) not in now_keys]
            cur = entry.get('current')
            info = {'now': now, 'gone': gone,
                    'current_gone': bool(cur) and _norm(cur) not in now_keys}
            return 'ask', entry, info
    except Exception:
        pass
    return 'drop', entry, ''


def _norm(p) -> str:
    import os
    try:
        return os.path.normcase(os.path.abspath(str(p)))
    except Exception:
        return str(p)


def still_there(entry: dict) -> bool:
    return inspect(entry)[0] == 'ask'


def review() -> tuple[list[tuple[str, dict, dict]], list[str]]:
    """Program start: (questions to ask, translation first; reasons for notes dropped because
    nothing is left to open). Dropped notes are removed here."""
    data = _read()
    asks, dropped = [], []
    for slot in SLOTS:
        entry = data.get(slot)
        if not entry:
            continue
        verdict, shown, info = inspect(entry, data.get('translation'))
        if verdict == 'ask':
            asks.append((slot, shown, info))
        else:
            clear(slot)
            if info:
                dropped.append(str(info))
    return asks, dropped


def pending() -> list[tuple[str, dict]]:
    return [(slot, entry) for slot, entry, _info in review()[0]]


def question_text(entry: dict, info: dict | None = None) -> str:
    info = info or {}
    kind = entry.get('kind')
    when = f" (마지막 기록 {entry['saved_at']})" if entry.get('saved_at') else ''
    refine = '켬' if entry.get('auto_refine') else '끔'
    if kind == 'single' and entry.get('result_gone'):
        name = Path(str(entry.get('path'))).name
        return (f"이전에 번역 뒤 재정비하던 결과 TXT({entry['result_gone']})가 없어졌습니다.\n\n원문: {name}\n\n"
                '원문을 열까요? [번역 시작 / 계속]을 누르면 저장된 번역으로 결과를 다시 만들고\n'
                '(번역한 부분은 다시 번역하지 않음) 재정비 자동 시작으로 재정비를 이어 합니다.')
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
        now = info.get('now')
        count = len(now) if now is not None else int(entry.get('files') or 0)
        what = f'끌어다 놓은 파일 {len(sel):,}개' if sel else '폴더'
        lines = [f'이전에 일괄 번역하던 {what}가 끝나지 않은 채 남아 있습니다{when}.', '', str(entry.get('root'))]
        gone = info.get('gone') or []
        cur = entry.get('current')
        line = f'파일 {count:,}개'
        if cur and info.get('current_gone'):
            line += f' · 하던 파일({Path(str(cur)).name})은 없어져 그다음 파일부터'
        elif cur and now is not None:
            pos = next((i for i, x in enumerate(now) if _norm(x) == _norm(cur)), None)
            if pos is not None:
                line += f' 중 {pos + 1:,}번째 파일({Path(str(cur)).name})까지 진행'
        lines.append(line)
        if gone:
            lines.append(f'그때 있던 파일 중 {len(gone):,}개는 지워졌거나 옮겨져 건너뜁니다')
        rel = entry.get('relation_label') or ''
        lines.append(f'{("파일 관계: " + rel + " · ") if rel else ""}끝나면 재정비 자동 시작: {refine}')
        lines += ['', '그때 목록과 설정대로 열까요?',
                  '열린 뒤 [번역 시작 / 계속]을 누르면 끝난 파일은 건너뛰고, 하던 파일(번역 · 재정비)부터 이어집니다.']
        return '\n'.join(lines)
    name = Path(str(entry.get('path'))).name
    return (f'이전에 재정비하던 TXT가 끝나지 않은 채 남아 있습니다{when}.\n\n{name}\n\n'
            '재정비 창을 이 파일로 열까요?\n'
            '[재정비 시작]을 누르면 다듬던 곳부터 이어집니다 (같은 윤문 모델일 때).')
