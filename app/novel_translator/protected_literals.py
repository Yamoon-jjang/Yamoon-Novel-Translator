from __future__ import annotations
import re
from dataclasses import dataclass

# Protect structural/literal strings that should survive translation byte-for-byte.
# This is a translation-integrity feature, not a content-filter masking system.
_PATTERNS = [
    re.compile(r'https?://[^\s<>]+', re.I),
    re.compile(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b'),
    re.compile(r'(?<!\w)(?:[A-Za-z]:\\|\\\\)[^\r\n<>|?*]+'),
    re.compile(r'`[^`\r\n]+`'),
    re.compile(r'<(?!P\d{6}>/?)[A-Za-z][^>]{0,300}>'),
    re.compile(r'\[(?:/?[A-Za-z][A-Za-z0-9=_ -]{0,80})\]'),
    re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'),
    re.compile(r'(?<!\w)(?:[＊*×○〇□■●]{2,})(?!\w)'),
    re.compile(r'［＃[^］\r\n]{1,200}］'),   # Aozora Bunko annotations: ［＃改ページ］ ［＃「X」に傍点］
]
_TOKEN_RE = re.compile(r'⟦NTLIT(\d{4})⟧')

@dataclass(frozen=True)
class ProtectedText:
    text: str
    mapping: dict[str, str]


def literal_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int,int]]=[]
    for pat in _PATTERNS:
        for m in pat.finditer(text):
            if any(not (m.end() <= a or m.start() >= b) for a,b in spans):
                continue
            spans.append((m.start(),m.end()))
    spans.sort()
    return spans


def protect_literals(text: str) -> ProtectedText:
    mapping: dict[str, str] = {}
    spans = literal_spans(text)
    if not spans:
        return ProtectedText(text,{})
    out=[]; last=0
    for idx,(a,b) in enumerate(spans,1):
        token=f'⟦NTLIT{idx:04d}⟧'
        mapping[token]=text[a:b]
        out.append(text[last:a]); out.append(token); last=b
    out.append(text[last:])
    return ProtectedText(''.join(out),mapping)


def restore_literals(text: str, mapping: dict[str,str]) -> str:
    out=text
    missing=[]
    for token,value in mapping.items():
        if token not in out:
            missing.append(token)
        else:
            out=out.replace(token,value)
    if missing:
        raise ValueError('보호 리터럴 토큰 누락: '+', '.join(missing[:4]))
    # Model must not invent unknown protection tokens.
    unknown=[m.group(0) for m in _TOKEN_RE.finditer(out) if m.group(0) not in mapping]
    if unknown:
        raise ValueError('알 수 없는 보호 리터럴 토큰이 번역 결과에 포함되었습니다.')
    return out


def count_protectable_literals(text: str) -> int:
    return len(protect_literals(text).mapping)
