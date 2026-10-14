"""Read the immutable build stamp bundled with this executable; no runtime Git dependency."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

_BUILD_FILE = Path(__file__).with_name('build_info.json')


def build_info() -> dict:
    try:
        info = json.loads(_BUILD_FILE.read_text(encoding='utf-8'))
        if isinstance(info, dict):
            return info
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _local_build_time(info: dict) -> str:
    raw = str(info.get('created_utc') or '')
    try:
        utc = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if utc.tzinfo is None:
            utc = utc.replace(tzinfo=timezone.utc)
        return utc.astimezone().strftime('%Y-%m-%d %H:%M')
    except (ValueError, OverflowError):
        return ''


def build_display() -> str:
    info = build_info()
    local_time = _local_build_time(info)
    if not local_time:
        return '개발본 (빌드 정보 없음)'
    sha = str(info.get('source_commit') or '')
    return local_time + ' (현지 시간)' + (' · ' + sha[:8] if len(sha) >= 8 else '')


def build_detail() -> str:
    info = build_info()
    if not info:
        return '빌드 정보가 없는 개발본입니다.'
    run_id = str(info.get('github_run_id') or '')
    run_url = str(info.get('github_run_url') or '')
    return ('빌드 시각 (현지): ' + (_local_build_time(info) or '?') +
            '\n빌드 시각 (UTC): ' + str(info.get('created_utc') or '?') +
            '\n소스 커밋: ' + str(info.get('source_commit') or '로컬 소스') +
            '\nGitHub Actions 실행 ID: ' + (run_id or '해당 없음') +
            ('\n실행 주소: ' + run_url if run_url else '') +
            '\n빌드 유형: ' + str(info.get('build_kind') or 'local'))
