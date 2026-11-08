"""Invisible marks in the source (v2.6.3).

Some e-book sites hide a mark in the text with zero-width characters (a copyright or
distributor mark written as a pattern of U+200B / U+200C between U+2063 ... U+2064, for
example). A model cannot copy such characters reliably: it drops some of them or writes
stray letters instead, and the pattern is lost either way.

So the marks never go to the model, and they are never removed from the book either:

  * strip(text) is what a request sends;
  * the paragraph text the job keeps (chunk identity, database, translation memory) is the
    original, marks included, so a job started before v2.6.3 continues where it stopped;
  * restore(original, body) writes each run of marks of the original paragraph back into
    its translation (or edit), in the original order, at the matching place: the start,
    the end, or the word boundary nearest to the same share of the paragraph.

A paragraph whose body already holds the original's marks in order (a paragraph kept as
source, a pass-through line) is returned unchanged, so nothing is ever written twice.
A text without such characters goes through every function unchanged.
"""
from __future__ import annotations
import re

# Zero-width space / non-joiner, left-to-right / right-to-left marks, word joiner,
# invisible function application / times / separator / plus, and a zero-width no-break
# space (U+FEFF) inside the text. A zero-width joiner (U+200D) is part of a run only next
# to one of these: alone it joins emoji (👨‍👩‍👧) and stays where it is.
_CORE = '​‌‎‏⁠⁡⁢⁣⁤﻿'
MARK_RE = re.compile(f'‍*[{_CORE}][{_CORE}‍]*')
_ANY_MARK = re.compile(f'[{_CORE}]')
# Where a mark may sit inside a translation: next to a space or a punctuation mark,
# never inside a word (it would break a word search in a reader).
_BOUNDARY = re.compile(r'[\s　.,!?…·:;"\'“”‘’「」『』()\[\]〈〉《》。，！？、：；（）]')


def has_marks(text: str) -> bool:
    return bool(text) and bool(_ANY_MARK.search(text))


def strip(text: str) -> str:
    """The text without its invisible marks (what a request sends)."""
    if not has_marks(text):
        return text
    return MARK_RE.sub('', text)


def count(text: str) -> int:
    """Number of mark characters in the text."""
    if not has_marks(text):
        return 0
    return sum(len(m.group(0)) for m in MARK_RE.finditer(text))


def runs(text: str) -> list[tuple[str, str, float]]:
    """[(run, where, share)] of the text's marks: where is 'start', 'end' or 'mid', and
    share is the part of the visible text before the run (0..1)."""
    if not has_marks(text):
        return []
    visible = strip(text)
    total = len(visible.strip()) or 1
    lead = len(visible) - len(visible.lstrip())
    out = []
    seen = 0
    pos = 0
    for m in MARK_RE.finditer(text):
        seen += len(MARK_RE.sub('', text[pos:m.start()]))
        pos = m.end()
        before = text[:m.start()]
        after = text[m.end():]
        if not MARK_RE.sub('', before).strip():
            where = 'start'
        elif not MARK_RE.sub('', after).strip():
            where = 'end'
        else:
            where = 'mid'
        share = min(1.0, max(0.0, (seen - lead) / total))
        out.append((m.group(0), where, share))
    return out


def _holds(body: str, marks: list[str]) -> bool:
    """The body already carries these runs, in this order."""
    at = 0
    for run in marks:
        i = body.find(run, at)
        if i < 0:
            return False
        at = i + len(run)
    return True


def _place(body: str, share: float) -> int:
    """The insertion point in `body` nearest to `share` of its visible text, moved to the
    closest word boundary (before a space or punctuation)."""
    core_start = len(body) - len(body.lstrip())
    core_end = len(body.rstrip())
    if core_end <= core_start:
        return core_start
    target = core_start + int(round(share * (core_end - core_start)))
    target = min(max(target, core_start), core_end)
    best = None
    for m in _BOUNDARY.finditer(body, core_start, core_end):
        for cand in (m.start(), m.end()):
            if core_start < cand < core_end and (best is None or abs(cand - target) < abs(best - target)):
                best = cand
    return best if best is not None else core_end


def restore(original: str, body: str) -> str:
    """`body` (a translation or edit of `original`) with the original's marks written back."""
    if not has_marks(original) or body is None:
        return body
    found = runs(original)
    if not found:
        return body
    if _holds(body, [r for r, _, _ in found]):
        return body
    out = strip(body)          # stray pieces of a mark the model wrote are not kept
    if out == strip(original):
        return original        # the source kept as it is (a skipped paragraph): marks exactly where they were
    lead_ws = out[:len(out) - len(out.lstrip())]
    tail_ws = out[len(out.rstrip()):]
    core = out.strip()
    if not core:
        # Nothing visible to place them in: keep them together where the text would be.
        return lead_ws + ''.join(r for r, _, _ in found) + tail_ws
    start = ''.join(r for r, w, _ in found if w == 'start')
    end = ''.join(r for r, w, _ in found if w == 'end')
    mids = [(r, s) for r, w, s in found if w == 'mid']
    # Middle runs from the last to the first so earlier positions stay valid; a later run
    # is never placed before an earlier one.
    points = []
    floor = 0
    for r, s in mids:
        p = max(_place(core, s), floor)
        points.append((p, r))
        floor = p
    for p, r in reversed(points):
        core = core[:p] + r + core[p:]
    return lead_ws + start + core + end + tail_ws
