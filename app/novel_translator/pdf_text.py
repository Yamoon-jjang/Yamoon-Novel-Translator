"""Text from PDF files, for translation.

A PDF only stores text as positioned lines. This module takes the text out with pypdf
(BSD-3-Clause, pure Python) and rebuilds plain novel text: repeated page headers and
footers and page numbers are removed, lines broken by the page width are joined back
into paragraphs, and chapter titles stay on their own lines. The result is ordinary
text, so everything after opening (chunking, translation, resume, saving) is the same
as for a TXT file.

The original layout is not kept: a PDF is opened for its words, not its look.
A PDF without any text (scanned pages) is reported as needing OCR; no OCR is done.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
import re
import unicodedata

PDF_SUFFIX = '.pdf'


from .source_formats import SourceTextError, NoTextError


class PdfTextError(SourceTextError):
    """A PDF whose text cannot be used. The message is shown to the user as is."""


class PdfNeedsOcrError(PdfTextError, NoTextError):
    """A PDF that holds pictures of pages (scanned) instead of text."""


OCR_MESSAGE = ('문자 인식(OCR)이 필요한 PDF입니다. 이 PDF에는 글자 정보가 없어(스캔한 그림 PDF) '
               '글자를 꺼낼 수 없습니다. 문자 인식(OCR) 프로그램으로 글자가 들어간 PDF나 TXT를 만든 뒤 열어 주세요.')


@dataclass
class PdfText:
    text: str
    pages: int            # pages in the file
    text_pages: int       # pages that gave text
    removed_lines: int    # header/footer/page-number lines removed

    @property
    def label(self) -> str:
        note = f'PDF {self.pages:,}쪽 · 글자 추출'
        empty = self.pages - self.text_pages
        if empty > 0:
            note += f' · 글자 없는 쪽 {empty:,}개(그림·스캔일 수 있음)'
        return note


def is_pdf(path) -> bool:
    try:
        return Path(path).suffix.casefold() == PDF_SUFFIX
    except Exception:
        return False


# ------------------------------------------------------------------ reading

def _reader(path: Path):
    try:
        from pypdf import PdfReader
    except Exception as e:   # pragma: no cover - packaging error
        raise PdfTextError(f'PDF 읽기 부품(pypdf)을 불러오지 못했습니다: {e}')
    logging.getLogger('pypdf').setLevel(logging.ERROR)   # damaged-file warnings are handled here
    try:
        reader = PdfReader(str(path), strict=False)
    except Exception as e:
        raise PdfTextError(f'PDF 파일을 읽지 못했습니다 (손상되었거나 PDF가 아닌 파일). {type(e).__name__}: {e}')
    if reader.is_encrypted:
        ok = False
        try:
            ok = bool(reader.decrypt(''))   # many PDFs only restrict printing/copying (no open password)
        except Exception:
            ok = False
        if not ok:
            raise PdfTextError('암호(보안)가 걸린 PDF라 글자를 꺼낼 수 없습니다. '
                               'PDF 보기 프로그램에서 암호를 푼 PDF로 다시 저장한 뒤 열어 주세요.')
    return reader


_GUTTER = re.compile(r'\S {6,}\S')


def _two_columns(text: str) -> bool:
    """Layout text of a page set in columns: most lines have a wide gap in the middle."""
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    return len(lines) >= 6 and sum(1 for l in lines if _GUTTER.search(l)) >= len(lines) * 0.4


def _page_text(page) -> str:
    """One page's text, line by line as it is printed.

    pypdf's layout mode rebuilds lines from the glyph positions, so a line drawn in
    several pieces (font fallback, kerning runs) stays one line and word spaces drawn as
    gaps are kept. Its plain mode follows the content stream instead; that is used when
    the page is set in columns (layout would interleave them) or when layout mode loses
    text or fails.
    """
    plain = page.extract_text() or ''
    try:
        layout = page.extract_text(extraction_mode='layout') or ''
    except Exception:
        return plain
    if _letters(layout) < _letters(plain) * 0.98 or _two_columns(layout):
        return plain
    return _even_out_line_gaps(layout)


def _even_out_line_gaps(text: str) -> str:
    """Layout mode turns wide line spacing into empty lines between all lines. The most
    common gap is the normal line spacing (no break); only a wider gap stays a break."""
    lines = text.split('\n')
    rows: list[tuple[int, str]] = []    # (empty lines before, line)
    empty = 0
    for line in lines:
        if line.strip():
            rows.append((empty, line)); empty = 0
        else:
            empty += 1
    gaps = [g for g, _ in rows[1:]]
    if not gaps:
        return text
    usual = max(set(gaps), key=gaps.count)
    if usual == 0:
        return text
    out = []
    for i, (g, line) in enumerate(rows):
        if i and g > usual:
            out.append('')
        out.append(line)
    return '\n'.join(out)


def read_pdf_pages(path, max_pages: int | None = None, progress=None) -> tuple[list[str], int]:
    """Raw text of each page (in order) and the file's page count.

    progress(done, total) is called after each page (the window keeps responding with it).
    """
    reader = _reader(Path(path))
    try:
        total = len(reader.pages)
    except Exception as e:
        raise PdfTextError(f'PDF 쪽 정보를 읽지 못했습니다. {type(e).__name__}: {e}')
    if total == 0:
        raise PdfTextError('쪽이 없는 PDF입니다.')
    pages: list[str] = []
    limit = total if max_pages is None else min(total, max(1, int(max_pages)))
    unreadable = 0
    for i in range(limit):
        try:
            pages.append(_page_text(reader.pages[i]))
        except Exception as e:
            if 'cryptography' in str(e) or 'AES' in str(e):
                raise PdfTextError('암호화 방식(AES)이 걸린 PDF라 글자를 꺼낼 수 없습니다. '
                                   'PDF 보기 프로그램에서 보안을 해제한 PDF로 다시 저장한 뒤 열어 주세요.')
            unreadable += 1
            pages.append('')   # one damaged page must not lose the whole book
        if progress is not None:
            try:
                progress(i + 1, limit)
            except Exception:
                pass
    if unreadable == limit:
        raise PdfTextError('PDF의 어느 쪽에서도 글자를 읽지 못했습니다 (손상된 PDF일 수 있음).')
    return pages, total


# ------------------------------------------------------------------ character classes

_CJK = (r'\u2e80-\u2fdf\u3000-\u303f\u3040-\u30ff\u3100-\u312f\u31f0-\u31ff\u3400-\u4dbf'
        r'\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef')   # not Hangul: Korean uses word spaces
_CJK_RE = re.compile(f'[{_CJK}]')
_CJK_GAP = re.compile(f'(?<=[{_CJK}])[ \\t]+(?=[{_CJK}])')
_LETTER = re.compile(r'[^\W\d_]', re.UNICODE)

_END_PUNCT = set('。．.!！?？…‥」』”’"\'）)】〉》〕］]～~：:;；—―·')
_SENTENCE_END = set('。．.!！?？…」』”"')
_OPEN_QUOTE = set('「『“‘"\'【〈《〔［[―—')


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in 'WF' else 1 for ch in s)


def _letters(s: str) -> int:
    return len(_LETTER.findall(s or ''))


# ------------------------------------------------------------------ titles & page numbers

_CH_NUM = r'[0-9０-９一二三四五六七八九十百千万零〇两兩壹贰貳叁參肆伍陆陸柒捌玖拾佰]+'
_CHAPTER_RE = re.compile(
    r'^\s*(?:'
    rf'第\s*{_CH_NUM}\s*[章节節回话話卷部篇幕集季]'
    r'|(?:序章|序幕|楔子|引子|序言|前言|尾声|尾聲|终章|終章|后记|後記|番外|外传|外傳|完结|完結|最终章|最終章)'
    r'|(?:プロローグ|エピローグ|あとがき|まえがき|幕間|間章)'
    r'|(?:chapter|prologue|epilogue|interlude|afterword|foreword|part|book|volume|act)\b'
    r'|(?:제\s*\d+\s*[장화부권편막회]|\d+\s*[장화]\b|프롤로그|에필로그|서장|종장|외전|후기|작가의\s*말|막간)'
    r')', re.IGNORECASE)


def is_chapter_title(line: str) -> bool:
    s = (line or '').strip()
    if not s or len(s) > 40 or not _CHAPTER_RE.match(s):
        return False
    # "第三章他走了很远的路。" style sentence: a title does not end like a sentence.
    return not (len(s) > 20 and s[-1] in '。．.!！?？')


_PAGE_NUM_RES = [
    re.compile(r'^[-–—~·\s]*\d{1,5}[-–—~·\s]*$'),
    re.compile(r'^(?:page|p\.?|pg\.?)\s*\d{1,5}(?:\s*(?:of|/)\s*\d{1,5})?$', re.IGNORECASE),
    re.compile(r'^\d{1,5}\s*(?:/|of)\s*\d{1,5}$', re.IGNORECASE),
    re.compile(rf'^第?\s*{_CH_NUM}\s*[页頁]$'),
    re.compile(r'^\d{1,5}\s*(?:ページ|쪽|페이지)$'),
    # front matter: a lowercase roman numeral (a word such as "mild" is not one)
    re.compile(r'^[-–—\s]*(?=[ivxlcdm])m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})[-–—\s]*$'),
]


def _is_page_number(line: str) -> bool:
    s = line.strip()
    return bool(s) and any(r.match(s) for r in _PAGE_NUM_RES)


def _edge_key(line: str) -> str:
    s = re.sub(r'\d+', '#', line.strip())
    return re.sub(r'\s+', ' ', s)


# ------------------------------------------------------------------ cleaning

_CONTROLS = re.compile(r'[\x01-\x08\x0b\x0e-\x1f\x7f]')
_PUA = re.compile(r'[\ue000-\uf8ff]')
_RADICALS = re.compile(r'[\u2e80-\u2eff\u2f00-\u2fdf]')
# Radicals Supplement characters have no compatibility mapping; their ideographs (Unihan):
_RADICAL_SUPPLEMENT = {0x2E9B: '旡', 0x2E9F: '母', 0x2EA0: '民', 0x2EA4: '爫', 0x2EA9: '王', 0x2EAC: '示',
    0x2EAE: '竹', 0x2EB6: '羊', 0x2EBC: '月', 0x2EC4: '西', 0x2EC5: '见', 0x2EC6: '角', 0x2EC8: '讠',
    0x2EC9: '贝', 0x2ECB: '车', 0x2ED1: '長', 0x2ED3: '长', 0x2ED4: '门', 0x2ED8: '青', 0x2EDB: '风',
    0x2EDC: '飞', 0x2EDD: '食', 0x2EE0: '饣', 0x2EE2: '马', 0x2EE4: '鬼', 0x2EE5: '鱼', 0x2EE6: '鸟',
    0x2EE7: '卤', 0x2EE8: '麦', 0x2EE9: '黄', 0x2EEB: '斉', 0x2EEC: '齐', 0x2EEE: '齿', 0x2EEF: '竜',
    0x2EF0: '龙', 0x2EF2: '亀', 0x2EF3: '龟'}


def _radical(ch: str) -> str:
    return _RADICAL_SUPPLEMENT.get(ord(ch)) or unicodedata.normalize('NFKC', ch)

_EDGE = 3   # lines at the top and the bottom of a page (pypdf may put a footer first)


def _normalize_page(text: str) -> list[str]:
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\x00', '').replace('\x0c', '\n').replace('\u00ad', '').replace('\u00a0', ' ').replace('\t', ' ')
    # Some PDF writers (Qt among them) give the space glyph a control code with no
    # character map entry: it is read as a space. Stray private-use codes are dropped,
    # and Kangxi radicals that fonts share with ideographs (⼭ for 山) become the ideograph.
    text = _CONTROLS.sub(' ', text)
    text = _PUA.sub('', text)
    text = _RADICALS.sub(lambda m: _radical(m.group()), text)
    lines = []
    for line in text.split('\n'):
        line = line.rstrip()
        if _CJK_RE.search(line):
            # Some PDFs give "这 是 一 个" (a space after every character): undo that, but
            # keep a real space such as the one in "第一章 出征".
            gaps = len(_CJK_GAP.findall(line))
            if gaps >= 3 and gaps * 3 >= len(_CJK_RE.findall(line)):
                line = _CJK_GAP.sub('', line)
        lines.append(line)
    return lines


def _edge_positions(lines: list[str]) -> list[int]:
    idx = [i for i, l in enumerate(lines) if l.strip()]
    if len(idx) <= 2 * _EDGE:
        return idx
    return idx[:_EDGE] + idx[-_EDGE:]


def _remove_page_furniture(pages: list[list[str]]) -> tuple[list[list[str]], int]:
    """Drop page numbers and lines repeated at the page edges (running headers/footers)."""
    removed = 0
    n = len(pages)
    typical = _typical_width([l for lines in pages for l in lines])
    counts: dict[tuple[str, str], int] = {}
    if n >= 3:
        for lines in pages:
            seen = set()
            idx = [i for i, l in enumerate(lines) if l.strip()]
            for side, picks in (('top', idx[:_EDGE]), ('bottom', idx[-_EDGE:])):
                for i in picks:
                    s = lines[i].strip()
                    # Running headers/footers are short; a full body line never counts.
                    if typical and _width(s) > typical * 0.7:
                        continue
                    if s[-1:] in _SENTENCE_END:   # a header is a name/title, not a sentence
                        continue
                    k = (side, _edge_key(s))
                    if k[1] and _letters(k[1]) and k not in seen:   # symbol-only lines are not headers
                        seen.add(k); counts[k] = counts.get(k, 0) + 1
    need = max(3, int(n * 0.4 + 0.999))
    repeated = {k for (_side, k), c in counts.items() if c >= need}
    kept_titles: set[str] = set()
    out = []
    for lines in pages:
        lines = list(lines)
        drop = set()
        for i in _edge_positions(lines):
            s = lines[i].strip()
            if _is_page_number(s):
                drop.add(i); continue
            if _edge_key(s) in repeated and not (typical and _width(s) > typical * 0.7) and s[-1:] not in _SENTENCE_END:
                # A running header that repeats the chapter title: the first time it is the
                # real chapter heading, so that occurrence stays.
                if is_chapter_title(s) and s not in kept_titles:
                    kept_titles.add(s); continue
                drop.add(i)
        removed += len(drop)
        out.append([l for i, l in enumerate(lines) if i not in drop])
    return out, removed


def _typical_width(lines: list[str]) -> int:
    widths = sorted(_width(l.strip()) for l in lines if _width(l.strip()) >= 8)
    if len(widths) < 5:
        return 0
    return widths[int(len(widths) * 0.8)]


def _join(a: str, b: str) -> str:
    if not a:
        return b
    a_last, b_first = a[-1], b[:1]
    if _CJK_RE.match(a_last) or _CJK_RE.match(b_first or ' '):
        return a + b
    if a_last == '-' and len(a) >= 2 and a[-2].isalpha() and b_first.islower():
        return a[:-1] + b                     # hyphenated word split across lines
    return a + ' ' + b


def _paragraphs(lines: list[str]) -> list[str]:
    """Join visual lines back into paragraphs."""
    content = [l for l in lines if l is not None]
    typical = _typical_width(content)
    starts = [l for l in content if l.strip()]
    indented = sum(1 for l in starts if l[:1] in (' ', '\t', '\u3000'))
    # Indentation is a reliable paragraph mark only when the document actually uses it.
    use_indent = bool(starts) and indented >= max(2, len(starts) * 0.08)
    paras: list[str] = []
    cur = ''
    prev_raw = ''
    blank = True
    for raw in content:
        s = raw.strip()
        if not s:
            if cur:
                paras.append(cur); cur = ''
            blank = True
            continue
        title = is_chapter_title(s)
        new = blank or title or not cur
        if not new:
            p = prev_raw.strip()
            p_end = p[-1:] in _END_PUNCT
            if is_chapter_title(p):
                new = True
            elif use_indent and raw[:1] in (' ', '\t', '\u3000'):
                new = True
            elif s[:1] in _OPEN_QUOTE and p_end:
                new = True
            elif typical and _width(p) < typical * (0.85 if p_end else 0.6):
                new = True                   # a short line ends its paragraph
            elif not typical and p_end and not use_indent:
                new = True
        if new and cur:
            paras.append(cur); cur = ''
        cur = _join(cur, s) if cur else s
        if title:
            paras.append(cur); cur = ''
            blank = True
        else:
            blank = False
        prev_raw = raw if not title else ''
    if cur:
        paras.append(cur)
    return paras


def clean_pdf_pages(pages: list[str]) -> tuple[str, int]:
    """Plain text from raw page texts, and how many header/footer lines were removed."""
    norm = [_normalize_page(p) for p in pages]
    norm, removed = _remove_page_furniture(norm)
    lines: list[str] = []
    for page in norm:
        # A page break is not a paragraph break: text continues on the next page, so blank
        # lines at the page edges are dropped.
        while page and not page[0].strip():
            page = page[1:]
        while page and not page[-1].strip():
            page = page[:-1]
        lines.extend(page)
    paras = _paragraphs(lines)
    out: list[str] = []
    for p in paras:
        p = re.sub(r'[ \t]{2,}', ' ', p).strip()
        if p:
            out.append(p)
    return '\n\n'.join(out) + ('\n' if out else ''), removed


def _looks_garbled(text: str) -> bool:
    """Fonts without a character map give private-use or replacement characters."""
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return False
    bad = sum(1 for ch in chars if ch == '\ufffd' or 0xe000 <= ord(ch) <= 0xf8ff)
    return bad / len(chars) > 0.3


def extract_pdf_text(path, max_pages: int | None = None, progress=None) -> PdfText:
    """Readable text of a PDF. Raises PdfNeedsOcrError for scanned PDFs, PdfTextError otherwise."""
    pages, total = read_pdf_pages(path, max_pages, progress)
    letters = [_letters(p) for p in pages]
    text_pages = sum(1 for n in letters if n >= 10)
    any_text_pages = sum(1 for n in letters if n > 0)
    all_letters = sum(letters)
    joined = '\n'.join(pages)
    if all_letters < 5 or (len(pages) >= 3 and text_pages <= len(pages) * 0.1 and all_letters < len(pages) * 20):
        raise PdfNeedsOcrError(OCR_MESSAGE)
    if _looks_garbled(joined):
        raise PdfNeedsOcrError('문자 인식(OCR)이 필요한 PDF입니다. 이 PDF의 글꼴에는 글자 정보가 없어 꺼낸 글자가 깨집니다. '
                               '문자 인식(OCR) 프로그램으로 글자가 들어간 PDF나 TXT를 만든 뒤 열어 주세요.')
    text, removed = clean_pdf_pages(pages)
    if not text.strip():
        raise PdfNeedsOcrError(OCR_MESSAGE)
    return PdfText(text=text, pages=total, text_pages=any_text_pages + (total - len(pages)), removed_lines=removed)


def pdf_sample_text(path, max_pages: int = 6) -> str:
    """The first pages' text, for a quick language check (empty when there is no text)."""
    try:
        pages, _ = read_pdf_pages(path, max_pages)
        return clean_pdf_pages(pages)[0]
    except Exception:
        return ''
