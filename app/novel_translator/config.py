from __future__ import annotations
import json, os, sys
from pathlib import Path
from dataclasses import dataclass

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', PACKAGE_ROOT))
if os.environ.get('NOVEL_TRANSLATOR_HOME'):
    APP_DIR = Path(os.environ['NOVEL_TRANSLATOR_HOME']).expanduser().resolve()
elif getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = PACKAGE_ROOT

# Editable side-by-side files are preferred. In a one-file build, bundled resources under
# _MEIPASS are used when side-by-side overrides are absent.
CONFIG_DIR = APP_DIR / 'config' if (APP_DIR / 'config').is_dir() else RESOURCE_ROOT / 'config'
PROFILE_DIR = APP_DIR / 'profiles' if (APP_DIR / 'profiles').is_dir() else RESOURCE_ROOT / 'profiles'


def manual_path() -> Path | None:
    """The user manual: next to the program first, then the bundled copy."""
    from . import MANUAL_FILENAME
    for candidate in (APP_DIR / MANUAL_FILENAME, RESOURCE_ROOT / 'docs' / MANUAL_FILENAME, PACKAGE_ROOT / 'docs' / MANUAL_FILENAME):
        if candidate.is_file():
            return candidate
    return None


def _usable_dir(path: Path) -> Path | None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / '.nt_write_probe'
        probe.write_bytes(b'ok')
        probe.unlink(missing_ok=True)
        return path
    except Exception:
        return None


def _select_data_dir() -> Path:
    candidates: list[Path] = []
    if os.environ.get('NOVEL_TRANSLATOR_DATA'):
        candidates.append(Path(os.environ['NOVEL_TRANSLATOR_DATA']).expanduser())
    if os.name == 'nt':
        local = os.environ.get('LOCALAPPDATA')
        if local:
            candidates.append(Path(local) / 'NovelTranslator')
        candidates.append(Path.home() / 'AppData' / 'Local' / 'NovelTranslator')
    else:
        xdg = os.environ.get('XDG_DATA_HOME')
        if xdg:
            candidates.append(Path(xdg) / 'NovelTranslator')
        candidates.append(Path.home() / '.local' / 'share' / 'NovelTranslator')
    for candidate in candidates:
        usable = _usable_dir(candidate)
        if usable is not None:
            return usable.resolve()
    raise RuntimeError('Novel Translator 데이터 폴더를 만들 수 없습니다.')

DATA_DIR = _select_data_dir()
LOG_DIR = DATA_DIR / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> dict:
    with path.open('r', encoding='utf-8') as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f'JSON 루트가 객체가 아닙니다: {path}')
    return value


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush(); os.fsync(f.fileno())
    tmp.replace(path)

@dataclass(frozen=True)
class RuntimePaths:
    app_dir: Path = APP_DIR
    resource_root: Path = RESOURCE_ROOT
    config_dir: Path = CONFIG_DIR
    profile_dir: Path = PROFILE_DIR
    data_dir: Path = DATA_DIR
    log_dir: Path = LOG_DIR
