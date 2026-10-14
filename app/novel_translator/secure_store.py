from __future__ import annotations
import base64, json, os, shutil
from datetime import datetime
from pathlib import Path

class SecretStore:
    """Windows DPAPI-backed local secret storage. No API key is logged or stored as plaintext."""
    def __init__(self, path: Path):
        self.path = path
        self.last_error = ''
        self.recovered_corrupt_path: Path | None = None

    def _protect(self, data: bytes) -> bytes:
        if os.name != 'nt':
            # Development-only fallback; Windows release uses DPAPI.
            return base64.b64encode(data)
        import ctypes
        from ctypes import wintypes
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
        crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        crypt32.CryptProtectData.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        buf = ctypes.create_string_buffer(data)
        in_blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
        out_blob = DATA_BLOB()
        if not crypt32.CryptProtectData(ctypes.byref(in_blob), 'NovelTranslator', None, None, None, 0x1, ctypes.byref(out_blob)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)

    def _unprotect(self, data: bytes) -> bytes:
        if os.name != 'nt':
            return base64.b64decode(data)
        import ctypes
        from ctypes import wintypes
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
        crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        buf = ctypes.create_string_buffer(data)
        in_blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
        out_blob = DATA_BLOB()
        if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0x1, ctypes.byref(out_blob)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)

    def save(self, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + '.tmp')
        with tmp.open('wb') as f:
            f.write(self._protect(raw)); f.flush(); os.fsync(f.fileno())
        tmp.replace(self.path)
        self.last_error = ''

    def load(self) -> dict:
        self.last_error = ''; self.recovered_corrupt_path = None
        if not self.path.exists():
            return {}
        try:
            obj = json.loads(self._unprotect(self.path.read_bytes()).decode('utf-8'))
            if not isinstance(obj, dict):
                raise ValueError('secret root is not an object')
            return obj
        except Exception as e:
            self.last_error = f'{type(e).__name__}: {e}'
            # Never overwrite/delete an unreadable key store. Preserve a timestamped copy for recovery/diagnosis.
            try:
                stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
                backup=self.path.with_name(self.path.name+f'.unreadable_{stamp}.bak')
                shutil.copy2(self.path, backup)
                self.recovered_corrupt_path=backup
            except Exception:
                pass
            return {}
