"""v2.5: steady refinement window, stop reaching the local LLM, safe name renames,
and no unreviewed suspect edit in Korean refinement."""
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication

from novel_translator.providers.base import TranslationResponse, TranslationCancelled, ProviderError, ErrorKind
from novel_translator.orchestrator import TranslationOrchestrator, KeyState
from novel_translator.profiles import ProfileManager
from novel_translator import name_rename as nr
from novel_translator.refinement_runtime import run_refinement


# ---------------------------------------------------------------- 재정비 창 크기 고정
def _dialog():
    from test_v20_refinement_api_integration import Host
    from novel_translator.ui.refinement_run_dialog import RefinementRunDialog
    app = QApplication.instance() or QApplication([])
    d = RefinementRunDialog(Host())
    d.show(); app.processEvents()
    return app, d


def test_refinement_window_keeps_one_size_from_start_to_finish(tmp_path):
    app, d = _dialog()
    try:
        class Running:
            def isRunning(self): return True
        size = d.size()
        d.worker = Running(); d._run_started = time.monotonic(); d.time_info.show()
        d.time_info.setText('교정 1,234/5,678 문단 · 원문 보존 12 · 경과 1:02:03 · 남은 예상 약 3:04:05')
        for stage in ('재정비 시작 중', '윤문 요청 중 · 1234~1245문단 · 로컬 LLM',
                      '내용 변경 가능성 감지(이름 사라짐(쭤징, 바이링, 리쉬안스)) · 연속성 확인 요청 중',
                      '연속성 검토 요청 실패 · 12문단 원문 보존(나중에 다시 시도 가능)'):
            for waited in (5, 59, 120, 1800):
                d._stage_text = stage; d._stage_started = time.monotonic() - waited
                d._tick(); app.processEvents()
                assert d.size() == size, (stage, waited)
        d.worker = None
        long = 'C:/Users/user/Documents/아주긴폴더이름이계속이어지는경로/소설/결과/' + '아주긴소설제목' * 4
        d._finished({'status': 'completed', 'done': 790, 'total': 800, 'changed': 780, 'identical': 10, 'preserved': 10,
                     'seconds': 3723, 'output': long + '.ko.refined.txt', 'report': long + '.ko.refined.txt.review.txt',
                     'guidance_source': 'translation', 'guidance': '표', 'residue_before': 3, 'residue_after': 1})
        d._thread_stopped(); app.processEvents()
        assert d.size() == size
        assert '총 소요 시간' in d.status.text() and '같은 폴더' in d.status.text()
        from pathlib import Path
        assert str(Path(long + '.ko.refined.txt')) in d.status.toolTip()   # the full path is still there
        d._finished({'status': 'stopped', 'seconds': 30}); app.processEvents()
        assert d.size() == size
    finally:
        d.close()


def test_refinement_window_texts_are_shorter_but_keep_the_rules():
    app, d = _dialog()
    try:
        from PySide6.QtWidgets import QLabel
        texts = ' '.join(l.text() for l in d.findChildren(QLabel))
        for must in ('로컬 LLM 전용', 'API 설정', '존댓말·반말', '원본은 바꾸지 않습니다', '샘플 분석', '검열 해제'):
            assert must in texts
        assert len(d.help.text()) < 200
        d._stage_text = '윤문 요청 중'; d._stage_started = time.monotonic()
        class Running:
            def isRunning(self): return True
        d.worker = Running(); d._tick()
        assert '응답 대기' in d.status.text() and '정책 차단' not in d.status.text()
        d.worker = None
    finally:
        d.close()


def test_a_long_output_path_never_widens_the_window(tmp_path):
    app, d = _dialog()
    try:
        width = d.width()
        deep = tmp_path / ('가' * 40) / ('나' * 40)
        deep.mkdir(parents=True)
        f = deep / (('긴제목' * 20) + '.ko.txt'); f.write_text('한국어 문장입니다.', encoding='utf-8')
        d._set_source(f); app.processEvents()
        assert d.width() == width
        assert str(d.output) == d.status.toolTip()
    finally:
        d.close()


