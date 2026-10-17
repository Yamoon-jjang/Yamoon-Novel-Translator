"""Reading PDF of a Korean translation, made with Qt's own PDF writer.

The original PDF's look is not copied: the text is set anew in a Korean font with
paragraph spacing, chapter titles (each starting a new page) and page numbers.
Nothing beyond PySide6 is needed.
"""
from __future__ import annotations

import os
from pathlib import Path

from .pdf_text import is_chapter_title

# Fonts tried in order; the first installed one that has Korean is used.
KOREAN_FONTS = ('Malgun Gothic', '맑은 고딕', 'Noto Serif CJK KR', 'Noto Sans CJK KR', 'Noto Sans KR',
                'NanumMyeongjo', 'NanumGothic', 'Batang', '바탕', 'Gulim', '굴림', 'Apple SD Gothic Neo')

PAGE_MM = (148.0, 210.0)       # A5: comfortable on screen and prints two-up on A4
MARGIN_MM = (18.0, 18.0, 18.0, 22.0)   # left, top, right, bottom (room for the page number)
BODY_PT = 10.5
TITLE_PT = 15.0
LINE_HEIGHT = 165              # percent
PARA_GAP_PT = 6.0


class PdfExportError(Exception):
    """Shown to the user as is."""


_WINDOWS_FONT_FILES = ('malgun.ttf', 'batang.ttc', 'gulim.ttc')


def _pick(families, korean) -> str:
    for name in KOREAN_FONTS:
        if name in families and name in korean:
            return name
    return sorted(korean)[0] if korean else ''


def korean_font_family() -> str:
    from PySide6.QtGui import QFontDatabase
    found = _pick(set(QFontDatabase.families()), set(QFontDatabase.families(QFontDatabase.WritingSystem.Korean)))
    if found:
        return found
    # Qt's list can miss the system fonts (e.g. a platform without the system font
    # database): load Windows' own Korean font files directly.
    fonts = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'
    for name in _WINDOWS_FONT_FILES:
        f = fonts / name
        if f.is_file():
            fid = QFontDatabase.addApplicationFont(str(f))
            loaded = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            if loaded:
                return loaded[0]
    raise PdfExportError('이 컴퓨터에서 한글 글꼴을 찾지 못해 PDF를 만들 수 없습니다. TXT로 저장해 주세요.')


