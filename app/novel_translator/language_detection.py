from __future__ import annotations
import re


def detect_language(text: str) -> tuple[str|None, float]:
    sample=text[:120000]
    kana=len(re.findall(r'[ぁ-ゟ゠-ヿ]',sample))
    han=len(re.findall(r'[一-龥]',sample))
    latin=len(re.findall(r'[A-Za-z]',sample))
    total=kana+han+latin
    if total < 20:return None,0.0
    if kana >= 12 and kana/max(1,total) >= 0.05:
        return 'ja', min(0.99,0.65+kana/max(1,total))
    if han >= 20 and kana == 0 and han/max(1,total) >= 0.30:
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
