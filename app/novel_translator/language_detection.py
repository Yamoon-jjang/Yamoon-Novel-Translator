from __future__ import annotations
import re


def detect_language(text: str) -> tuple[str|None, float]:
    sample=text[:120000]
    # ・ ー ゠ are also used in Chinese text (约翰・史密斯, 啊ー), so they are not counted as kana.
    kana=len(re.findall(r'[ぁ-ゟァ-ヺヽ-ヿ]',sample))
    han=len(re.findall(r'[一-龥]',sample))
    latin=len(re.findall(r'[A-Za-z]',sample))
    total=kana+han+latin
    if total < 20:return None,0.0
    if kana >= 12 and kana/max(1,total) >= 0.05:
        return 'ja', min(0.99,0.65+kana/max(1,total))
    if han >= 20 and kana/max(1,total) < 0.01 and han/max(1,total) >= 0.30:
        return 'zh', min(0.98,0.60+han/max(1,total)*0.35)
    if latin >= 30 and (han+kana)/max(1,total) < 0.08:
        return 'en', min(0.99,0.70+latin/max(1,total)*0.25)
    return None,0.0


def detect_language_bytes(raw: bytes) -> tuple[str|None, float, str|None]:
    """Detect language from legacy TXT bytes without trusting the currently selected profile.

    This is only a preflight warning aid; it never changes the user's selected
    language automatically. Multiple legacy encodings can decode the same bytes,
    so script confidence and an encoding/language plausibility bonus are compared.
    """
    if not raw:
        return None, 0.0, None
    candidates: list[tuple[str, str]]=[]
    if raw.startswith(b'\xef\xbb\xbf'):
        try: candidates.append((raw.decode('utf-8-sig'),'UTF-8 BOM'))
        except UnicodeDecodeError: pass
    elif raw.startswith((b'\xff\xfe',b'\xfe\xff')):
        try: candidates.append((raw.decode('utf-16'),'UTF-16'))
        except UnicodeDecodeError: pass
    else:
        for cut in (0,1,2,3):
            try:
                candidates.append((raw[:len(raw)-cut].decode('utf-8'),'UTF-8')); break
            except UnicodeDecodeError:
                continue
        if raw.count(0)/max(1,len(raw)) >= 0.08:
            for enc,label in (('utf-16-le','UTF-16 LE'),('utf-16-be','UTF-16 BE')):
                try: candidates.append((raw.decode(enc),label))
                except UnicodeDecodeError: pass
        for enc,label in (('cp932','CP932'),('euc_jp','EUC-JP'),('gb18030','GB18030'),('cp950','Big5'),('big5hkscs','Big5'),('cp1252','Windows-1252')):
            # A preflight sample may end in the middle of a multi-byte character.
            for cut in (0,1,2,3):
                try:
                    candidates.append((raw[:len(raw)-cut].decode(enc),label)); break
                except (UnicodeDecodeError,LookupError):
                    continue
    best=(None,0.0,None,-1.0)
    for text,label in candidates:
        lang,conf=detect_language(text)
        if not lang:
            continue
        bonus=0.0
        if lang=='ja' and label in {'CP932','EUC-JP'}: bonus=0.12
        elif lang=='zh' and label in {'GB18030','Big5'}: bonus=0.10
        elif lang=='en' and label in {'UTF-8','UTF-8 BOM','Windows-1252'}: bonus=0.08
        score=min(1.0,conf+bonus)
        if score>best[3]: best=(lang,min(0.99,score),label,score)
    return best[0],best[1],best[2]


# ---------------------------------------------------------------- already-damaged text
# A file that was once opened with the wrong encoding and saved again keeps the wrong
# characters for good (깨진 글자): GBK Chinese read as Korean CP949 shows Hangul mixed
# with odd Hanja, and so on. Decoding cannot fix that, but it can be noticed before a
# translation (and the glossary built from it) is spent on garbage.
from .textio import _COMMON_ZH, _COMMON_JA, _COMMON_JA_KANJI

_HANGUL = re.compile(r'[가-힣]')
_HAN = re.compile(r'[一-龥]')
_KANA = re.compile(r'[ぁ-ゟァ-ヺ]')
_LATIN_SUP = re.compile(r'[À-ÿ]')
_EN_MOJIBAKE = re.compile(r'â€|Ã[ -¿]|Â[ -¿]')

