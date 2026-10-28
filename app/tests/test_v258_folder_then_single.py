"""v2.5.8 (same-version fix): a folder run is stopped, then one of its files is opened and
translated on its own.

The file must go on with the job the folder started: the same glossary, speech table and
book memo are shown, nothing is asked, no name extraction / speech table is made again, and
only the untranslated rest is requested. Resuming the folder afterwards translates nothing of
that file again. Holds for 각 파일 독립 작품 and 한 작품의 연속 파일 (whose glossary and speech
table belong to the folder, and whose job also carries the previous file's closing context).
"""
from __future__ import annotations

import time

import pytest

from test_alpha17 import qt_app, sandbox  # noqa: F401
from test_v258_folder_terms_speech import (world, book, _new_window, _open_folder, _run, _saved_chunks,  # noqa: F401
                                           _jobs, _log, _no_double_translation)


def _single_run(qt_app, w, timeout=60):
    w.last_result_path = None
    w.start_translation()
    end = time.time() + timeout
    while time.time() < end:
        qt_app.processEvents(); time.sleep(0.005)
        if (w.last_result_path is not None and not (w.worker and w.worker.isRunning())
                and w._term_thread is None and w.start.text() == '번역 시작 / 계속'):
            break
    for _ in range(30):
        qt_app.processEvents()
    assert time.time() < end, w.error_view.toPlainText()[-1500:]


@pytest.mark.parametrize('relation', ['independent', 'continuous'])
@pytest.mark.parametrize('memo', ['', '임우는 주인공이다. 존댓말을 쓴다.'])
def test_folder_file_translated_alone_after_a_stop_goes_on_with_the_folder_job(qt_app, world, monkeypatch, relation, memo):
    books = world.root / 'books'; books.mkdir()
    for n, names, tag in (('1.txt', ['林雨', '苏晴'], 'A'), ('2.txt', ['林雨', '赵敏'], 'B'), ('3.txt', ['周芸', '韩冬'], 'C')):
        (books / n).write_text(book(names, tag, n=60), encoding='utf-8')
    w = _new_window(world, monkeypatch)
    asks = []
    monkeypatch.setattr(w, '_ask_identity_change', lambda info: asks.append(info) or 'cancel')
    try:
        _open_folder(w, books, relation)
        if memo:
            w.batch_memory_common.setChecked(True)
            w.book_memory.setPlainText(memo)
        assert _run(qt_app, w, stop_when=lambda: _saved_chunks(world, '2.txt') >= 1) == 'stopped'
        job_terms = _jobs(world, books / '2.txt')[-1]
        folder_rows = None
        # what the folder job of 2.txt used
        w._open_txt_path(str(books / '2.txt'))
        assert w.input_mode == 'single'
        assert w._terms() == job_terms, 'the glossary the folder job translated with'
        assert w._speech_rows(), 'the speech table the folder job used'
        assert w.book_memory.toPlainText().strip() == memo
        tc, sc, before = len(world.term_calls), len(world.speech_calls), len(world.translated)
        world.run += 1
        _single_run(qt_app, w)
        assert not asks, [a.get('labels') for a in asks]
        assert not world.asked, world.asked
        assert world.term_calls[tc:] == [] and world.speech_calls[sc:] == [], 'nothing analysed again'
        paras = [x for x in (books / '2.txt').read_text(encoding='utf-8').split('\n\n') if x]
        asked_for = [b for f, b, r in world.translated[before:]]
        assert 0 < len(asked_for) < len(paras), 'only the untranslated rest'
        assert _jobs(world, books / '2.txt') == [job_terms], 'still the folder job'
        # The folder again: file 2 is finished, nothing of it is requested.
        before = len(world.translated)
        _open_folder(w, books, relation)
        assert _run(qt_app, w) == 'done'
        assert [b for f, b, r in world.translated[before:] if f == '2.txt'] == []
        assert (books / '한국어_번역' / '2.ko.txt').exists() and (books / '2.ko.txt').exists()
        assert not world.asked and not asks
    finally:
        w.close()


def test_what_the_user_set_for_a_file_alone_is_not_overwritten_by_a_continuous_folder(qt_app, world, monkeypatch):
    books = world.root / 'books'; books.mkdir()
    for n, names, tag in (('1.txt', ['林雨', '苏晴'], 'A'), ('2.txt', ['林雨', '赵敏'], 'B')):
        (books / n).write_text(book(names, tag, n=30), encoding='utf-8')
    w = _new_window(world, monkeypatch)
    try:
        w._open_txt_path(str(books / '2.txt'))
        w._fill_term_table({'赵敏': '조민'}, {'赵敏': '조민'}); w._save_workspace()
        _open_folder(w, books, 'continuous')
        assert _run(qt_app, w) == 'done'
        w._open_txt_path(str(books / '2.txt'))
        assert w._terms() == {'赵敏': '조민'}, 'the file entry the user made keeps its own glossary'
    finally:
        w.close()
