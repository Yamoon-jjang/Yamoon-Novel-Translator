from __future__ import annotations
import json, os, hashlib
from datetime import datetime
from pathlib import Path

class WorkspaceStore:
    """Small crash-safe UI/workspace store. Secrets never belong here."""
    def __init__(self, path: Path):
        self.path = path
        self.last_error = ''
        self.recovered_corrupt_path: Path | None = None

    def load_all(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            obj=json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(obj,dict):
                raise ValueError('workspace root is not an object')
            return obj
        except Exception as e:
            self.last_error=f'{type(e).__name__}: {e}'
            # Preserve the damaged user data once; subsequent reads remain empty until
            # a valid workspace file is saved, without repeatedly cloning the same file.
            if self.recovered_corrupt_path is None:
                try:
                    stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
                    backup=self.path.with_name(self.path.name+f'.corrupt_{stamp}.bak')
                    backup.write_bytes(self.path.read_bytes())
                    self.recovered_corrupt_path=backup
                except Exception:
                    pass
            return {}

    def save_all(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True,exist_ok=True)
        tmp=self.path.with_suffix(self.path.suffix+'.tmp')
        with tmp.open('w',encoding='utf-8',newline='\n') as f:
            json.dump(data,f,ensure_ascii=False,indent=2)
            f.flush(); os.fsync(f.fileno())
        tmp.replace(self.path)

    @staticmethod
    def key(input_kind: str, path: Path|None, profile_id: str, content_sha256: str = '') -> str:
        # A single book follows its content, not its current filesystem location.
        # Folder jobs remain path-scoped because a folder is an ordered batch definition.
        if input_kind == 'single' and content_sha256:
            identity = f'sha256:{content_sha256}'
        else:
            identity = str(path.resolve()) if path else ''
        raw=f'{input_kind}|{identity}|{profile_id}'
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def get(self, key: str) -> dict:
        return dict(self.load_all().get(key) or {})

    def put(self, key: str, payload: dict) -> None:
        data=self.load_all(); data[key]=dict(payload); self.save_all(data)
