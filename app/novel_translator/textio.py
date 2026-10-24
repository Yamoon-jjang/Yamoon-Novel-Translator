from __future__ import annotations
from pathlib import Path
import re

_COMMON_ZH = set('的一是不了人我在有他她这這为為之大来來上个個国國到说說们們地也子时時道出而要于於就下得可你年生自会會着著去之过過家学學对對能多天小')
_COMMON_JA = set('のにをがはとでしもかたてないるれからですますというそのこのあっへ')

import codecs

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
    return score

def decode_txt_bytes(raw: bytes, source_language: str | None = None, forced_encoding: str | None = None) -> tuple[str, str]:
    if forced_encoding and forced_encoding.lower() not in {'auto','자동'}:
        aliases = {
            'utf-8':'utf-8-sig', 'utf-8-sig':'utf-8-sig',
            'utf-16-le':'utf-16-le', 'utf-16-be':'utf-16-be',
            'cp932':'cp932', 'shift_jis':'cp932', 'sjis':'cp932', 'euc_jp':'euc_jp', 'euc-jp':'euc_jp',
            'gb18030':'gb18030', 'gbk':'gb18030', 'big5':'cp950', 'cp950':'cp950', 'big5hkscs':'big5hkscs',
            'cp1252':'cp1252', 'windows-1252':'cp1252',
        }
        enc=aliases.get(forced_encoding.lower(), forced_encoding)
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

    if source_language == 'ja':
        encs = [('cp932','CP932 / Shift-JIS'), ('euc_jp','EUC-JP'), ('shift_jis','Shift-JIS'), ('gb18030','GB18030'), ('cp950','Big5')]
    elif source_language == 'zh':
        # GB18030 is a superset used for modern Simplified Chinese. Traditional Chinese files are
        # Windows Big5 (CP950: has 裏 恒 碁 € box drawing … that strict Big5 lacks) or Big5-HKSCS.
        encs = [('gb18030','GB18030 / GBK'), ('cp950','Big5'), ('big5hkscs','Big5-HKSCS'), ('cp932','CP932')]
    elif source_language == 'en':
        encs = [('cp1252','Windows-1252'), ('latin-1','Latin-1')]
    else:
        encs = [('cp932','CP932 / Shift-JIS'), ('euc_jp','EUC-JP'), ('gb18030','GB18030 / GBK'), ('cp950','Big5'), ('big5hkscs','Big5-HKSCS'), ('cp1252','Windows-1252')]
    for enc, label in encs:
        try: candidates.append((raw.decode(enc, 'nt_cp1252_fallback' if enc == 'cp1252' else 'strict'), label))
        except (UnicodeDecodeError, LookupError): pass
    if not candidates and source_language in {'zh', 'ja'}:
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
