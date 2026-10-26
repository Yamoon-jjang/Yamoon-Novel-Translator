"""v2.5.4: a resumed book does not keep pausing on the same few failed chunks.

On resume the chunks that failed before come back to back (everything between them is
saved). When the model fails them again with a FORMAT error (too little Korean, a list
of user names left in English ...), that is about the text, not the server, so it must
not trip the three-in-a-row safety pause. New chunks keep the safety pause as before.
"""
from __future__ import annotations

import hashlib
import re
from unittest.mock import patch

from novel_translator.providers.base import TranslationResponse
from novel_translator.profiles import ProfileManager
from novel_translator import orchestrator as orch_mod
from novel_translator.ui import worker as wm

from test_alpha17 import qt_app  # noqa: F401
from test_v252_resume_speed import Provider, _book, _chunks, _db, _run


class SourceBackProvider(Provider):
    """Copies the source back (too little Korean -> FORMAT) for the chosen paragraphs."""
    bad: set[str] = set()

    def translate(self, req, key):
        cls = type(self)
        cls.calls += 1; cls.prompts.append(req.user_prompt)
        src = req.user_prompt.split('[SOURCE - ')[-1]
        ids = []
        for i in re.findall(r'<P(\d{6})>', src):
            if i not in ids:
                ids.append(i)
        if any(i in cls.bad for i in ids):
            return TranslationResponse('\n\n'.join(f'<P{i}>\n他站在窗边看着外面的雨，心里很沉重。他站在窗边看着外面的雨。' for i in ids))
        return TranslationResponse('\n\n'.join(f'<P{i}>\n그는 창가에 서서 밖에 내리는 비를 오래 바라보았다.' for i in ids))


def _resume(tmp, text, provider):
    src = tmp / 'book.txt'; raw = text.encode('utf-8')
    prof = ProfileManager().set_active('zh-ko'); ps, cs = _chunks(text)
    provider.calls = 0; provider.prompts = []
    with patch.object(wm, 'DATA_DIR', tmp / 'data'), patch.object(wm, 'GeminiProvider', provider), \
            patch.object(orch_mod.TranslationOrchestrator, '_pace', lambda self: None), \
            patch.object(orch_mod.TranslationOrchestrator, '_interruptible_sleep', lambda self, s: None):
        w = wm.TranslationWorker(src, raw, prof, cs, 'fixture', [{'id': 'k', 'secret': 'd', 'enabled': True}], {'他': '그'},
                                 'balanced', tmp / 'out', all_paragraphs=ps)
        w.max_requests_per_chunk = 999
        res = []; issues = []
        w.result_ready.connect(lambda *a: res.append(a)); w.issue.connect(issues.append)
        w.run()
    return w, (res[-1] if res else None), issues, cs


def _fail_chunks(tmp, text, idx):
    db = _db(tmp)
    proj = db.latest_project_for_source(hashlib.sha256(text.encode()).hexdigest(), 'zh', 'zh-ko')
    for i in idx:
        db.set_chunk_status(int(proj['id']), i, 'FAILED', error_type='FORMAT', error_message='응답에 한국어가 너무 적습니다')
    return db, int(proj['id'])


def test_earlier_failed_chunks_failing_again_do_not_pause_the_resume(tmp_path, qt_app):
    text = _book(1200)
    _, res, _, _, cs = _run(tmp_path, text, {'他': '그'}, stop_at=8)
    assert res[0] == 'stopped' and len(cs) > 14
    bad_chunks = [1, 2, 3, 4]
    _fail_chunks(tmp_path, text, bad_chunks)
    SourceBackProvider.bad = {f'{p.pid:06d}' for i in bad_chunks for p in cs[i].paragraphs}
    w, res, issues, cs = _resume(tmp_path, text, SourceBackProvider)
    assert not any('안전 일시정지' in x for x in issues), issues
    assert res[0] != 'paused'
    # every chunk that was never reached is translated in this same run
    db = _db(tmp_path)
    proj = db.latest_project_for_source(hashlib.sha256(text.encode()).hexdigest(), 'zh', 'zh-ko')
    left = {int(r['chunk_index']) for r in db.pending_chunks(int(proj['id']))}
    assert left == set(bad_chunks)


