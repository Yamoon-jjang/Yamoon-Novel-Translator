from __future__ import annotations
import re
from dataclasses import dataclass

# Protect structural/literal strings that should survive translation byte-for-byte.
# This is a translation-integrity feature, not a content-filter masking system.
#
# Angle and square brackets are only protected when they are real markup. Novels use
# them for sound effects, system windows and shouted words (<SPANK>, <Slurp, slurp>,
# [System], [Quest Complete]); those are prose and must reach the model to be
# translated (<찰싹>, [시스템]). Markup is a known HTML / BBCode tag name, or a tag
# whose attributes are written name="value".
_HTML_TAGS = (r'a|abbr|b|big|blockquote|br|center|cite|code|del|div|em|font|h[1-6]|hr|i|img|ins|kbd|'
              r'li|mark|ol|p|pre|q|rb|rp|rt|rtc|ruby|s|samp|small|span|strike|strong|sub|sup|'
              r'table|tbody|td|tfoot|th|thead|tr|tt|u|ul|var|wbr|think|thinking|reasoning')
_ATTR = r'''\s+[A-Za-z_:][\w:.-]*\s*=\s*(?:"[^"<>\r\n]{0,200}"|'[^'<>\r\n]{0,200}'|[^\s"'<>=`]{1,200})'''
_BBCODE = (r'b|i|u|s|url|img|quote|color|colour|size|font|center|left|right|align|code|'
           r'spoiler|list|\*|sup|sub|table|tr|td|th|hr|br|email|indent|youtube|media|strike')
_PATTERNS = [
    re.compile(r'https?://[^\s<>]+', re.I),
    re.compile(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b'),
    re.compile(r'(?<!\w)(?:[A-Za-z]:\\|\\\\)[^\r\n<>|?*]+'),
    re.compile(r'`[^`\r\n]+`'),
    # Known HTML tag (opening, closing or self-closing), attributes only as name=value.
    re.compile(r'</?(?:' + _HTML_TAGS + r')(?![\w-])(?:' + _ATTR + r'){0,20}\s*/?>', re.I),
    # Any other tag name only when it carries name="value" attributes (<code value="...">, <ruby rb='...'>).
    re.compile(r'<(?!P\d{6}>)[A-Za-z][\w:-]{0,40}(?:\s+[A-Za-z_:][\w:.-]*\s*=\s*(?:"[^"<>\r\n]{0,200}"|\'[^\'<>\r\n]{0,200}\'))+\s*/?>'),
    re.compile(r'\[/?(?:' + _BBCODE + r')(?:=[^\]\r\n]{1,80})?\]', re.I),
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


# A bracketed English word that is prose, not markup: <WHACK>, <Slurp, slurp>, [System].
# Before v2.5.6 these were protected (kept in English); the helpers below find them so a
# line made only of them is translated, and an earlier result that still holds them
# verbatim is translated again.
_BRACKET_PROSE = re.compile(r'[<＜〈\[［【][A-Za-z](?:[^<>＜＞〈〉\[\]［］【】\r\n]{0,80}?[^\s<>＜＞〈〉\[\]［］【】])?[>＞〉\]］】]')


def bracket_prose(text: str) -> list[str]:
    if not text or not _BRACKET_PROSE.search(text):
        return []
    spans = literal_spans(text)
    out = []
    for m in _BRACKET_PROSE.finditer(text):
        if re.fullmatch(r'[<＜]P\d{6}[>＞]', m.group(0)):
            continue
        if any(not (m.end() <= a or m.start() >= b) for a, b in spans):
            continue
        out.append(m.group(0))
    return out


def stale_bracket_prose(source: str, translation: str) -> bool:
    """The translation still holds a bracketed English word of the source unchanged."""
    if not translation:
        return False
    return any(w in translation for w in bracket_prose(source))


def count_protectable_literals(text: str) -> int:
    return len(protect_literals(text).mapping)
