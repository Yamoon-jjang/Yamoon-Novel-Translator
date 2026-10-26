from __future__ import annotations
from pathlib import Path
import re

_COMMON_ZH = set('的一是不了人我在有他她这這为為之大来來上个個国國到说說们們地也子时時道出而要于於就下得可你年生自会會着著去之过過家学學对對能多天小')
_COMMON_JA = set('のにをがはとでしもかたてないるれからですますというそのこのあっへ')

import codecs

# ---------------------------------------------------------------- encodings (one place)
# Every TXT encoding the app knows is listed here: the 원문 인코딩 box, the names a saved
# setting may use, the order each source language tries, and what language detection
# compares. Other modules import these instead of keeping their own lists.

# The TXT 인코딩 box: (shown name, setting value), in this order.
ENCODING_CHOICES = (
    ('자동 감지', 'auto'), ('UTF-8', 'utf-8'), ('일본어 CP932 / Shift-JIS', 'cp932'),
    ('일본어 EUC-JP', 'euc_jp'), ('중국어 GB18030 / GBK', 'gb18030'),
    ('중국어 번체 Big5 (CP950)', 'cp950'), ('영어 Windows-1252', 'cp1252'),
    ('UTF-16 LE', 'utf-16-le'), ('UTF-16 BE', 'utf-16-be'),
)

# A chosen (manual) encoding name -> Python codec.
ENCODING_ALIASES = {
    'utf-8': 'utf-8-sig', 'utf-8-sig': 'utf-8-sig',
    'utf-16-le': 'utf-16-le', 'utf-16-be': 'utf-16-be',
    'cp932': 'cp932', 'shift_jis': 'cp932', 'sjis': 'cp932', 'euc_jp': 'euc_jp', 'euc-jp': 'euc_jp',
    'gb18030': 'gb18030', 'gbk': 'gb18030', 'big5': 'cp950', 'cp950': 'cp950', 'big5hkscs': 'big5hkscs',
    'cp1252': 'cp1252', 'windows-1252': 'cp1252',
}

# Legacy (non-UTF) encodings tried for each source language, in order: (codec, label).
# GB18030 is a superset used for modern Simplified Chinese. Traditional Chinese files are
# Windows Big5 (CP950: has 裏 恒 碁 € box drawing … that strict Big5 lacks) or Big5-HKSCS.
# Korean novels (refinement) are mostly CP949 (EUC-KR); only the Korean setting tries it,
# because Chinese/Japanese bytes also "decode" as CP949.
LEGACY_CANDIDATES = {
    'ja': (('cp932','CP932 / Shift-JIS'), ('euc_jp','EUC-JP'), ('shift_jis','Shift-JIS'), ('gb18030','GB18030'), ('cp950','Big5')),
    'zh': (('gb18030','GB18030 / GBK'), ('cp950','Big5'), ('big5hkscs','Big5-HKSCS'), ('cp932','CP932')),
    'en': (('cp1252','Windows-1252'), ('latin-1','Latin-1')),
    'ko': (('cp949','CP949 / EUC-KR'), ('cp932','CP932 / Shift-JIS'), ('euc_jp','EUC-JP'), ('gb18030','GB18030 / GBK'), ('cp950','Big5'), ('cp1252','Windows-1252')),
    None: (('cp932','CP932 / Shift-JIS'), ('euc_jp','EUC-JP'), ('gb18030','GB18030 / GBK'), ('cp950','Big5'), ('big5hkscs','Big5-HKSCS'), ('cp1252','Windows-1252')),
}

# What language detection compares for a BOM-less, non-UTF-8 file: (codec, label).
DETECT_CANDIDATES = (('cp932','CP932'), ('euc_jp','EUC-JP'), ('gb18030','GB18030'), ('cp950','Big5'), ('big5hkscs','Big5'), ('cp1252','Windows-1252'))


def saved_encoding(value):
    """A setting saved by an older version (big5) as the value the box uses now."""
    return 'cp950' if value == 'big5' else value

# Very common kanji in Japanese prose: EUC-JP bytes misread as GB18030 keep the kana
# (same byte rows) but turn kanji into rare Chinese characters, so this tells them apart.
_COMMON_JA_KANJI = set('人日一大年出本中子見国言上分生手自行者二間事思時気会十家女三前方入小地合後目長場代私下立部学物月田何来彼話体動社知理山内同心発高実作当新世今書度明五戦力名金性対意用男主通関文屋感業定持道外取所現俺僕君様彼女')


