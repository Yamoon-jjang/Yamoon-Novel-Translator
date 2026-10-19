"""v2.3: the automatic speech table (인물 말투표) never looks frozen, can be skipped from the
main button, cancels at once, and Qt's standard buttons are Korean.

The analysis itself (prompts, engine order, fallback batches, parsing) must stay exactly
as before: these tests also pin that down."""
from __future__ import annotations
import json, threading, time

from PySide6.QtWidgets import QApplication, QMessageBox, QDialogButtonBox

from novel_translator.providers.base import TranslationResponse, TranslationRequest, TranslationCancelled
from novel_translator.ui import main_window as ui
from test_alpha17 import _window, _fake_worker_class, _wait, qt_app, sandbox  # noqa: F401 (pytest fixtures)

NAMES = {'李天明': '이천명', '王小月': '왕소월', '陈屿': '진서', '赵敏': '조민'}
_LINES = []
for i in range(120):
    a, b = list(NAMES)[i % 4], list(NAMES)[(i + 1) % 4]
    _LINES += [f'{a}看着{b}，慢慢地说道。', f'“{b}，我们明天一起去城里看看吧，好不好？”']
BOOK = '\n\n'.join(_LINES)
TABLE = '이천명\t왕소월\t반말\t친구\n왕소월\t이천명\t해요체\t후배'


def _setup(qt_app, sandbox, monkeypatch, speech_reply, auto_terms=False):
    root, cfg = sandbox
    app_cfg = json.loads((cfg / 'app.json').read_text(encoding='utf-8'))
    app_cfg['auto_extract_terms'] = auto_terms; app_cfg['auto_speech_table'] = True
    (cfg / 'app.json').write_text(json.dumps(app_cfg, ensure_ascii=False), encoding='utf-8')
    started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _fake_worker_class(started))
    w = _window(monkeypatch)
    monkeypatch.setattr(ui.QMessageBox, 'warning', lambda *a, **k: None)
    calls = []

    class P:
        def __init__(self, pid): self.pid = pid
        def translate(self, req, secret):
            calls.append((self.pid, req.purpose, req.system_prompt, req.user_prompt, req.cancel))
            if req.purpose == 'json':
                return TranslationResponse(json.dumps(NAMES, ensure_ascii=False), 'STOP', {})
            out = speech_reply(self.pid, req)
            if isinstance(out, Exception): raise out
            return TranslationResponse(out, 'STOP', {})
    monkeypatch.setattr(ui, '_make_provider', lambda pid, *a, **k: P(pid))
    engines = [('gemini', 'k', 'm', '', None)]
    monkeypatch.setattr(w, '_extraction_engines', lambda purpose='speech': list(engines))
    p = root / 'book.txt'; p.write_text(BOOK, encoding='utf-8')
    w._open_txt_path(str(p)); w._apply_profile('zh-ko')
    if not auto_terms:
        from PySide6.QtWidgets import QTableWidgetItem
        for a, b in NAMES.items():
            r = w.term_table.rowCount(); w.term_table.insertRow(r)
            w.term_table.setItem(r, 0, QTableWidgetItem(a)); w.term_table.setItem(r, 1, QTableWidgetItem(b))
    return w, calls, started


def _blocking_reply(release):
    def reply(pid, req):
        # A slow engine: answers only when released (or when the request is cancelled).
        t0 = time.time()
        while not release.is_set() and time.time() - t0 < 20:
            if req.cancel is not None and req.cancel():
                raise TranslationCancelled('사용자 취소')
            time.sleep(0.02)
        return TABLE
    return reply