def reading_blocks(text: str) -> list[tuple[str, str]]:
    """('title' | 'para' | 'gap', text) in reading order."""
    out: list[tuple[str, str]] = []
    blank = 0
    for line in (text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        s = line.strip()
        if not s:
            blank += 1
            continue
        if blank >= 2 and out and out[-1][0] == 'para':
            out.append(('gap', ''))           # a scene break (several blank lines) stays visible
        blank = 0
        out.append(('title' if is_chapter_title(s) else 'para', s))
    return out


def write_reading_pdf(path, text: str, title: str = '') -> int:
    """Write `text` as a reading PDF at `path`. Returns the number of pages."""
    from PySide6.QtCore import QMarginsF, QRectF, QSizeF, Qt
    from PySide6.QtGui import (QFont, QPageLayout, QPageSize, QPainter, QPdfWriter, QTextBlockFormat,
                               QTextCharFormat, QTextCursor, QTextDocument, QTextFormat, QTextOption)

    blocks = reading_blocks(text)
    if not blocks:
        raise PdfExportError('저장할 번역문이 없습니다.')
    family = korean_font_family()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')

    writer = QPdfWriter(str(tmp))
    writer.setResolution(300)
    writer.setPageLayout(QPageLayout(QPageSize(QSizeF(*PAGE_MM), QPageSize.Unit.Millimeter, 'A5'),
                                     QPageLayout.Orientation.Portrait, QMarginsF(0, 0, 0, 0),
                                     QPageLayout.Unit.Millimeter))
    writer.setTitle(title or path.stem)
    writer.setCreator('야문 Novel Translator')
    dpmm = writer.resolution() / 25.4
    page_w, page_h = PAGE_MM[0] * dpmm, PAGE_MM[1] * dpmm
    left, top, right, bottom = (m * dpmm for m in MARGIN_MM)
    body_w, body_h = page_w - left - right, page_h - top - bottom

    doc = QTextDocument()
    doc.documentLayout().setPaintDevice(writer)   # point sizes in the writer's resolution
    doc.setDocumentMargin(0)
    doc.setPageSize(QSizeF(body_w, body_h))
    opt = QTextOption(); opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    doc.setDefaultTextOption(opt)
    body_font = QFont(family); body_font.setPointSizeF(BODY_PT)
    doc.setDefaultFont(body_font)

    para_fmt = QTextBlockFormat()
    para_fmt.setLineHeight(LINE_HEIGHT, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
    para_fmt.setBottomMargin(PARA_GAP_PT * writer.resolution() / 72)
    para_fmt.setAlignment(Qt.AlignmentFlag.AlignJustify)   # even right edge, like a printed book
    para_char = QTextCharFormat(); para_char.setFont(body_font)

    title_font = QFont(family); title_font.setPointSizeF(TITLE_PT); title_font.setBold(True)
    title_char = QTextCharFormat(); title_char.setFont(title_font)
    gap_fmt = QTextBlockFormat(para_fmt); gap_fmt.setAlignment(Qt.AlignmentFlag.AlignCenter)

    cursor = QTextCursor(doc)
    first = True
    for kind, body in blocks:
        if kind == 'title':
            fmt = QTextBlockFormat()
            fmt.setAlignment(Qt.AlignmentFlag.AlignCenter)
            fmt.setTopMargin(24 * writer.resolution() / 72)
            fmt.setBottomMargin(18 * writer.resolution() / 72)
            fmt.setLineHeight(130, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            if not first:
                fmt.setPageBreakPolicy(QTextFormat.PageBreakFlag.PageBreak_AlwaysBefore)   # a chapter starts a page
            char = title_char
        elif kind == 'gap':
            fmt, char, body = gap_fmt, para_char, '*'
        else:
            fmt, char = para_fmt, para_char
        if first:
            cursor.setBlockFormat(fmt); cursor.setBlockCharFormat(char)
        else:
            cursor.insertBlock(fmt, char)
        cursor.insertText(body, char)
        first = False

    pages = max(1, doc.pageCount())
    # Each text line goes on exactly one page. (Drawing the document page by page with a
    # clip would put a paragraph that crosses a page break on both pages, hidden but
    # present in the PDF's text: copying or searching would show it twice.)
    by_page: list[list[tuple]] = [[] for _ in range(pages)]
    block = doc.begin()
    while block.isValid():
        lay = block.layout()
        origin = lay.position()
        for j in range(lay.lineCount()):
            line = lay.lineAt(j)
            middle = origin.y() + line.y() + line.height() / 2
            page = min(pages - 1, max(0, int(middle // body_h)))
            by_page[page].append((line, origin))
        block = block.next()
    num_font = QFont(family); num_font.setPointSizeF(8.5)
    painter = QPainter()
    if not painter.begin(writer):
        raise PdfExportError('PDF 파일을 만들 수 없습니다 (저장 위치에 쓸 수 없음).')
    try:
        for i in range(pages):
            if i:
                writer.newPage()
            painter.save()
            painter.translate(left, top - i * body_h)
            painter.setPen(Qt.GlobalColor.black)
            for line, origin in by_page[i]:
                line.draw(painter, origin)
            painter.restore()
            painter.setFont(num_font)
            painter.drawText(QRectF(0, page_h - bottom + 6 * dpmm, page_w, 8 * dpmm),
                             int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), f'- {i + 1} -')
    finally:
        painter.end()
        # The document's layout points at the writer: release the document first, then the
        # writer (which closes the file), so nothing is left pointing at a deleted device.
        del painter, cursor, block, by_page, doc
        del writer
    if not tmp.is_file() or tmp.stat().st_size == 0:
        raise PdfExportError('PDF 파일을 만들지 못했습니다.')
    os.replace(tmp, path)
    return pages