def _cp1252_fallback(err):
    # The five bytes cp1252 leaves undefined (0x81 0x8D 0x8F 0x90 0x9D): keep them as
    # Latin-1 instead of failing the whole file over one stray byte.
    return raw_latin1(err.object[err.start:err.end]), err.end


def raw_latin1(b: bytes) -> str:
    return b.decode('latin-1')


codecs.register_error('nt_cp1252_fallback', _cp1252_fallback)


def _controls_penalty(text: str) -> int:
    # C0 controls and C1 controls (U+0080-U+009F: what cp1252 quotes become when a
    # file is wrongly read as Latin-1).
    return sum(1 for ch in text if (ord(ch) < 32 and ch not in '\r\n\t') or 0x80 <= ord(ch) <= 0x9f)

def _score(text: str, lang: str | None) -> float:
    if not text: return -1e9
    controls = _controls_penalty(text)
    replacement = text.count('\ufffd')
    printable = sum(ch.isprintable() or ch in '\r\n\t' for ch in text)
    score = printable / max(1, len(text)) * 20 - controls * 20 - replacement * 40
    if lang == 'ja':
        kana = len(re.findall(r'[ぁ-ゟ゠-ヿ]', text))
        common = sum(ch in _COMMON_JA for ch in text)
        half = len(re.findall(r'[ｦ-ﾟ]', text))
        kanji = sum(ch in _COMMON_JA_KANJI for ch in text)
        score += kana * 4 + common * 2 + kanji * 3 - half * 1.0
    elif lang == 'zh':
        cjk = len(re.findall(r'[一-龥]', text))
        common = sum(ch in _COMMON_ZH for ch in text)
        kana = len(re.findall(r'[ぁ-ゟ゠-ヿ]', text))
        score += cjk * 0.25 + common * 4 - kana * 4
    elif lang == 'en':
        ascii_letters = len(re.findall(r'[A-Za-z]', text))
        score += ascii_letters * 0.15
    elif lang == 'ko':
        hangul = len(re.findall(r'[가-힣]', text))
        score += hangul * 4
    return score

