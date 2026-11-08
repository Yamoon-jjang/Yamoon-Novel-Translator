from __future__ import annotations
import os, re, hashlib
from pathlib import Path
from datetime import datetime
from .textio import directory_writable, atomic_write_text
from .validation import parse_tagged_translation
from .punctuation import korean_punctuation, _LETTERS
from .hidden_marks import restore as restore_marks


def _has_letters(text: str) -> bool:
    return bool(_LETTERS.search(text or ''))

_ID_RE=re.compile(r'(?m)^<P(\d{6})>\s*$')

# A TXT copied from a web page often keeps HTML line breaks (문장<br>문장, 문장<br>\n).
# They are protected during translation so they come back exactly, but a reading TXT
# must show them as line breaks, not as "<br>". A <br> already followed by a newline
# is just dropped; one at the very start or end of a paragraph adds nothing.
_HTML_BR=re.compile(r'[ \t　]*<br\s*/?>[ \t　]*(?:\r?\n)?', re.I)


def html_line_breaks(text: str) -> str:
    if not text or '<' not in text:
        return text
    out=_HTML_BR.sub('\n', text)
    if out == text:
        return text
    bare=_HTML_BR.sub('', text)   # newlines that were there without the tags
    lead=bare[:len(bare)-len(bare.lstrip('\r\n'))]
    tail=bare[len(bare.rstrip('\r\n')):]
    return lead+out.strip('\r\n')+tail


def _row_value(row, key: str, default=''):
    try:
        return row[key]
    except Exception:
        try:
            return row.get(key,default)
        except Exception:
            return default



# --- which book wrote which result file (v2.6.1) ------------------------------------
# With one output folder for every result, two different books of the same file name
# (A\\1.txt and B\\1.txt) render to the same 1.ko.txt. A result is replaced or retired
# only by the book that wrote it; another book's result is first kept as a dated .bak.
OWNERS_FILE = 'output_owners.json'
OWNERS_MAX = 3000
SAME_BOOK_PREFIX = 8000   # characters compared when the writer is unknown or another path


def _path_key(path) -> str:
    try:
        return str(Path(path).resolve()).casefold()
    except OSError:
        return str(path).casefold()


def _owners_path(root=None) -> Path:
    if root is None:
        from .config import DATA_DIR
        root = DATA_DIR
    return Path(root) / OWNERS_FILE


def _load_owners(root=None) -> dict:
    try:
        from .config import load_json
        data = load_json(_owners_path(root)).get('owners')
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_owners(owners: dict, root=None) -> None:
    try:
        from .config import save_json
        items = list(owners.items())[-OWNERS_MAX:]
        save_json(_owners_path(root), {'schema': 1, 'owners': dict(items)})
    except Exception:
        pass   # never stops a result from being saved


def _normalized(text: str) -> str:
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _same_book(existing: Path, new_text: str) -> bool:
    """The file already there starts like the result being written now (the same book:
    a resumed, moved or renamed job). A different book of the same name does not."""
    try:
        with existing.open('rb') as f:
            raw = f.read(SAME_BOOK_PREFIX * 4 + 16)
        old = _normalized(raw.decode('utf-8-sig', 'ignore'))[:SAME_BOOK_PREFIX]
    except OSError:
        return True    # unreadable: leave it to the atomic replace as before
    new = _normalized(new_text)[:SAME_BOOK_PREFIX]
    if not old.strip():
        return True    # an empty file holds nothing to keep
    return old == new or new.startswith(old) or old.startswith(new)


def _keep_aside(path: Path) -> Path:
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    backup = path.with_name(path.name + f'.before_{stamp}.bak')
    path.replace(backup)
    return backup


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


def not_started(row) -> bool:
    """A chunk the job has not tried yet (or whose request a stop cut off): no translation
    and no error recorded. Failed and partial chunks are not this."""
    return row['status'] == 'PENDING' and not (row['error_type'] or '') and not _row_value(row, 'translation', '')


