"""Text from EPUB files, for translation (Python standard library only).

An EPUB is a zip of XHTML pages. The pages are read in the book's reading order (the
OPF spine) and turned into plain text: one paragraph per block (p, div, li ...),
headings as their own lines, ruby readings (furigana) and scripts left out. The book's
look (styles, images) is not kept. The result is ordinary text, so translating it is
the same as translating a TXT.
"""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

from .source_formats import SourceTextError, NoTextError

MAX_MEMBER_BYTES = 64 * 1024 * 1024      # one page; a zip bomb must not fill the memory
MAX_TOTAL_BYTES = 512 * 1024 * 1024


class EpubTextError(SourceTextError):
    """An EPUB whose text cannot be used. The message is shown to the user as is."""


class EpubNoTextError(EpubTextError, NoTextError):
    """An EPUB made only of pictures (comics, scanned books)."""


NO_TEXT_MESSAGE = ('글자가 없는 EPUB입니다 (그림으로만 된 책·만화 등). 번역할 글자가 없어 열지 않았습니다. '
                   '문자 인식(OCR)으로 글자를 만든 TXT를 쓰세요.')
DRM_MESSAGE = ('복제 방지(DRM)가 걸린 EPUB이라 글자를 꺼낼 수 없습니다. '
               '구매한 서점의 앱에서만 읽을 수 있는 파일입니다.')


@dataclass
class EpubText:
    text: str
    documents: int        # pages (XHTML files) in reading order
    text_documents: int   # pages that gave text

    @property
    def label(self) -> str:
        return f'EPUB {self.documents:,}개 문서 · 글자 추출'


def is_epub(path) -> bool:
    try:
        return Path(path).suffix.casefold() == '.epub'
    except Exception:
        return False


# ------------------------------------------------------------------ zip / package

def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def _read(z: zipfile.ZipFile, name: str, budget: list[int]) -> bytes:
    try:
        info = z.getinfo(name)
    except KeyError:
        # Some EPUBs differ in letter case or percent-encoding between the OPF and the zip.
        low = name.casefold()
        info = next((i for i in z.infolist() if i.filename.casefold() == low
                     or unquote(i.filename).casefold() == low), None)
        if info is None:
            raise KeyError(name)
    if info.file_size > MAX_MEMBER_BYTES:
        raise EpubTextError(f'EPUB 안의 파일 하나가 너무 큽니다 ({info.filename}).')
    budget[0] += info.file_size
    if budget[0] > MAX_TOTAL_BYTES:
        raise EpubTextError('EPUB 안의 글이 너무 커서 열 수 없습니다.')
    with z.open(info) as f:
        data = f.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        raise EpubTextError(f'EPUB 안의 파일 하나가 너무 큽니다 ({info.filename}).')
    return data


def _xml(data: bytes, what: str):
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise EpubTextError(f'EPUB 구조 파일({what})이 손상되었습니다. {e}')


def _spine_documents(z: zipfile.ZipFile, budget: list[int]) -> list[str]:
    """Zip names of the content pages in reading order."""
    try:
        container = _xml(_read(z, 'META-INF/container.xml', budget), 'container.xml')
    except KeyError:
        raise EpubTextError('EPUB 구조 파일(META-INF/container.xml)이 없습니다. 올바른 EPUB이 아닙니다.')
    opf_path = ''
    for el in container.iter():
        if _local(el.tag) == 'rootfile' and el.get('full-path'):
            if (el.get('media-type') or 'application/oebps-package+xml') == 'application/oebps-package+xml':
                opf_path = el.get('full-path'); break
    if not opf_path:
        raise EpubTextError('EPUB의 책 정보 파일(OPF)을 찾지 못했습니다.')
    try:
        opf = _xml(_read(z, opf_path, budget), opf_path)
    except KeyError:
        raise EpubTextError(f'EPUB의 책 정보 파일({opf_path})이 없습니다.')
    base = posixpath.dirname(opf_path)
    manifest: dict[str, tuple[str, str]] = {}
    spine: list[str] = []
    for el in opf.iter():
        tag = _local(el.tag)
        if tag == 'item' and el.get('id') and el.get('href'):
            href = unquote(el.get('href').split('#', 1)[0])
            name = posixpath.normpath(posixpath.join(base, href)) if base else posixpath.normpath(href)
            manifest[el.get('id')] = (name, (el.get('media-type') or '').lower())
        elif tag == 'itemref' and el.get('idref'):
            spine.append(el.get('idref'))
    docs: list[str] = []
    for idref in spine:
        name, media = manifest.get(idref, ('', ''))
        if not name or name in docs:
            continue
        if media in ('application/xhtml+xml', 'text/html') or name.lower().endswith(('.xhtml', '.html', '.htm')):
            docs.append(name)
    if not docs:   # a broken spine: fall back to the manifest's pages in listed order
        docs = [n for n, m in manifest.values()
                if m in ('application/xhtml+xml', 'text/html') or n.lower().endswith(('.xhtml', '.html', '.htm'))]
    return docs


_FONT_OBFUSCATION = {'http://www.idpf.org/2008/embedding', 'http://ns.adobe.com/pdf/enc#RC'}


