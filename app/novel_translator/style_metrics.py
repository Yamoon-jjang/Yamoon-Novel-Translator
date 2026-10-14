"""번역투 점검: counts common translationese patterns in a Korean translation.

Runs locally on the result text (no API request). Used to compare the two 문장 스타일
("보통" / "자연스럽게") on the same book: the job log shows the count and the rate per
1,000 Korean characters next to the unfinished-paragraph count.
"""
from __future__ import annotations
import re

# (label, pattern). Each match counts once. The patterns are deliberately narrow so that
# ordinary Korean is not counted: 그녀 is counted only as a sentence subject/object.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ('그녀는/그녀가', re.compile(r'그녀(?:는|가|를|의|에게)')),
    ('~것이었다', re.compile(r'것이었다')),
    ('~것을 느꼈다', re.compile(r'것을 느꼈다')),
    ('~에 의해', re.compile(r'에 의해')),
    ('~되어지다', re.compile(r'되어(?:지|졌)')),
    ('~을 가지고 있다', re.compile(r'(?:을|를) 가지고 있')),
    ('문장 첫 "그리고"', re.compile(r'(?:^|(?<=[.!?…”"]\s))그리고 ', re.M)),
]


def count(text: str) -> dict[str, int]:
    """Matches per pattern (only patterns that occur)."""
    t = str(text or '')
    out = {}
    for label, rx in PATTERNS:
        n = len(rx.findall(t))
        if n:
            out[label] = n
    return out


def summary(text: str) -> tuple[int, float, dict[str, int]]:
    """(total matches, matches per 1,000 Hangul characters, per-pattern counts)."""
    t = str(text or '')
    found = count(t)
    total = sum(found.values())
    hangul = len(re.findall(r'[가-힣]', t))
    rate = (total * 1000.0 / hangul) if hangul else 0.0
    return total, rate, found
