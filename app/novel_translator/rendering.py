from __future__ import annotations
import os, re, hashlib
from pathlib import Path
from datetime import datetime
from .textio import directory_writable, atomic_write_text
from .validation import parse_tagged_translation
from .punctuation import korean_punctuation, _LETTERS


def _has_letters(text: str) -> bool:
    return bool(_LETTERS.search(text or ''))

_ID_RE=re.compile(r'(?m)^<P(\d{6})>\s*$')


def _row_value(row, key: str, default=''):
    try:
        return row[key]
    except Exception:
        try:
            return row.get(key,default)
        except Exception:
            return default



def has_name_twin(source_path: Path) -> bool:
    """A PDF/EPUB with another book file of the same name beside it (book.pdf + book.txt / book.epub)."""
    source_path=Path(source_path)
    if source_path.suffix.casefold() not in ('.pdf','.epub'):
        return False
    try:
        stem=source_path.stem.casefold(); own=source_path.name.casefold()
        names={stem+'.txt',stem+'.pdf',stem+'.epub'}-{own}
        return any(x.name.casefold() in names and x.is_file() for x in source_path.parent.iterdir())
    except OSError:
        return False


def output_stem(source_path: Path) -> str:
    """The book name results are saved under (book.txt / book.pdf / book.epub -> book)."""
    stem=Path(source_path).stem
    # Re-processing a prior partial result should converge back to the original book name.
    # Never overwrite the selected source file itself if the new state is still partial.
    if stem.lower().endswith('.ko.partial'):
        stem=stem[:-len('.ko.partial')]
    elif stem.lower().endswith('.ko'):
        stem=stem[:-len('.ko')]
    if has_name_twin(source_path):
        # book.txt, book.pdf and book.epub in one folder are separate sources: a PDF/EPUB
        # result must not replace another one's book.ko.txt (or its partial), so it is
        # saved as book.pdf.ko.txt / book.epub.ko.txt. A TXT keeps book.ko.txt.
        stem+=Path(source_path).suffix.lower()
    return stem


def safe_output_filename(source_path: Path, suffix: str, disambiguate: bool=False, max_chars: int=220) -> str:
    stem=output_stem(source_path)
    extra=''
    if disambiguate:
        extra='_'+hashlib.sha256(str(source_path.resolve()).encode('utf-8','surrogatepass')).hexdigest()[:8]
    room=max(16,max_chars-len(suffix)-len(extra))
    if len(stem)>room:
        digest=hashlib.sha256(stem.encode('utf-8','surrogatepass')).hexdigest()[:8]
        stem=stem[:max(8,room-9)]+'_'+digest
    return stem+extra+suffix


