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


# The text taken out of a PDF is kept, so opening the same PDF again (to continue its
# translation) takes a moment instead of reading every page again. The key is the file's
# content, the program version and the PDF reader's version: any of them changing reads
# the PDF again. Only the newest files are kept.
PDF_CACHE_KEEP = 30


def _pdf_cache_key(path: Path) -> str:
    import hashlib
    from . import __version__
    try:
        import pypdf
        reader = str(getattr(pypdf, '__version__', ''))
    except Exception:
        reader = ''
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    h.update(f'|{__version__}|{reader}'.encode('utf-8'))
    return h.hexdigest()


def _pdf_from_cache(path: Path, cache_dir: Path | None):
    import json
    from .pdf_text import PdfText
    if cache_dir is None:
        return None, ''
    try:
        key = _pdf_cache_key(path)
    except Exception:
        return None, ''
    try:
        data = json.loads((cache_dir / f'{key}.json').read_text(encoding='utf-8'))
        result = PdfText(text=str(data['text']), pages=int(data['pages']), text_pages=int(data['text_pages']),
                         removed_lines=int(data['removed_lines']))
        try:
            import os, time
            now = time.time(); os.utime(cache_dir / f'{key}.json', (now, now))   # recently used
        except Exception:
            pass
        return result, key
    except Exception:
        return None, key


def _pdf_to_cache(result, key: str, cache_dir: Path | None) -> None:
    import json, os
    if cache_dir is None or not key:
        return
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = cache_dir / f'{key}.json'
        tmp = target.with_suffix('.tmp')
        tmp.write_text(json.dumps({'text': result.text, 'pages': result.pages, 'text_pages': result.text_pages,
                                   'removed_lines': result.removed_lines}, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, target)
        files = sorted(cache_dir.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[PDF_CACHE_KEEP:]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass


def extract_document(path, progress=None, cache_dir=None):
    """Text of a PDF or EPUB: an object with .text and .label. progress(done, total) for PDF pages.
    cache_dir: where a PDF's taken-out text is kept for the next opening (None = not kept)."""
    kind = document_kind(path)
    if kind == 'PDF':
        from .pdf_text import extract_pdf_text
        folder = Path(cache_dir) if cache_dir else None
        cached, key = _pdf_from_cache(Path(path), folder)
        if cached is not None:
            if progress is not None:
                try:
                    progress(cached.pages, cached.pages)
                except Exception:
                    pass
            return cached
        result = extract_pdf_text(path, progress=progress)
        _pdf_to_cache(result, key, folder)
        return result
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
