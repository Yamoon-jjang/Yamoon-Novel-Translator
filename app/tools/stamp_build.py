"""Generate a build-specific metadata stamp immediately before PyInstaller packaging."""
from __future__ import annotations
import datetime
import json
import os
import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    try:
        commit = subprocess.check_output(['git', 'rev-parse', '--verify', 'HEAD'], cwd=root, stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        commit = ''
    if commit and (len(commit) != 40 or any(ch not in '0123456789abcdefABCDEF' for ch in commit)):
        raise RuntimeError('Invalid Git commit identity')
    stamp = {
        'schema': 1,
        'created_utc': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source_commit': commit,
        'github_run_id': os.environ.get('GITHUB_RUN_ID', ''),
        'github_run_url': (os.environ.get('GITHUB_SERVER_URL', 'https://github.com') + '/' + os.environ.get('GITHUB_REPOSITORY', '') + '/actions/runs/' + os.environ['GITHUB_RUN_ID']) if os.environ.get('GITHUB_RUN_ID') and os.environ.get('GITHUB_REPOSITORY') else '',
        'build_kind': 'github-actions' if os.environ.get('GITHUB_ACTIONS') == 'true' else 'local',
    }
    dest = root / 'novel_translator' / 'build_info.json'
    dest.write_text(json.dumps(stamp, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('BUILD_STAMP=' + stamp['created_utc'] + '/' + (commit[:8] if commit else 'local'))


if __name__ == '__main__':
    main()
