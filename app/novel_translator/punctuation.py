"""Korean punctuation for translated text.

Chinese/Japanese full-width punctuation that a model copies into the Korean
translation (。，、！？：；（）「」・〜‥, full-width digits/symbols, the
ideographic space inside a line) is converted to the punctuation Korean text uses, with the
space Korean needs after a sentence or clause mark (Chinese and Japanese text has
no spaces). Quotation marks follow the chosen style. A dash right inside a quotation
mark ("사실은――", "――저기"), a dash that opens a new sentence inside the same speech
("무슨 일입니까. ――자,") and a Japanese middle-dot ellipsis (・・・) become "……" (v2.6.6).

Applied when the result file is written and to the live 번역 결과 (진행 중) view, so both
show the same text: it never changes the translation request or the job identity, and books translated earlier are cleaned the next
time their result is saved. Paragraphs kept as Chinese/Japanese/English source
are left untouched; paragraphs made only of punctuation (e.g. “。。。。。”) are
cleaned too.
"""
from __future__ import annotations
import re

QUOTE_STYLES = ('straight', 'curly', 'keep')

_HANGUL = re.compile(r'[가-힣]')
_LETTERS = re.compile(r'[A-Za-z가-힣ぁ-ゟ゠-ヿ一-龥Ａ-Ｚａ-ｚ]')
_CJK_MARKS = {'。': '.', '．': '.', '，': ',', '、': ',', '！': '!', '？': '?', '：': ':', '；': ';'}
_RUN = re.compile('[' + ''.join(_CJK_MARKS) + ']+')
# After a converted mark a space is added only before a word or an opening mark.
_NEEDS_SPACE = re.compile(r'[가-힣A-Za-z0-9一-龥ぁ-ゟ゠-ヿ“‘(\[「『《〈【※]')
# Full-width ASCII block U+FF01..U+FF5E -> ASCII (the marks above are handled first).
_FULLWIDTH = str.maketrans({chr(c): chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F) if chr(c) not in _CJK_MARKS})


# Japanese-specific marks: name separator ・, wave dash 〜, two-dot leader ‥.
# 【】 and ※ are kept: Korean web novels use them as they are.
# Two or more Japanese middle dots (・・・, ･･･) are an ellipsis, not a name separator.
_NAKAGURO_ELLIPSIS = re.compile('[・･]{2,}')
_JA_EXTRA = str.maketrans({'・': '·', '〜': '~', '‥': '…'})


# A Japanese long-vowel mark (ー, ｰ) or a 一 used as one right after Korean.
_ELONGATION = re.compile(r'(?<=[가-힣~!?…])[ーｰ一]+(?=$|[\s!?…~.,\'"”’」』)\]])')


def should_clean(text: str) -> bool:
    """Korean text, or a line with no letters at all (punctuation/digits only)."""
    return bool(text) and (bool(_HANGUL.search(text)) or not _LETTERS.search(text))


# A decimal point, thousands separator or clock colon between digits: converted
# without the space a sentence mark gets (3．5 -> 3.5, not "3. 5").
_NUM_MARK = re.compile(r'(?<=[0-9])[．，：](?=[0-9])')


def _convert_runs(s: str) -> str:
    def repl(m):
        out = ''.join(_CJK_MARKS[ch] for ch in m.group(0))
        nxt = s[m.end():m.end() + 1]
        return out + (' ' if nxt and _NEEDS_SPACE.match(nxt) else '')
    return _RUN.sub(repl, s)


def _quotes(s: str, style: str) -> str:
    if style == 'straight':
        return s.translate(str.maketrans({'“': '"', '”': '"', '„': '"', '「': '"', '」': '"',
                                          '‘': "'", '’': "'"}))
    if style == 'curly':
        return s.translate(str.maketrans({'「': '“', '」': '”'}))
    return s


# A dash that trails off or opens a line of speech (「実は――」 "사실은――" “我——”) reads as
# "……" in Korean. Only a dash right inside a quotation mark, or one that opens a new sentence inside
# the same speech ("무슨 일입니까. ――자,"), is changed: a dash in the middle of
# a sentence (――야마자키 탐정 사무소――), a scene-break line (――――) and a dash outside the
# quotes stay as they are. A straight quote counts only when the line's quotes pair up, so an
# opening and a closing quote are never mistaken for each other.
_DASHES = '―—─–'
_SPEECH_DASH = re.compile('[' + _DASHES + ']+')
_OPEN_Q = '“‘「『'
_CLOSE_Q = '”’」』'
_AFTER_DASH = '!?♡♥~'   # "그건――!" "아앗――♡": the dash still ends the speech


def _restarts_speech(s: str, a: int, b: int, role: dict, marks: list) -> bool:
    """A dash that opens a new sentence inside the same line of speech: "무슨 일입니까. ――자,
    진정하세요." The dash follows a sentence end (. ! ?) inside an open quotation and words
    follow it. After an ellipsis ("끝났어…… ――이제") and outside the quotes ("그는 말했다. ――그런데")
    it stays."""
    k = a - 1
    while k >= 0 and s[k] == ' ':
        k -= 1
    if k < 0 or s[k] not in '.!?' or b >= len(s) or s[b] == ' ' or role.get(b) == 'close':
        return False
    before = [i for i in marks if i < a]
    return bool(before) and role[before[-1]] == 'open'


def _speech_dashes(s: str) -> str:
    if not _SPEECH_DASH.search(s):
        return s
    straight_ok = s.count('"') % 2 == 0
    role = {}                      # index of a quotation mark -> 'open' / 'close'
    n_straight = 0
    for i, ch in enumerate(s):
        if ch in _OPEN_Q:
            role[i] = 'open'
        elif ch in _CLOSE_Q:
            role[i] = 'close'
        elif ch == '"' and straight_ok:
            role[i] = 'open' if n_straight % 2 == 0 else 'close'
            n_straight += 1
    marks = sorted(role)           # quotation-mark positions, in order
    out, last = [], 0
    for m in _SPEECH_DASH.finditer(s):
        a, b = m.start(), m.end()
        e = b
        while e < len(s) and s[e] in _AFTER_DASH:
            e += 1
        if role.get(a - 1) == 'open' or role.get(e) == 'close' or _restarts_speech(s, a, b, role, marks):
            out.append(s[last:a]); out.append('……'); last = b
    out.append(s[last:])
    return ''.join(out)


def korean_punctuation(text: str, quote_style: str = 'straight') -> str:
    if not should_clean(text):
        return text
    lines = []
    for line in text.split('\n'):
        indent = re.match(r'[ \t　]*', line).group(0)      # leading indentation kept as is
        body = line[len(indent):]
        body = body.translate(_FULLWIDTH).replace('\u3000', ' ')   # digits/symbols first so spacing sees '5', not '５'
        body = _NUM_MARK.sub(lambda m: _CJK_MARKS[m.group(0)], body)   # 3．5 / 1，000 / 10：30: no space
        body = _convert_runs(body)
        body = _NAKAGURO_ELLIPSIS.sub('……', body)   # そう・・・ -> 그래……, not 그래···
        body = body.translate(_JA_EXTRA)
        body = _ELONGATION.sub(lambda m: '~' * min(3, len(m.group(0))), body)   # 안 돼ー! / 안 돼一! -> 안 돼~!
        body = _quotes(body, quote_style)
        body = _speech_dashes(body)
        body = re.sub(r'(?<=\S) {2,}(?=\S)', ' ', body)
        lines.append(indent + body)
    return '\n'.join(lines)