def test_auto_speech_shows_progress_instead_of_ready_and_offers_skip(qt_app, sandbox, monkeypatch):
    release = threading.Event()
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, _blocking_reply(release))
    try:
        w.start_translation()
        assert _wait(qt_app, lambda: isinstance(w._term_thread, ui.SpeechExtractThread) and w._analysis_stage, 5)
        _wait(qt_app, lambda: '경과' in w.status.text(), 3)
        st = w.status.text()
        assert '준비됨' not in st, st
        assert '말투표 만드는 중' in st and '경과' in st
        api = w.api_runtime_label.text()
        assert '인물 말투표 만드는 중' in api and 'Gemini 요청 중' in api and '경과' in api
        assert w.start.isEnabled() and w.start.text() == '말투표 건너뛰고 번역 시작'
        release.set()
        assert _wait(qt_app, lambda: started and not w._extracting(), 10)
        assert len(w._speech_rows()) == 2
        assert w.start.text() != '말투표 건너뛰고 번역 시작'
    finally:
        release.set(); w.close()


def test_skip_starts_the_translation_at_once_without_the_table(qt_app, sandbox, monkeypatch):
    release = threading.Event()
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, _blocking_reply(release))
    try:
        w.start_translation()
        assert _wait(qt_app, lambda: isinstance(w._term_thread, ui.SpeechExtractThread), 5)
        t0 = time.time()
        w.start.click()   # [말투표 건너뛰고 번역 시작]
        assert _wait(qt_app, lambda: bool(started), 5)
        assert time.time() - t0 < 3, 'skip must not wait for the slow engine'
        assert w._speech_rows() == []
        assert '건너뜀 (사용자 선택)' in w.error_view.toPlainText()
        # Not asked again for the same book on the next start.
        n = len(calls); release.set()
        _wait(qt_app, lambda: not (w.worker and w.worker.isRunning()), 5)
        w.start_translation()
        assert _wait(qt_app, lambda: len(started) == 2, 5)
        assert len(calls) == n
    finally:
        release.set(); w.close()


def test_cancel_is_immediate_and_the_next_start_tries_again(qt_app, sandbox, monkeypatch):
    release = threading.Event()
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, _blocking_reply(release))
    try:
        w.start_translation()
        assert _wait(qt_app, lambda: isinstance(w._term_thread, ui.SpeechExtractThread), 5)
        t0 = time.time()
        w._generate_speech()   # [말투표 생성 취소] on the 인물 말투 tab
        assert _wait(qt_app, lambda: w._term_thread is None, 5)
        assert time.time() - t0 < 3
        assert not started and '취소' in w.status.text()
        release.set()
        w.start_translation()   # cancelled is not finished: build it this time
        assert _wait(qt_app, lambda: started and not w._extracting(), 10)
        assert len(w._speech_rows()) == 2
    finally:
        release.set(); w.close()


def test_speech_analysis_requests_are_unchanged(qt_app, sandbox, monkeypatch):
    """Same single full-context request with the same prompts as before; only a cancel
    check is attached."""
    from novel_translator.speech_levels import build_prompts, sample_dialogue
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, lambda pid, req: TABLE)
    try:
        w._generate_speech()
        assert _wait(qt_app, lambda: not w._extracting() and w._speech_rows(), 10)
        speech = [c for c in calls if c[1] == 'analysis']
        assert len(speech) == 1
        names = w._speech_names()
        from novel_translator.lexical_guidance import extra_pack
        lang = w.pm.active.source_language
        cues = [c for c in list(getattr(w.pm.active, 'lexical_guidance', {}) or {}) + list(extra_pack(lang)) if 0 < len(c) <= 4]
        sysp, user = build_prompts(sample_dialogue(w._speech_source(), names, cues=cues), names, lang)
        assert speech[0][2] == sysp and speech[0][3] == user
        assert callable(speech[0][4]) and speech[0][4]() is False
    finally:
        w.close()


def test_auto_terms_then_speech_progress(qt_app, sandbox, monkeypatch):
    w, calls, started = _setup(qt_app, sandbox, monkeypatch, lambda pid, req: TABLE, auto_terms=True)
    try:
        w.start_translation()
        assert _wait(qt_app, lambda: started and not w._extracting(), 10)
        assert [c[1] for c in calls] == ['json', 'analysis']
        log = w.error_view.toPlainText()
        assert '인명·용어 자동 추출 · 새 항목 4개' in log and '인물 말투표 자동 생성 · 2쌍' in log
    finally:
        w.close()


