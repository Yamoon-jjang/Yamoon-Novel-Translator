"""v2.6.3: the program version is written in one place (novel_translator/__init__.py).

version_info.txt (the EXE's file version, read by PyInstaller) must say the same, the
engines' User-Agent follows it, and the build workflow reads it instead of its own copy.
"""
from __future__ import annotations

import re
from pathlib import Path

import novel_translator as nt
from novel_translator.providers import base, gemini, openai, local_llm

APP = Path(__file__).resolve().parents[1]
REPO = APP.parent


def _four(v: str) -> tuple[int, ...]:
    parts = [int(x) for x in v.split('.')]
    return tuple(parts + [0] * (4 - len(parts)))


def test_version_label_follows_the_version():
    assert re.fullmatch(r'\d+(\.\d+){1,3}', nt.__version__)
    assert nt.VERSION_LABEL == 'v' + nt.__version__


def test_exe_version_resource_says_the_same():
    info = (APP / 'version_info.txt').read_text(encoding='utf-8')
    four = _four(nt.__version__)
    assert f"filevers=({', '.join(map(str, four))})" in info
    assert f"prodvers=({', '.join(map(str, four))})" in info
    assert f"StringStruct('FileVersion', '{'.'.join(map(str, four))}')" in info
    assert f"StringStruct('ProductVersion', '{nt.__version__}')" in info


def test_every_engine_sends_the_program_version():
    assert base.USER_AGENT == f'NovelTranslator/{nt.__version__}'
    for mod in (gemini, openai, local_llm):
        src = Path(mod.__file__).read_text(encoding='utf-8')
        assert 'USER_AGENT' in src and not re.search(r"NovelTranslator/\d", src)


def test_build_workflow_has_no_version_of_its_own():
    wf = REPO / '.github' / 'workflows' / 'build.yml'
    if not wf.is_file():
        return   # the app folder alone (packaged source)
    text = wf.read_text(encoding='utf-8')
    assert not re.search(r"v?\d+\.\d+\.\d+(\.\d+)?'", text.replace('3.12', ''))   # no '2.6.2' etc.
    assert '__version__' in text and 'APP_VERSION' in text and 'constraints-build.txt' in text
