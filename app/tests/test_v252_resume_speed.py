"""v2.5.2: continuing a long book after stopping and restarting the program.

- settings that make a different job (glossary, 작품 메모, mode, a new program version ...)
  no longer throw a half-translated book away without asking;
- the job identity itself is unchanged (books from v2.5 continue as they were);
- a long book's glossary no longer makes every local LLM request (and its context) larger;
- big books start faster (one transaction), the live view and the unfinished list are
  complete and short, the glossary question / speech table are not asked again mid-book;
- opening decodes a book once, PDFs are read by several processes and kept, pasted text
  gets its source language, the window comes back where it was.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from novel_translator.providers.base import TranslationResponse, TranslationRequest
from novel_translator.chunking import split_paragraphs, mark_translatable, make_chunks
from novel_translator.profiles import ProfileManager
from novel_translator.storage import Database
from novel_translator import orchestrator as orch_mod
from novel_translator import prompting
from novel_translator.ui import worker as wm
from novel_translator.ui import main_window as ui

from test_alpha17 import qt_app, sandbox, _window, _wait  # noqa: F401


# ---------------------------------------------------------------- helpers
class Provider:
    """Answers every paragraph ID with a Korean sentence; can stop the worker at call N."""
    prompts: list[str] = []
    stop_at = None
    worker = None
    calls = 0

    def __init__(self, *a, **k):
        self.last_usage = None

    def translate(self, req, key):
        cls = type(self)
        cls.calls += 1; cls.prompts.append(req.user_prompt)
        if cls.stop_at is not None and cls.calls >= cls.stop_at and cls.worker is not None:
            cls.worker.stop()
        ids = []
        for i in re.findall(r'<P(\d{6})>', req.user_prompt.split('[SOURCE - ')[-1]):
            if i not in ids:
                ids.append(i)
        return TranslationResponse('\n\n'.join(f'<P{i}>\n그는 창가에 서서 밖에 내리는 비를 오래 바라보았다.' for i in ids))


def _book(n=400, extra=''):
    return '\n\n'.join(f'第{i}段。他站在窗边看着外面的雨，心里很沉重。{extra}' for i in range(n))


def _chunks(text, lang='zh', target=1000):
    ps = mark_translatable(split_paragraphs(text), lang)
    return ps, make_chunks(ps, lang, target, 1, 4000)


def _run(tmp, text, terms, stop_at=None, mode='balanced', book_memory='', target=1000, restart=False):
    src = tmp / 'book.txt'; raw = text.encode('utf-8'); src.write_bytes(raw)
    prof = ProfileManager().set_active('zh-ko')
    ps, cs = _chunks(text, target=target)
    Provider.calls = 0; Provider.prompts = []; Provider.stop_at = stop_at
    with patch.object(wm, 'DATA_DIR', tmp / 'data'), patch.object(wm, 'GeminiProvider', Provider), \
            patch.object(orch_mod.TranslationOrchestrator, '_pace', lambda self: None):
        w = wm.TranslationWorker(src, raw, prof, cs, 'fixture', [{'id': 'k', 'secret': 'dummy', 'enabled': True}], dict(terms),
                                 mode, tmp / 'out', book_memory, all_paragraphs=ps, restart_existing=restart)
        w.max_requests_per_chunk = 999
        Provider.worker = w
        res = []; live = []; issues = []
        w.result_ready.connect(lambda *a: res.append(a)); w.failed.connect(lambda m: res.append(('FAILED', m)))
        w.chunk_translated.connect(lambda a, b, t: live.append(t)); w.issue.connect(issues.append)
        w.run()
    Provider.worker = None
    return w, res[-1] if res else None, live, issues, cs


def _db(tmp):
    return Database(tmp / 'data' / 'translations.db')


def _probe(tmp, text, terms, mode='balanced', book_memory=''):
    src = tmp / 'book.txt'; raw = text.encode('utf-8')
    prof = ProfileManager().set_active('zh-ko'); ps, cs = _chunks(text)
    w = wm.TranslationWorker(src, raw, prof, cs, 'fixture', [], dict(terms), mode, tmp / 'out', book_memory, all_paragraphs=ps)
    import hashlib
    return w, hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- job identity is unchanged
def test_job_identity_is_exactly_the_v25_one(qt_app):
    """Books started with v2.5 must continue, not start over: same fingerprint and memory key."""
    expected = {
        ('zh-ko', 2, 'balanced', 'normal', ''): ('8bb3d1a4b4c79f2e13ef61fe15ba03ce37e03c66cd185f0898e27e05e3e26277', '2a0d6b11d1cc8a2b3d8bb04aee6a39051caa163a8c02b58273a2aa467f51e632'),
        ('ja-ko', 0, 'quality', 'natural', '인물 관계 메모'): ('4fbbe76e78128cc0137e10fc5e0192bc6418574e8a0887d7b016771feddb6638', '39c72201e97a00c3cff3ea8e5109b2867679aca7b2f45e681209686358d8072c'),
        ('en-ko', 2, 'speed', 'normal', '인물 관계 메모'): ('0298fc5dab961c83affb430c1c17b784f9c8d1eac707c993ebb083f521e5422e', '4bf05ad8c39c54b0da8ce067f546484d479860337c22535d1f947c76f9a69520'),
    }
    # Other tests may leave the worker module's config helpers patched; use the real ones.
    from novel_translator import config as real_config, translation_settings as real_settings
    with patch.object(wm, 'load_json', real_config.load_json), patch.object(wm, 'CONFIG_DIR', real_config.CONFIG_DIR), \
            patch.object(wm, 'canonical_kind', real_settings.canonical_kind):
        _check_identity(expected)


def _check_identity(expected):
    texts = {'zh-ko': '他站在窗边。\n\n她笑了。' * 50, 'ja-ko': '彼は窓辺に立った。\n\n彼女は笑った。' * 50, 'en-ko': 'He stood.\n\nShe smiled.' * 50}
    for (prof_id, nterms, mode, style, mem), (fp, key) in expected.items():
        prof = ProfileManager().set_active(prof_id); lang = prof.source_language; text = texts[prof_id]
        ps = mark_translatable(split_paragraphs(text), lang); cs = make_chunks(ps, lang, 1000, 1, 4000)
        terms = {'他': '그', '林若雪': '임약설'} if nterms else {}
        w = wm.TranslationWorker(Path('x.txt'), text.encode(), prof, cs, 'm', [], terms, mode, None, mem, {'이름': 'a'}, '', sentence_style=style)
        assert (w._fingerprint(), w._settings_key()) == (fp, key), prof_id


# ---------------------------------------------------------------- settings changed after a stop
def test_added_term_after_stop_asks_and_continue_keeps_the_translated_part(tmp_path, qt_app):
    text = _book()
    _, res, _, _, cs = _run(tmp_path, text, {'他': '그'}, stop_at=4)
    assert res[0] == 'stopped' and len(cs) > 8
    probe, sha = _probe(tmp_path, text, {'他': '그', '窗': '창'})
    info = probe.identity_change(_db(tmp_path), sha)
    assert info and info['labels'] == ['확정 용어집'] and info['added'] == ['窗'] and not info['unknown_other']
    assert info['done'] == 3
    probe.adopt_previous(_db(tmp_path), info)
    _, res, _, issues, _ = _run(tmp_path, text, {'他': '그', '窗': '창'})
    assert res[0] == 'completed' and Provider.calls == len(cs) - 3   # only the chunks not translated yet
    # the next start is an ordinary resume: nothing to ask
    probe, sha = _probe(tmp_path, text, {'他': '그', '窗': '창'})
    assert probe.identity_change(_db(tmp_path), sha) is None


def test_starting_over_is_still_possible_and_is_the_old_behaviour(tmp_path, qt_app):
    text = _book()
    _, _, _, _, cs = _run(tmp_path, text, {'他': '그'}, stop_at=4)
    _, res, _, _, _ = _run(tmp_path, text, {'他': '그', '窗': '창'})   # no adopt: a new job
    assert res[0] == 'completed' and Provider.calls == len(cs)


def test_other_settings_are_named(tmp_path, qt_app):
    text = _book()
    _run(tmp_path, text, {'他': '그'}, stop_at=4)
    probe, sha = _probe(tmp_path, text, {'他': '그'}, mode='quality', book_memory='주인공은 형사')
    info = probe.identity_change(_db(tmp_path), sha)
    assert '작품 메모' in info['labels'] and '번역 모드(빠르게·균형·품질)' in info['labels']
    assert not info['added'] and not info['removed']


def test_pure_rename_is_still_applied_without_asking(tmp_path, qt_app):
    text = _book()
    _run(tmp_path, text, {'他': '그녀석'}, stop_at=4)
    probe, sha = _probe(tmp_path, text, {'他': '그사람'})
    assert probe.identity_change(_db(tmp_path), sha) is None


def test_rename_mixed_with_an_addition_is_carried_into_the_saved_part(tmp_path, qt_app):
    text = _book()
    _run(tmp_path, text, {'他': '그녀석', '林若雪': '임약설'}, stop_at=4)
    db = _db(tmp_path)
    probe, sha = _probe(tmp_path, text, {'他': '그녀석', '林若雪': '린뤄쉐', '窗': '창'})
    info = probe.identity_change(db, sha)
    assert info['renames'] == {'임약설': '린뤄쉐'} and info['added'] == ['窗']


def test_a_run_that_already_started_over_hands_back_to_the_bigger_job(tmp_path, qt_app):
    """v2.5 started over silently: the new job has 1 chunk, the old one more. Offer the old one."""
    text = _book()
    _, _, _, _, cs = _run(tmp_path, text, {'他': '그'}, stop_at=6)          # 5 chunks saved
    _run(tmp_path, text, {'他': '그', '窗': '창'}, stop_at=2)              # started over: 1 chunk
    probe, sha = _probe(tmp_path, text, {'他': '그', '窗': '창'})
    info = probe.identity_change(_db(tmp_path), sha)
    assert info and info['done'] == 5 and info['done_now'] == 1 and info['current_project_id']
    probe.adopt_previous(_db(tmp_path), info)
    _, res, _, _, _ = _run(tmp_path, text, {'他': '그', '窗': '창'})
    # the old job's 5 chunks are kept; the other run's chunk is reused as translation memory
    assert res[0] == 'completed' and Provider.calls <= len(cs) - 5


def test_continue_across_a_new_chunk_layout_keeps_paragraphs_and_drops_old_rows(tmp_path, qt_app):
    text = _book()
    _, _, _, _, cs = _run(tmp_path, text, {'他': '그'}, stop_at=6)
    probe, sha = _probe(tmp_path, text, {'他': '그'}, mode='quality')
    info = probe.identity_change(_db(tmp_path), sha)
    assert info
    probe.adopt_previous(_db(tmp_path), info)
    w, res, _, issues, cs2 = _run(tmp_path, text, {'他': '그'}, mode='quality', target=700)
    assert res[0] == 'completed'
    db = _db(tmp_path)
    proj = db.find_project(str(tmp_path / 'book.txt'), 'zh', 'zh-ko', sha, w._fingerprint())
    rows = db.all_chunks(int(proj['id']))
    assert [int(r['chunk_index']) for r in rows] == [c.index for c in cs2]
    assert all(r['status'] == 'COMPLETED' for r in rows)
    out = Path(res[1]).read_text(encoding='utf-8-sig')
    assert '他站在' not in out


# ---------------------------------------------------------------- big books: setup in one transaction
def test_batched_upsert_matches_the_one_by_one_upsert(tmp_path):
    a = Database(tmp_path / 'a.db'); b = Database(tmp_path / 'b.db')
    _, cs = _chunks(_book(200))
    pa = a.create_project('x', 'zh', 'zh-ko', 's'); pb = b.create_project('x', 'zh', 'zh-ko', 's')
    for c in cs: a.upsert_chunk(pa, c.index, c.source_text)
    b.upsert_chunks(pb, cs)
    a.set_chunk_status(pa, 1, 'COMPLETED', translation='t'); b.set_chunk_statuses(pb, [(1, 'COMPLETED', {'translation': 't'})])
    _, cs2 = _chunks(_book(200, extra='变'))
    for c in cs2[:3]: a.upsert_chunk(pa, c.index, c.source_text)
    b.upsert_chunks(pb, cs2[:3])
    cols = ('chunk_index', 'source_text', 'source_sha', 'status', 'translation')
    assert [tuple(r[k] for k in cols) for r in a.all_chunks(pa)] == [tuple(r[k] for k in cols) for r in b.all_chunks(pb)]


def test_resume_reports_preparation_and_requests_only_what_is_left(tmp_path, qt_app):
    text = _book()
    _, _, _, _, cs = _run(tmp_path, text, {'他': '그'}, stop_at=4)
    states = []
    src = tmp_path / 'book.txt'; raw = text.encode('utf-8')
    prof = ProfileManager().set_active('zh-ko'); ps, cs = _chunks(text)
    Provider.calls = 0; Provider.stop_at = None
    with patch.object(wm, 'DATA_DIR', tmp_path / 'data'), patch.object(wm, 'GeminiProvider', Provider), \
            patch.object(orch_mod.TranslationOrchestrator, '_pace', lambda self: None):
        w = wm.TranslationWorker(src, raw, prof, cs, 'fixture', [{'id': 'k', 'secret': 'd', 'enabled': True}], {'他': '그'}, 'balanced', tmp_path / 'out', all_paragraphs=ps)
        w.api_state.connect(states.append); w.run()
    assert states[0].startswith('번역 준비 중') and Provider.calls == len(cs) - 3


# ---------------------------------------------------------------- live view and unfinished list
def test_live_view_gets_saved_chunks_after_the_last_new_one(tmp_path, qt_app):
    text = _book()
    _, _, _, _, cs = _run(tmp_path, text, {'他': '그'}, stop_at=None)
    db = _db(tmp_path)
    proj = db.latest_project_for_source(__import__('hashlib').sha256(text.encode()).hexdigest(), 'zh', 'zh-ko')
    db.set_chunk_status(int(proj['id']), 1, 'FAILED', error_type='TRANSIENT', error_message='x')   # one early chunk left
    _, res, live, _, _ = _run(tmp_path, text, {'他': '그'})
    shown = '\n'.join(live)
    assert res[0] == 'completed' and Provider.calls == 1
    assert shown.count('그는 창가에') == len(split_paragraphs(text))   # every paragraph shown, the tail included


def test_stopped_job_lists_unreached_chunks_on_one_line(tmp_path, qt_app):
    text = _book()
    w, res, _, _, cs = _run(tmp_path, text, {'他': '그'}, stop_at=3)
    assert res[0] == 'stopped'
    lines = w.pending_report.splitlines()
    assert sum(1 for l in lines if l.startswith('아직 번역 전')) == 1
    assert not any(l.startswith('청크 ') for l in lines)
    assert w.pending_breakdown == (len(cs) - 2, 0)
    assert w.pending_text(len(cs) - 2) == f'미완료 {len(cs) - 2:,}청크 (아직 번역 전)'


# ---------------------------------------------------------------- long glossary
def _name(i):
    # distinct three-character names: no two share a pair of neighbouring characters
    return ''.join(chr(0x5000 + 3 * i + k) for k in range(3))


def _terms(n):
    return {_name(i): f'인물{i:03d}호' for i in range(n)}


def test_small_glossary_is_sent_exactly_as_before():
    terms = _terms(prompting.TERM_FILTER_MIN)
    assert prompting.relevant_terms(terms, '他走了。') is terms


def test_long_glossary_sends_the_terms_of_this_source_and_partial_names():
    terms = _terms(300); terms.update({'林若雪': '임약설', 'John Smith': '존 스미스', 'Dwarf': '드워프', 'アリス・ウェルズ': '앨리스 웰즈'})
    src = f'若雪说，{_name(7)}来了，{_name(9)[1:]}也来了。Smith and the Dwarves met アリス.'
    got = prompting.relevant_terms(terms, src)
    assert set(got) == {_name(7), _name(9), '林若雪', 'John Smith', 'Dwarf', 'アリス・ウェルズ'}
    assert list(got) == [k for k in terms if k in got]   # glossary order kept


def test_long_glossary_request_keeps_local_context_at_its_setting(tmp_path, qt_app):
    """300 terms used to raise Ollama's context 12,288 → 16,384 (more memory, slower)."""
    from novel_translator.providers.local_llm import LocalLLMProvider
    prof = ProfileManager().set_active('zh-ko')
    seen = []
    class P:
        last_usage = None
        def translate(self, req, key):
            seen.append(req)
            return TranslationResponse('\n\n'.join(f'<P{i:06d}>\n그는 창가에 서서 비를 바라보았다.' for i in range(12)))
    o = orch_mod.TranslationOrchestrator(P(), [orch_mod.KeyState('k', 's')], 'gemma4', sleep_fn=lambda s: None)
    src = '\n\n'.join(f'<P{i:06d}>\n他站在窗边看着外面的雨。{_name(i % 3)}来了。' for i in range(12))
    terms = _terms(300)
    o.translate_chunk(prof, src, terms, '그는 창가에 서서 밖에 내리는 비를 바라보았다. ' * 110)
    prompt = seen[0].user_prompt
    assert f'{_name(1)} => 인물001호' in prompt and _name(150) not in prompt
    assert LocalLLMProvider().context_for(seen[0]) == LocalLLMProvider.DEFAULT_NUM_CTX
    small = _terms(20)
    o.translate_chunk(prof, src, small, '')
    assert all(f'{a} => {b}' in seen[-1].user_prompt for a, b in small.items())


