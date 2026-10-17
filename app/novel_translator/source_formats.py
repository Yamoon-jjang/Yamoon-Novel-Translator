"""Book files other than TXT (PDF, EPUB) that are opened for their text.

Their text is taken out once, on opening; from then on the job is the same as for a
TXT. The errors below carry a message that is shown to the user as is.
"""
from __future__ import annotations

from pathlib import Path

# suffix -> label shown to the user
DOCUMENT_KINDS = {'.pdf': 'PDF', '.epub': 'EPUB'}


class SourceTextError(Exception):
    """A book file whose text cannot be used (damaged, password, DRM ...)."""


class NoTextError(SourceTextError):
    """A book file that holds only pictures (scanned PDF, image-only EPUB)."""


def document_kind(path) -> str:
    """'PDF' / 'EPUB' for a book file whose text is taken out, '' for TXT and others."""
    try:
        return DOCUMENT_KINDS.get(Path(path).suffix.casefold(), '')
    except Exception:
        return ''


def extract_document(path, progress=None):
    """Text of a PDF or EPUB: an object with .text and .label. progress(done, total) for PDF pages."""
    kind = document_kind(path)
    if kind == 'PDF':
        from .pdf_text import extract_pdf_text
        return extract_pdf_text(path, progress=progress)
    if kind == 'EPUB':
        from .epub_text import extract_epub_text
        return extract_epub_text(path)
    raise SourceTextError('PDF·EPUB 파일이 아닙니다.')


def document_sample_text(path) -> str:
    """The first part of a PDF/EPUB's text, for a quick language check ('' when none)."""
    kind = document_kind(path)
    try:
        if kind == 'PDF':
            from .pdf_text import pdf_sample_text
            return pdf_sample_text(path)
        if kind == 'EPUB':
            from .epub_text import extract_epub_text
            return extract_epub_text(path, max_chars=20000).text
    except Exception:
        return ''
    return ''
