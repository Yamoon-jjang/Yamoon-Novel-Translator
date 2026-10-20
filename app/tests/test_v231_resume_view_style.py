"""v2.3.1: a resumed job shows the already translated part in the live view, and a
문장 스타일 change during a run applies from the next chunk (no re-translation)."""
from __future__ import annotations
import re

from test_refusal_recovery import ScriptedProvider, make_worker, rows, _korean
from novel_translator.prompting import NATURAL_PROSE

NATURAL_HEAD = NATURAL_PROSE.splitlines()[0]
TEXT = '\n\n'.join(f'　第{i}の段落です。' + 'あ' * 70 for i in range(8))


def _mark(body: str) -> str:
    m = re.search(r'第(\d+)の', body)
    n = int(m.group(1)) if m else 99
    return _korean(body) + ' 표식' + chr(0xAC00 + n * 3)


def _chunk_of(request) -> set[int]:
    return {int(x) for x in re.findall(r'第(\d+)の', request.user_prompt.split('[SOURCE', 1)[-1])}


def test_resumed_job_shows_the_saved_part_first_and_does_not_request_it_again(tmp_path):
    holder = {}

    def stop_after_two(prompt, src, body):
        if '第3の' in body:
            holder['w'].stop()
        return _mark(body)
    first = ScriptedProvider(stop_after_two)
    w, chunks, _ = make_worker(tmp_path, TEXT, first)
    assert len(chunks) >= 5
    holder['w'] = w
    w.run()
    done = [r for r in rows(tmp_path) if r['status'] == 'COMPLETED']
    assert done and len(done) < len(chunks), 'the first run stopped part-way'
    saved_marks = re.findall(r'표식(.)', ''.join(r['translation'] for r in done))
    assert saved_marks

    second = ScriptedProvider(lambda p, s, b: _mark(b))
    w2, _, _ = make_worker(tmp_path, TEXT, second)
    shown = []
    w2.chunk_translated.connect(lambda pos, total, text: shown.append(text))
    w2.run()
    # The saved part comes first, in one block, in reading order; then the new chunks.
    first_block_marks = re.findall(r'표식(.)', shown[0])
    assert first_block_marks == saved_marks
    all_marks = re.findall(r'표식(.)', '\n'.join(shown))
    assert all_marks == sorted(all_marks, key=lambda c: ord(c)) and len(all_marks) == len(set(all_marks)) == 8
    # Saved chunks are never requested again.
    asked = set().union(*(_chunk_of(r) for r in second.requests))
    saved_numbers = {(ord(c) - 0xAC00) // 3 for c in saved_marks}
    assert asked and not (asked & saved_numbers)
    out = (tmp_path / 'out' / 'novel.ko.txt').read_text(encoding='utf-8')
    assert len(re.findall(r'표식', out)) == 8


def test_first_run_live_view_is_unchanged(tmp_path):
    p = ScriptedProvider(lambda pr, s, b: _mark(b))
    w, chunks, _ = make_worker(tmp_path, TEXT, p)
    shown = []
    w.chunk_translated.connect(lambda pos, total, text: shown.append((pos, text)))
    w.run()
    assert [pos for pos, _ in shown] == list(range(1, len(chunks) + 1))   # one block per chunk, as before


def test_style_change_during_a_run_applies_from_the_next_chunk(tmp_path):
    holder = {}

    def switch_on_third(prompt, src, body):
        if '第3の' in body:
            holder['w'].set_sentence_style('natural')
        return _mark(body)
    p = ScriptedProvider(switch_on_third)
    w, chunks, events = make_worker(tmp_path, TEXT, p)
    holder['w'] = w
    fp = w._fingerprint()
    w.run()
    styled = [(min(_chunk_of(r)), NATURAL_HEAD in r.system_prompt) for r in p.requests if _chunk_of(r)]
    switched_at = next(n for n, nat in styled if nat)
    assert all(not nat for n, nat in styled if n < switched_at)
    assert all(nat for n, nat in styled if n >= switched_at)
    assert switched_at > 3, 'the chunk in flight finishes with the style it started with'
    assert w.sentence_style == 'natural' and len(w.style_changes) == 1
    assert any('문장 스타일 변경' in e and '자연스럽게' in e for e in events)
    assert w._fingerprint() == fp, 'the style is not part of the job identity'
    assert all(r['status'] == 'COMPLETED' for r in rows(tmp_path))
    # Each chunk was requested once: nothing was translated again.
    asked = [min(_chunk_of(r)) for r in p.requests if _chunk_of(r)]
    assert len(asked) == len(set(asked))


def test_window_hands_a_style_change_to_the_running_job(qt_app, sandbox, monkeypatch):
    from test_alpha17 import _window
    w = _window(monkeypatch)
    got = []

    class Running:
        sentence_style = 'normal'
        def isRunning(self): return True
        def set_sentence_style(self, style): got.append(style)
    try:
        w.sentence_style.setCurrentIndex(w.sentence_style.findData('normal'))   # not running yet
        w.worker = Running()
        w.sentence_style.setCurrentIndex(w.sentence_style.findData('natural'))
        assert got == ['natural']
        assert '다음 청크부터 적용' in w.status.text()
        w.sentence_style.setCurrentIndex(w.sentence_style.findData('normal'))
        assert got == ['natural', 'normal']
        w.worker = None
        w.sentence_style.setCurrentIndex(w.sentence_style.findData('natural'))
        assert got == ['natural', 'normal'] and '다음 번역부터 적용' in w.status.text()
    finally:
        w.worker = None; w.close()


from test_alpha17 import qt_app, sandbox  # noqa: E402,F401 (pytest fixtures)
