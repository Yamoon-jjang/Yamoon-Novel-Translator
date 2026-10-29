"""v2.6: one-line shift check for Korean refinement.

A refinement request carries up to 36 paragraphs (3,500 characters for a local LLM), so a
dialogue-heavy stretch can be many short lines. A local model can then keep every paragraph
ID but put each edited line one ID off from some line on: no ID is missing and every line is
of a plausible length, so the ID and per-paragraph checks pass it, and the total length does
not change, so the continuity review is not asked either. The same check the translator uses
(v2.5.9, validation.dialogue_shift_run) now rejects such a reply: the group is asked again
in halves, and no edited line is kept under the wrong paragraph.

Costs: nothing when the reply lines up (no extra request), one split when a shift is seen.
"""
from __future__ import annotations

import re

from novel_translator.chunking import split_paragraphs
from novel_translator.providers.base import TranslationResponse
from novel_translator.refinement_runtime import (run_refinement, shift_run, ShiftedReply,
                                                   group_paragraphs)
from novel_translator.validation import SHIFT_RUN_MIN

_TAG = re.compile(r'(?m)^<P(\d{6})>\n([^\n]*)')


from refine_shift_corpus import SOURCES, EDITS

_EDIT = dict(zip(SOURCES, EDITS))


def edit(text: str) -> str:
    """The natural literary edit of one line of the scene (see refine_shift_corpus)."""
    return _EDIT[text]


def scene(n=None, start=0):
    lines = SOURCES[start:start + n] if n else SOURCES[start:]
    return '\n'.join(lines)


class Editor:
    """Local-model stand-in. With `shift_from` set, a request of at least `shift_min`
    paragraphs comes back shifted one line from that position on (all IDs present)."""
    name = 'local'

    def __init__(self, shift_from=None, shift_min=20):
        self.shift_from = shift_from; self.shift_min = shift_min
        self.edits = []; self.analysis = 0

    def translate(self, request, key):
        hits = _TAG.findall(request.user_prompt)
        if request.purpose != 'edit' or not hits:
            self.analysis += 1
            return TranslationResponse('SAME' if '원문:' in request.user_prompt else '')
        self.edits.append(len(hits))
        bodies = [edit(t) for _, t in hits]
        if self.shift_from is not None and len(hits) >= self.shift_min:
            k = min(self.shift_from, len(hits) - 2)
            bodies = bodies[:k] + bodies[k + 1:] + [bodies[-1]]          # one line off from k on
        return TranslationResponse('\n\n'.join(f'<P{pid}>\n{b}' for (pid, _), b in zip(hits, bodies)))


def _refine(tmp_path, text, editor, **kw):
    src = tmp_path/'book.ko.txt'; src.write_text(text, encoding='utf-8')
    out = tmp_path/'book.ko.refined.txt'
    events = []
    result = run_refinement(src, out, editor, 'm', 'k', checkpoint_dir=tmp_path/'cache',
                            event=events.append, **kw)
    return result, out, events


def _lines(path):
    return [l for l in path.read_text(encoding='utf-8-sig').replace('\r\n', '\n').split('\n') if l.strip()]


def test_shifted_reply_is_never_kept_and_every_line_ends_under_its_own_paragraph(tmp_path):
    text = scene()
    editor = Editor(shift_from=5)
    result, out, events = _refine(tmp_path, text, editor)
    assert result['status'] == 'completed' and result['issues'] == 0
    assert _lines(out) == [edit(l) for l in text.split('\n')]           # nothing sits one line off
    assert any(e.startswith('문단 밀림 의심') for e in events), events
    assert '나눠 다시 요청' in next(e for e in events if e.startswith('문단 밀림 의심'))


def test_without_the_check_the_same_reply_would_be_kept_shifted():
    """What v2.5.9 refinement did: the shifted reply passes every older check."""
    from novel_translator.refinement_runtime import model_reply_to_text, verified_partial_reply, review_reason
    paras = split_paragraphs(scene(20, start=8))
    reply = Editor(shift_from=4).translate(type('R', (), {
        'purpose': 'edit', 'user_prompt': '\n\n'.join(f'<P{p.pid:06d}>\n{p.text}' for p in paras)})(), 'k').text
    bodies = model_reply_to_text(reply, paras)                         # older checks: accepted
    assert bodies != [edit(p.text) for p in paras]                     # ... although shifted
    assert review_reason(paras, bodies) in ('', '존댓말·반말 변화')        # a review only by chance
    accepted, _, _ = verified_partial_reply(reply, paras)
    assert shift_run(paras, accepted) >= SHIFT_RUN_MIN                 # v2.6 sees it


