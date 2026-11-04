"""v2.6.2 Korean refinement: result file, context, size and the window.

- The refined TXT follows the translation result's 문장부호 and 따옴표 settings (the model's
  curly quotes no longer come through), and keeps every paragraph's own leading indentation.
- Resume files keep the model's text as it was written; only the result file is finalized,
  so resuming, carrying edits over and retrying preserved paragraphs work as before.
- Each group is edited after the already edited text before it (not the unedited one).
- Books under 30 MB; the resume file is written compactly.
- The window announces a long run (requests, rough time) before it starts and shows the edited
  paragraphs live, optionally beside the text before editing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from novel_translator.chunking import split_paragraphs
from novel_translator.config import load_json
from novel_translator.refinement_runtime import (run_refinement, finalize_body, assemble, estimate_run,
                                                   content_checkpoint_path, MAX_REFINEMENT_BYTES,
                                                   MAX_REFINEMENT_LABEL, group_paragraphs)
from novel_translator.providers.base import TranslationResponse
from test_refinement_reference_and_safety import Editor, write
from test_alpha17 import qt_app, sandbox  # noqa: F401 (fixtures)


def curly(pid, text):
    """A model that edits and writes its own typography: curly quotes, no indentation."""
    t = text.lstrip(' \t　').replace('어색한', '자연스러운')
    t = re.sub(r'"([^"]*)"', r'“\1”', t)
    return t.replace("'", '’')


BOOK = ('　어색한 첫 문단이다.\n'
        '　"어색한 대사야." 그가 말했다.\n'
        '  \'어색한 생각\'을 했다.\n'
        '들여쓰기 없는 어색한 문단이다.\n')


# ---------------------------------------------------------------- finalize_body
@pytest.mark.parametrize('orig,body,punct,style,want', [
    ('　그는 왔다.', '그는 왔다.', None, 'straight', '　그는 왔다.'),           # indent back
    ('  그는 왔다.', '　　그는 왔다.', None, 'straight', '  그는 왔다.'),       # original's indent wins
    ('그는 왔다.', '   그는 왔다.', None, 'straight', '그는 왔다.'),                  # none added
    ('　"가자."', '“가자.”', True, 'straight', '　"가자."'),
    ('"가자."', '“가자.”', True, 'curly', '“가자.”'),
    ('"가자."', '“가자.”', True, 'keep', '“가자.”'),
    ('"가자."', '“가자.”', False, 'straight', '“가자.”'),                           # 문장부호 off: as written
    ('그래.', '그래。。。', True, 'straight', '그래...'),
    ('"가자."', '「가자。」', True, 'straight', '"가자."'),
])
def test_finalize_body(orig, body, punct, style, want):
    assert finalize_body(orig, body, bool(punct), style) == want


def test_assemble_without_settings_is_unchanged():
    ps = split_paragraphs(BOOK)
    done = {str(p.pid): curly(p.pid, p.text) for p in ps}
    raw = ''.join(p.prefix_before + done[str(p.pid)] + p.separator_after for p in ps)
    assert assemble(ps, done) == raw                      # old call: exactly as before


# ---------------------------------------------------------------- full runs
@pytest.mark.parametrize('style,expect_open', [('straight', '"'), ('curly', '“'), ('keep', '“')])
def test_result_follows_quote_setting_and_keeps_indentation(tmp_path, style, expect_open):
    src = write(tmp_path, 'book.ko.txt', BOOK)
    out = tmp_path / 'book.ko.refined.txt'
    r = run_refinement(src, out, Editor(curly), 'm', '', checkpoint_dir=tmp_path / 'cp',
                       links_root=tmp_path / 'links', punct=True, quote_style=style)
    text = out.read_text(encoding='utf-8-sig')
    lines = text.split('\n')
    assert lines[0] == '　자연스러운 첫 문단이다.'
    assert lines[1].startswith('　' + expect_open + '자연스러운 대사야.')
    assert lines[2].startswith('  ')                                    # spaces indent kept as spaces
    assert lines[3] == '들여쓰기 없는 자연스러운 문단이다.'
    if style == 'straight':
        assert not re.search('[“”‘’]', text)
    assert r['changed'] == 4 and r['preserved'] == 0
    assert src.read_text(encoding='utf-8') == BOOK                      # original untouched


def test_settings_off_writes_the_model_text_as_before(tmp_path):
    src = write(tmp_path, 'book.ko.txt', BOOK)
    out = tmp_path / 'r.txt'
    run_refinement(src, out, Editor(curly), 'm', '', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path / 'l')
    text = out.read_text(encoding='utf-8-sig')
    assert '“자연스러운 대사야.”' in text and text.startswith('자연스러운')   # punct=None: raw (pre-v2.6.2)


def test_preserved_paragraph_follows_the_setting_and_counts_are_honest(tmp_path):
    book = '“어색한 대사야.” 그가 말했다.\n어색한 서술이다.\n'
    src = write(tmp_path, 'b.txt', book)
    # The model refuses paragraph 1 (kept as the original), edits paragraph 2.
    ed = Editor(lambda pid, t: '죄송하지만 이 내용은 교정해 드릴 수 없습니다.' if pid == 1 else t.replace('어색한', '자연스러운'))
    out = tmp_path / 'r.txt'
    r = run_refinement(src, out, ed, 'm', '', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path / 'l',
                       punct=True, quote_style='straight')
    lines = out.read_text(encoding='utf-8-sig').split('\n')
    assert lines[0] == '"어색한 대사야." 그가 말했다.'        # the original text, the chosen quote style
    assert r['preserved'] == 1 and r['changed'] == 1


def test_punctuation_alone_is_not_counted_as_a_change(tmp_path):
    src = write(tmp_path, 'b.txt', '“그래.” 그가 말했다.\n')
    out = tmp_path / 'r.txt'
    r = run_refinement(src, out, Editor(lambda pid, t: t), 'm', '', checkpoint_dir=tmp_path / 'cp',
                       links_root=tmp_path / 'l', punct=True, quote_style='straight')
    assert out.read_text(encoding='utf-8-sig').startswith('"그래."')
    assert r['changed'] == 0 and r['identical'] == 1


def test_resume_file_keeps_the_raw_edit_and_resume_finishes_the_same(tmp_path):
    lines = [f'　"어색한 대사 {i}번이야." 그가 말했다.' for i in range(1, 121)]
    book = '\n'.join(lines) + '\n'
    src = write(tmp_path, 'long.ko.txt', book)
    cp_dir = tmp_path / 'cp'
    calls = {'n': 0}
    stop_after = 2
    class Stopper(Editor):
        def translate(self, req, key):
            r = super().translate(req, key)
            if req.purpose != 'analysis' and 'SAME' not in r.text:
                calls['n'] += 1
            return r
    ed = Stopper(curly)
    r1 = run_refinement(src, tmp_path / 'out.txt', ed, 'm', '', checkpoint_dir=cp_dir, links_root=tmp_path / 'l',
                        punct=True, quote_style='straight', max_items=20,
                        stop=lambda: calls['n'] >= stop_after)
    assert r1['status'] == 'stopped'
    state = load_json(next(cp_dir.glob('text-*.json')))
    kept = list(state['completed'].values())
    assert kept and all(v.startswith('“') for v in kept)                # raw model text in the resume file
    raw_cp = next(cp_dir.glob('text-*.json')).read_text(encoding='utf-8')
    assert '\n  ' not in raw_cp                                         # compact, not indented
    r2 = run_refinement(src, tmp_path / 'out.txt', Stopper(curly), 'm', '', checkpoint_dir=cp_dir,
                        links_root=tmp_path / 'l', punct=True, quote_style='straight', max_items=20)
    assert r2['status'] == 'completed' and r2['done'] == 120
    out = (tmp_path / 'out.txt').read_text(encoding='utf-8-sig').split('\n')
    assert out[0] == '　"자연스러운 대사 1번이야." 그가 말했다.'
    assert out[119] == '　"자연스러운 대사 120번이야." 그가 말했다.'


def test_retry_of_preserved_paragraphs_updates_its_own_finalized_result(tmp_path):
    from novel_translator.refinement_runtime import retry_status
    book = '“어색한 대사야.” 그가 말했다.\n어색한 서술이다.\n'
    src = write(tmp_path, 'b.txt', book)
    out = tmp_path / 'r.txt'
    first = Editor(lambda pid, t: '죄송하지만 이 내용은 교정해 드릴 수 없습니다.' if pid == 1 else t.replace('어색한', '자연스러운'))
    run_refinement(src, out, first, 'local-model', '', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path / 'l',
                   punct=True, quote_style='straight')
    assert retry_status(src, out, 'local', 'local-model', checkpoint_dir=tmp_path / 'cp') == 1
    second = Editor(curly)
    r = run_refinement(src, out, second, 'local-model', '', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path / 'l',
                       punct=True, quote_style='straight', update_existing=True)
    assert r['preserved'] == 0
    assert out.read_text(encoding='utf-8-sig').split('\n')[0] == '"자연스러운 대사야." 그가 말했다.'


def test_previous_context_is_the_edited_text(tmp_path):
    lines = [f'어색한 문단 {i}번이다.' for i in range(1, 31)]
    src = write(tmp_path, 'c.txt', '\n'.join(lines) + '\n')
    prompts = []
    class Spy(Editor):
        def translate(self, req, key):
            if req.purpose != 'analysis':
                prompts.append(req.user_prompt)
            return super().translate(req, key)
    run_refinement(src, tmp_path / 'r.txt', Spy(), 'm', '', checkpoint_dir=tmp_path / 'cp',
                   links_root=tmp_path / 'l', max_items=10)
    later = [p for p in prompts if '앞 문맥' in p]
    assert len(later) == 2
    ctx = later[0].split('앞 문맥(교정 대상 아님): ', 1)[1].split('\n교정 대상:', 1)[0]
    assert '자연스러운 문단 10번이다.' in ctx and '어색한 문단 10번이다.' not in ctx
    target = later[0].split('교정 대상:', 1)[1]
    assert '어색한 문단 11번이다.' in target                               # the group itself is the unedited text


def test_live_rows_are_the_kept_paragraphs_in_result_form(tmp_path):
    src = write(tmp_path, 'b.ko.txt', BOOK)
    seen = []
    run_refinement(src, tmp_path / 'r.txt', Editor(curly), 'm', '', checkpoint_dir=tmp_path / 'cp',
                   links_root=tmp_path / 'l', punct=True, quote_style='straight', on_edited=seen.extend)
    assert [pid for pid, _, _ in seen] == [1, 2, 3, 4]
    assert seen[1][1] == '　"어색한 대사야." 그가 말했다.'             # before
    assert seen[1][2] == '　"자연스러운 대사야." 그가 말했다.'          # after, as the file holds it
    bad = []
    def boom(rows): bad.append(rows); raise RuntimeError('view gone')
    r = run_refinement(src, tmp_path / 'r2.txt', Editor(curly), 'm', '', checkpoint_dir=tmp_path / 'cp2',
                       links_root=tmp_path / 'l2', on_edited=boom)
    assert r['status'] == 'completed' and bad                          # a broken view never stops a run


def test_refining_the_refined_result_keeps_the_translation_table(tmp_path):
    from novel_translator.refinement_links import record_translation, find_reference
    book = '“어색한 대사야.” 은지가 말했다.\n민수가 어색한 웃음을 지었다.\n'
    src = write(tmp_path, 'b.ko.txt', book)
    record_translation(src, book, [{'speaker': '은지', 'listener': '민수', 'level': '반말'}], {'恩智': '은지'},
                       'b.txt', tmp_path / 'l')
    out = tmp_path / 'b.ko.refined.txt'
    r = run_refinement(src, out, Editor(curly), 'm', '', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path / 'l',
                       punct=True, quote_style='straight')
    assert r['guidance_source'] == 'translation'
    text = out.read_text(encoding='utf-8-sig')
    assert find_reference(out, text, tmp_path / 'l')                    # the finalized file is what is linked


# ---------------------------------------------------------------- size and estimate
def test_limit_is_30mb():
    assert MAX_REFINEMENT_BYTES == 30_000_000 and MAX_REFINEMENT_LABEL == '30MB'


def test_estimate_counts_requests_and_only_a_resumable_run(tmp_path):
    lines = [f'그는 {i}번째 문장을 천천히 읽었다.' for i in range(1, 1001)]
    src = write(tmp_path, 'e.txt', '\n'.join(lines))
    est = estimate_run(src, 'local', 'gemma', checkpoint_dir=tmp_path / 'cp')
    groups = group_paragraphs(split_paragraphs('\n'.join(lines)), 3500, 36)
    assert est['paragraphs'] == 1000 and est['requests'] == len(groups) and est['done'] == 0
    stop = {'n': 0}
    class Count(Editor):
        def translate(self, req, key):
            stop['n'] += 1
            return super().translate(req, key)
    run_refinement(src, tmp_path / 'o.txt', Count(), 'gemma', '', checkpoint_dir=tmp_path / 'cp',
                   links_root=tmp_path / 'l', stop=lambda: stop['n'] >= 6)
    est2 = estimate_run(src, 'local', 'gemma', checkpoint_dir=tmp_path / 'cp')
    assert est2['done'] > 0 and est2['requests_left'] < est['requests']
    other = estimate_run(src, 'local', 'qwen', checkpoint_dir=tmp_path / 'cp')
    assert other['done'] == 0 and other['requests_left'] == est['requests']   # another model starts over


# ---------------------------------------------------------------- window
def _host(root, last=None):
    from PySide6.QtWidgets import QWidget
    host = QWidget(); host.secrets = {}; host.source_path = None; host.last_result_path = last
    host._selected_model_id = lambda: ''
    host.custom_output_dir = None
    return host


@pytest.fixture
def cfg_dir(sandbox, monkeypatch):
    import novel_translator.ui.refinement_run_dialog as rd
    root, cfg = sandbox
    monkeypatch.setattr(rd, 'CONFIG_DIR', cfg)
    return root, cfg


def test_live_view_and_compare(qt_app, cfg_dir):
    import novel_translator.ui.refinement_run_dialog as rd
    root, cfg = cfg_dir
    d = rd.RefinementRunDialog(_host(root))
    assert d.live.isReadOnly() and not d.compare.isChecked()
    d._edited([(1, '　어색한 문장.', '　자연스러운 문장.')])
    assert d.live.toPlainText() == '　자연스러운 문장.'
    d.compare.setChecked(True)
    assert '[1] 전: 어색한 문장.' in d.live.toPlainText() and '[1] 후: 자연스러운 문장.' in d.live.toPlainText()
    assert json.loads((cfg / 'app.json').read_text(encoding='utf-8'))['refine_live_compare'] is True
    d2 = rd.RefinementRunDialog(_host(root))
    assert d2.compare.isChecked()                                      # remembered
    for i in range(rd.LIVE_ROWS + 50):
        d2._edited([(i, f'전 {i}', f'후 {i}')])
    text = d2.live.toPlainText()
    assert '후 49]' not in text and f'후: 후 {rd.LIVE_ROWS + 49}' in text and '전: 전 49\n' not in text
    assert len(d2._live_rows) == rd.LIVE_ROWS


def _box(rd, answers, asked):
    class Box:
        Question = rd.QMessageBox.Question; Yes = rd.QMessageBox.Yes; No = rd.QMessageBox.No
        def __init__(self, *a): asked.append(a[1] if len(a) > 1 else '')
        def setDefaultButton(self, b): pass
        def exec(self): return answers.pop(0)
    return Box


@pytest.mark.parametrize('n,expect_notice', [(100, False), (20000, True)])
def test_long_run_notice(qt_app, cfg_dir, monkeypatch, n, expect_notice):
    import novel_translator.ui.refinement_run_dialog as rd
    root, _ = cfg_dir
    src = root / 'book.ko.txt'
    src.write_text('\n'.join(f'그는 {i}번째 문장을 천천히 소리 내어 읽고 또 읽었다.' for i in range(n)), encoding='utf-8')
    d = rd.RefinementRunDialog(_host(root))
    d._set_source(src)
    asked = []
    monkeypatch.setattr(rd, 'QMessageBox', _box(rd, [rd.QMessageBox.No], asked))
    assert d._confirm_long_run('gemma') is (not expect_notice)
    assert asked == (['오래 걸리는 재정비'] if expect_notice else [])


def test_long_run_notice_no_cancels_and_retry_skips_it(qt_app, cfg_dir, monkeypatch):
    import novel_translator.ui.refinement_run_dialog as rd
    root, _ = cfg_dir
    src = root / 'book.ko.txt'
    src.write_text('\n'.join(f'그는 {i}번째 문장을 천천히 소리 내어 읽고 또 읽었다.' for i in range(20000)), encoding='utf-8')
    d = rd.RefinementRunDialog(_host(root))
    d._set_source(src)
    d._fill_engines = lambda: None
    d._routes_ready = True
    d._editing_route = ('local', 'gemma', '', {}); d._analysis_route = ('local', 'gemma', '', {})
    started = []
    monkeypatch.setattr(rd, 'RefinementWorker', lambda *a, **k: started.append(k) or (_ for _ in ()).throw(SystemExit))
    asked = []
    monkeypatch.setattr(rd, 'QMessageBox', _box(rd, [rd.QMessageBox.No], asked))
    monkeypatch.setattr(rd, 'resume_conflict', lambda *a, **k: (0, ''))
    d._start()
    assert asked == ['오래 걸리는 재정비'] and not started              # [아니요]: nothing starts
    # A retry of preserved paragraphs (result exists, retry available) never shows the notice.
    d.output.write_text('결과', encoding='utf-8')
    monkeypatch.setattr(rd, 'retry_status', lambda *a, **k: 3)
    asked.clear()
    monkeypatch.setattr(rd.QMessageBox, 'question', staticmethod(lambda *a, **k: rd.QMessageBox.Yes), raising=False)
    with pytest.raises(SystemExit):
        d._start()
    assert '오래 걸리는 재정비' not in asked and started and started[-1].get('update_existing') is True


# ---------------------------------------------------------------- refinement's own marks survive
@pytest.mark.parametrize('edited,want', [
    ('“어디 가?”', '"어디 가?"'),                       # a question mark the edit added
    ('“뭐라고?!”', '"뭐라고?!"'),
    ('“안 돼……!”', '"안 돼……!"'),
    ('그는 말이 없었다…….', '그는 말이 없었다…….'),
    ('“응…” 그녀가 고개를 끄덕였다.', '"응…" 그녀가 고개를 끄덕였다.'),
    ('“오빠~” 하고 불렀다.', '"오빠~" 하고 불렀다.'),
    ('그건—그러니까—말이야.', '그건—그러니까—말이야.'),
    ('3.5킬로그램, 10:30에 도착했다.', '3.5킬로그램, 10:30에 도착했다.'),
    ('“OK, it’s fine.” 그녀가 말했다.', '"OK, it\'s fine." 그녀가 말했다.'),
    ('“왜？” 그는 되물었다。', '"왜?" 그는 되물었다.'),
])
def test_marks_the_edit_added_are_kept(edited, want):
    assert finalize_body('원문.', edited, True, 'straight') == want


# ---------------------------------------------------------------- run log and counts
def _run(tmp_path, src, editor, **kw):
    out = kw.pop('output', tmp_path / (src.stem + '.refined.txt'))
    lines, stats = [], []
    r = run_refinement(src, out, editor, 'gemma', '', checkpoint_dir=tmp_path / 'cp', links_root=tmp_path / 'l',
                       log_path=out.with_name(out.name + '.log.txt'), on_log=lines.append, on_stats=stats.append, **kw)
    return r, out, lines, stats


def test_run_log_file_and_counts(tmp_path):
    src = write(tmp_path, 'b.ko.txt', '\n'.join(f'어색한 문장 {i}번이다.' for i in range(1, 41)))
    r, out, lines, stats = _run(tmp_path, src, Editor(), max_items=10)
    log = out.with_name(out.name + '.log.txt').read_text(encoding='utf-8-sig')
    assert r['status'] == 'completed'
    assert '===== 재정비 시작 · b.ko.txt' in log and '윤문 요청 4묶음' in log and '===== 정비 완료' in log
    assert log.count('✓ 요청 ') == 4 and '✓ 요청 4/4 · 31~40문단 · 변경 10 · 동일 0' in log
    assert '윤문 요청 중' not in log                                     # the per-request status is not logged
    assert all(re.match(r'^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ', l) for l in log.splitlines())
    assert '\r\n' in out.with_name(out.name + '.log.txt').read_bytes().decode('utf-8-sig')
    assert lines[0].startswith('===== 재정비 시작') and lines[-1].startswith('===== 정비 완료')
    last = stats[-1]
    assert last['groups'] == 4 and last['group'] == 4 and last['requests'] == 4 and last['retries'] == 0
    assert last['changed'] == 40 and last['identical'] == 0 and last['replies'] == 4


def test_retries_and_reviews_are_counted(tmp_path):
    from test_refinement_runtime import Dummy
    src = write(tmp_path, 'a.txt', '어색한 첫 문단입니다.\n어색한 둘째 문단입니다.')
    out = tmp_path / 'a.ko.refined.txt'; stats = []
    class Wrap(Dummy):
        name = 'local'
    run_refinement(src, out, Wrap(bad_group=True), 'm', 'test', checkpoint_dir=tmp_path / 'cp',
                   links_root=tmp_path / 'l', on_stats=stats.append)
    assert stats[-1]['requests'] == 3 and stats[-1]['retries'] == 2     # one group, asked again in halves
    src2 = write(tmp_path, 'c.txt', '"어색한 대사입니다요." 그가 말했다.\n')
    stats2 = []
    run_refinement(src2, tmp_path / 'c.r.txt', Editor(lambda pid, t: '"그래." 그가 말했다.'), 'm', '',
                   checkpoint_dir=tmp_path / 'cp2', links_root=tmp_path / 'l2', on_stats=stats2.append)
    assert stats2[-1]['reviews'] == 1                                   # 존댓말 → 반말: a continuity review


def test_log_continues_on_resume_and_a_new_run_keeps_the_previous_log(tmp_path):
    src = write(tmp_path, 'r.ko.txt', '\n'.join(f'어색한 문장 {i}번이다.' for i in range(1, 41)))
    out = tmp_path / 'r.ko.refined.txt'; logf = out.with_name(out.name + '.log.txt')
    n = {'c': 0}
    class Count(Editor):
        def translate(self, req, key):
            n['c'] += 1
            return super().translate(req, key)
    r1, _, _, _ = _run(tmp_path, src, Count(), max_items=10, output=out, stop=lambda: n['c'] >= 2)
    assert r1['status'] == 'stopped'
    text = logf.read_text(encoding='utf-8-sig')
    assert '===== 안전 중지 · 다듬은 ' in text and '다시 시작하면 이어서 진행' in text
    r2, _, _, stats = _run(tmp_path, src, Editor(), max_items=10, output=out)
    text = logf.read_text(encoding='utf-8-sig')
    assert r2['status'] == 'completed' and text.count('===== 재정비 시작') == 1 and '===== 이어서 시작' in text
    assert stats[0]['changed'] > 0                                       # counts include the earlier run
    # A fresh run on another copy of the text with the same output name: the old log is kept aside.
    out.unlink(); (tmp_path / 'cp').rename(tmp_path / 'cp_old')
    src2 = write(tmp_path, 'r2.ko.txt', '새로운 어색한 문장이다.')
    _run(tmp_path, src2, Editor(), output=out)
    assert '===== 재정비 시작 · r2.ko.txt' in logf.read_text(encoding='utf-8-sig')
    prev = logf.with_name(logf.name[:-len('.txt')] + '.이전.txt')
    assert prev.exists() and '===== 이어서 시작' in prev.read_text(encoding='utf-8-sig')


def test_no_log_file_without_a_path_and_a_broken_callback_never_stops(tmp_path):
    src = write(tmp_path, 'n.ko.txt', '어색한 문장이다.')
    def boom(_): raise RuntimeError('view gone')
    r = run_refinement(src, tmp_path / 'n.r.txt', Editor(), 'm', '', checkpoint_dir=tmp_path / 'cp',
                       links_root=tmp_path / 'l', on_log=boom, on_stats=boom)
    assert r['status'] == 'completed' and not list(tmp_path.glob('*.log.txt'))


def test_unwritable_log_never_stops_a_run(tmp_path):
    from novel_translator.refinement_runtime import append_run_log
    (tmp_path / 'dir.log.txt').mkdir()
    append_run_log(tmp_path / 'dir.log.txt', '줄')                         # a folder in its place: ignored
    src = write(tmp_path, 'u.ko.txt', '어색한 문장이다.')
    r = run_refinement(src, tmp_path / 'u.r.txt', Editor(), 'm', '', checkpoint_dir=tmp_path / 'cp',
                       links_root=tmp_path / 'l', log_path=tmp_path / 'dir.log.txt')
    assert r['status'] == 'completed'


# ---------------------------------------------------------------- window: counts, log, size
def test_finish_time_text():
    import datetime as dt
    from novel_translator.ui.refinement_run_dialog import finish_time_text
    base = dt.datetime(2026, 11, 4, 14, 0).timestamp()          # Wednesday
    assert finish_time_text(3600, base) == '오늘 오후 3:00'
    assert finish_time_text(14 * 3600, base) == '내일 오전 4:00'
    assert finish_time_text(44 * 3600 + 40 * 60, base) == '11월 6일(금) 오전 10:40'
    assert finish_time_text(10 * 3600, base) == '내일 오전 12:00'


def test_counts_and_log_in_the_window(qt_app, cfg_dir):
    import novel_translator.ui.refinement_run_dialog as rd
    root, _ = cfg_dir
    d = rd.RefinementRunDialog(_host(root))
    assert [d.tabs.tabText(i) for i in range(d.tabs.count())] == ['실시간 결과', '작업 기록']
    assert d.log_view.maximumBlockCount() == rd.LOG_ROWS == 500 and rd.LIVE_ROWS == 500
    d._counts = (120, 400, 3)
    d._stats_changed({'groups': 12, 'group': 4, 'requests': 5, 'retries': 1, 'reviews': 2,
                      'reply_last': 41.2, 'reply_total': 170.0, 'replies': 5, 'changed': 100, 'identical': 17})
    text = d.stats_info.text()
    assert '요청  4/12묶음 · 보낸 요청 5회 · 재요청 1회 · 연속성 확인 2회' in text
    assert '결과  실제 변경 100 · 원문과 동일 17 · 원문 보존 3문단' in text
    assert '최근 응답 41초 · 평균 34초' in text
    for i in range(rd.LOG_ROWS + 20):
        d._log_line(f'줄 {i}')
    assert d.log_view.blockCount() == rd.LOG_ROWS and '줄 19\n' not in d.log_view.toPlainText()
    assert re.match(r'^\[\d\d:\d\d:\d\d\] • 줄 20$', d.log_view.toPlainText().splitlines()[0])


def test_failure_goes_to_the_log(qt_app, cfg_dir):
    import novel_translator.ui.refinement_run_dialog as rd
    root, _ = cfg_dir
    src = root / 'f.ko.txt'; src.write_text('그는 웃었다.', encoding='utf-8')
    d = rd.RefinementRunDialog(_host(root)); d._set_source(src); d._run_started = 0
    d._failed('ProviderError: 서버 연결 실패')
    assert '재정비 오류 · ProviderError' in d.log_view.toPlainText()
    assert '재정비 오류 · ProviderError' in rd.run_log_path(d.output).read_text(encoding='utf-8-sig')


def test_window_size_and_place_are_remembered(qt_app, cfg_dir, monkeypatch, tmp_path):
    import novel_translator.ui.refinement_run_dialog as rd
    monkeypatch.setattr(rd, 'DATA_DIR', tmp_path / 'data')
    root, _ = cfg_dir
    d = rd.RefinementRunDialog(_host(root)); d.show(); qt_app.processEvents()
    # A wider window at the height the screen allows (a build machine's screen may be small:
    # Qt keeps a restored window on the screen, so the test never asks for more than fits).
    d.resize(d.width() + 60, d.height()); qt_app.processEvents()
    saved = (d.width(), d.height())
    d.reject()
    assert (tmp_path / 'data' / rd.WINDOW_STATE_FILE).exists()
    d2 = rd.RefinementRunDialog(_host(root))
    # Windows may adjust a restored window by a few pixels to keep its frame on a small screen.
    assert abs(d2.width() - saved[0]) <= 8 and abs(d2.height() - saved[1]) <= 8 and d2.width() > 720
    hidden = rd.RefinementRunDialog(_host(root)); hidden.resize(300, 300)
    (tmp_path / 'data' / rd.WINDOW_STATE_FILE).unlink(); hidden.reject()
    assert not (tmp_path / 'data' / rd.WINDOW_STATE_FILE).exists()      # never shown: nothing saved


def test_window_keeps_its_size_with_the_counts_shown(qt_app, cfg_dir, monkeypatch, tmp_path):
    import novel_translator.ui.refinement_run_dialog as rd
    monkeypatch.setattr(rd, 'DATA_DIR', tmp_path / 'data')
    root, _ = cfg_dir
    d = rd.RefinementRunDialog(_host(root)); d.show(); qt_app.processEvents()
    size = d.size()
    d._counts = (44520, 120000, 208); d.stats_info.show(); d.time_info.show()
    d._stats_changed({'groups': 3355, 'group': 1241, 'requests': 1300, 'retries': 17, 'reviews': 52,
                      'reply_last': 41, 'reply_total': 34 * 1300, 'replies': 1300, 'changed': 38102, 'identical': 6210})
    d.time_info.setText('교정 44,520/120,000 문단 · 원문 보존 208 · 경과 11:42:10 · 남은 예상 약 19:05:00')
    for i in range(30):
        d._log_line('✓ 요청 1,241/3,355 · 44,521~44,556문단 · 변경 31 · 동일 5 · 응답 38초')
    qt_app.processEvents()
    assert d.size() == size
    assert d.layout().totalHeightForWidth(d.width()) <= d.height() + 1
    d.close()