# ---------------------------------------------------------------- 중지가 로컬 LLM 생성까지 멈춤
def test_stop_reaches_the_running_request_so_a_local_llm_stops_generating():
    cancelled = threading.Event(); stopped_early = threading.Event(); seen = []
    class StreamingProvider:
        def translate(self, request, key):
            seen.append(request.cancel)
            for _ in range(300):                    # a long stream; checks the stop like Ollama's reader
                if request.cancel is not None and request.cancel():
                    stopped_early.set()
                    raise TranslationCancelled('사용자 취소')
                time.sleep(0.01)
            return TranslationResponse('<P000001>\n끝까지 생성된 번역입니다.')
    orch = TranslationOrchestrator(StreamingProvider(), [KeyState('test', 'dummy')], 'fixture', cancel_check=cancelled.is_set)
    threading.Timer(0.1, cancelled.set).start()
    with pytest.raises(TranslationCancelled):
        orch.translate_chunk(ProfileManager().active, '<P000001>\n日本語の文章です。', {})
    assert seen and seen[0] is not None
    assert stopped_early.wait(1)                     # the request itself stopped, not only the job


def test_request_without_a_stop_check_is_unchanged():
    seen = []
    class Provider:
        def translate(self, request, key):
            seen.append(request.cancel)
            return TranslationResponse('<P000001>\n한국어 번역입니다.')
    orch = TranslationOrchestrator(Provider(), [KeyState('test', 'dummy')], 'fixture')
    orch.translate_chunk(ProfileManager().active, '<P000001>\n日本語の文章です。', {})
    assert seen == [None]


def test_local_llm_stream_closes_on_stop():
    from novel_translator.providers.local_llm import LocalLLMProvider
    from novel_translator.providers.base import TranslationRequest
    closed = []
    class Stream:
        def iter_lines(self):
            for i in range(1000):
                yield ('{"message":{"content":"가"}}').encode()
        def close(self): closed.append(True)
    p = LocalLLMProvider.__new__(LocalLLMProvider)
    stop = {'n': 0}
    def cancel():
        stop['n'] += 1
        return stop['n'] > 3
    req = TranslationRequest('m', 's', 'u', cancel=cancel)
    with pytest.raises(TranslationCancelled):
        p._read_stream(Stream(), req)
    assert closed == [True]


# ---------------------------------------------------------------- 이름 변경이 일반 낱말을 건드리지 않음
def test_rename_leaves_words_that_only_start_with_the_name():
    out, n, skipped = nr.apply_counted('하늘색 원피스를 입은 하늘이 들어왔다. 하늘나라 하늘아래 하늘이야기',
                                       {'하늘': '하늬'})
    assert out == '하늘색 원피스를 입은 하늬가 들어왔다. 하늘나라 하늘아래 하늘이야기' and n == 1 and skipped == 4
    assert nr.apply('백영화를 본 백영이', {'백영': '바이'})[0] == '백영화를 본 바이가'


def test_rename_still_follows_particles_endings_and_forms_of_address():
    t = ('좌경공자 좌경사형 좌경께서 좌경한테 좌경처럼 좌경였다 좌경과의 좌경으로서 좌경이랑은 좌경님 좌경씨 좌경네 '
         '좌경들 좌경언니 좌경누나 좌경오빠 좌경선배 좌경의 좌경도 좌경만 좌경에게 좌경까지 좌경부터 좌경보다 좌경이다')
    out, n, skipped = nr.apply_counted(t, {'좌경': '쭤징'})
    assert '좌경' not in out and skipped == 0
    assert '쭤징이었다' in out and '쭤징과의' in out and '쭤징으로서' in out and '쭤징이다' in out
    assert nr.apply('소희와의 약속. 소희는요?', {'소희': '소현'})[0] == '소현과의 약속. 소현은요?'


def test_rename_in_the_stored_book_reports_what_it_left_alone(tmp_path):
    from novel_translator.storage import Database
    db = Database(tmp_path / 't.db')
    pid = db.create_project('a.txt', 'zh', 'zh-ko', 'sha', 'fp')
    db.upsert_chunk(pid, 0, '源')
    db.set_chunk_status(pid, 0, 'COMPLETED', translation='하늘색 옷의 하늘이 웃었다.', provider='x', model='m',
                        error_type=None, error_message=None, retry_count=0, fallback_used=0, prompt_version='v')
    assert db.rename_in_translations(pid, {'하늘': '하늬'}) == 1
    assert db.last_rename_skipped == 1
    assert db.completed_translations(pid)[0]['translation'] == '하늘색 옷의 하늬가 웃었다.'


# ---------------------------------------------------------------- 재정비: 검토 못 한 의심 윤문은 남기지 않음
class _Shortener:
    """Edits by cutting each paragraph (a suspect change); the review answers as set."""
    name = 'shortener'
    def __init__(self, review='error', on_review=None):
        self.review = review; self.on_review = on_review; self.calls = 0
    def translate(self, req, key):
        import re
        self.calls += 1
        if '두 한국어 장면을 비교' in req.system_prompt:
            if self.on_review: self.on_review()
            if self.review == 'error':
                raise ProviderError(ErrorKind.TRANSIENT, '일시적 검토 오류')
            return TranslationResponse(self.review)
        hits = list(re.finditer(r'(?m)^<P(\d{6})>\n([^\n]*)', req.user_prompt))
        return TranslationResponse('\n\n'.join('<P' + m.group(1) + '>\n' + m.group(2)[:max(10, int(len(m.group(2)) * .6))] for m in hits))


