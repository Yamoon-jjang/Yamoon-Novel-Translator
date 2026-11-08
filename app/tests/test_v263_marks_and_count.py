"""v2.6.3: invisible marks (zero-width characters) and the shown character count.

- A copyright/distributor mark hidden in the source with zero-width characters never goes to
  a model, and it is written back into the result in the same paragraph, in the same order:
  complete results, partial results of a stopped run, the live view, a resumed run and
  Korean refinement alike.
- The job a book had before v2.6.3 is the same job (chunk text and fingerprint unchanged),
  so a half-translated book continues instead of starting over.
- A book without such characters is handled exactly as before.
- The shown character count leaves line breaks out (a Windows CR LF is not two characters).
"""
from __future__ import annotations

import re

from novel_translator import hidden_marks as hm
from novel_translator.chunking import split_paragraphs, mark_translatable, make_chunks
from novel_translator.refinement_runtime import finalize_body, assemble, refinement_prompt
from novel_translator.ui import main_window as ui

from test_alpha17 import qt_app, sandbox, _window  # noqa: F401
import test_v252_resume_speed as rs

MARK = '⁣​​‌​‌‌​⁤'
ZW = re.compile('[​-‏⁠-⁤﻿]')


def _marked_book(n=300):
    paras = [f'第{i}段。他站在窗边看着外面的雨，心里很沉重。' for i in range(n)]
    paras[0] = '第一章' + MARK + ' 红色叶脉'
    paras[150] = '她笑了。' + MARK
    paras[299] = MARK + '最后一段。他走了。'
    return '\n\n'.join(paras)


def _requests_have_no_marks():
    return not any(ZW.search(p) for p in rs.Provider.prompts)


# ---------------------------------------------------------------- the module itself
def test_text_without_marks_goes_through_unchanged():
    for t in ['', '他站在窗边。', 'He said "hi".\r\n', '👨‍👩‍👧 가족']:
        assert hm.strip(t) == t and hm.restore(t, '번역문') == '번역문' and hm.count(t) == 0
    assert hm.restore('原文', None) is None


def test_restore_puts_every_run_back_in_order():
    src = '​开头。中间‌的话。结尾' + MARK
    out = hm.restore(src, '시작. 중간의 말. 끝')
    assert hm.strip(out) == '시작. 중간의 말. 끝'
    assert out.startswith('​') and out.endswith(MARK) and '‌' in out
    assert [m.group(0) for m in hm.MARK_RE.finditer(out)] == [m.group(0) for m in hm.MARK_RE.finditer(src)]
    # never twice, and never inside a word
    assert hm.restore(src, out) == out and hm.restore(src, src) == src
    mid = hm.restore('第一章' + MARK + ' 红色叶脉', '제1장 붉은 잎맥')
    assert mid == '제1장' + MARK + ' 붉은 잎맥'


def test_a_paragraph_kept_as_source_gets_its_marks_exactly_back():
    src = '他站\u200b在窗边，' + MARK + '看着雨。'
    assert hm.restore(src, hm.strip(src)) == src


def test_stray_pieces_a_model_wrote_are_replaced_by_the_real_mark():
    out = hm.restore('第一章' + MARK + ' 红色叶脉', '제1장​ 붉은 잎맥')
    assert hm.strip(out) == '제1장 붉은 잎맥' and out.count(MARK) == 1 and hm.count(out) == len(MARK)


def test_emoji_joiner_alone_is_not_a_mark():
    assert hm.strip('a👨‍👩b') == 'a👨‍👩b'
    assert hm.strip('a‍​b') == 'ab'


# ---------------------------------------------------------------- translation flow
def test_complete_result_keeps_the_marks_and_requests_never_carry_them(tmp_path, qt_app):
    text = _marked_book()
    w, res, live, issues, cs = rs._run(tmp_path, text, {})
    assert res[0] == 'completed' and _requests_have_no_marks()
    out = (tmp_path / 'out' / 'book.ko.txt').read_text(encoding='utf-8-sig')
    assert out.count(MARK) == 3 and hm.count(out) == 3 * len(MARK)
    assert out.split('\n')[0].startswith('그는') and MARK in out.split('\n')[0]
    assert out.rstrip().startswith(MARK) is False   # the first paragraph's mark sits after its first word
    assert ''.join(live).count(MARK) == 3           # the live view (copy / save while running) too


