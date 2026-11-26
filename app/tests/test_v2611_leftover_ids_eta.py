"""v2.6.11: what a user saw after a folder run of a Chinese novel.

1. "P017860:" on its own line and "…</P001454:" at a sentence end inside the Korean result:
   paragraph IDs the model wrote in a broken form (no angle brackets / a colon for ">") that the
   tag parser did not see. They are now taken out when the answer comes in, and when the result
   is written or shown, so a book translated before comes out clean without translating again.
2. The same paragraphs came back as "미번역 남음" on every run and the 미완료 목록 tab was empty:
   a Hanja gloss in brackets ("무림(武林)") counted as untranslated, the model wrote it again
   each time, and a finished book has no unfinished chunks to list. Glosses no longer count,
   what is still left is listed with its place, and a fully translated book is not told to
   "press 다시 시작".
3. A meaningless Latin fragment ("ber", "ing") in the Korean of a Chinese / Japanese book is
   found and that paragraph is translated again.
4. The remaining time while retrying the untranslated parts is counted in paragraphs.
5. A new folder run keeps the earlier 번역_요약.txt as 번역_요약_날짜-시각.txt.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

from novel_translator.batch import discover_txt_files, is_generated_txt, keep_summary_aside
from novel_translator.chunking import Paragraph, Chunk
from novel_translator.rendering import render_snapshot
from novel_translator.validation import (leftover_needs_fix, normalize_translation_output, parse_tagged_translation,
                                         strip_id_residue, stray_latin, validate_translation)

from test_v264_mixed_english_and_glued_hanja import _Provider, _rows, _worker
from test_alpha17 import qt_app, sandbox, _window  # noqa: F401 (fixtures)


# ---------------------------------------------------------------- 1. paragraph ID pieces
SRC1 = '<P017860>\n两人经过两次交手，生疏感似乎消失了，'
SRC2 = '<P001454>\n他笑了。\n\n<P001455>\n她走了。'


def _parsed(raw, src):
    out = normalize_translation_output(raw, src)
    validate_translation(src, out)
    return parse_tagged_translation(out)


def test_bare_id_of_a_single_paragraph_becomes_its_tag():
    for raw in ('P017860:\n두 사람은 서먹함이 사라진 듯,', 'P017860: 두 사람은 서먹함이 사라진 듯,',
                '<P017860>\nP017860:\n두 사람은 서먹함이 사라진 듯,', '**P017860**:\n두 사람은 서먹함이 사라진 듯,'):
        assert _parsed(raw, SRC1) == {17860: '두 사람은 서먹함이 사라진 듯,'}, raw


def test_broken_closing_and_bare_ids_in_several_paragraphs():
    want = {1454: '그가 웃었다.', 1455: '그녀가 떠났다.'}
    for raw in ('<P001454>\n그가 웃었다.</P001454:\n\n<P001455>\n그녀가 떠났다.',
                '<P001454>\n그가 웃었다.</P001454>\n\n<P001455>\n그녀가 떠났다.',
                '<P001454>\n그가 웃었다.\n\nP001455:\n그녀가 떠났다.',
                '<P001454>\n그가 웃었다.\nP001455: 그녀가 떠났다.',
                '<P001454>\n그가 웃었다.<P001455:\n그녀가 떠났다.',
                '<P001454>\n그가 웃었다. </ P001454 >\n\n<P001455>\n그녀가 떠났다.'):
        assert _parsed(raw, SRC2) == want, raw


def test_ids_not_sent_and_prose_are_never_touched():
    src = '<P000001>\n编号P123456:是他的。'
    out = normalize_translation_output('<P000001>\n번호 P123456:은 그의 것이다.', src)
    assert parse_tagged_translation(out) == {1: '번호 P123456:은 그의 것이다.'}
    # a stray ID of another request is not this request's: left for the other checks
    out = normalize_translation_output('<P000001>\n그는 웃었다.</P999999:', '<P000001>\n他笑了。')
    assert parse_tagged_translation(out)[1].endswith('</P999999:')


def test_saved_text_is_cleaned_when_shown():
    assert strip_id_residue('P017860:\n두 사람은 듯,', 17860) == '두 사람은 듯,'
    assert strip_id_residue('P017861:\n두 사람은 듯,', 17860) == '두 사람은 듯,'          # a neighbour's ID
    assert strip_id_residue('　그가 웃었다.</P001454:', 1454) == '　그가 웃었다.'   # indent kept
    assert strip_id_residue('그 P123456: 번호', 1) == '그 P123456: 번호'                  # far away: prose
    assert strip_id_residue('평범한 문장.', 5) == '평범한 문장.'
    assert strip_id_residue('P000005:\n원문', 5, source='P000005: 원문') == 'P000005:\n원문'  # the source's own


def test_result_file_of_an_earlier_build_comes_out_clean(tmp_path):
    ps = [Paragraph(17860, '两人经过两次交手。', '\n', '', True), Paragraph(17861, '他笑了。', '\n', '', True)]
    chunk = Chunk(0, ps, 10, 10)
    src_tagged = '<P017860>\n两人经过两次交手。\n\n<P017861>\n他笑了。'
    row = {'chunk_index': 0, 'status': 'COMPLETED', 'source_text': src_tagged, 'error_type': None, 'error_message': None,
           'translation': '<P017860>\nP017860:\n두 사람은 두 번 겨루었다.\n\n<P017861>\n그가 웃었다.</P017861:'}
    source = tmp_path / 'book.txt'; source.write_text('两人经过两次交手。\n他笑了。\n', encoding='utf-8')
    out, failed, report, _ = render_snapshot([chunk], [row], source, tmp_path / 'out', tmp_path / 'fb', ps,
                                             owners_root=tmp_path / 'own')
    text = Path(out).read_text(encoding='utf-8-sig')
    assert not failed and 'P0178' not in text
    assert '두 사람은 두 번 겨루었다.' in text and '그가 웃었다.' in text


def test_an_id_answer_from_the_model_is_stored_clean(tmp_path):
    text = '两人经过两次交手，生疏感似乎消失了。\n\n他笑了。'
    prov = _Provider({'两人': 'P000001:\n두 사람은 두 번 겨루며 서먹함이 사라졌다.', '他笑': '그가 웃었다.'})
    w, _ = _worker(tmp_path, text, prov)
    w.run()
    stored = _rows(tmp_path)[0]['translation']
    assert 'P000001:' not in stored and '서먹함이 사라졌다' in stored
    assert w.leftover_count == 0


# ---------------------------------------------------------------- 2. glosses and the endless loop
def test_hanja_glosses_are_not_untranslated():
    for ok in ('그는 무림(武林)의 고수였다.', '강호（江湖）에서 이름을 날렸다.', '도(道)와 덕(德)을 말했다.', '검 (劍)을 뽑았다.'):
        assert not leftover_needs_fix(ok), ok
    for bad in ('철钳처럼 붙잡았다', '그 남자 武林 고수', '그는 「天下第一」 현판을 보았다', 'たべ(よみ)가 남았다', '윤滑하다'):
        assert leftover_needs_fix(bad), bad


def test_a_two_character_gloss_is_accepted_without_any_repair(tmp_path):
    text = '他是武林中的绝顶高手。'
    prov = _Provider({'他是武林': '그는 무림(武林)에서 손꼽히는 고수였다.'})
    w, events = _worker(tmp_path, text, prov)
    w.run()
    assert w.leftover_count == 0 and w.leftover_report == ''
    assert len(prov.requests) == 1, 'no repair request, no retry'


def test_what_is_left_is_listed_with_its_place(tmp_path):
    text = '江浩的手像铁钳一样紧紧抓住她的右腿，她完全动弹不得。'
    bad = '강호의 손이 철钳처럼 그녀의 오른쪽 다리를 꽉 붙잡고 있어 전혀 움직일 수 없었다.'
    w, _ = _worker(tmp_path, text, _Provider({'江浩': bad}))
    w.run()
    assert w.leftover_count == 1
    rows = [l for l in w.leftover_report.splitlines() if l.startswith('청크 ')]
    assert len(rows) == 1 and 'P000001' in rows[0] and '钳' in rows[0] and '철钳처럼' in rows[0]


# ---------------------------------------------------------------- 3. stray Latin fragments
def test_stray_latin_rule():
    assert stray_latin('他说了一句话。', '그는 ber 한마디 했다.', 'zh') == ['ber']
    assert stray_latin('他走了。', '그가 떠났ing다.', 'ja') == ['ing']
    for src, body in (('他说OK。', '그는 OK라고 했다.'), ('走了五公里。', '5km를 걸었다.'), ('他说 hello 你好', '그는 hello라고 말했다.'),
                      ('等级5', 'Lv.5 레벨'), ('他', '그는 <br> 갔다'), ('宁', '이건 Ning 이다'), ('他', '⟦NT_SKIP⟧'),
                      ('网址', 'www.example.com 에 있다'), ('他', 'SKILL 발동')):
        assert stray_latin(src, body, 'zh') == [], body
    assert stray_latin('he', '그는 ber 했다', 'en') == []                   # English books are never checked
    assert stray_latin('他', '그는 kel 했다', 'zh', allowed='켈kel') == []    # in the confirmed glossary


def test_a_stray_fragment_is_found_and_only_that_paragraph_is_sent_again(tmp_path):
    text = '他说了一句话。\n\n她笑了。'
    prov = _Provider({'他说': '그는 ber 한마디 했다.', '她笑': '그녀가 웃었다.'})
    w, events = _worker(tmp_path, text, prov)
    w.run()
    assert w.leftover_count == 1 and 'ber' in w.leftover_report
    # the next run (automatic retry) translates just that paragraph again
    prov2 = _Provider({'他说': '그는 한마디 했다.', '她笑': '(보내면 안 됨)'})
    w2, _ = _worker(tmp_path, text, prov2)
    w2.run()
    assert w2.leftover_count == 0
    sent = '\n'.join(r.user_prompt for r in prov2.requests)
    assert '他说' in sent and '她笑' not in sent
    final = _rows(tmp_path)[0]['translation']
    assert '그는 한마디 했다.' in final and '그녀가 웃었다.' in final and 'ber' not in final


# ---------------------------------------------------------------- 4. remaining time of a retry run
def test_retry_estimate_counts_paragraphs(qt_app, sandbox, monkeypatch):
    win = _window(monkeypatch)
    try:
        win.input_mode = 'single'
        book = Path('book.txt')
        main = SimpleNamespace(eta_left=set(), eta_samples=[9.0] * 30, eta_all=[9.0] * 30, eta_trouble_only=False, source_path=book)
        win._eta_keep_history(main)                                      # the main run: 9 s per chunk
        w = SimpleNamespace(eta_left={1, 2, 3}, eta_samples=[], eta_all=[], eta_trouble_only=True, source_path=book,
                            eta_paras={1: 2, 2: 1, 3: 3}, eta_paras_done=0)
        win.worker = w
        assert round(win._retry_eta_seconds()) == 18                     # 6 paragraphs x (9 s / 3)
        w.eta_left = {3}; w.eta_all = [12.0, 6.0]; w.eta_paras_done = 3   # measured 6 s per paragraph
        assert round(win._retry_eta_seconds()) == round(3 * ((6.0 * 3 + 3.0 * 7) / 10))
        w.eta_all = [60.0] * 4; w.eta_paras_done = 12                    # enough measured: own speed only
        w.eta_left = {3}
        assert round(win._retry_eta_seconds()) == 60
        win.worker = SimpleNamespace(eta_left={1}, eta_samples=[], eta_all=[], eta_trouble_only=True,
                                     source_path=Path('other.txt'), eta_paras={1: 4}, eta_paras_done=0)
        assert win._retry_eta_seconds() is None                          # another book, nothing measured yet
        win.worker = SimpleNamespace(eta_left={1}, eta_samples=[], eta_all=[], eta_trouble_only=False,
                                     source_path=book, eta_paras={}, eta_paras_done=0)
        assert win._retry_eta_seconds() is None                          # an ordinary run: the old estimate
    finally:
        win.worker = None; win.close()


def test_worker_knows_how_many_paragraphs_a_retry_sends(tmp_path):
    text = '他说了一句话。\n\n她笑了。\n\n他们走了。'
    w, _ = _worker(tmp_path, text, _Provider({'他说': '그는 ber 한마디 했다.', '她笑': '그녀가 웃었다.', '他们': '그들은 떠났다.'}))
    w.run()
    w2, _ = _worker(tmp_path, text, _Provider({'他说': '그는 한마디 했다.', '她笑': 'x', '他们': 'x'}))
    seen = {}
    orig = w2._eta_paragraphs
    w2._eta_paragraphs = lambda *a: seen.setdefault('v', orig(*a))
    w2.run()
    assert w2.eta_trouble_only is True and seen['v'] == {0: 1}           # one paragraph, not the whole chunk
    assert w2.eta_paras_done == 1


# ---------------------------------------------------------------- 5. earlier folder summaries
def test_an_earlier_summary_is_kept_aside(tmp_path):
    s = tmp_path / '번역_요약.txt'
    assert keep_summary_aside(s) is None                                  # nothing yet
    s.write_text('첫 번째', encoding='utf-8')
    kept = keep_summary_aside(s)
    assert kept and kept.read_text(encoding='utf-8') == '첫 번째' and not s.exists()
    assert re.fullmatch(r'번역_요약_\d{8}-\d{6}\.txt', kept.name)
    s.write_text('두 번째', encoding='utf-8'); os.utime(s, (kept.stat().st_mtime, kept.stat().st_mtime))
    kept2 = keep_summary_aside(s)
    assert kept2 != kept and kept2.read_text(encoding='utf-8') == '두 번째'   # same second: a new name
    for i in range(8):
        s.write_text(str(i), encoding='utf-8'); t = time.time() - 1000 + i * 10; os.utime(s, (t, t)); keep_summary_aside(s)
    old = [p for p in tmp_path.iterdir() if p.name.startswith('번역_요약_')]
    assert len(old) == 5                                                  # only the newest few


def test_kept_summaries_are_never_taken_as_books(tmp_path):
    (tmp_path / '번역_요약_20261125-204000.txt').write_text('요약', encoding='utf-8')
    (tmp_path / '번역_요약.txt').write_text('요약', encoding='utf-8')
    (tmp_path / '번역_요약_메모.txt').write_text('책', encoding='utf-8')   # a book named like this still counts
    (tmp_path / 'book.txt').write_text('他', encoding='utf-8')
    names = sorted(p.name for p in discover_txt_files(tmp_path))
    assert names == ['book.txt', '번역_요약_메모.txt']
    assert is_generated_txt(Path('번역_요약_20261125-204000_2.txt'))


# ---------------------------------------------------------------- 2b. what the window says at the end
import novel_translator.ui.main_window as ui  # noqa: E402
from test_alpha17 import _fake_worker_class, _wait  # noqa: E402
from test_v241_folder_auto_retry import _folder  # noqa: E402

REPORT = ('한자·가나·영어 조각이 남은 문단 (번역은 끝남)\n※ 안내\n\n'
          '청크 3\tP017860\t남은 글자: 钳\t…철钳처럼 붙잡고…\n')


def _leftover_worker(started, scripts):
    """scripts: {file name: [(status, failed chunks, leftover paragraphs), ...]}; leftovers come with a list."""
    Base = _fake_worker_class(started)

    class Scripted(Base):
        def run(self):
            name = self.source_path.name
            n = sum(1 for s in started if s.source_path.name == name) - 1
            seq = scripts[name]; status, failed, leftover = seq[min(n, len(seq) - 1)]
            self.out_dir.mkdir(parents=True, exist_ok=True)
            out = self.out_dir / (self.source_path.stem + ('.ko.txt' if status == 'completed' else '.ko.partial.txt'))
            out.write_text('번역', encoding='utf-8'); self.leftover_count = leftover
            self.leftover_report = REPORT if leftover else ''
            self.pending_report = ('' if status == 'completed' else
                                   '야문 Novel Translator 미완료 구간\n\n' + ''.join(f'청크 {i+1}\tPARTIAL\tg\tm\tREFUSAL\t일부\n' for i in range(failed)))
            self.result_ready.emit(status, str(out), failed); self.finished_ok.emit(str(out))
    return Scripted


def _run(qt_app, monkeypatch, sandbox, scripts, folder=True):
    root, _ = sandbox; started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _leftover_worker(started, scripts))
    w = _window(monkeypatch); infos = []
    monkeypatch.setattr(ui.QMessageBox, 'information', lambda *a, **k: infos.append((a[1], a[2])))
    if folder:
        books = _folder(root, tuple(scripts))
        w._prepare_new_input(); w.input_mode = 'folder'; w.batch_root = books
        w._apply_profile('zh-ko'); w._scan_batch_files(); w._update_ready_state()
    else:
        p = root / 'book.txt'; p.write_text('第一章\n\n他静静地看着窗外的雨。', encoding='utf-8')
        w._open_txt_path(str(p))
    w.start_translation()
    assert _wait(qt_app, lambda: infos), infos
    for _ in range(20): qt_app.processEvents()
    return w, infos, started


def test_single_file_fully_translated_is_not_told_to_start_again(qt_app, sandbox, monkeypatch):
    w, infos, started = _run(qt_app, monkeypatch, sandbox, {'book.txt': [('completed', 0, 1)]}, folder=False)
    try:
        assert len(started) == 2                                          # one automatic retry, no progress: stop
        title, text = infos[-1]
        assert title == '번역 완료 · 확인할 곳 있음'
        assert '모든 문단이 번역되었습니다' in text and '[번역 시작 / 계속]을 누르면' not in text
        assert 'P017860' in w.pending_view.toPlainText()                  # the list is not empty any more
    finally: w.close()


def test_single_file_with_source_left_is_unchanged(qt_app, sandbox, monkeypatch):
    w, infos, _ = _run(qt_app, monkeypatch, sandbox, {'book.txt': [('partial', 2, 1)]}, folder=False)
    try:
        title, text = infos[-1]
        assert title == '번역 완료 · 일부 남음' and '[번역 시작 / 계속]을 누르면' in text
        pending = w.pending_view.toPlainText()
        assert '청크 1\tPARTIAL' in pending and 'P017860' in pending      # both lists
    finally: w.close()


def test_folder_fully_translated_files_list_their_places(qt_app, sandbox, monkeypatch):
    w, infos, started = _run(qt_app, monkeypatch, sandbox, {'1.txt': [('completed', 0, 1)], '2.txt': [('completed', 0, 0)]})
    try:
        title, text = infos[-1]
        assert title == '폴더 번역 완료 · 확인할 곳 있음' and '1.txt' in text
        assert '[번역 시작 / 계속]을 누르면' not in text
        pending = w.pending_view.toPlainText()
        assert '── 1.txt ──' in pending and 'P017860' in pending and '2.txt' not in pending
        summary = w.last_result_path.read_text(encoding='utf-8')
        assert '확인할 곳(한자·가나·영어 조각)이 남은 파일 1개' in summary and 'P017860' in summary
        assert '번역되지 않은 부분이 남은 파일' not in summary
        assert '모든 문단 번역됨 · 확인할 곳이 남은 파일 1개' in w.error_view.toPlainText()
    finally: w.close()


def test_folder_with_source_left_still_says_so(qt_app, sandbox, monkeypatch):
    w, infos, _ = _run(qt_app, monkeypatch, sandbox, {'1.txt': [('partial', 2, 0)], '2.txt': [('completed', 0, 1)]})
    try:
        title, text = infos[-1]
        assert title == '폴더 번역 완료 · 일부 남음'
        summary = w.last_result_path.read_text(encoding='utf-8')
        assert '번역되지 않은 부분이 남은 파일 1개' in summary and '확인할 곳(한자·가나·영어 조각)이 남은 파일 1개' in summary
    finally: w.close()


def test_a_second_folder_run_keeps_the_first_summary(qt_app, sandbox, monkeypatch):
    w, infos, _ = _run(qt_app, monkeypatch, sandbox, {'1.txt': [('completed', 0, 0)]})
    try:
        out = w.last_result_path.parent
        first = w.last_result_path.read_text(encoding='utf-8')
        infos.clear(); w.start_translation()
        assert _wait(qt_app, lambda: infos)
        kept = [p for p in out.iterdir() if p.name.startswith('번역_요약_')]
        assert len(kept) == 1 and kept[0].read_text(encoding='utf-8') == first
        assert (out / '번역_요약.txt').exists()
    finally: w.close()


def test_removing_an_id_piece_never_glues_two_words():
    src = '<P001454>\n他笑了。\n\n<P001455>\n她走了。'
    for raw in ('<P001454>\n그가 웃었다. </P001454> 그리고\n\n<P001455>\n그녀가 떠났다.',
                '<P001454>\n그가 웃었다. </P001454: 그리고\n\n<P001455>\n그녀가 떠났다.'):
        assert parse_tagged_translation(normalize_translation_output(raw, src))[1454] == '그가 웃었다. 그리고', raw
    assert _parsed('<P001454>\n그가 웃었다.\n\n<P001455 >\n그녀가 떠났다.', src) == {1454: '그가 웃었다.', 1455: '그녀가 떠났다.'}
    assert strip_id_residue('웃었다. </P001454> 그리고', 1454) == '웃었다. 그리고'
