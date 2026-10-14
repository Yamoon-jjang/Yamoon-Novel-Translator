"""Conservative, offline evidence index for Korean-novel refinement.

This module makes suggestions, never edits prose or determines that similar names
necessarily identify the same character. User-confirmed decisions remain authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import Counter, defaultdict
import re
import unicodedata

from .chunking import split_paragraphs

_HAN = re.compile(r'[\u3400-\u9fff\uf900-\ufaff]')
_KANA = re.compile(r'[\u3040-\u30ff\uff66-\uff9f]')
_HANGUL = re.compile(r'[가-힣]')
_HAN_SPAN = re.compile(r'[\u3400-\u9fff\uf900-\ufaff]{1,24}')
_KANA_SPAN = re.compile(r'[\u3040-\u30ff\uff66-\uff9f]{2,40}')
_QUOTES = re.compile(r'[“「『"]([^”」』"\n]{1,350})[”」』"]')
_NAME_BOUNDARY = r'(?<![가-힣A-Za-z0-9]){}(?=$|[^가-힣A-Za-z0-9]|(?:에게서|께서|부터|까지|에서|에게|은|는|이|가|을|를|과|와|의|도|만)(?=[^가-힣A-Za-z0-9]|$))'


@dataclass(frozen=True)
class Evidence:
    paragraph_id: int
    excerpt: str
    span_start: int
    span_end: int


@dataclass(frozen=True)
class Candidate:
    kind: str
    spelling: str
    occurrences: int
    evidence: tuple[Evidence, ...]


def _excerpt(text: str, a: int, b: int, context: int = 45) -> str:
    return text[max(0, a-context):min(len(text), b+context)]


def _safe_name(name: str) -> str:
    name = unicodedata.normalize('NFC', str(name or '').strip())
    if not 2 <= len(name) <= 32 or '\n' in name or '\r' in name:
        raise ValueError('이름은 2~32자이며 줄바꿈을 포함할 수 없습니다.')
    return name


def analyze_source(text: str, known_names=(), max_evidence: int = 12) -> list[Candidate]:
    """Return exact-source matches only, with stable paragraph IDs and offsets.

    Candidates are *not* replacement instructions or inferred entity merges.
    Known names must be supplied by the user/confirmed glossary, not guessed from
    arbitrary common Korean words. Foreign-script spans are diagnostic candidates.
    """
    if not isinstance(text, str):
        raise TypeError('text must be str')
    if not 1 <= max_evidence <= 100:
        raise ValueError('max_evidence must be 1..100')
    names = sorted({_safe_name(n) for n in known_names}, key=lambda n: (-len(n), n))
    patterns = [(n, re.compile(_NAME_BOUNDARY.format(re.escape(n)))) for n in names]
    found = defaultdict(list)
    counts = Counter()
    for paragraph in split_paragraphs(text):
        body = paragraph.text
        hits = []
        for kind, rx in (('han', _HAN_SPAN), ('kana', _KANA_SPAN)):
            for m in rx.finditer(body):
                hits.append((kind, m.group(), m.start(), m.end()))
        for name, rx in patterns:
            for m in rx.finditer(body):
                hits.append(('name', name, m.start(), m.end()))
        for kind, spelling, start, end in hits:
            key = (kind, spelling)
            counts[key] += 1
            if len(found[key]) < max_evidence:
                found[key].append(Evidence(paragraph.pid, _excerpt(body, start, end), start, end))
    return [Candidate(kind, spelling, n, tuple(found[kind, spelling]))
            for (kind, spelling), n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]


def dialogue_evidence(text: str, max_evidence: int = 100) -> tuple[Evidence, ...]:
    """Collect quoted dialogue for later model review, without guessing speaker."""
    if not 1 <= max_evidence <= 5000:
        raise ValueError('max_evidence must be 1..5000')
    out = []
    for paragraph in split_paragraphs(text):
        for m in _QUOTES.finditer(paragraph.text):
            if len(out) >= max_evidence:
                return tuple(out)
            out.append(Evidence(paragraph.pid, _excerpt(paragraph.text, m.start(), m.end()), m.start(), m.end()))
    return tuple(out)


def verify_evidence(text: str, evidence: Evidence, spelling: str = '') -> bool:
    paragraphs = split_paragraphs(text)
    if evidence.paragraph_id < 1 or evidence.paragraph_id > len(paragraphs):
        return False
    body = paragraphs[evidence.paragraph_id-1].text
    if not (0 <= evidence.span_start < evidence.span_end <= len(body)):
        return False
    return (not spelling or body[evidence.span_start:evidence.span_end] == spelling) and (
        evidence.excerpt == _excerpt(body, evidence.span_start, evidence.span_end)
    )


def propose_alias(alias: str, canonical: str, confirmed: dict[str, str], rejected=()) -> dict:
    """Return a review-only proposal; confirmed mappings must never be overwritten."""
    alias, canonical = _safe_name(alias), _safe_name(canonical)
    if alias == canonical:
        raise ValueError('동일한 표기는 병합 후보가 아닙니다.')
    if alias in rejected or alias in confirmed:
        return {'status': 'conflict' if alias in confirmed and confirmed[alias] != canonical else 'protected',
                'alias': alias, 'canonical': canonical}
    return {'status': 'review', 'alias': alias, 'canonical': canonical}