def render_snapshot(chunks, rows, source_path: Path, preferred_dir: Path, fallback_dir: Path, all_paragraphs=None, korean_punct: bool = True, quote_style: str = 'straight'):
    """Render complete/partial TXT without ever dropping source text.

    Failed translatable paragraphs are written as their original source text, with no
    intrusive marker inside the novel. Details are shown in the UI unfinished list.
    Paragraphs classified as already-Korean/pass-through never enter the provider and
    are copied through unchanged.
    """
    failed=[r for r in rows if r['status']!='COMPLETED']
    suffix='.ko.partial.txt' if failed else '.ko.txt'
    use_fallback=not directory_writable(preferred_dir)
    output_dir=fallback_dir if use_fallback else preferred_dir
    output_dir.mkdir(parents=True,exist_ok=True)
    out=output_dir/safe_output_filename(source_path,suffix,disambiguate=use_fallback)
    if os.name=='nt' and len(str(out.resolve())) >= 240:
        use_fallback=True; output_dir=fallback_dir; output_dir.mkdir(parents=True,exist_ok=True)
        out=output_dir/safe_output_filename(source_path,suffix,disambiguate=True,max_chars=150)
    try:
        if out.resolve() == source_path.resolve():
            retry_name=safe_output_filename(source_path,'.retry'+suffix,disambiguate=use_fallback,max_chars=150 if use_fallback and os.name=='nt' else 220)
            out=output_dir/retry_name
    except Exception:
        pass
    translated_by_pid={}; failed_by_pid={}
    for r in rows:
        src_ids=[int(x) for x in _ID_RE.findall(r['source_text'] or '')]
        if r['status']=='COMPLETED':
            translated_by_pid.update(parse_tagged_translation(r['translation'] or ''))
        elif r['status']=='PARTIAL' and r['translation']:
            # Translated paragraphs of a partial chunk are used; its isolated
            # paragraphs already carry their source text inside the translation.
            translated_by_pid.update(parse_tagged_translation(r['translation'] or ''))
        else:
            for pid in src_ids:
                failed_by_pid[pid]=(int(r['chunk_index']),r['error_type'] or r['status'] or 'PENDING',r['error_message'] or '')
    full_path=output_dir/safe_output_filename(source_path,'.ko.txt',disambiguate=use_fallback, max_chars=150 if use_fallback and os.name=='nt' else 220)
    partial_path=output_dir/safe_output_filename(source_path,'.ko.partial.txt',disambiguate=use_fallback, max_chars=150 if use_fallback and os.name=='nt' else 220)
    failed_path=output_dir/safe_output_filename(source_path,'.ko.failed.txt',disambiguate=use_fallback, max_chars=150 if use_fallback and os.name=='nt' else 220)
    if all_paragraphs is None:
        paragraphs=[p for chunk in chunks for p in chunk.paragraphs]
    else:
        paragraphs=list(all_paragraphs)
    rendered=[]
    for paragraph in paragraphs:
        if not getattr(paragraph,'translatable',True):
            body=paragraph.text
            if korean_punct and not _has_letters(body):
                body=korean_punctuation(body,quote_style)   # punctuation-only line, e.g. “。。。。。”
        elif paragraph.pid in translated_by_pid:
            body=translated_by_pid[paragraph.pid]
            if korean_punct:
                body=korean_punctuation(body,quote_style)
        else:
            # Never create a hole in the readable output. The raw source is safer
            # than a missing scene and enables DB-less mixed-language retry later.
            body=paragraph.text
        rendered.append(paragraph.prefix_before+body+paragraph.separator_after)
    # Atomic replacement leaves an existing file untouched if the write fails.
    # Avoid creating a dated backup beside the source on every snapshot.
    atomic_write_text(out,''.join(rendered))
    # The unfinished list is no longer written as a .ko.failed.txt beside the result (resume
    # reads the database; the list is shown in the app). One left by an earlier build goes.
    # Only after a complete result is durable is the stale partial retired as well.
    # Never remove the selected source (which may itself be a prior partial).
    for obsolete in ((failed_path,) if failed else (partial_path, failed_path)):
        if obsolete == out or not obsolete.exists():
            continue
        try:
            if obsolete.resolve() != source_path.resolve():
                obsolete.unlink()
        except OSError:
            pass
    # Preserve the old completed result only when a new partial result supersedes it.
    # Regular snapshots do not generate dated backups.
    if failed and full_path.exists() and full_path.resolve() != source_path.resolve():
        stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        full_path.replace(full_path.with_name(full_path.name+f'.before_{stamp}.bak'))
    report=None
    if failed:
        # Text for the app's 미완료 목록 tab (no file is written).
        lines=['야문 Novel Translator 미완료 구간','※ 본문 결과에는 미완료 구간의 원문이 그대로 유지됩니다.','※ PARTIAL 청크는 번역된 문단은 반영되고, 표시된 문단만 원문으로 남습니다. 같은 작업을 다시 시작하면 이 청크만 재시도합니다.','']
        for r in failed:
            lines.append(f'청크 {int(r["chunk_index"])+1}\t{r["status"]}\t{_row_value(r,"provider","") or ""}\t{_row_value(r,"model","") or ""}\t{r["error_type"] or ""}\t{r["error_message"] or ""}')
        report='\n'.join(lines)+'\n'
    return out,failed,report,output_dir
