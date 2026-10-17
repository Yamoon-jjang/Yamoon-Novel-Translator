from __future__ import annotations
from pathlib import Path
import re

GENERATED_SUFFIXES = ('.ko.txt', '.ko.partial.txt', '.ko.failed.txt', '.ko.pdf', '.ko.epub')
SUMMARY_NAME = '번역_요약.txt'
SOURCE_SUFFIXES = ('.txt', '.pdf', '.epub')   # a folder run translates TXT, PDF and EPUB files

def is_generated_txt(path: Path) -> bool:
    low=path.name.lower()
    return low.endswith(GENERATED_SUFFIXES) or path.name == SUMMARY_NAME

def discover_txt_files(root: Path, include_subfolders: bool=False, excluded_root: Path|None=None) -> list[Path]:
    # Preserve the caller's lexical path (including a possible Windows 8.3 alias)
    # while resolving only for scanning/exclusion checks.
    lexical_root=Path(root)
    scan_root=lexical_root.resolve()
    excluded=excluded_root.resolve() if excluded_root else None
    it=scan_root.rglob('*') if include_subfolders else scan_root.iterdir()
    out=[]
    for p in it:
        if not p.is_file() or p.suffix.casefold() not in SOURCE_SUFFIXES or is_generated_txt(p):
            continue
        rp=p.resolve()
        if excluded and (rp == excluded or excluded in rp.parents):
            continue
        rel=p.relative_to(scan_root)
        out.append(lexical_root / rel)
    def natural_key(path: Path):
        rel=str(path.relative_to(lexical_root)).casefold().replace('\\','/')
        return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', rel)]
    return sorted(out,key=natural_key)

def output_dir_for(source: Path, batch_root: Path, output_root: Path) -> Path:
    try:
        rel_parent=source.parent.resolve().relative_to(batch_root.resolve())
    except Exception:
        rel_parent=Path()
    return output_root / rel_parent

def should_continue_after_result(relation: str, status: str) -> bool:
    """Whether a folder queue may advance after one file finishes.

    The queue must finish the whole folder unattended. Completed, partial (some
    paragraphs kept as source) and unreadable files all advance; in continuous mode an
    unreadable file simply passes no context to the next one. Only an explicit user
    stop ends the queue. Temporary pauses (quota, rate limit, network) are resumed
    automatically by the main window before this decision is made.
    """
    status=(status or '').strip().lower()
    return status in {'completed','partial','file_error','skipped'}
