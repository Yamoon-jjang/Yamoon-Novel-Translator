"""Applying a changed name rendering to Korean text that is already translated.

When the only change to a book's confirmed glossary is the Korean side of existing
entries (좌경 → 쭤징), the book does not need to be translated again: the stored
translations get the new rendering, the result file is written again, and no request
is sent. Anything else (an added/removed entry, a changed source term) is a real
glossary change and keeps the normal behaviour.

The replacement is careful about three Korean details:
  * an occurrence directly after another Hangul syllable is part of a longer word
    (흑백영화 contains 백영) and is left alone;
  * an occurrence directly followed by a syllable that cannot start a particle, an ending or
    a form of address is part of a longer word too (하늘 → 하늬 leaves 하늘색 and 하늘나라,
    백영 → 바이 leaves 백영화); it is left alone and counted as skipped;
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

# First syllables that may follow a name directly (no particle from the table above):
# other particles and endings (의·도·만·에게·께서·한테·처럼·보다·까지·부터·마저·조차·밖에·뿐·
# 쯤·같이·더러·대로·였다) and forms of address written together with the name
# (님·씨·네·들·군·양·형·언니·누나·오빠·선배·사형·사부·공자·소저·낭자·대인·부인·장군·도령·
# 전하·마마). Anything else makes the name part of a longer word.
_NEXT_OK = frozenset('의도만에께한처보까부마조밖뿐쯤같더대였'
                     '님씨네들군양형언누오선사공소낭장도전')
# After a particle the word may still go on (하늬는요, 하늬와의, 하늬이다), but 하늘나라
# (나 + 라) or 하늘아래 (아 + 래) are other words.
_AFTER_PARTICLE_OK = frozenset('요도는은만서의')


def _attached_ok(p: str, nxt: str) -> bool:
    """True when what follows the name keeps it a free-standing name."""
    if not nxt:
        return True
    if not p:
        return nxt in _NEXT_OK
    if p == '이':
        return True                       # 하늬이다 · 하늬이었다 · 하늬이가 (애칭)
    return nxt in _AFTER_PARTICLE_OK


def apply_counted(text: str, renames: dict[str, str]) -> tuple[str, int, int]:
    """(text, replaced, skipped): skipped counts occurrences left alone as part of a longer word."""
    if not text or not renames:
        return text, 0, 0
    count = 0
    skipped = 0
    # Longest old names first so 이훤시 is never hit through a shorter name.
    for old in sorted(renames, key=len, reverse=True):
        new = renames[old]
        rx = re.compile('(?<![가-힣])' + re.escape(old) + '(' + '|'.join(_ALL_PARTICLES) + ')?([가-힣]?)')
        def sub(m):
            nonlocal count, skipped
            p, nxt = m.group(1) or '', m.group(2) or ''
            if not _attached_ok(p, nxt):
                skipped += 1
                return m.group(0)
            count += 1
            vowel = _final(new[-1]) == 0
            if p and (not nxt or p != '이'):
                p = _fix_particle(p, new[-1])     # a particle (also 과의, 은요): follow the new last syllable
            elif p == '이' and nxt == '었' and vowel:
                p, nxt = '', '였'                  # 좌경이었다 → 쭤지였다
            elif not p and nxt == '였' and _final(new[-1]) > 0:
                nxt = '이었'                       # 소희였다 → 소현이었다
            return new + p + nxt
        text = rx.sub(sub, text)
    return text, count, skipped