def test_a_lined_up_reply_costs_no_extra_request(tmp_path):
    text = '\n'.join(SOURCES * 3)
    paras = split_paragraphs(text)
    groups = group_paragraphs(paras, 3500, 36)
    editor = Editor()
    result, out, events = _refine(tmp_path, text, editor, max_chars=3500)
    assert result['status'] == 'completed'
    assert editor.edits == [len(g) for g in groups]                    # one request per group, as before
    assert not any('밀림' in e for e in events)


def test_small_groups_are_not_checked():
    paras = split_paragraphs('"가."\n그가 웃었다.\n"와."')
    shifted = {paras[0].pid: '그가 웃었다.', paras[1].pid: '"와."', paras[2].pid: '"와."'}
    assert shift_run(paras, shifted) == 0


def test_natural_edits_are_never_taken_for_a_shift():
    """Every group of 4~36 lines of a real-style edit (speech tags moved, sentences merged,
    endings and words changed): no check fires, so no request is ever added."""
    paras = split_paragraphs(scene())
    bodies = {p.pid: EDITS[p.pid - 1] for p in paras}
    for size in range(SHIFT_RUN_MIN + 1, 37):
        for at in range(0, len(paras) - size + 1):
            group = paras[at:at + size]
            assert shift_run(group, {p.pid: bodies[p.pid] for p in group}) < SHIFT_RUN_MIN, (size, at)


def test_lines_left_unedited_are_not_a_shift_either():
    """A model that edits only some lines and returns the rest as they were."""
    paras = split_paragraphs(scene())
    for keep_every in (2, 3, 5):
        bodies = {p.pid: (p.text if p.pid % keep_every == 0 else EDITS[p.pid - 1]) for p in paras}
        for at in range(0, len(paras) - 36 + 1):
            group = paras[at:at + 36]
            assert shift_run(group, {p.pid: bodies[p.pid] for p in group}) < SHIFT_RUN_MIN


def test_shifts_both_ways_are_caught_far_more_often_than_by_quotes_alone():
    """Measured over every group size 8~36, every start and both directions: the quote
    check alone (the translator's) misses shifts inside narration; the wording check adds
    them. A shift of only the last few lines of a group can slip (3 lines are needed)."""
    from novel_translator.refinement_runtime import escape_protocol_literals
    from novel_translator.validation import dialogue_shift_run
    paras = split_paragraphs(scene())
    total = by_quote = caught = 0
    for size in range(8, 37, 4):
        for at in range(0, len(paras) - size + 1, 5):
            group = paras[at:at + size]
            right = [EDITS[p.pid - 1] for p in group]
            for k in range(0, size - 2):
                for wrong in (right[:k] + right[k + 1:] + [right[-1]], right[:k] + [right[k]] + right[k:-1]):
                    bodies = {p.pid: b for p, b in zip(group, wrong)}
                    src = '\n'.join(f'<P{p.pid:06d}>\n{escape_protocol_literals(p.text)}' for p in group)
                    out = '\n'.join(f'<P{p.pid:06d}>\n{escape_protocol_literals(bodies[p.pid])}' for p in group)
                    total += 1
                    by_quote += dialogue_shift_run(src, out) >= SHIFT_RUN_MIN
                    hit = shift_run(group, bodies) >= SHIFT_RUN_MIN
                    caught += hit
                    if k <= size - 7:
                        assert hit, (size, at, k)       # 6+ shifted lines are always caught
    assert caught > by_quote and caught / total > 0.85, (caught, by_quote, total)


def test_a_partial_reply_that_is_shifted_is_split_too(tmp_path):
    """A shifted reply with one line refused: the accepted lines are not kept either."""
    text = scene(30, start=10)

    class Partial(Editor):
        def translate(self, request, key):
            r = super().translate(request, key)
            if request.purpose != 'edit':
                return r
            if len(_TAG.findall(request.user_prompt)) >= self.shift_min:   # only the shifted reply
                parts = r.text.split('\n\n')
                pid = parts[-1].split('\n', 1)[0]
                parts[-1] = pid + '\n죄송하지만 이 문단은 교정해 드릴 수 없습니다.'
                return TranslationResponse('\n\n'.join(parts))
            return r
    result, out, events = _refine(tmp_path, text, Partial(shift_from=3))
    assert result['status'] == 'completed'
    assert _lines(out) == [edit(l) for l in text.split('\n')]
    assert any(e.startswith('문단 밀림 의심') for e in events)


def test_shifted_reply_exception_is_a_format_error():
    assert issubclass(ShiftedReply, ValueError)