def test_new_chunks_still_pause_after_three_format_failures(tmp_path, qt_app):
    text = _book(1200)
    _, res, _, _, cs = _run(tmp_path, text, {'他': '그'}, stop_at=3)
    assert res[0] == 'stopped'
    SourceBackProvider.bad = {f'{p.pid:06d}' for c in cs for p in c.paragraphs}   # everything fails now
    w, res, issues, cs = _resume(tmp_path, text, SourceBackProvider)
    assert any('연속적인 네트워크/응답 형식 오류' in x for x in issues) or res[0] == 'paused'


def test_known_failed_chunks_then_new_failures_still_pause(tmp_path, qt_app):
    """The exemption is only for the earlier failed chunks: a broken server on new chunks still pauses."""
    text = _book(1200)
    _, res, _, _, cs = _run(tmp_path, text, {'他': '그'}, stop_at=8)
    _fail_chunks(tmp_path, text, [1, 2, 3, 4])
    SourceBackProvider.bad = {f'{p.pid:06d}' for c in cs for p in c.paragraphs}
    w, res, issues, cs = _resume(tmp_path, text, SourceBackProvider)
    assert res[0] == 'paused'
    db = _db(tmp_path)
    proj = db.latest_project_for_source(hashlib.sha256(text.encode()).hexdigest(), 'zh', 'zh-ko')
    failed_new = [int(r['chunk_index']) for r in db.pending_chunks(int(proj['id'])) if r['status'] == 'FAILED' and int(r['chunk_index']) > 4]
    assert len(failed_new) == 3   # the pause comes after exactly three new failures


# ---------------------------------------------------------------- user-name lists left in English
from novel_translator.validation import untranslated_residue, validate_translation, is_name_list  # noqa: E402
from novel_translator.providers.base import ProviderError  # noqa: E402
import pytest  # noqa: E402

HANDLES = 'am, lastjuan, Jamielynn, Ecchi, darkwolf77, Rennie, mochi_bun, quietreader, Zhaoyun, kitsune, Bramble'


def test_a_list_of_user_names_is_not_called_avoidance():
    src = f'Thanks to my patrons this month: {HANDLES}. See you next chapter!'
    out = f'이번 달 후원자분들께 감사드립니다: {HANDLES}. 다음 화에서 만나요!'
    assert is_name_list(HANDLES)
    assert untranslated_residue(src, out) == ''


def test_untranslated_dialogue_or_moaning_is_still_caught():
    for kept in ('Oh, yes, harder, please, don\'t stop, more, more, oh god, yes',
                 'she said that she would never ever go back to that house again'):
        src = f'He held her close. "{kept}" she whispered against his neck.'
        out = f'그는 그녀를 꼭 끌어안았다. "{kept}" 그녀가 그의 목덜미에 속삭였다.'
        assert not is_name_list(kept)
        assert untranslated_residue(src, out), kept


def test_a_chunk_that_is_mostly_a_name_list_is_not_called_untranslated():
    names = ', '.join(f'{h}{i}' for i in range(3) for h in HANDLES.split(', '))   # 33 different handles
    src = f'<P000001>\nAuthor note: thank you all!\n\n<P000002>\n{names}'
    out = f'<P000001>\n작가 후기: 모두 고맙습니다!\n\n<P000002>\n{names}'
    validate_translation(src, out)   # no "too little Korean"


def test_a_chunk_left_in_english_is_still_rejected():
    para = 'He walked to the window and looked out at the rain for a long time, thinking about her. ' * 3
    src = f'<P000001>\n{para}\n\n<P000002>\n{para}'
    out = f'<P000001>\n{para}\n\n<P000002>\n{para}'
    with pytest.raises(ProviderError):
        validate_translation(src, out)


# ---------------------------------------------------------------- remaining time of a resumed book
from types import SimpleNamespace  # noqa: E402
from pathlib import Path  # noqa: E402
from test_alpha17 import sandbox, _window  # noqa: E402,F401


def test_resume_speed_samples_count_only_new_ordinary_chunks(tmp_path, qt_app):
    text = _book(1200)
    _, res, _, _, cs = _run(tmp_path, text, {'他': '그'}, stop_at=None)
    assert res[0] == 'completed'
    db, pid = _fail_chunks(tmp_path, text, [2])                  # one earlier trouble chunk
    for i in (5, 7, 9, 11):                                        # saved chunks mixed with new ones
        db.set_chunk_status(pid, i, 'PENDING', error_type=None, error_message=None)
    SourceBackProvider.bad = set()
    w, res, issues, cs = _resume(tmp_path, text, SourceBackProvider)
    assert w.eta_left == set()                                     # everything this run had to do is done
    assert len(w.eta_samples) == 4 and len(w.eta_all) == 5         # the trouble chunk is no speed sample


