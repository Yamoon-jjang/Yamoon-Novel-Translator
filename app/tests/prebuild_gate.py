from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from novel_translator.runtime_audit import self_test_report  # noqa: E402

SKIP_DIRS={'.git','.pytest_cache','__pycache__','build','dist'}
SECRET_PATTERNS=[
    re.compile(r'AIza[0-9A-Za-z_-]{20,}'),
    re.compile(r'(?<![0-9A-Za-z])AQ\.[0-9A-Za-z_-]{20,}'),
    re.compile(r'sk-[0-9A-Za-z_-]{20,}'),
]

def fail(msg: str):
    print('PREBUILD_GATE=FAIL')
    print(msg)
    raise SystemExit(1)

def iter_files():
    for p in ROOT.rglob('*'):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p

def main():
    ok, report=self_test_report()
    print(report)
    if not ok: fail('runtime self-test failed')

    # Parse all Python source without importing the GUI/runtime dependencies.
    py_count=0
    for p in iter_files():
        if p.suffix.lower()=='.py':
            py_count += 1
            try: ast.parse(p.read_text(encoding='utf-8'), filename=str(p))
            except Exception as e: fail(f'Python parse error: {p.relative_to(ROOT)}: {e}')

    json_count=0
    for p in list((ROOT/'config').glob('*.json')) + list((ROOT/'profiles').glob('*.json')):
        json_count += 1
        try: json.loads(p.read_text(encoding='utf-8'))
        except Exception as e: fail(f'JSON parse error: {p.relative_to(ROOT)}: {e}')

    workflow=ROOT/'.github'/'workflows'/'windows-portable.yml'
    if not workflow.is_file(): fail('Windows CI build workflow missing')
    wf=workflow.read_text(encoding='utf-8',errors='replace')
    for token in ('windows-latest','requirements-build.txt','build_windows.bat','actions/upload-artifact@v4'):
        if token not in wf: fail(f'Windows CI workflow missing: {token}')

    required=['NovelTranslator.spec','build_windows.bat','requirements-build.txt','README.txt','LICENSE.txt','THIRD_PARTY_NOTICES.txt','run.py','tests/postbuild_windows.ps1']
    missing=[x for x in required if not (ROOT/x).is_file()]
    if missing: fail('missing build files: '+', '.join(missing))

    build_reqs=(ROOT/'requirements-build.txt').read_text(encoding='utf-8').lower()
    for required_pkg in ('pyside6','requests','certifi','pyinstaller','pytest'):
        if required_pkg not in build_reqs:
            fail(f'requirements-build.txt missing {required_pkg}')

    bat=(ROOT/'build_windows.bat').read_text(encoding='utf-8',errors='replace')
    if '--self-test' not in bat: fail('build_windows.bat does not run built EXE --self-test')
    if '--gui-self-test' not in bat: fail('build_windows.bat does not run built EXE --gui-self-test')
    if '--engine-self-test' not in bat: fail('build_windows.bat does not run built EXE --engine-self-test')
    if '--secret-self-test' not in bat: fail('build_windows.bat does not run built EXE --secret-self-test')
    if 'qt_qpa_platform=offscreen' in bat.lower(): fail('GUI self-test must exercise the native Windows qwindows platform, not offscreen')
    run_text=(ROOT/'run.py').read_text(encoding='utf-8',errors='replace')
    if '--gui-self-test' not in run_text or 'gui_self_test' not in run_text: fail('run.py does not expose GUI self-test')
    if '--engine-self-test' not in run_text or 'engine_self_test' not in run_text: fail('run.py does not expose engine self-test')
    if '--secret-self-test' not in run_text or 'secret_store_self_test' not in run_text: fail('run.py does not expose secret-store self-test')
    if 'prebuild_gate.py' not in bat: fail('build_windows.bat does not run prebuild gate')
    if 'fuzz_text_audit.py' not in bat: fail('build_windows.bat does not run adversarial TXT fuzz audit')
    if 'release_matrix_audit.py' not in bat or not (ROOT/'tests/release_matrix_audit.py').is_file():
        fail('release scenario matrix gate missing')
    if 'postbuild_windows.ps1' not in bat: fail('build_windows.bat does not run relocated portable gate')
    ps=(ROOT/'tests'/'postbuild_windows.ps1').read_text(encoding='utf-8',errors='replace')
    for token in ('Novel Translator 한글 경로 QA','--self-test','--engine-self-test','--secret-self-test','--gui-self-test'):
        if token not in ps: fail(f'postbuild_windows.ps1 missing: {token}')

    app=json.loads((ROOT/'config/app.json').read_text(encoding='utf-8'))
    if (app.get('refusal_recovery') or {}).get('fallback_provider') is not True:
        fail('fallback_provider must be enabled for the tested Gemini -> OpenAI recovery path')
    pcfg=json.loads((ROOT/'config/providers.json').read_text(encoding='utf-8'))
    orow=next((x for x in pcfg.get('providers',[]) if x.get('id')=='openai'),None)
    if not orow or not str(orow.get('endpoint') or '').startswith('https://api.openai.com/') or not str(orow.get('endpoint') or '').endswith('/responses'):
        fail('official OpenAI Responses fallback provider is missing or malformed')
    if not (ROOT/'novel_translator'/'providers'/'openai.py').is_file():
        fail('OpenAI provider adapter missing')
    if str(orow.get('default_model') or '') != 'gpt-6.1-sol':
        fail('OpenAI fallback default model must be gpt-6.1-sol for the pre-EXE candidate')
    if not (ROOT/'novel_translator'/'ui'/'advanced_dialog.py').is_file():
        fail('advanced settings dialog missing')
    app_src=(ROOT/'novel_translator'/'app.py').read_text(encoding='utf-8')
    if 'AdvancedSettingsDialog' not in app_src:
        fail('GUI self-test does not construct the advanced settings dialog')

    apikey_src=(ROOT/'novel_translator'/'api_keys.py').read_text(encoding='utf-8')
    if 'MAX_KEYS_PER_PROVIDER = 10' not in apikey_src:
        fail('provider key pool maximum must be 10')
    orch_src=(ROOT/'novel_translator'/'orchestrator.py').read_text(encoding='utf-8')
    if 'return ready[0]' not in orch_src or '_rr' in orch_src:
        fail('API key pool must use sticky priority order, not round-robin')

    modes=app.get('modes') or {}
    expected={'quality':'medium','balanced':'low','speed':'minimal'}
    for mode,thinking in expected.items():
        if (modes.get(mode) or {}).get('thinking_level') != thinking:
            fail(f'{mode} thinking_level mismatch')
        if (modes.get(mode) or {}).get('request_concurrency') != 1:
            fail(f'{mode} request_concurrency must be 1')
        if (modes.get(mode) or {}).get('file_concurrency') != 1:
            fail(f'{mode} file_concurrency must be 1')


    # Catch common GUI-only startup failures without importing PySide6: signal connections
    # such as self.button.clicked.connect(self.some_method) must reference a method that is
    # actually defined on that UI class. Pure Python syntax checks cannot catch this.
    for ui_path in sorted((ROOT/'novel_translator'/'ui').glob('*.py')):
        tree=ast.parse(ui_path.read_text(encoding='utf-8'), filename=str(ui_path))
        for cls in (n for n in tree.body if isinstance(n,ast.ClassDef)):
            methods={n.name for n in cls.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
            missing_slots=set()
            for node in ast.walk(cls):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='connect' and node.args:
                    arg=node.args[0]
                    if isinstance(arg,ast.Attribute) and isinstance(arg.value,ast.Name) and arg.value.id=='self' and arg.attr not in methods:
                        missing_slots.add(arg.attr)
            if missing_slots:
                fail(f'{ui_path.name}:{cls.name} signal connects to missing methods: '+', '.join(sorted(missing_slots)))

    main_ui=(ROOT/'novel_translator'/'ui'/'main_window.py').read_text(encoding='utf-8')
    if 'def _term_conflicts' not in main_ui or '확정 용어 충돌' not in main_ui:
        fail('term conflict preflight is missing')
    if '상태 / 오류 기록' not in main_ui:
        fail('persistent status/error record tab missing')

    worker_src=(ROOT/'novel_translator'/'ui'/'worker.py').read_text(encoding='utf-8')
    if 'user_prompt_contract_sha256' not in worker_src:
        fail('resume fingerprint does not cover the user-prompt contract')
    if "'lexical_guidance': self.profile.lexical_guidance" not in worker_src:
        fail('resume fingerprint does not cover the automatic language pack')

    # Source/release package must never contain a plausible live API secret.
    secret_hits=[]
    text_ext={'.py','.json','.txt','.md','.bat','.spec','.toml','.ini','.yaml','.yml'}
    for p in iter_files():
        if p.suffix.lower() not in text_ext: continue
        try: txt=p.read_text(encoding='utf-8',errors='ignore')
        except Exception: continue
        for pat in SECRET_PATTERNS:
            if pat.search(txt): secret_hits.append(str(p.relative_to(ROOT))); break
    if secret_hits: fail('possible API secret found: '+', '.join(secret_hits))

    # Packaging hygiene: no caches should ship in the source handoff.
    dirty=[str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.name=='__pycache__' or p.name=='.pytest_cache' or p.suffix=='.pyc']
    # During development these can exist; print them so the packaging step can remove them.
    print(f'python_files={py_count} json_files={json_count}')
    if dirty: print(f'package_cleanup_required={len(dirty)}')
    print('PREBUILD_GATE=PASS')

if __name__=='__main__':
    main()
