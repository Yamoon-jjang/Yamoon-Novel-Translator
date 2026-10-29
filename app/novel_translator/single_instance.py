"""One window at a time (v2.6).

Two windows on the same data folder would translate the same unfinished parts of a
book twice, overwrite each other's .ko.txt, settings and shared glossary, and the first
one to finish could shut the PC down under the other. So a second start never opens a
second window: it brings the running window to the front and exits.

The check is keyed by the data folder (%LOCALAPPDATA%\\NovelTranslator), not by the
program's file name or folder, so a renamed NovelTranslator.exe or a copy of the whole
program folder is caught too. Another Windows user account has its own data folder and
is not affected.

Windows: a named mutex. Windows frees it when the program ends in any way (crash
included), so a stale lock can never keep the program from starting. Other systems
(tests): an exclusive lock on a file in the data folder, freed the same way.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ERROR_ALREADY_EXISTS = 183
WINDOW_FILE = 'running_window.json'

_held = None   # mutex handle / open lock file; kept for the life of the process


def instance_key(data_dir) -> str:
    """Same data folder → same key, whatever the exe is called or where it lies."""
    text = os.path.normcase(os.path.abspath(str(data_dir)))
    return 'YamoonNovelTranslator.' + hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]


def _acquire_windows(key: str) -> bool:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    handle = k32.CreateMutexW(None, False, 'Local\\' + key)
    err = ctypes.get_last_error()
    if not handle:
        return True    # cannot tell: never stop the program from starting
    if err == ERROR_ALREADY_EXISTS:
        k32.CloseHandle(handle)
        return False
    global _held
    _held = handle
    return True


def _acquire_posix(data_dir: Path, key: str) -> bool:
    import fcntl
    f = open(Path(data_dir) / f'.{key}.lock', 'a+')
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    global _held
    _held = f
    return True


def acquire(data_dir) -> bool:
    """True when this is the only running window for `data_dir` (and now holds it)."""
    if _held is not None:
        return True
    key = instance_key(data_dir)
    try:
        if os.name == 'nt':
            return _acquire_windows(key)
        return _acquire_posix(Path(data_dir), key)
    except Exception:
        return True    # a check that fails must never block the program


def remember_window(data_dir, hwnd: int) -> None:
    """The running window writes its handle so a second start can bring it forward."""
    try:
        path = Path(data_dir) / WINDOW_FILE
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'pid': os.getpid(), 'hwnd': int(hwnd)}), encoding='utf-8')
        tmp.replace(path)
    except Exception:
        pass


def bring_running_window_forward(data_dir) -> bool:
    """From the second start: restore and raise the running window. False when it could
    not be found (e.g. the first one is still starting)."""
    if os.name != 'nt':
        return False
    try:
        info = json.loads((Path(data_dir) / WINDOW_FILE).read_text(encoding='utf-8'))
        pid, hwnd = int(info['pid']), int(info['hwnd'])
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL('user32', use_last_error=True)
        u32.IsWindow.argtypes = (wintypes.HWND,)
        u32.IsIconic.argtypes = (wintypes.HWND,)
        u32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        u32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        u32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        if not u32.IsWindow(hwnd):
            return False
        owner = wintypes.DWORD(0)
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return False      # an old handle now used by some other program
        u32.ShowWindow(hwnd, 9 if u32.IsIconic(hwnd) else 5)   # SW_RESTORE / SW_SHOW
        u32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False
