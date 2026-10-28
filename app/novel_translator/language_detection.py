from __future__ import annotations
import re
from .textio import DETECT_CANDIDATES, _sniff_utf16_cjk


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
    # CJK bytes misread as Windows-1252 also give many A-Z letters (their second bytes), but
    # with accented letters for nearly every other byte: real English / European text has few.
    accented=len(re.findall(r'[\u00c0-\u024f]',sample))
    if latin >= 30 and (han+kana)/max(1,total) < 0.08 and accented < latin*0.3:
        return 'en', min(0.99,0.70+latin/max(1,total)*0.25)
    return None,0.0


_KO_COMMON=set('이다는의에고을를가한하지서로으기도사대수리자어아나게그니인일해있시내었것들정면만보라거요말았주우소오전')


def detect_language_bytes(raw: bytes) -> tuple[str|None, float, str|None]:
    """Detect language from legacy TXT bytes without trusting the currently selected profile.

    This is only a preflight warning aid; it never changes the user's selected
    language automatically. Multiple legacy encodings can decode the same bytes,
    so script confidence and an encoding/language plausibility bonus are compared.
    """
    if not raw:
        return None, 0.0, None
    candidates: list[tuple[str, str]]=[]
    damaged: list[tuple[str, str, float]]=[]
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
        else:
            # UTF-8 damaged in many places (lines cut at a fixed byte count): the same rule as reading
            loose=raw.decode('utf-8','replace'); bad=loose.count('\ufffd'); good=sum(1 for ch in loose if ord(ch)>0x7f and ch!='\ufffd')
            if good>=50 and bad*4<=good: candidates.append((loose,'UTF-8'))
        u16=_sniff_utf16_cjk(raw[:len(raw)//2*2])
        if u16:
            try: candidates.append((raw[:len(raw)//2*2].decode(u16,'replace'),'UTF-16'))
            except LookupError: pass
        if raw.count(0)/max(1,len(raw)) >= 0.08:
            for enc,label in (('utf-16-le','UTF-16 LE'),('utf-16-be','UTF-16 BE')):
                try: candidates.append((raw.decode(enc),label))
                except UnicodeDecodeError: pass
        if not candidates:
            # Korean CP949 (EUC-KR) text is no translation source: do not call it Chinese or
            # English because its bytes also read as GB18030 / Windows-1252.
            for cut in (0,1):
                try: ko=raw[:len(raw)-cut].decode('cp949')
                except UnicodeDecodeError: continue
                wide=[ch for ch in ko[:120000] if ord(ch)>0x7f]
                # the most frequent Korean syllables: GBK / Big5 bytes read as CP949 also give
                # Hangul, but random rare syllables
                if len(wide)>=20 and sum(ch in _KO_COMMON for ch in wide)>=len(wide)*0.12:
                    return None, 0.0, None
                break
        for enc,label in DETECT_CANDIDATES:
            # A preflight sample may end in the middle of a multi-byte character.
            for cut in (0,1,2,3):
                try:
                    candidates.append((raw[:len(raw)-cut].decode(enc),label)); break
                except (UnicodeDecodeError,LookupError):
                    continue
            else:
                # A file cut at fixed byte counts: the damaged reading, if only a few places break
                # (not Windows-1252: it reads almost any bytes, so failing means it is not that)
                if enc=='cp1252': continue
                try: t=raw.decode(enc,'replace')
                except LookupError: continue
                bad=t.count('\ufffd'); wide=sum(1 for ch in t if ord(ch)>0x7f)
                if 0<bad<=wide*0.05: damaged.append((t,label,bad/max(1,wide)))
    best=(None,0.0,None,-1.0)
    for text,label,penalty in [(t,l,0.0) for t,l in candidates]+[(t,l,r*4) for t,l,r in damaged]:
        lang,conf=detect_language(text)
        if not lang:
            continue
        bonus=0.0
        if lang=='ja' and label in {'CP932','EUC-JP'}: bonus=0.12
        elif lang=='zh' and label in {'GB18030','Big5'}: bonus=0.10
        elif lang=='en' and label in {'UTF-8','UTF-8 BOM','Windows-1252'}: bonus=0.08
        score=min(1.0,conf+bonus)-penalty   # a damaged reading never beats an equally good clean one
        if score>best[3]: best=(lang,min(0.99,score),label,score)
    return best[0],best[1],best[2]

