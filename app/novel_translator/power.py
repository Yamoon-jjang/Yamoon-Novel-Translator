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
