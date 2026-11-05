"""v2.6.2 보완 (same-version redeploy): honest counts and a quieter comparison.

- A paragraph is "changed" only when the result file differs: quotation-mark shape,
  Chinese/Japanese punctuation and the spaces at either end no longer count as an edit,
  and the spaces at the end of a line stay as in the original.
- The live comparison shows an unchanged paragraph on one line, "(변경 없음)".
- The run log warns once when the model leaves most paragraphs as they were.
- The finished window says the counts once (결과 line), keeps its size, shows the resize mark.
- The editing instruction is the v2.6.2 one, unchanged (a stronger one tried on 2026-11-04
  edited more but also dropped dialogue and changed meaning, so it was not kept).
"""
from __future__ import annotations

import re

import pytest

from novel_translator.chunking import split_paragraphs
from novel_translator.refinement_runtime import (run_refinement, refinement_prompt, same_result,
                                                   finalize_body)
from novel_translator.providers.base import TranslationResponse, ProviderError, ErrorKind
from test_refinement_reference_and_safety import Editor, write, REVIEW
from test_alpha17 import qt_app, sandbox  # noqa: F401 (fixtures)

LONG = '그는 그녀의 그 말에 대해 아무런 대답도 하지 않은 채 창밖을 바라보는 것이었다.'


# ---------------------------------------------------------------- what counts as a change
@pytest.mark.parametrize('orig,body,same', [
    ('"히익!" 부인의 입에서 날카로운 비명이 터져 나왔다.', '“히익!” 부인의 입에서 날카로운 비명이 터져 나왔다.', True),
    ('"히익!" 부인의 입에서 비명이 터져 나왔다.', '"히익!" 부인의 입에서 비명이 터져 나왔다. ', True),
    ('그는 웃었다. ', '그는 웃었다.', True),
    ('　그는 웃었다.', '그는 웃었다.', True),
    ('"왜?" 그가 물었다.', '“왜？” 그가 물었다。', True),
    ('"서너 번은은 하게 만들 테니까."', '"서너 번은 하게 만들 테니까."', False),
    ('그는 웃었다.', '그는 빙긋 웃었다.', False),
    ('"어디 가." 그녀가 물었다.', '"어디 가?" 그녀가 물었다.', False),
])
def test_same_result(orig, body, same):
    assert same_result(orig, body) is same


def test_end_of_line_spaces_follow_the_original():
    assert finalize_body('그는 웃었다.  ', '그는 웃었다.', True, 'straight') == '그는 웃었다.  '
    assert finalize_body('그는 웃었다.', '그는 웃었다.   ', True, 'straight') == '그는 웃었다.'
    assert finalize_body('　그는 웃었다.', '  그는 웃었다. ', None, 'straight') == '　그는 웃었다.'


def test_quote_only_edits_are_not_counted_as_changes(tmp_path):
    book = '\n'.join(f'"짧은 대사 {i}."' for i in range(10))
    src = write(tmp_path, 'q.ko.txt', book)
    curl = lambda pid, t: re.sub(r'"([^"]*)"', r'“\1”', t)
    stats, lines = [], []
    out = tmp_path / 'q.r.txt'
    r = run_refinement(src, out, Editor(curl), 'm', '', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path / 'l',
                       punct=True, on_stats=stats.append, on_log=lines.append, log_path=tmp_path / 'q.log.txt')
    assert r['changed'] == 0 and r['identical'] == 10
    assert stats[-1]['changed'] == 0 and stats[-1]['identical'] == 10
    assert any('변경 0 · 동일 10' in l for l in lines)
    assert '실제 변경 0문단 · 원문과 동일 10문단' in (tmp_path / 'q.r.txt.review.txt').read_text(encoding='utf-8-sig')


# ---------------------------------------------------------------- the instruction
# ---------------------------------------------------------------- 집중 윤문
class Lazy(Editor):
    """Copies every paragraph back on the first ask; edits on the 집중 윤문 ask."""
    def __init__(self, second=lambda pid, t: t.replace('바라보는 것이었다', '바라보았다').replace('그녀의 그 말에 대해', '그 말에')):
        super().__init__(lambda pid, t: t)
        self.second = second; self.prompts = []
    def translate(self, req, key):
        if req.purpose == 'edit':
            self.prompts.append(req.user_prompt)
        if req.purpose == 'edit' and '그대로 돌아왔습니다' in req.user_prompt:
            self.calls.append('focus')
            hits = re.findall(r'(?m)^<P(\d{6})>\n([^\n]*)', req.user_prompt.split('교정 대상:')[1])
            return TranslationResponse('\n\n'.join(f'<P{pid}>\n{self.second(int(pid), t)}' for pid, t in hits))
        return super().translate(req, key)


