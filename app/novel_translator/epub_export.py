"""Reading EPUB of a Korean translation (Python standard library only).

The original EPUB's look is not copied: the translation is laid out anew as an EPUB 3
book (with an EPUB 2 table of contents for older readers). Each chapter title starts a
new section and appears in the table of contents; paragraphs get spacing; the reader
app's own Korean font is used (fonts are not embedded). An e-book has no fixed pages,
so page numbers are the reader app's.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from pathlib import Path
import os
import uuid
import zipfile

from .pdf_export import reading_blocks


class EpubExportError(Exception):
    """Shown to the user as is."""


CSS = """@charset "utf-8";
body { font-family: "Noto Serif KR", "Nanum Myeongjo", "NanumMyeongjo", "Batang", "Malgun Gothic", serif;
       line-height: 1.75; margin: 0 4%; word-break: keep-all; }
p { margin: 0 0 0.85em 0; text-indent: 0; text-align: justify; }
h1 { font-size: 1.35em; font-weight: bold; text-align: center; margin: 2.5em 0 1.6em 0; page-break-after: avoid; }
p.gap { text-align: center; margin: 1.2em 0; }
"""


def _sections(text: str, title: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """[(section title, blocks)] — a new section at each chapter title."""
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    for kind, body in reading_blocks(text):
        if kind == 'title' or not sections:
            sections.append((body if kind == 'title' else title, []))
        sections[-1][1].append((kind, body))
    return sections


def _xhtml(title: str, blocks: list[tuple[str, str]]) -> str:
    body = []
    for kind, text in blocks:
        if kind == 'title':
            body.append(f'<h1>{escape(text)}</h1>')
        elif kind == 'gap':
            body.append('<p class="gap">*</p>')
        else:
            body.append(f'<p>{escape(text)}</p>')
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="ko" xml:lang="ko">\n'
            f'<head><meta charset="utf-8"/><title>{escape(title)}</title>'
            '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n<body>\n'
            + '\n'.join(body) + '\n</body>\n</html>\n')


def write_reading_epub(path, text: str, title: str = '') -> int:
    """Write `text` as a reading EPUB at `path`. Returns the number of sections."""
    path = Path(path)
    title = (title or path.stem).strip() or '번역'
    sections = _sections(text, title)
    if not sections:
        raise EpubExportError('저장할 번역문이 없습니다.')
    book_id = f'urn:uuid:{uuid.uuid4()}'
    modified = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    names = [f'chap{i + 1:04d}.xhtml' for i in range(len(sections))]

    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                '<item id="css" href="style.css" media-type="text/css"/>']
    manifest += [f'<item id="c{i + 1}" href="{n}" media-type="application/xhtml+xml"/>' for i, n in enumerate(names)]
    spine = ''.join(f'<itemref idref="c{i + 1}"/>' for i in range(len(names)))
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="ko">\n'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
           f'<dc:identifier id="bookid">{book_id}</dc:identifier>\n'
           f'<dc:title>{escape(title)}</dc:title>\n<dc:language>ko</dc:language>\n'
           '<dc:creator>야문 Novel Translator</dc:creator>\n'
           f'<meta property="dcterms:modified">{modified}</meta>\n</metadata>\n'
           '<manifest>\n' + '\n'.join(manifest) + '\n</manifest>\n'
           f'<spine toc="ncx">{spine}</spine>\n</package>\n')
    toc_items = ''.join(f'<li><a href="{n}">{escape(t)}</a></li>' for n, (t, _) in zip(names, sections))
    nav = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
           '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="ko" xml:lang="ko">\n'
           f'<head><meta charset="utf-8"/><title>{escape(title)}</title></head>\n<body>\n'
           f'<nav epub:type="toc" id="toc"><h1>목차</h1><ol>{toc_items}</ol></nav>\n</body>\n</html>\n')
    points = ''.join(f'<navPoint id="p{i + 1}" playOrder="{i + 1}"><navLabel><text>{escape(t)}</text></navLabel>'
                     f'<content src="{n}"/></navPoint>' for i, (n, (t, _)) in enumerate(zip(names, sections)))
    ncx = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="ko">\n'
           f'<head><meta name="dtb:uid" content="{book_id}"/></head>\n'
           f'<docTitle><text>{escape(title)}</text></docTitle>\n<navMap>{points}</navMap>\n</ncx>\n')
    container = ('<?xml version="1.0" encoding="utf-8"?>\n'
                 '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
                 '</container>\n')

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    try:
        with zipfile.ZipFile(tmp, 'w') as z:
            # The mimetype entry must come first and be stored uncompressed.
            z.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            put = lambda name, data: z.writestr(name, data.encode('utf-8'), compress_type=zipfile.ZIP_DEFLATED)
            put('META-INF/container.xml', container)
            put('OEBPS/content.opf', opf)
            put('OEBPS/nav.xhtml', nav)
            put('OEBPS/toc.ncx', ncx)
            put('OEBPS/style.css', CSS)
            for n, (t, blocks) in zip(names, sections):
                put(f'OEBPS/{n}', _xhtml(t, blocks))
        os.replace(tmp, path)
    except Exception:
        try: tmp.unlink()
        except OSError: pass
        raise
    return len(sections)
