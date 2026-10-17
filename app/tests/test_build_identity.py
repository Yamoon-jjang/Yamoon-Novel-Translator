"""Build identification and source-mode UI guidance; no Windows packaging needed."""
from __future__ import annotations
import json
from pathlib import Path

from novel_translator import VERSION_LABEL
from novel_translator import build_identity


def test_version_unchanged_and_missing_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(build_identity, '_BUILD_FILE', tmp_path / 'missing.json')
    assert VERSION_LABEL == 'v2.2'
    assert '개발본' in build_identity.build_display()
    assert '정보' in build_identity.build_detail()


def test_build_metadata_and_distinct_builds(monkeypatch, tmp_path):
    stamp = tmp_path / 'build_info.json'
    monkeypatch.setattr(build_identity, '_BUILD_FILE', stamp)
    sha = 'a' * 40
    payload = {'schema': 1, 'created_utc': '2026-10-12T12:34:56Z', 'source_commit': sha, 'build_kind': 'github-actions'}
    stamp.write_text(json.dumps(payload), encoding='utf-8')
    assert ' (현지 시간)' in build_identity.build_display()
    assert 'aaaaaaaa' in build_identity.build_display()
    assert sha in build_identity.build_detail()
    payload['created_utc'] = '2026-10-12T12:35:56Z'
    stamp.write_text(json.dumps(payload), encoding='utf-8')
    assert build_identity._local_build_time(payload) in build_identity.build_display()


def test_corrupt_metadata_does_not_break_about(monkeypatch, tmp_path):
    stamp = tmp_path / 'build_info.json'
    stamp.write_text('{not-json', encoding='utf-8')
    monkeypatch.setattr(build_identity, '_BUILD_FILE', stamp)
    assert '개발본' in build_identity.build_display()


def test_language_help_is_scoped_to_source_picker():
    ui = (Path(__file__).resolve().parents[1] / 'novel_translator' / 'ui' / 'main_window.py').read_text(encoding='utf-8')
    for token in ("'zh-ko':", "'ja-ko':", "'en-ko':", "'ko-refine':", 'Qt.ToolTipRole', 'self.lang.setToolTip'):
        assert token in ui
    # Refinement has not been advertised as an active language profile prematurely.
    assert "self.lang.addItem('한국어 소설 재정비'" not in ui

def test_local_time_conversion_and_workflow_identity(monkeypatch, tmp_path):
    from datetime import datetime, timezone, timedelta
    import novel_translator.build_identity as mod
    path=tmp_path/'build_info.json'
    payload={'created_utc':'2026-10-12T21:42:21Z',
             'source_commit':'a'*40,'github_run_id':'123456789',
             'github_run_url':'https://github.com/example/project/actions/runs/123456789'}
    path.write_text(json.dumps(payload),encoding='utf-8')
    monkeypatch.setattr(mod,'_BUILD_FILE',path)
    from unittest.mock import patch
    with patch.object(mod, 'datetime') as dt:
        class Local(datetime):
            def astimezone(self, tz=None):
                return super().astimezone(timezone(timedelta(hours=9)))
        dt.fromisoformat=Local.fromisoformat
        assert '2026-10-13 06:42' in mod.build_display()
    assert '123456789' in mod.build_detail()
    assert 'https://github.com/example/project/actions/runs/123456789' in mod.build_detail()