def _check_drm(z: zipfile.ZipFile, docs: list[str], budget: list[int]) -> None:
    """Encrypted pages mean DRM (font obfuscation alone is harmless)."""
    try:
        data = _read(z, 'META-INF/encryption.xml', budget)
    except KeyError:
        return
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return
    wanted = {d.casefold() for d in docs}
    for enc in root.iter():
        if _local(enc.tag) != 'EncryptedData':
            continue
        algo = ''
        uri = ''
        for el in enc.iter():
            t = _local(el.tag)
            if t == 'EncryptionMethod':
                algo = el.get('Algorithm') or ''
            elif t == 'CipherReference':
                uri = unquote(el.get('URI') or '')
        if algo in _FONT_OBFUSCATION:
            continue
        if posixpath.normpath(uri).casefold() in wanted or uri.lower().endswith(('.xhtml', '.html', '.htm')):
            raise EpubTextError(DRM_MESSAGE)


# ------------------------------------------------------------------ XHTML -> text

_BLOCK = {'p', 'div', 'li', 'blockquote', 'section', 'article', 'aside', 'header', 'footer', 'main',
          'tr', 'dt', 'dd', 'pre', 'figure', 'figcaption', 'table', 'ul', 'ol', 'dl', 'hr', 'nav',
          'address', 'center', 'body'}
_HEAD = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
_SKIP = {'head', 'script', 'style', 'rt', 'rp', 'title', 'template', 'noscript'}
_CJK = (r'\u2e80-\u2fdf\u3000-\u303f\u3040-\u30ff\u3100-\u312f\u31f0-\u31ff\u3400-\u4dbf'
        r'\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef')   # not Hangul: Korean uses word spaces
_CJK_BREAK = re.compile(f'(?<=[{_CJK}])[ \\t\\r]*\\n[ \\t\\r\\n]*(?=[{_CJK}])')
_SPACES = re.compile(r'[ \t\r\n\f\u00a0]+')


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []   # ('title'|'para', text)
        self.buf: list[str] = []
        self.skip = 0
        self.heading = 0
        self.pre = 0

    def _flush(self):
        raw = ''.join(self.buf); self.buf = []
        if self.pre:
            text = raw.strip('\n')
            for line in text.split('\n'):
                if line.strip():
                    self.blocks.append(('para', line.rstrip()))
            return
        text = _CJK_BREAK.sub('', raw)            # a source line break inside CJK text is not a space
        text = _SPACES.sub(' ', text).strip()
        if text:
            self.blocks.append(('title' if self.heading else 'para', text))

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP:
            self.skip += 1; return
        if self.skip:
            return
        if tag in _BLOCK or tag in _HEAD:
            self._flush()
            if tag in _HEAD: self.heading += 1
            if tag == 'pre': self.pre += 1
        elif tag == 'br':
            if self.pre: self.buf.append('\n')
            else: self._flush()

    def handle_startendtag(self, tag, attrs):
        if tag.lower() == 'br' and not self.skip:
            if self.pre: self.buf.append('\n')
            else: self._flush()
        elif tag.lower() == 'hr' and not self.skip:
            self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1); return
        if self.skip:
            return
        if tag in _BLOCK or tag in _HEAD:
            self._flush()
            if tag in _HEAD: self.heading = max(0, self.heading - 1)
            if tag == 'pre': self.pre = max(0, self.pre - 1)

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _decode(data: bytes) -> str:
    if data.startswith(b'\xef\xbb\xbf'):
        return data[3:].decode('utf-8', 'replace')
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16', 'replace')
    m = re.search(rb'<\?xml[^>]*encoding=["\']([A-Za-z0-9_.:-]+)["\']', data[:200])
    if not m:
        m = re.search(rb'<meta[^>]+charset=["\']?([A-Za-z0-9_.:-]+)', data[:2048], re.IGNORECASE)
    enc = m.group(1).decode('ascii', 'ignore') if m else 'utf-8'
    try:
        return data.decode(enc, 'replace')
    except LookupError:
        return data.decode('utf-8', 'replace')


def xhtml_blocks(markup: str) -> list[tuple[str, str]]:
    p = _Collector()
    try:
        p.feed(markup)
        p.close()
    except Exception:
        p._flush()
    return p.blocks


# ------------------------------------------------------------------ public

def extract_epub_text(path, max_chars: int | None = None) -> EpubText:
    """Readable text of an EPUB. Raises EpubNoTextError for picture-only books, EpubTextError otherwise."""
    path = Path(path)
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise EpubTextError(f'EPUB 파일을 열지 못했습니다 (손상되었거나 EPUB이 아닌 파일). {type(e).__name__}: {e}')
    budget = [0]
    with z:
        docs = _spine_documents(z, budget)
        if not docs:
            raise EpubNoTextError(NO_TEXT_MESSAGE)
        _check_drm(z, docs, budget)
        paras: list[str] = []
        text_docs = 0
        total = 0
        for name in docs:
            try:
                data = _read(z, name, budget)
            except KeyError:
                continue          # a page listed but missing: keep the rest of the book
            blocks = xhtml_blocks(_decode(data))
            if blocks:
                text_docs += 1
            for kind, text in blocks:
                paras.append(text)
                total += len(text)
            if max_chars is not None and total >= max_chars:
                break
    if not paras or sum(len(p.strip()) for p in paras) < 2:
        raise EpubNoTextError(NO_TEXT_MESSAGE)
    return EpubText(text='\n\n'.join(paras) + '\n', documents=len(docs), text_documents=text_docs)
