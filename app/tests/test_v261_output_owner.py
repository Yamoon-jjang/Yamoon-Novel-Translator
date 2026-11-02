"""v2.6.1: one output folder for every result — two books of the same file name must not
replace each other's .ko.txt / .ko.partial.txt. The same book (resumed, moved, renamed)
replaces its own result exactly as before, with no extra backup files."""
from novel_translator.chunking import split_paragraphs, make_chunks
from novel_translator.rendering import render_snapshot


def _book(folder, name, sentences, done=True, first_only=False, label=None):
    folder.mkdir(parents=True, exist_ok=True)
    text = '\n\n'.join(sentences)
    src = folder / name
    src.write_text(text, encoding='utf-8')
    paras = split_paragraphs(text)
    chunks = make_chunks(paras, 'zh', target_tokens=1, min_tokens=1, max_tokens=3)
    rows = []
    for c in chunks:
        ok = done and (not first_only or c.index == 0)
        rows.append({'chunk_index': c.index, 'source_text': c.source_text,
                     'status': 'COMPLETED' if ok else 'PENDING',
                     'translation': '\n\n'.join(f'<P{p.pid:06d}>\n{label or src.parent.name} 번역 {p.pid}' for p in c.paragraphs) if ok else None,
                     'error_type': None, 'error_message': None})
    return src, chunks, rows


def _render(src, chunks, rows, out, owners):
    return render_snapshot(chunks, rows, src, out, out / 'fallback', owners_root=owners)


def test_another_book_of_the_same_name_is_kept_as_backup(tmp_path):
    out = tmp_path / 'out'; out.mkdir(); owners = tmp_path / 'data'
    a = _book(tmp_path / 'A', '1.txt', ['甲一。', '甲二。', '甲三。'])
    b = _book(tmp_path / 'B', '1.txt', ['乙一。', '乙二。', '乙三。'])
    first, _, _, _ = _render(*a, out, owners)
    a_text = first.read_text(encoding='utf-8-sig')
    second, _, _, _ = _render(*b, out, owners)
    assert second == first                                    # same name, as always
    assert 'B 번역' in second.read_text(encoding='utf-8-sig')
    baks = list(out.glob('1.ko.txt.before_*.bak'))
    assert len(baks) == 1 and baks[0].read_text(encoding='utf-8-sig') == a_text


def test_the_same_book_replaces_its_own_result_without_backups(tmp_path):
    out = tmp_path / 'out'; out.mkdir(); owners = tmp_path / 'data'
    a = _book(tmp_path / 'A', 'book.txt', ['甲一。', '甲二。', '甲三。'])
    for _ in range(3):
        _render(*a, out, owners)
    assert sorted(p.name for p in out.iterdir() if p.is_file()) == ['book.ko.txt']


def test_a_moved_book_and_a_result_from_an_earlier_version_are_not_backed_up(tmp_path):
    out = tmp_path / 'out'; out.mkdir(); owners = tmp_path / 'data'
    a = _book(tmp_path / 'A', 'book.txt', ['甲一。', '甲二。', '甲三。'])
    _render(*a, out, owners)
    # Same text in another folder (the book was moved): no writer record matches,
    # but the result is the same book, so it is replaced in place.
    moved = _book(tmp_path / 'moved', 'book.txt', ['甲一。', '甲二。', '甲三。'], label='A')
    _render(*moved, out, owners)
    assert sorted(p.name for p in out.iterdir() if p.is_file()) == ['book.ko.txt']
    (owners / 'output_owners.json').unlink()                   # result written by v2.6 (no record)
    _render(*a, out, owners)
    assert sorted(p.name for p in out.iterdir() if p.is_file()) == ['book.ko.txt']
    # ...while an earlier version's result of a different book is still kept.
    (owners / 'output_owners.json').unlink()
    other = _book(tmp_path / 'B', 'book.txt', ['乙一。', '乙二。', '乙三。'])
    _render(*other, out, owners)
    assert len(list(out.glob('book.ko.txt.before_*.bak'))) == 1


def test_a_partial_of_another_book_is_not_deleted_or_moved(tmp_path):
    out = tmp_path / 'out'; out.mkdir(); owners = tmp_path / 'data'
    a_partial = _book(tmp_path / 'A', 'book.txt', ['甲一。', '甲二。', '甲三。'], first_only=True)
    p_out, failed, _, _ = _render(*a_partial, out, owners)
    assert p_out.name == 'book.ko.partial.txt' and failed
    b = _book(tmp_path / 'B', 'book.txt', ['乙一。', '乙二。', '乙三。'])
    full, failed, _, _ = _render(*b, out, owners)
    assert full.name == 'book.ko.txt' and not failed
    assert (out / 'book.ko.partial.txt').exists()               # A's unfinished result stays
    # A finishing later keeps B's complete result as a .bak instead of overwriting it.
    a_full = _book(tmp_path / 'A', 'book.txt', ['甲一。', '甲二。', '甲三。'])
    _render(*a_full, out, owners)
    assert 'A 번역' in (out / 'book.ko.txt').read_text(encoding='utf-8-sig')
    baks = list(out.glob('book.ko.txt.before_*.bak'))
    assert len(baks) == 1 and 'B 번역' in baks[0].read_text(encoding='utf-8-sig')
    assert not (out / 'book.ko.partial.txt').exists()           # A's own partial retired


def test_own_partial_superseding_own_full_keeps_the_v26_backup_rule(tmp_path):
    out = tmp_path / 'out'; out.mkdir(); owners = tmp_path / 'data'
    full = _book(tmp_path / 'A', 'book.txt', ['甲一。', '甲二。', '甲三。'])
    _render(*full, out, owners)
    part = _book(tmp_path / 'A', 'book.txt', ['甲一。', '甲二。', '甲三。'], first_only=True)
    _render(*part, out, owners)
    names = sorted(p.name for p in out.iterdir() if p.is_file())
    assert 'book.ko.partial.txt' in names and 'book.ko.txt' not in names
    assert len([n for n in names if n.startswith('book.ko.txt.before_')]) == 1