def test_partial_result_of_a_stopped_run_keeps_every_mark_once(tmp_path, qt_app):
    text = _marked_book()
    w, res, live, issues, cs = rs._run(tmp_path, text, {}, stop_at=3)
    assert res[0] == 'stopped' and _requests_have_no_marks()
    part = (tmp_path / 'out' / 'book.ko.partial.txt').read_text(encoding='utf-8-sig')
    assert part.count(MARK) == 3 and hm.count(part) == 3 * len(MARK)
    # untranslated paragraphs are the source as it was, marks included
    assert MARK + '最后一段。他走了。' in part
    # resume: the same job continues (no chunk starts over) and the marks stay once each
    done_before = sum(1 for r in rs._db(tmp_path).all_chunks(1) if r['status'] == 'COMPLETED')
    w, res, live, issues, cs = rs._run(tmp_path, text, {})
    assert res[0] == 'completed' and rs.Provider.calls == len(cs) - done_before
    out = (tmp_path / 'out' / 'book.ko.txt').read_text(encoding='utf-8-sig')
    assert out.count(MARK) == 3 and hm.count(out) == 3 * len(MARK)


def test_the_job_identity_still_holds_the_marks(tmp_path, qt_app):
    """Chunk text (database, translation memory, fingerprint) is the source as it is:
    a book started before v2.6.3 is the same job."""
    text = _marked_book()
    rs._run(tmp_path, text, {}, stop_at=2)
    rows = rs._db(tmp_path).all_chunks(1)
    assert sum(r['source_text'].count(MARK) for r in rows) == 3
    ps = mark_translatable(split_paragraphs(text), 'zh'); cs = make_chunks(ps, 'zh', 1000, 1, 4000)
    assert [c.source_text for c in cs] == [r['source_text'] for r in rows]


def test_a_book_without_marks_is_translated_as_before(tmp_path, qt_app):
    text = rs._book()
    w, res, live, issues, cs = rs._run(tmp_path, text, {})
    out = (tmp_path / 'out' / 'book.ko.txt').read_text(encoding='utf-8-sig')
    assert res[0] == 'completed' and hm.count(out) == 0
    # the requests are exactly the chunks' text
    sources = [c.source_text for c in cs]
    assert all(any(s in p for p in rs.Provider.prompts) for s in sources)


# ---------------------------------------------------------------- Korean refinement
def test_refinement_never_sends_marks_and_writes_them_back():
    text = '　제1장' + MARK + ' 붉은 잎맥\n\n그는 웃었다.'
    ps = split_paragraphs(text)
    system, user = refinement_prompt(ps)
    assert not ZW.search(user)
    edited = {str(ps[0].pid): '제1장 붉은 잎맥', str(ps[1].pid): '그가 웃었다.'}
    out = assemble(ps, edited, True, 'straight')
    assert out.count(MARK) == 1 and out.startswith('　제1장' + MARK)
    raw = assemble(ps, edited)
    assert raw.count(MARK) == 1
    assert finalize_body(ps[0].text, ps[0].text, True) == ps[0].text   # unchanged = once


# ---------------------------------------------------------------- shown character count
def test_character_count_leaves_line_breaks_out():
    assert ui.MainWindow._char_count('가나\r\n다라\n마') == 5
    assert ui.MainWindow._char_count('가나\n다라\n마') == 5
    assert ui.MainWindow._char_count('') == 0 and ui.MainWindow._char_count(None) == 0
    assert ui.MainWindow._char_count('第一章' + MARK + ' 红色叶脉\r\n') == 8   # invisible marks are no letters


def test_opened_crlf_file_and_pasted_text_show_the_same_count(qt_app, sandbox, monkeypatch, tmp_path):
    body = '他站在窗边。\r\n\r\n她笑了。\r\n'
    f = tmp_path / 'crlf.txt'; f.write_bytes(body.encode('utf-8'))
    w = _window(monkeypatch)
    try:
        w._open_txt_path_now(str(f))
        assert '10자(줄바꿈 제외)' in w.file_label.text()
        w.preview.setPlainText(body)   # pasted: Qt keeps LF only
        assert w._char_count(w.preview.toPlainText()) == 10
    finally:
        w.close()


def test_opening_a_marked_book_says_the_marks_are_kept(qt_app, sandbox, monkeypatch, tmp_path):
    f = tmp_path / 'marked.txt'; f.write_bytes(('第一章' + MARK + ' 红色叶脉\n\n他走了。').encode('utf-8'))
    w = _window(monkeypatch)
    try:
        w._open_txt_path_now(str(f))
        log = w.error_view.toPlainText()
        assert f'보이지 않는 표식 1곳(문자 {len(MARK)}개)' in log and '보존' in log
        assert MARK in w.source_text   # the source itself is never changed
    finally:
        w.close()