# ---------------------------------------------------------------- low change rate
def test_low_change_rate_is_warned_once(tmp_path):
    lines_in = [f'{i}번째 문단에서 그는 아무 말도 하지 않고 창밖을 바라보았다.' for i in range(200)]
    src = write(tmp_path, 'w.ko.txt', '\n'.join(lines_in))
    ed = Lazy(second=lambda pid, t: t)                       # copies everything, even on 집중 윤문
    lines = []
    run_refinement(src, tmp_path / 'w.r.txt', ed, 'm', '', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path / 'l',
                   on_log=lines.append, log_path=tmp_path / 'w.log.txt')
    warns = [l for l in lines if l.startswith('⚠ 변경률 낮음')]
    assert len(warns) == 1 and '0%만 문장이 바뀜' in warns[0]
    assert '⚠ 변경률 낮음' in (tmp_path / 'w.log.txt').read_text(encoding='utf-8-sig')
    lines2 = []
    run_refinement(src, tmp_path / 'w2.r.txt', Editor(lambda pid, t: t.replace('바라보았다', '내다보았다')), 'm', '',
                   checkpoint_dir=tmp_path / 'cp2', links_root=tmp_path / 'l2', on_log=lines2.append)
    assert not any(l.startswith('⚠') for l in lines2)


# ---------------------------------------------------------------- window
def test_unchanged_paragraph_is_one_line_in_the_comparison(qt_app, sandbox, monkeypatch):
    import novel_translator.ui.refinement_run_dialog as rd
    from test_v262_refinement_output import _host
    root, cfg = sandbox
    monkeypatch.setattr(rd, 'CONFIG_DIR', cfg)
    d = rd.RefinementRunDialog(_host(root))
    d.compare.setChecked(True)
    d._edited([(489, '"히익!" 부인의 입에서 날카로운 비명이 터져 나왔다.', '"히익!" 부인의 입에서 날카로운 비명이 터져 나왔다. '),
               (576, '"적어도 서너 번은은 하게 만들 테니까."라며 웃었다.', '"적어도 서너 번은 하게 만들 테니까."라며 웃었다.')])
    text = d.live.toPlainText()
    assert '[489] (변경 없음) "히익!" 부인의 입에서 날카로운 비명이 터져 나왔다.' in text and '[489] 전:' not in text
    assert '[576] 전: "적어도 서너 번은은' in text and '[576] 후: "적어도 서너 번은 하게' in text
    d._counts = (10, 100, 0)
    d._stats_changed({'groups': 3, 'group': 1, 'requests': 2, 'changed': 7, 'identical': 3})
    assert '실제 변경 7 (변경률 70%)' in d.stats_info.text()


def test_finished_window_says_the_counts_once_and_shows_the_resize_mark(qt_app, sandbox, monkeypatch):
    import novel_translator.ui.refinement_run_dialog as rd
    from test_v262_refinement_output import _host
    root, cfg = sandbox
    monkeypatch.setattr(rd, 'CONFIG_DIR', cfg)
    d = rd.RefinementRunDialog(_host(root)); d.show(); qt_app.processEvents()
    assert d.isSizeGripEnabled() and rd.STATUS_LINES == 3
    size = d.size()
    d._stats_changed({'groups': 10, 'group': 10, 'requests': 12, 'changed': 1, 'identical': 1})
    d._finished({'status': 'completed', 'done': 790, 'total': 800, 'changed': 700, 'identical': 90, 'preserved': 10,
                 'seconds': 3723, 'output': str(root / '책.ko.refined.txt'), 'report': str(root / '책.ko.refined.txt.review.txt'),
                 'guidance_source': 'translation', 'guidance': '표', 'residue_before': 3, 'residue_after': 1})
    qt_app.processEvents()
    assert '실제 변경' not in d.status.text() and '총 소요 시간' in d.status.text() and '보존한 문단은' in d.status.text()
    assert '실제 변경 700 (변경률 88%) · 원문과 동일 90 · 원문 보존 10문단' in d.stats_info.text()
    assert d.size() == size
    d.close()


@pytest.mark.parametrize('orig,body,only', [
    ('"후후후. 얼마나 놀랐을까."', '"후후후, 얼마나 놀랐을까."', True),
    ('그는 웃었다, 그리고 갔다.', '그는 웃었다. 그리고 갔다.', True),
    ('"어디 가." 그녀가 물었다.', '"어디 가?" 그녀가 물었다.', False),       # an added question mark is kept
    ('"후후후. 얼마나 놀랐을까."', '"후후후, 얼마나 놀랐을지."', False),
    ('그는 웃었다.', '그는 웃었다.', False),
])
def test_only_period_comma(orig, body, only):
    from novel_translator.refinement_runtime import only_period_comma
    assert only_period_comma(orig, body) is only


def test_period_comma_only_edit_keeps_the_original(tmp_path):
    book = '"후후후. 얼마나 놀랐을까. 하지만 여기서 서두르면 안 돼."\n"어디 가." 그녀가 물었다.'
    src = write(tmp_path, 'pc.ko.txt', book)
    def edit(pid, t):
        return t.replace('후후후.', '후후후,') if pid == 1 else t.replace('어디 가.', '어디 가?')
    seen = []
    r = run_refinement(src, tmp_path / 'pc.r.txt', Editor(edit), 'm', '', checkpoint_dir=tmp_path / 'cp',
                       links_root=tmp_path / 'l', punct=True, on_edited=seen.extend)
    out = (tmp_path / 'pc.r.txt').read_text(encoding='utf-8-sig').split('\n')
    assert out[0] == '"후후후. 얼마나 놀랐을까. 하지만 여기서 서두르면 안 돼."' and out[1] == '"어디 가?" 그녀가 물었다.'
    assert r['changed'] == 1 and r['identical'] == 1
    assert seen[0][2] == seen[0][1]                                      # the live view shows it unchanged