def test_window_estimate_uses_only_what_is_left_and_the_measured_speed(qt_app, sandbox, monkeypatch):
    win = _window(monkeypatch)
    try:
        win.input_mode = 'single'
        win.worker = SimpleNamespace(eta_left=set(range(8000)), eta_samples=[11.0] * 5, eta_all=[11.0] * 5,
                                     eta_trouble_only=False, source_path=Path('book.txt'))
        assert win._eta_seconds() is None                          # too few new chunks yet
        win.worker.eta_samples = [11.0] * 12
        assert round(win._eta_seconds()) == 88000                  # 8,000 chunks x 11 s, shown after 12 new chunks
        win._eta_keep_history(win.worker)                           # an automatic resume of the same book ...
        win.worker = SimpleNamespace(eta_left=set(range(7000)), eta_samples=[], eta_all=[],
                                     eta_trouble_only=False, source_path=Path('book.txt'))
        assert round(win._eta_seconds()) == 77000                  # ... shows the estimate at once
        win.worker = SimpleNamespace(eta_left=set(range(100)), eta_samples=[], eta_all=[],
                                     eta_trouble_only=False, source_path=Path('other.txt'))
        assert win._eta_seconds() is None                          # another book does not borrow the speed
    finally:
        win.worker = None; win.close()


def test_same_speed_gives_a_steady_estimate(qt_app, sandbox, monkeypatch):
    """The reported bug: 11 s per chunk all along, yet the estimate went 10.7 h -> 23.8 h."""
    win = _window(monkeypatch)
    try:
        win.input_mode = 'single'
        left = set(range(8162)); samples = []
        win.worker = SimpleNamespace(eta_left=left, eta_samples=samples, eta_all=samples, eta_trouble_only=False, source_path=Path('b.txt'))
        seen = []
        for i in range(1000):
            left.discard(i); samples.append(11.0)
            if len(samples) in (12, 300, 1000):
                seen.append(win._eta_seconds() + 11.0 * len(samples))   # estimate + time already spent
        assert max(seen) - min(seen) < 1.0                          # the finish time does not move
    finally:
        win.worker = None; win.close()


def test_opening_a_big_file_shows_a_note_first_and_removes_it(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    book = root / 'big.txt'
    book.write_bytes(('他站在窗边看着外面的雨，心里很沉重。\n\n' * 40000).encode('utf-8'))   # > 1 MB
    win = _window(monkeypatch)
    try:
        seen = {}
        real = win._open_txt_path_now
        def spy(fn):
            lab = win._busy_note
            seen['note'] = lab.text() if lab is not None else ''
            seen['visible'] = bool(lab is not None and not lab.isHidden())   # the test window itself is not shown
            return real(fn)
        monkeypatch.setattr(win, '_open_txt_path_now', spy)
        win._open_txt_path(str(book))
        assert seen['visible'] and '파일을 여는 중입니다' in seen['note'] and 'big.txt' in seen['note']
        assert win._busy_note is None and win.source_text                       # note gone, file open
        from PySide6.QtWidgets import QApplication
        assert QApplication.overrideCursor() is None                             # wait cursor restored
    finally:
        win.close()


def test_small_file_opens_without_the_note(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    book = root / 'small.txt'; book.write_bytes('他笑了。\n\n她也笑了。'.encode('utf-8'))
    win = _window(monkeypatch)
    try:
        shown = []
        monkeypatch.setattr(win, '_busy_note_show', lambda t: shown.append(t))
        win._open_txt_path(str(book))
        assert not shown and win.source_text
    finally:
        win.close()


def test_a_broken_big_file_hides_the_note_before_the_message(qt_app, sandbox, monkeypatch):
    root, _ = sandbox
    book = root / 'big.txt'; book.write_bytes(b'x' * 1_200_000)
    win = _window(monkeypatch)
    try:
        notes = []
        monkeypatch.setattr(win, '_read_source', lambda p: (_ for _ in ()).throw(OSError('denied')))
        from novel_translator.ui import main_window as mw
        monkeypatch.setattr(mw.QMessageBox, 'critical', lambda *a, **k: notes.append(win._busy_note))
        win._open_txt_path(str(book))
        assert notes == [None]
    finally:
        win.close()