def test_long_glossary_in_a_real_run(tmp_path, qt_app):
    terms = _terms(200); terms['他'] = '그'
    _run(tmp_path, _book(60), terms)
    assert Provider.prompts and all(_name(150) not in p and '他 => 그' in p for p in Provider.prompts)


# ---------------------------------------------------------------- window: questions not repeated
def test_glossary_question_defaults_to_continuing(qt_app, sandbox, monkeypatch):
    w = _window(monkeypatch)
    try:
        captured = {}
        def fake_exec(box):
            captured['default'] = box.defaultButton().text(); return 0
        monkeypatch.setattr(QMessageBox, 'exec', fake_exec)
        w._ask_extract_on_resume()
        assert captured['default'] == '용어집 없이 이어서 번역'
    finally:
        w.close()


def test_choices_for_a_book_survive_a_program_restart(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    book = root / 'b.txt'; book.write_bytes(_book(30).encode('utf-8'))   # same bytes on Windows (no CRLF)
    w = _window(monkeypatch)
    try:
        w._open_txt_path(str(book))
        w._terms_declined_saved = True; w._speech_done_saved = True; w._save_workspace()
    finally:
        w.close()
    w2 = _window(monkeypatch)
    try:
        w2._open_txt_path(str(book))
        assert w2._terms_declined_saved and w2._speech_done_saved
        other = root / 'c.txt'; other.write_bytes(_book(31).encode('utf-8'))   # same bytes on Windows (no CRLF)
        w2._open_txt_path(str(other))
        assert not w2._terms_declined_saved and not w2._speech_done_saved
    finally:
        w2.close()


def test_speech_table_is_not_built_again_in_the_middle_of_a_book(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    book = root / 'b.txt'; book.write_bytes(_book(30).encode('utf-8'))   # same bytes on Windows (no CRLF)
    w = _window(monkeypatch)
    try:
        w._open_txt_path(str(book)); w.auto_speech.setChecked(True)
        monkeypatch.setattr(w, '_speech_people', lambda: 3)
        monkeypatch.setattr(w, '_book_has_progress', lambda: True)
        assert w._speech_auto_due()
        w._speech_done_saved = True
        assert not w._speech_auto_due()                 # tried before, part already translated
        monkeypatch.setattr(w, '_book_has_progress', lambda: False)
        assert w._speech_auto_due()                     # nothing translated yet: may try again
    finally:
        w.close()


def test_identity_question_defaults_to_continuing(qt_app, sandbox, monkeypatch):
    w = _window(monkeypatch)
    try:
        captured = {}
        def fake_exec(box):
            captured['default'] = box.defaultButton().text(); captured['text'] = box.text(); return 0
        monkeypatch.setattr(QMessageBox, 'exec', fake_exec)
        w._ask_identity_change({'done': 120, 'total': 4000, 'labels': ['확정 용어집'], 'added': ['窗'], 'removed': [], 'changed': [], 'renames': {}})
        assert captured['default'] == '이어서 번역' and '120/4,000청크' in captured['text'] and '窗' in captured['text']
        buttons = {}
        def fake_exec2(box):
            buttons['all'] = [b.text() for b in box.buttons()]; return 0
        monkeypatch.setattr(QMessageBox, 'exec', fake_exec2)
        w._ask_identity_change({'done': 1, 'total': 2, 'labels': [], 'added': [], 'removed': [], 'changed': [], 'renames': {}})
        assert {'이어서 번역', '처음부터 다시 번역', '취소'} <= set(buttons['all'])   # named so in the manual
    finally:
        w.close()


# ---------------------------------------------------------------- opening a book
def test_opening_a_txt_decodes_and_shows_it_once(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    book = root / 'b.txt'; book.write_bytes(_book(50).encode('utf-8'))   # same bytes on Windows (no CRLF)
    w = _window(monkeypatch)
    try:
        calls = {'decode': 0, 'show': 0}
        real_decode = ui.decode_txt_bytes
        monkeypatch.setattr(ui, 'decode_txt_bytes', lambda *a, **k: (calls.__setitem__('decode', calls['decode'] + 1), real_decode(*a, **k))[1])
        real_show = w._set_preview_source
        monkeypatch.setattr(w, '_set_preview_source', lambda t: (calls.__setitem__('show', calls['show'] + 1), real_show(t))[1])
        w._open_txt_path(str(book))
        assert calls == {'decode': 0, 'show': 1}   # read_source_file decoded it; the window reuses that
        assert w.preview.toPlainText() == w.source_text and w.chunks
    finally:
        w.close()


# ---------------------------------------------------------------- pasted text
def test_pasted_text_sets_the_source_language(qt_app, sandbox, monkeypatch):
    w = _window(monkeypatch)
    try:
        w._prepare_new_input(); w.input_mode = 'none'; w._apply_profile('ja-ko')
        w._set_preview_source('')
        text = _book(20)
        w.preview.setPlainText(text)
        w._detect_paste_language()
        assert w.pm.active.id == 'zh-ko' and w.lang.currentData() == 'zh-ko'
        assert w.preview.toPlainText() == text
    finally:
        w.close()


def test_pasted_korean_or_hand_picked_language_is_left_alone(qt_app, sandbox, monkeypatch):
    w = _window(monkeypatch)
    try:
        w._prepare_new_input(); w.input_mode = 'none'; w._apply_profile('ja-ko'); w._set_preview_source('')
        w.preview.setPlainText('그날 밤 그는 창가에 서서 오래도록 비를 바라보았다. 존 스미스가 John이라고 불렀다. ' * 20)
        w._detect_paste_language()
        assert w.pm.active.id == 'ja-ko'
        w.preview.setPlainText('')
        w.lang.setCurrentIndex(w.lang.findData('en-ko'))       # picked by hand
        w.preview.setPlainText(_book(20))
        w._detect_paste_language()
        assert w.pm.active.id == 'en-ko'
    finally:
        w.close()


def test_second_paste_in_another_language_keeps_the_new_text(qt_app, sandbox, monkeypatch):
    w = _window(monkeypatch)
    try:
        w._prepare_new_input(); w.input_mode = 'none'; w._apply_profile('zh-ko'); w._set_preview_source('')
        w.preview.setPlainText(_book(20)); w._detect_paste_language()
        assert w._use_preview_as_source()                     # first paste became the source
        ja = '\n\n'.join('彼は窓辺に立って、外の雨をじっと見ていた。' for _ in range(30))
        w.preview.setPlainText(ja); w._detect_paste_language()
        assert w.pm.active.id == 'ja-ko' and w.preview.toPlainText() == ja and w.source_text == ja
    finally:
        w.close()


# ---------------------------------------------------------------- window position
def test_window_position_and_size_come_back(qt_app, sandbox, monkeypatch):
    import base64
    root, _ = sandbox
    w = _window(monkeypatch)
    try:
        w.move(37, 51); w.show(); qt_app.processEvents()
        saved = bytes(w.saveGeometry())
        w.close()   # closing saves it
    finally:
        w.close()
    data = json.loads((root / 'data' / 'window_state.json').read_text(encoding='utf-8'))
    assert base64.b64decode(data['geometry'])[:8] == saved[:8]
    w2 = _window(monkeypatch)
    try:
        got = []
        monkeypatch.setattr(w2, 'restoreGeometry', lambda b: (got.append(bytes(b)), True)[1])
        assert w2.restore_window_state() and got == [base64.b64decode(data['geometry'])]
    finally:
        w2.close()


def test_no_saved_window_state_is_fine(qt_app, sandbox, monkeypatch):
    w = _window(monkeypatch)
    try:
        assert w.restore_window_state() is False
    finally:
        w.close()


# ---------------------------------------------------------------- PDF
def _pdf(tmp, pages=12):
    from test_pdf_epub import _qt_pdf
    body = [[f'他站在窗边看着外面的雨，第{p}页第{i}行。' for i in range(20)] for p in range(pages)]
    return _qt_pdf(tmp / 'book.pdf', body, header='第一卷 雨夜')


def test_pdf_pages_read_by_helper_processes_give_the_same_text(tmp_path, qt_app):
    from novel_translator import pdf_text
    path = _pdf(tmp_path, 12)
    one = pdf_text.extract_pdf_text(path, workers=1)
    two = pdf_text.extract_pdf_text(path, workers=2)
    assert one == two and one.pages == 12


def test_pdf_text_is_kept_for_the_next_opening(tmp_path, qt_app, monkeypatch):
    from novel_translator import source_formats, pdf_text
    path = _pdf(tmp_path, 4); cache = tmp_path / 'cache'
    first = source_formats.extract_document(path, cache_dir=cache)
    monkeypatch.setattr(pdf_text, 'extract_pdf_text', lambda *a, **k: (_ for _ in ()).throw(AssertionError('read again')))
    again = source_formats.extract_document(path, cache_dir=cache)
    assert again.text == first.text and again.label == first.label
    # another program version reads the PDF again
    import novel_translator
    monkeypatch.setattr(novel_translator, '__version__', '9.9.9')
    with pytest.raises(AssertionError):
        source_formats.extract_document(path, cache_dir=cache)


def test_pdf_cache_keeps_only_the_newest(tmp_path, qt_app, monkeypatch):
    from novel_translator import source_formats
    monkeypatch.setattr(source_formats, 'PDF_CACHE_KEEP', 2)
    cache = tmp_path / 'cache'
    for i in range(4):
        d = tmp_path / f'd{i}'; d.mkdir()
        from test_pdf_epub import _qt_pdf
        p = _qt_pdf(d / 'b.pdf', [[f'他第{i}次站在窗边看着外面的雨。'] * 10])
        source_formats.extract_document(p, cache_dir=cache)
    assert len(list(cache.glob('*.json'))) == 2


def test_pdf_without_cache_folder_writes_nothing(tmp_path, qt_app):
    from novel_translator import source_formats
    path = _pdf(tmp_path, 2)
    source_formats.extract_document(path)
    assert not (tmp_path / 'cache').exists()