def decode_txt_bytes(raw: bytes, source_language: str | None = None, forced_encoding: str | None = None) -> tuple[str, str]:
    if forced_encoding and forced_encoding.lower() not in {'auto','자동'}:
        enc=ENCODING_ALIASES.get(forced_encoding.lower(), forced_encoding)
        text=raw.decode(enc)
        return text, f'{forced_encoding} (수동)'

    # BOMs are authoritative.
    if raw.startswith(b'\xef\xbb\xbf'): return raw.decode('utf-8-sig'), 'UTF-8 BOM'
    if raw.startswith(b'\xff\xfe'): return raw.decode('utf-16'), 'UTF-16 LE'
    if raw.startswith(b'\xfe\xff'): return raw.decode('utf-16'), 'UTF-16 BE'

    nul_ratio = raw.count(0) / max(1, len(raw))
    # BOM-less UTF-16 of mostly ASCII text is also valid UTF-8 ("H\x00e\x00…"): check it first.
    if nul_ratio >= 0.3:
        for enc, label in [('utf-16-le','UTF-16 LE (추정)'), ('utf-16-be','UTF-16 BE (추정)')]:
            try:
                text = raw.decode(enc)
            except UnicodeDecodeError:
                continue
            if _controls_penalty(text) <= len(text) * 0.01:
                return text, label

    # UTF-8 should win whenever it is valid; this covers the overwhelming majority of modern TXT files.
    try:
        return raw.decode('utf-8'), 'UTF-8'
    except UnicodeDecodeError:
        pass
    # A UTF-8 file with one or two stray bytes (e.g. a single cp1252 quote pasted in):
    # keep the UTF-8 reading and mark the bad bytes, instead of reading the whole file as Latin-1.
    loose = raw.decode('utf-8', 'replace')
    bad = loose.count('\ufffd'); good = sum(1 for ch in loose if ord(ch) > 0x7f and ch != '\ufffd')
    if bad <= max(2, len(loose) // 5000) and good >= 10 * bad:
        return loose, f'UTF-8 (깨진 바이트 {bad}개 대체)'

    # BOM-less UTF-16 is considered only when NUL-byte structure strongly suggests it.
    candidates: list[tuple[str, str]] = []
    if nul_ratio >= 0.08:
        for enc, label in [('utf-16-le','UTF-16 LE (추정)'), ('utf-16-be','UTF-16 BE (추정)')]:
            try: candidates.append((raw.decode(enc), label))
            except UnicodeDecodeError: pass

    encs = LEGACY_CANDIDATES.get(source_language, LEGACY_CANDIDATES[None])
    for enc, label in encs:
        try: candidates.append((raw.decode(enc, 'nt_cp1252_fallback' if enc == 'cp1252' else 'strict'), label))
        except (UnicodeDecodeError, LookupError): pass
    if not candidates and source_language in {'zh', 'ja', 'ko'}:
        # One stray or cut-off byte in an otherwise valid CJK file: keep that reading and
        # mark the bad bytes, rather than failing or switching to a Western encoding.
        loose = []
        for enc, label in encs:
            try:
                t = raw.decode(enc, 'replace')
            except LookupError:
                continue
            bad = t.count('\ufffd')
            if bad <= max(3, len(t) // 5000):
                loose.append((t, f'{label} (깨진 바이트 {bad}개 대체)'))
        if loose:
            return max(loose, key=lambda x: _score(x[0], source_language))
    if not candidates and source_language is not None and sum(b < 0x80 for b in raw) >= 0.9 * len(raw):
        return decode_txt_bytes(raw, None)   # mostly ASCII (an English file under another language setting)
    if not candidates:
        raise UnicodeError('TXT 인코딩을 판별하지 못했습니다.')
    text, label = max(candidates, key=lambda x: _score(x[0], source_language))
    return text, label


# ---------------------------------------------------------------- already-damaged text
# A file that was once opened with the wrong encoding and saved again keeps the wrong
# characters for good (깨진 글자): GBK Chinese read as Korean CP949 shows Hangul mixed
# with odd Hanja, and so on. Decoding cannot fix that, but it can be noticed before a
# translation (and the glossary built from it) is spent on garbage.

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

def read_txt_sample(path: Path, max_bytes: int = 262144) -> bytes:
    """Read only a bounded prefix for lightweight preflight/language detection.

    The real translation path still reads the complete file inside the worker.
    """
    with Path(path).open('rb') as f:
        return f.read(max(1024, int(max_bytes)))

def read_txt(path: Path, source_language: str | None = None) -> tuple[str, str]:
    return decode_txt_bytes(path.read_bytes(), source_language)


def read_source_file(path: Path, source_language: str | None = None, forced_encoding: str | None = None, progress=None, cache_dir=None) -> tuple[str, str, bytes]:
    """Text, reading label and source bytes of a file to translate.

    A TXT is decoded exactly as before. A PDF's or EPUB's text is taken out and cleaned
    up (pdf_text / epub_text); its UTF-8 bytes then stand for the file everywhere the TXT
    bytes do (resume identity, language detection), so the rest of the job is the same
    as for TXT. The TXT encoding setting does not apply to a PDF or EPUB.
    """
    from .source_formats import document_kind, extract_document
    path = Path(path)
    if document_kind(path):
        result = extract_document(path, progress, cache_dir=cache_dir)
        return result.text, result.label, result.text.encode('utf-8')
    raw = path.read_bytes()
    text, label = decode_txt_bytes(raw, source_language, forced_encoding)
    return text, label, raw


def directory_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe=path/'.novel_translator_write_probe'
        probe.write_bytes(b'ok'); probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def atomic_write_text(path: Path, text: str, encoding: str='utf-8-sig', newline: str='\r\n', backup_existing: bool=False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    # Normalize internally then let TextIO perform requested newline conversion.
    normalized=text.replace('\r\n','\n').replace('\r','\n')
    with tmp.open('w',encoding=encoding,newline=newline) as f:
        f.write(normalized); f.flush();
        import os; os.fsync(f.fileno())
    if backup_existing and path.exists():
        try:
            old=path.read_bytes(); new=tmp.read_bytes()
            if old!=new:
                from datetime import datetime
                stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
                backup=path.with_name(path.name+f'.before_{stamp}.bak')
                if not backup.exists(): backup.write_bytes(old)
        except Exception:
            pass
    tmp.replace(path)