def test_review_failure_keeps_the_original_and_can_be_retried(tmp_path):
    from test_refinement_runtime import Dummy
    from novel_translator.refinement_runtime import retry_status
    src = tmp_path / 'b.txt'; original = '한 문장이 아주 길게 작성된 예시입니다. ' * 8
    src.write_text(original, encoding='utf-8')
    jobs = tmp_path / 'jobs'
    res = run_refinement(src, tmp_path / 'out.txt', _Shortener('error'), 'model', 'key',
                         checkpoint_dir=jobs, analysis_engine=(Dummy(), 'analysis', 'test'))
    assert res['preserved'] == 1 and res['changed'] == 0
    assert (tmp_path / 'out.txt').read_text(encoding='utf-8-sig') == original
    assert retry_status(src, tmp_path / 'out.txt', 'shortener', 'model', checkpoint_dir=jobs) == 1
    # A successful review later keeps the same, now reviewed, edit.
    res2 = run_refinement(src, tmp_path / 'out.txt', _Shortener('SAME'), 'model', 'key',
                          checkpoint_dir=jobs, analysis_engine=(Dummy(), 'analysis', 'test'), update_existing=True)
    assert res2['preserved'] == 0 and res2['changed'] == 1


def test_a_reviewed_same_edit_is_still_kept(tmp_path):
    from test_refinement_runtime import Dummy
    src = tmp_path / 'c.txt'; original = '한 문장이 아주 길게 작성된 예시입니다. ' * 8
    src.write_text(original, encoding='utf-8')
    res = run_refinement(src, tmp_path / 'out.txt', _Shortener('SAME'), 'model', 'key',
                         checkpoint_dir=tmp_path / 'jobs', analysis_engine=(Dummy(), 'analysis', 'test'))
    assert res['changed'] == 1 and res['preserved'] == 0 and res['issues'] == 0


# ---------------------------------------------------------------- v2.5 보완: API 설정 모델 표시 · 재정비 뒤 클린리더
def test_api_settings_show_the_saved_local_model_by_name_before_the_list_arrives(monkeypatch):
    import novel_translator.ui.api_dialog as ad
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ad.LocalModelsThread, 'start', lambda self: None)     # the list has not arrived yet
    model = 'gemma4-26b-abliterated:latest'
    d = ad.ApiDialog([], 'gemini-3.1-flash-lite', {}, '', None,
                     local_settings={'model': model, 'endpoint': 'http://localhost:11434/v1/chat/completions'},
                     engines=[{'id': 'local', 'enabled': True}],
                     extraction_engines={'terms': 'local', 'speech': 'local', 'model': model},
                     refinement_editing={'provider': 'local', 'model': model})
    try:
        before = (d.analysis_model.currentText(), d.editing_model.currentText())
        assert before == (model, model)                       # no "저장된 모델 · …" first
        d._local_models_loaded([model, 'gemma4-12b:latest'], '')
        assert (d.analysis_model.currentText(), d.editing_model.currentText()) == before   # nothing changes
        assert d._role_selected_model(d.analysis_model) == model
        d._local_models_loaded(['other:latest'], '')           # a name the server does not have is still marked
        assert d.analysis_model.currentText() == '저장된 모델 · ' + model
        assert d._role_selected_model(d.analysis_model) == model
    finally:
        d.close()


def _finish(d, tmp_path, preserved=0):
    out = tmp_path / '책.ko.refined.txt'; out.write_text('결과', encoding='utf-8')
    d._finished({'status': 'completed', 'done': 3 - preserved, 'total': 3, 'changed': 2, 'identical': 1 - preserved,
                 'preserved': preserved, 'seconds': 5, 'output': str(out), 'report': str(out) + '.review.txt',
                 'guidance_source': 'analysis'})
    d._thread_stopped()
    return out


def test_finished_refinement_offers_the_clean_reader_in_place_of_start(tmp_path):
    app, d = _dialog()
    opened = []
    d.host.launch_cleanreader = lambda files, parent=None: opened.append((list(files), parent)) or 1
    try:
        assert d.start.isVisible() and not d.reader.isVisible()
        size = d.size()
        out = _finish(d, tmp_path); app.processEvents()
        assert d.reader.isVisible() and not d.start.isVisible() and d.size() == size
        d.reader.click()
        assert opened == [([out], d)]
        # Choosing another TXT brings [재정비 시작] back.
        f = tmp_path / '다음.txt'; f.write_text('다음 책입니다.', encoding='utf-8'); d._set_source(f); app.processEvents()
        assert d.start.isVisible() and not d.reader.isVisible()
    finally:
        d.close()


