from __future__ import annotations
import os

# Windows SetThreadExecutionState flags.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def set_translation_awake(enabled: bool) -> bool:
    """Keep the Windows system awake while a long translation is running.

    The display is deliberately *not* kept awake. On non-Windows platforms this
    is a harmless no-op so source/self tests remain portable. Windows clears the
    request automatically if the process exits unexpectedly.
    """
    if os.name != 'nt':
        return True
    try:
        import ctypes
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if enabled else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:
        return False


SHUTDOWN_COUNTDOWN_SECONDS = 60


def shutdown_blocked() -> bool:
    """Tests and self-tests set NOVEL_TRANSLATOR_NO_SHUTDOWN so no run can turn a PC off."""
    return bool(os.environ.get('NOVEL_TRANSLATOR_NO_SHUTDOWN'))


def request_system_shutdown() -> bool:
    """Ask Windows to shut the PC down (after the app has saved and is closing).

    `shutdown /s /t 0`: no administrator rights needed. With a timeout above 0 Windows
    implies /f (programs closed without asking), so 0 is used on purpose: another program
    holding unsaved work still asks to save, and Windows waits for that answer instead
    of losing the work. Non-Windows systems and test runs never shut down."""
    if os.name != 'nt' or shutdown_blocked():
        return False
    try:
        import subprocess
        root = os.environ.get('SystemRoot') or os.environ.get('windir') or r'C:\Windows'
        exe = os.path.join(root, 'System32', 'shutdown.exe')
        subprocess.Popen([exe, '/s', '/t', '0'], creationflags=0x08000000,   # CREATE_NO_WINDOW
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
        return True
    except Exception:
        return False
