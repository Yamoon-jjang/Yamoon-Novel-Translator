"""v2.6.7: an unfinished translation result (book.ko.partial.txt) is refined too, and the
finished translation (book.ko.txt, same folder) later takes those edits: only the paragraphs
the finished translation changed (the ones left untranslated before) are edited again.

- Only the sibling in the same folder, the same editing model and grouping; never another book.
- A finished run on an unfinished result keeps its resume file (until the finished
  translation takes it, or the keep period ends); a finished run on any other TXT does not.
- The reference table of the finished translation's record is used.
"""
from __future__ import annotations

from pathlib import Path

from novel_translator.refinement_runtime import (run_refinement, unfinished_sibling, is_unfinished_result,
                                                 retry_status, resume_conflict)
from novel_translator.refinement_links import record_translation
from test_refinement_reference_and_safety import Editor, write

DONE = [f'어색한 문장 {i}번입니다. 그는 어색한 표정으로 웃었다.' for i in range(1, 31)]
LEFT = {5: '他静静地看着窗外的雨。', 22: '“你到底想怎么样？”她冷冷地问。'}   # left untranslated
FIXED = {5: '어색한 그는 조용히 창밖의 비를 바라보았다.', 22: '"대체 어쩌자는 거야?" 그녀가 어색한 목소리로 차갑게 물었다.'}


def _text(fixed=False):
    paras = list(DONE)
    for i, t in (FIXED if fixed else LEFT).items():
        paras[i - 1] = t
    return '\n\n'.join(paras)


def _run(src, out, cp, model='m', **kw):
    ed, events = Editor(), []
    res = run_refinement(src, out, ed, model, 'k', checkpoint_dir=cp, links_root=src.parent,
                         max_items=5, event=events.append, **kw)
    return res, ed, events


def test_names():
    assert unfinished_sibling(Path('/b/책.ko.txt')) == Path('/b/책.ko.partial.txt')
    assert unfinished_sibling(Path('/b/책.KO.TXT')).name == '책.ko.partial.txt'
    for other in ('책.txt', '책.ko.partial.txt', '책.ko.refined.txt', '책.ko.txt.bak'):
        assert unfinished_sibling(Path('/b') / other) is None, other
    assert is_unfinished_result('책.ko.partial.txt') and not is_unfinished_result('책.ko.txt')


def test_finished_translation_edits_only_its_new_paragraphs(tmp_path):
    cp = tmp_path / 'cp'
    part = write(tmp_path, '책.ko.partial.txt', _text())
    res, ed, _ = _run(part, tmp_path / '책.ko.partial.refined.txt', cp)
    assert res['status'] == 'completed' and ed.calls.count('edit') == 6
    assert len(list(cp.glob('*.json'))) == 1                         # kept for the finished translation
    # The translation is finished: the .ko.partial.txt is replaced by .ko.txt.
    part.unlink()
    final = write(tmp_path, '책.ko.txt', _text(fixed=True))
    res, ed, events = _run(final, tmp_path / '책.ko.refined.txt', cp)
    assert res['status'] == 'completed', res
    assert any('미완료 번역(책.ko.partial.txt)에서 다듬어 둔 28문단' in e for e in events), events
    assert ed.calls.count('edit') == 2, ed.calls                      # P5 and P22 only
    out = (tmp_path / '책.ko.refined.txt').read_text(encoding='utf-8-sig')
    assert '어색한' not in out and out.count('자연스러운') == 2 * 28 + 2
    assert len(list(cp.glob('*.json'))) == 0                          # nothing left over


def test_a_stopped_unfinished_run_is_taken_too(tmp_path):
    cp = tmp_path / 'cp'
    part = write(tmp_path, '책.ko.partial.txt', _text())
    ed = Editor()
    res = run_refinement(part, tmp_path / 'p.txt', ed, 'm', 'k', checkpoint_dir=cp, links_root=tmp_path,
                         max_items=5, stop=lambda: ed.calls.count('edit') >= 2)
    assert res['status'] == 'stopped' and res['done'] == 10
    final = write(tmp_path, '책.ko.txt', _text(fixed=True))
    res, ed, events = _run(final, tmp_path / '책.ko.refined.txt', cp)
    assert any('에서 다듬어 둔 9문단' in e for e in events), events   # P5 was translated since
    assert res['status'] == 'completed' and res['done'] == 30


def test_never_from_another_folder_or_another_model(tmp_path):
    cp = tmp_path / 'cp'
    (tmp_path / 'a').mkdir(); (tmp_path / 'b').mkdir()
    part = write(tmp_path / 'a', '책.ko.partial.txt', _text())
    _run(part, tmp_path / 'a' / 'p.txt', cp)
    other = write(tmp_path / 'b', '책.ko.txt', _text(fixed=True))   # same name, another folder
    res, ed, events = _run(other, tmp_path / 'b' / 'r.txt', cp)
    assert not any('이어서 사용' in e for e in events) and ed.calls.count('edit') == 6
    final = write(tmp_path / 'a', '책.ko.txt', _text(fixed=True))
    res, ed, events = _run(final, tmp_path / 'a' / 'r.txt', cp, model='other')   # another editing model
    assert not any('이어서 사용' in e for e in events) and ed.calls.count('edit') == 6
    assert len(list(cp.glob('*.json'))) == 1                          # the unfinished run is kept for 'm'


def test_any_other_finished_run_leaves_no_resume_file(tmp_path):
    cp = tmp_path / 'cp'
    for name in ('책.ko.txt', '책.txt', '책.ko.refined.txt'):
        src = write(tmp_path, name, _text(fixed=True))
        res, ed, _ = _run(src, tmp_path / (name + '.out'), cp)
        assert res['status'] == 'completed' and res['preserved'] == 0
    assert len(list(cp.glob('*.json'))) == 0                          # as before v2.6.7


def test_unfinished_result_finished_run_answers_like_before(tmp_path):
    """The kept file is a finished run: no 'resume conflict' question and no retry offer
    when nothing was preserved (the window then keeps the earlier result as before)."""
    cp = tmp_path / 'cp'
    part = write(tmp_path, '책.ko.partial.txt', _text())
    out = tmp_path / '책.ko.partial.refined.txt'
    _run(part, out, cp)
    assert resume_conflict(part, 'local', 'other', checkpoint_dir=cp, max_items=5) == (0, '')
    assert retry_status(part, out, 'local', 'm', checkpoint_dir=cp, max_items=5) == 0


def test_the_finished_translation_record_is_used(tmp_path):
    cp = tmp_path / 'cp'
    part = write(tmp_path, '책.ko.partial.txt', _text())
    speech = [('은지', '민수', '반말', '친구')]
    record_translation(part, _text(), speech, {'恩智': '은지'}, '책.txt', tmp_path)
    _run(part, tmp_path / 'p.txt', cp)
    final = write(tmp_path, '책.ko.txt', _text(fixed=True))
    record_translation(final, _text(fixed=True), speech, {'恩智': '은지', '民秀': '민수'}, '책.txt', tmp_path)
    res, ed, events = _run(final, tmp_path / 'r.txt', cp)
    assert res['guidance_source'] == 'translation' and '민수' in res['guidance']
    assert 'analysis' not in ed.calls