def test_local_stream_stops_when_cancelled():
    from novel_translator.providers.local_llm import LocalLLMProvider
    closed = []
    class R:
        status_code = 200
        def iter_lines(self):
            for i in range(1000):
                yield json.dumps({'message': {'content': f'줄{i}\n'}}).encode()
        def close(self): closed.append(True)
    flag = {'n': 0}
    def cancel():
        flag['n'] += 1
        return flag['n'] > 3
    p = LocalLLMProvider('http://localhost:11434', 300)
    req = TranslationRequest('m', 's', 'u', purpose='analysis', cancel=cancel)
    try:
        p._read_stream(R(), req); raise AssertionError('not cancelled')
    except TranslationCancelled:
        pass
    assert closed
    # Without a cancel check (every translation request) the stream is read to the end.
    data = p._read_stream(R(), TranslationRequest('m', 's', 'u'))
    assert data['message']['content'].count('\n') == 1000


def test_qt_standard_buttons_are_korean(qt_app):
    from novel_translator.theme import install_korean_qt, _QT_TRANSLATORS
    from novel_translator.ui.api_dialog import ApiDialog
    d = ApiDialog([], 'gemini-3.1-flash-lite', None, '', None)
    assert sorted(b.text() for bb in d.findChildren(QDialogButtonBox) for b in bb.buttons()) == ['저장', '취소']
    d.close()
    assert install_korean_qt(qt_app)
    try:
        mb = QMessageBox(QMessageBox.Question, '', '', QMessageBox.Yes | QMessageBox.No)
        assert mb.button(QMessageBox.Yes).text() == '예(&Y)' and mb.button(QMessageBox.No).text() == '아니요(&N)'
        mb.deleteLater()
    finally:
        while _QT_TRANSLATORS:
            qt_app.removeTranslator(_QT_TRANSLATORS.pop())


def test_spec_bundles_qt_translations():
    """PyInstaller collects qtbase_*.qm for QtCore; the GUI self-test (run on the built EXE
    by the build gate) fails if Korean Qt texts are missing."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / 'novel_translator' / 'app.py').read_text(encoding='utf-8')
    assert 'install_korean_qt(app)' in src and 'qtbase_ko.qm' in src


def _wheel(widget, dy=-120):
    from PySide6.QtCore import QPointF, QPoint, Qt
    from PySide6.QtGui import QWheelEvent
    pos = QPointF(widget.rect().center())
    ev = QWheelEvent(pos, QPointF(widget.mapToGlobal(pos.toPoint())), QPoint(0, 0), QPoint(0, dy),
                     Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(widget, ev)


def test_wheel_never_changes_a_table_choice(qt_app, sandbox, monkeypatch):
    """인물 말투표 말투 칸: the wheel scrolls the table, it never switches 반말 → 해요체.
    The choice changes only from the list (click) or the keyboard."""
    w = _window(monkeypatch)
    try:
        w._set_speech_rows([{'speaker': '이천명', 'listener': '왕소월', 'level': '반말', 'note': ''}])
        combo = w.speech_table.cellWidget(0, 2)
        for focused in (False, True):
            if focused: combo.setFocus()
            _wheel(combo); _wheel(combo, 120); _wheel(combo)
            assert combo.currentText() == '반말'
        combo.setCurrentIndex(combo.findText('해요체'))   # picking from the list still works
        assert w._speech_rows()[0]['level'] == '해요체'
    finally:
        w.close()
    from novel_translator.ui.term_conflict_dialog import TermConflictDialog
    d = TermConflictDialog([('李天明', '이천명', '리톈밍')])
    try:
        box = d.table.cellWidget(0, 3); before = box.currentIndex()
        _wheel(box); _wheel(box, 120)
        assert box.currentIndex() == before
    finally:
        d.close()
