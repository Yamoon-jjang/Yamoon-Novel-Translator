"""Clear the "downloaded from the web" mark (the Zone.Identifier stream) from the
program's OWN files.

Windows marks every file extracted from a downloaded zip. With Smart App Control on,
the program then starts when its exe is double-clicked, but a desktop or taskbar
shortcut to it is refused ("애플리케이션 제어 정책에서 이 파일을 차단했습니다").
The user has already chosen to run the program, so on each start it removes the mark
from the files in its own folder - the same as Windows' "차단 해제" / Unblock-File -
and every later start through a shortcut works.

Only files under the program folder are touched (never the user's books, results or
settings), only on Windows, only in the packaged program, and every error is ignored.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

STREAM = ':Zone.Identifier'
MAX_FILES = 5000          # the program folder holds a few hundred files; never walk anything huge


def _has_mark(path: str) -> bool:
    try:
        return os.path.exists(path + STREAM)
    except Exception:
        return False


def clear_marks(app_dir: Path | str, *, last: str | None = None, has_mark=_has_mark, remove=os.remove) -> int:
    """Remove the mark from every marked file under `app_dir`. `last` (the exe) is cleared
    after everything else, because the next start only looks at the exe to decide whether
    anything is left to do. Returns how many files were cleared."""
    root = str(app_dir)
    marked = []; seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            seen += 1
            if seen > MAX_FILES:
                break
            path = os.path.join(dirpath, name)
            if has_mark(path):
                marked.append(path)
        if seen > MAX_FILES:
            break
    if last:
        key = os.path.normcase(os.path.abspath(last))
        marked.sort(key=lambda p: os.path.normcase(os.path.abspath(p)) == key)
    cleared = 0
    for path in marked:
        try:
            remove(path + STREAM); cleared += 1
        except Exception:
            pass
    return cleared


def clear_own_marks_at_start() -> int:
    """First thing at a normal start of the packaged Windows program, before the Qt
    libraries are loaded: clear the marks so this very start loads the program's files
    unmarked and every later start through a shortcut works. No window, no console.
    Fast (a few hundred files); does nothing once the exe is no longer marked."""
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return 0
    exe = os.path.abspath(sys.executable)
    if not _has_mark(exe):
        return 0          # already cleared (or never marked): no folder walk
    return clear_marks(os.path.dirname(exe), last=exe)