# (wrongly read as, really was): the usual ways a CJK/English file gets damaged.
_REPAIRS = (('cp949', 'gb18030'), ('cp949', 'cp950'), ('cp949', 'cp932'),
            ('gb18030', 'cp950'), ('cp950', 'gb18030'), ('gb18030', 'cp932'), ('cp932', 'gb18030'),
            ('cp1252', 'utf-8'), ('latin-1', 'gb18030'), ('latin-1', 'cp932'), ('latin-1', 'cp950'))


def _plausibility(text: str, lang: str) -> float:
    """Share of very common characters among the CJK characters (0..1): real prose is
    high, characters produced by a wrong encoding are almost never the common ones."""
    if lang == 'ja':
        cjk = len(_HAN.findall(text)) + len(_KANA.findall(text))
        good = len(_KANA.findall(text)) + sum(ch in _COMMON_JA_KANJI for ch in text)
    else:
        cjk = len(_HAN.findall(text))
        good = sum(ch in _COMMON_ZH for ch in text)
    return good / cjk if cjk >= 200 else 0.0


def damaged_text_reason(text: str, expected: str | None) -> str:
    """'' for normal text; otherwise one sentence saying the text looks already broken.

    Only reports when the evidence is strong: a usual wrong-encoding pattern is present
    AND undoing that mistake on a sample gives far more natural text. Normal Chinese,
    Japanese, English or Korean books never match both, so this never warns on them."""
    if not text or expected not in {'zh', 'ja', 'en'}:
        return ''
    sample = text[:60000]
    if expected == 'en':
        hits = len(_EN_MOJIBAKE.findall(sample))
        if hits >= 20 and hits * 3000 >= len(sample):
            return '영어 원문의 따옴표·기호가 â€™ 같은 깨진 글자로 저장되어 있습니다 (UTF-8 파일을 다른 인코딩으로 열었다가 다시 저장한 흔적).'
        return ''
    hangul = len(_HANGUL.findall(sample)); han = len(_HAN.findall(sample)); kana = len(_KANA.findall(sample))
    latin = len(_LATIN_SUP.findall(sample))
    probe = sample[:20000]
    before = _plausibility(probe, expected)
    # The visible sign of each usual mistake decides which undo is tried. Every sign only
    # leads to a check: the warning needs the undone sample to read far more naturally.
    if hangul >= 100:
        kind, wrongs = 'korean', {'cp949'}
    elif latin >= 200 and latin >= 2 * (han + kana):
        kind, wrongs = 'latin', {'cp1252', 'latin-1'}
    elif han >= 500 and before < 0.10:
        kind, wrongs = 'cjk', {'gb18030', 'cp950', 'cp932'}
    else:
        return ''
    best = 0.0
    for wrong, real in _REPAIRS:
        if wrong not in wrongs:
            continue
        try:
            fixed = probe.encode(wrong, 'replace').decode(real, 'replace')
        except (LookupError, UnicodeError):
            continue
        # Judge the undone text in the language its real encoding is for: junk decoded as
        # GBK can contain kana (GBK has a kana block), which must not look like Japanese.
        target = {'gb18030': 'zh', 'cp950': 'zh', 'cp932': 'ja'}.get(real, expected)
        best = max(best, _plausibility(fixed, target))
    if best < 0.12 or best < before * 3:
        return ''
    tail = '잘못 열었다가 다시 저장해 이미 글자가 깨진 파일로 보입니다.'
    if kind == 'korean':
        return '원문에 한글과 엉뚱한 한자가 뒤섞여 있습니다. 중국어·일본어 파일을 한국어 인코딩으로 ' + tail
    if kind == 'latin':
        return '원문이 À·Ã 같은 서유럽 글자로 깨져 있습니다. 중국어·일본어 파일을 서유럽 인코딩으로 ' + tail
    return '원문 한자가 뜻이 통하지 않는 글자들로 되어 있습니다. 간체(GBK)·번체(Big5)·일본어 파일을 서로 다른 인코딩으로 ' + tail