def test_start_stays_beside_the_reader_while_preserved_paragraphs_can_be_retried(tmp_path):
    app, d = _dialog()
    try:
        _finish(d, tmp_path, preserved=1); app.processEvents()
        assert d.reader.isVisible() and d.start.isVisible() and d.start.isEnabled()
        d._finished({'status': 'stopped', 'seconds': 3}); app.processEvents()
        d._show_done(False); app.processEvents()
        assert d.start.isVisible() and not d.reader.isVisible()
    finally:
        d.close()


def test_main_window_clean_reader_launch_is_shared(monkeypatch, tmp_path):
    from novel_translator.ui import main_window as mw
    exe = tmp_path / 'YamoonCleanReader.exe'; exe.write_bytes(b'MZ')
    launched = []
    monkeypatch.setattr(mw.subprocess, 'Popen', lambda args, **k: launched.append(args))
    class Host:
        status = None
        def _find_cleanreader(self): return exe
    h = Host()
    out = tmp_path / 'a.ko.refined.txt'; out.write_text('x', encoding='utf-8')
    assert mw.MainWindow.launch_cleanreader(h, [out]) == 1
    assert launched == [[str(exe), str(out)]]


# ---------------------------------------------------------------- v2.5 보완 2: 모델 목록이 첫 클릭부터 다 보임
def _api(monkeypatch, model='gemma4-26b-abliterated:latest'):
    import novel_translator.ui.api_dialog as ad
    monkeypatch.setattr(ad.LocalModelsThread, 'start', lambda self: None)
    return ad, ad.ApiDialog([], 'gemini-3.1-flash-lite', {}, '', None,
                            local_settings={'model': model, 'endpoint': 'http://localhost:11434/v1/chat/completions'},
                            engines=[{'id': 'local', 'enabled': True}],
                            extraction_engines={'terms': 'local', 'speech': 'local', 'model': model},
                            refinement_editing={'provider': 'local', 'model': model})


def test_model_fields_list_every_model_from_the_first_click(monkeypatch):
    app = QApplication.instance() or QApplication([])
    names = ['gemma4-12b:latest', 'gemma4-26b-abliterated:latest', 'qwen3:14b']
    ad, d = _api(monkeypatch)
    d._local_models_loaded(names, ''); d.close()           # an earlier opening fetched the list
    ad, d = _api(monkeypatch)                                # opened again: the lookup has not answered yet
    try:
        items = lambda c: [c.itemText(i) for i in range(c.count())]
        assert items(d.local_model) == names
        assert items(d.analysis_model)[1:] == names and items(d.editing_model)[1:] == names
        assert d.analysis_model.currentText() == 'gemma4-26b-abliterated:latest'
    finally:
        d.close()


def test_the_same_list_arriving_leaves_an_open_list_alone(monkeypatch):
    app = QApplication.instance() or QApplication([])
    names = ['gemma4-12b:latest', 'gemma4-26b-abliterated:latest']
    ad, d = _api(monkeypatch); d._local_models_loaded(names, ''); d.close()
    ad, d = _api(monkeypatch)
    try:
        d.show(); app.processEvents()
        rebuilt = []
        orig = d._role_model_changed
        d._role_model_changed = lambda *a, **k: (rebuilt.append(a), orig(*a, **k))
        d.analysis_model.showPopup(); app.processEvents()
        d._local_models_loaded(names, ''); app.processEvents()
        assert not rebuilt and d.analysis_model.view().isVisible()
        assert '등록된 모델 2개' in d.local_models_status.text()
    finally:
        d.close()


def test_a_changed_list_reopens_an_open_list_at_its_new_size(monkeypatch):
    app = QApplication.instance() or QApplication([])
    ad, d = _api(monkeypatch)
    try:
        d.show(); app.processEvents()
        shown = []
        d.analysis_model.showPopup(); app.processEvents()
        orig_show = d.analysis_model.showPopup
        d.analysis_model.showPopup = lambda: (shown.append(d.analysis_model.count()), orig_show())
        d._local_models_loaded(['a:latest', 'gemma4-26b-abliterated:latest', 'c:latest'], ''); app.processEvents()
        assert shown == [4]                                  # reopened once, with every model in it
    finally:
        d.close()