def render_snapshot(chunks, rows, source_path: Path, preferred_dir: Path, fallback_dir: Path, all_paragraphs=None, korean_punct: bool = True, quote_style: str = 'straight', owners_root=None):
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
            body=html_line_breaks(paragraph.text)
            if korean_punct and not _has_letters(body):
                body=korean_punctuation(body,quote_style)   # punctuation-only line, e.g. “。。。。。”
        elif paragraph.pid in translated_by_pid:
            body=html_line_breaks(translated_by_pid[paragraph.pid])
            if korean_punct:
                body=korean_punctuation(body,quote_style)
            # The source's invisible marks (zero-width characters) are written back into the
            # translation; a paragraph kept as source already has them (v2.6.3).
            body=restore_marks(paragraph.text,body)
        else:
            # Never create a hole in the readable output. The raw source is safer
            # than a missing scene and enables DB-less mixed-language retry later.
            body=html_line_breaks(paragraph.text)
        rendered.append(paragraph.prefix_before+body+paragraph.separator_after)
    text=''.join(rendered)
    owners=_load_owners(owners_root); me=_path_key(source_path); owners_changed=False

    def other_book(path: Path) -> bool:
        """`path` holds the result of a different book (never true for this book's own)."""
        writer=owners.get(_path_key(path))
        return writer is not None and writer!=me

    # Another book's result of the same name is kept as a dated .bak, never silently
    # replaced (v2.6.1). This book's own result (resumed, moved or renamed) is replaced as before.
    try:
        if out.exists() and out.resolve()!=source_path.resolve() and owners.get(_path_key(out))!=me:
            if not _same_book(out,text):
                _keep_aside(out)
    except OSError:
        pass
    # Atomic replacement leaves an existing file untouched if the write fails.
    # Avoid creating a dated backup beside the source on every snapshot.
    atomic_write_text(out,text)
    if owners.get(_path_key(out))!=me:
        owners.pop(_path_key(out),None); owners[_path_key(out)]=me; owners_changed=True
    # The unfinished list is no longer written as a .ko.failed.txt beside the result (resume
    # reads the database; the list is shown in the app). One left by an earlier build goes.
    # Only after a complete result is durable is the stale partial retired as well.
    # Never remove the selected source (which may itself be a prior partial), nor another
    # book's partial of the same name.
    for obsolete in ((failed_path,) if failed else (partial_path, failed_path)):
        if obsolete == out or not obsolete.exists() or other_book(obsolete):
            continue
        try:
            if obsolete.resolve() != source_path.resolve():
                obsolete.unlink()
                if owners.pop(_path_key(obsolete),None) is not None: owners_changed=True
        except OSError:
            pass
    # Preserve the old completed result only when a new partial result supersedes it.
    # Regular snapshots do not generate dated backups. Another book's result stays put.
    if (failed and full_path.exists() and full_path.resolve() != source_path.resolve()
            and not other_book(full_path)):
        _keep_aside(full_path)
        if owners.pop(_path_key(full_path),None) is not None: owners_changed=True
    if owners_changed:
        _save_owners(owners,owners_root)
    report=None
    if failed:
        # Text for the app's 미완료 목록 tab (no file is written).
        lines=['야문 Novel Translator 미완료 구간','※ 본문 결과에는 미완료 구간의 원문이 그대로 유지됩니다.','※ PARTIAL 청크는 번역된 문단은 반영되고, 표시된 문단만 원문으로 남습니다. 같은 작업을 다시 시작하면 이 청크만 재시도합니다.','']
        # Chunks a stopped job has not reached yet are one line, not one line each (a book
        # stopped early would list thousands); chunks that were tried are listed as before.
        unstarted=[int(r['chunk_index']) for r in failed if not_started(r)]
        if unstarted:
            lines.append(f'아직 번역 전 {len(unstarted):,}청크 (청크 {min(unstarted)+1:,}~{max(unstarted)+1:,} 사이) · 이어서 번역하면 차례로 번역합니다')
        for r in failed:
            if not_started(r):
                continue
            lines.append(f'청크 {int(r["chunk_index"])+1}\t{r["status"]}\t{_row_value(r,"provider","") or ""}\t{_row_value(r,"model","") or ""}\t{r["error_type"] or ""}\t{r["error_message"] or ""}')
        report='\n'.join(lines)+'\n'
    return out,failed,report,output_dir
