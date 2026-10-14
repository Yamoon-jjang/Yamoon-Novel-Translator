"""Applying a changed name rendering to Korean text that is already translated.

When the only change to a book's confirmed glossary is the Korean side of existing
entries (좌경 → 쭤징), the book does not need to be translated again: the stored
translations get the new rendering, the result file is written again, and no request
is sent. Anything else (an added/removed entry, a changed source term) is a real
glossary change and keeps the normal behaviour.

The replacement is careful about two Korean details:
  * an occurrence directly after another Hangul syllable is part of a longer word
    (흑백영화 contains 백영) and is left alone;
  * when the new name ends differently (with or without a final consonant), the
    particle right after it follows: 좌경은 → 쭤지는, 소희를 → 소현을, 백영으로 → 바이로.
"""
from __future__ import annotations
import re

_HANGUL = re.compile(r'[가-힣]')

# (after a final consonant, after a vowel). Longest first so 이라고 wins over 이.
_PARTICLES = [('이라고', '라고'), ('이라는', '라는'), ('이에요', '예요'), ('이랑', '랑'), ('이나', '나'),
              ('이란', '란'), ('이야', '야'), ('이여', '여'), ('으로', '로'),
              ('은', '는'), ('이', '가'), ('을', '를'), ('과', '와'), ('아', '야')]


def _final(ch: str) -> int:
    """Index of the final consonant of a Hangul syllable (0 = none), -1 if not Hangul."""
    if not ch or not ('가' <= ch <= '힣'):
        return -1
    return (ord(ch) - 0xAC00) % 28


def renames_between(old_terms: dict, new_terms: dict) -> dict[str, str]:
    """{old Korean: new Korean} when the glossaries differ ONLY in the Korean side of the
    same source terms; {} otherwise (or when a change would be ambiguous)."""
    old = {str(k).strip(): str(v).strip() for k, v in (old_terms or {}).items() if str(k).strip()}
    new = {str(k).strip(): str(v).strip() for k, v in (new_terms or {}).items() if str(k).strip()}
    if not old or set(old) != set(new):
        return {}
    changes = {src: (old[src], new[src]) for src in old if old[src] != new[src]}
    if not changes:
        return {}
    out = {}
    olds = list(old.values())
    for src, (a, b) in changes.items():
        if len(a) < 2 or not b or not _HANGUL.search(a):
            return {}                      # too short to replace safely
        if olds.count(a) > 1 or a in out:
            return {}                      # two source terms shared this rendering
        if any(a in o for o in olds if o != a):
            return {}                      # part of another name (백영 inside 백영화)
        out[a] = b
    return out


def _fix_particle(p: str, new_last: str) -> str:
    fn = _final(new_last)
    if fn < 0:
        return p
    for with_c, without in _PARTICLES:
        if p in (with_c, without):
            if with_c == '으로':
                return without if fn in (0, 8) else with_c      # a final ㄹ takes 로
            return with_c if fn else without
    return p


_ALL_PARTICLES = sorted({x for pair in _PARTICLES for x in pair}, key=len, reverse=True)


def apply(text: str, renames: dict[str, str]) -> tuple[str, int]:
    """Text with every free-standing old rendering replaced. Returns (text, count)."""
    if not text or not renames:
        return text, 0
    count = 0
    # Longest old names first so 이훤시 is never hit through a shorter name.
    for old in sorted(renames, key=len, reverse=True):
        new = renames[old]
        rx = re.compile('(?<![가-힣])' + re.escape(old) + '(' + '|'.join(_ALL_PARTICLES) + ')?([가-힣]?)')
        def sub(m):
            nonlocal count
            count += 1
            p, nxt = m.group(1) or '', m.group(2) or ''
            vowel = _final(new[-1]) == 0
            if p and not nxt:
                p = _fix_particle(p, new[-1])     # a particle: follow the new last syllable
            elif p == '이' and nxt == '었' and vowel:
                p, nxt = '', '였'                  # 좌경이었다 → 쭤지였다
            elif not p and nxt == '였' and _final(new[-1]) > 0:
                nxt = '이었'                       # 소희였다 → 소현이었다
            return new + p + nxt
        text = rx.sub(sub, text)
    return text, count
