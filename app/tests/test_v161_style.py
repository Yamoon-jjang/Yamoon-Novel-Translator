"""v1.61 문장 스타일 (보통 / 자연스럽게)."""
from __future__ import annotations
from pathlib import Path

from novel_translator import prompting as pr
from novel_translator import style_metrics as sm
from novel_translator.profiles import ProfileManager
from novel_translator.ui import main_window as ui
import novel_translator.ui.worker as worker_mod
from test_refusal_recovery import ScriptedProvider, make_worker
from test_alpha17 import _window, _fake_worker_class, _wait, qt_app, sandbox  # noqa: F401 (pytest fixtures)

PIDS = ('zh-ko', 'ja-ko', 'en-ko')


def _section(prompt: str, title: str) -> str:
    i = prompt.index(title)
    j = prompt.find('\n\n', prompt.index('\n', i) + 1)
    return prompt[i:j if j > 0 else None]


def test_normal_style_is_the_unchanged_prompt_and_natural_keeps_every_content_rule():
    pm = ProfileManager()
    for pid in PIDS:
        p = pm.set_active(pid)
        normal = pr.build_system_prompt(p, 'novel')
        assert pr.build_system_prompt(p, 'novel', 'normal') == normal
        assert pr.build_system_prompt(p, 'novel', 'anything-else') == normal
        nat = pr.build_system_prompt(p, 'novel', 'natural')
        assert nat != normal and pr.NATURAL_PROSE in nat and '번역투(피할 것)' in nat
        # refusal handling, fidelity and output contract are carried over word for word
        for title in ('TRANSLATION-ONLY CONTRACT', 'SCOPE', 'FIDELITY', 'OUTPUT SCRIPT', 'OUTPUT CONTRACT'):
            assert _section(nat, title) == _section(normal, title), (pid, title)
        assert pr.SKIP_MARKER in nat and '한국어 번역 작업 지침' in nat
        assert nat.index('한국 소설 문장') < nat.index('한국어 번역 작업 지침')   # the prose section comes first
        # documents never get the novel style
        assert pr.build_system_prompt(p, 'document', 'natural') == pr.build_system_prompt(p, 'document')


def test_natural_style_frees_sentence_shape_lines():
    pm = ProfileManager()
    zh = pr.build_system_prompt(pm.set_active('zh-ko'), 'novel', 'natural')
    assert '문장 수와 정보는 줄이지' not in zh and '문장을 나누거나 합치는 것은' in zh
    assert '문장 수와 정보는 줄이지' in pr.build_system_prompt(pm.set_active('zh-ko'), 'novel')
    ja = pr.build_system_prompt(pm.set_active('ja-ko'), 'novel', 'natural')
    assert 'Use a neutral Korean construction' not in ja and 'omit the subject or use the name' in ja
    assert 'not permission to guess gender' in ja                          # the content rule itself stays


def test_style_changes_requests_and_memory_key_but_not_the_job_identity(tmp_path):
    prov = ScriptedProvider()
    w1, chunks, _ = make_worker(tmp_path, '彼は窓の外を見た。\n\n雨が降っていた。', prov)
    fp1, key1 = w1._fingerprint(), w1._settings_key()
    w1.run()                                   # make_worker swaps the provider globally: run before the next one
    assert w1.sentence_style == 'normal'
    assert prov.requests and all(pr.NATURAL_PROSE not in r.system_prompt for r in prov.requests)
    (tmp_path / 'b').mkdir(); prov2 = ScriptedProvider()
    w2, chunks2, _ = make_worker(tmp_path / 'b', '彼は窓の外を見た。\n\n雨が降っていた。', prov2)
    w3 = worker_mod.TranslationWorker(w2.source_path, w2.source_bytes, w2.profile, chunks2, 'gemini-3.8-flash',
        [{'id': 'fake', 'secret': 'not-real', 'project_group': 'P1', 'enabled': True}], {}, 'balanced',
        tmp_path / 'b' / 'out', sentence_style='natural')
    assert w3.sentence_style == 'natural'
    assert w3._fingerprint() == fp1             # switching the style never restarts a book
    assert w3._settings_key() != key1           # ... but reuse never mixes the two styles
    w3.run()
    assert prov2.requests and all(pr.NATURAL_PROSE in r.system_prompt for r in prov2.requests)


def test_translationese_counter():
    text = ('그녀는 문을 열었다. 그리고 그녀가 웃었다.\n그것은 그가 떠나는 것이었다. 안도감이 밀려오는 것을 느꼈다.\n'
            '문은 바람에 의해 닫혔다. 그는 힘을 가지고 있었다. 결정되어졌다.')
    total, rate, found = sm.summary(text)
    assert found == {'그녀는/그녀가': 2, '~것이었다': 1, '~것을 느꼈다': 1, '~에 의해': 1, '~되어지다': 1,
                     '~을 가지고 있다': 1, '문장 첫 "그리고"': 1}
    assert total == 8 and rate > 0
    assert sm.summary('그는 말없이 고개만 끄덕이고는 돌아서서 방을 나갔다.')[0] == 0
    assert sm.summary('')[:2] == (0, 0.0)


def _style_worker_class(started, text):
    Base = _fake_worker_class(started)
    class StyleWorker(Base):
        def __init__(self, *a, **k):
            super().__init__(*a, **k); self.sentence_style = k.get('sentence_style', 'normal'); self.leftover_count = 0
        def run(self):
            self.out_dir.mkdir(parents=True, exist_ok=True)
            out = self.out_dir / (self.source_path.stem + '.ko.txt'); out.write_text(text, encoding='utf-8')
            self.result_ready.emit('completed', str(out), 0); self.finished_ok.emit(str(out))
    return StyleWorker


def test_style_option_is_per_language_reaches_the_worker_and_logs_the_check(qt_app, sandbox, monkeypatch):
    root, _ = sandbox; started = []
    monkeypatch.setattr(ui, 'TranslationWorker', _style_worker_class(started, '그녀는 웃었다. 그녀가 말했다.'))
    w = _window(monkeypatch)
    monkeypatch.setattr(ui.QMessageBox, 'information', lambda *a, **k: None)
    try:
        assert w.sentence_style.currentData() == 'natural'                 # new novel default
        w.lang.setCurrentIndex(w.lang.findData('zh-ko'))
        assert w.pm.active.id == 'zh-ko'
        w.sentence_style.setCurrentIndex(w.sentence_style.findData('normal'))
        w.sentence_style.setCurrentIndex(w.sentence_style.findData('natural'))
        assert ui.load_json(ui.CONFIG_DIR / 'app.json')['sentence_style']['zh-ko'] == 'natural'
        w.lang.setCurrentIndex(w.lang.findData('ja-ko')); assert w.sentence_style.currentData() == 'natural'
        w.lang.setCurrentIndex(w.lang.findData('zh-ko')); assert w.sentence_style.currentData() == 'natural'
        w.lang.setCurrentIndex(w.lang.findData('en-ko')); assert w.sentence_style.currentData() == 'natural'
        w.sentence_style.setCurrentIndex(w.sentence_style.findData('normal'))
        assert ui.load_json(ui.CONFIG_DIR / 'app.json')['sentence_style']['en-ko'] == 'normal'
        w.lang.setCurrentIndex(w.lang.findData('zh-ko')); assert w.sentence_style.currentData() == 'natural'
        w.lang.setCurrentIndex(w.lang.findData('en-ko')); assert w.sentence_style.currentData() == 'normal'
        w.lang.setCurrentIndex(w.lang.findData('zh-ko'))
        src = root / 'book.txt'; src.write_text('第一章\n\n他静静地看着窗外的雨。', encoding='utf-8')
        w._open_txt_path(str(src)); w.start_translation()
        assert _wait(qt_app, lambda: '작업 끝' in w.error_view.toPlainText())
        assert started and started[-1].sentence_style == 'natural'
        log = w.error_view.toPlainText()
        assert '문장 점검 · 문장 스타일 자연스럽게 · 번역투 표현 2개' in log and '미번역 없음' in log
        w.content_kind = 'document'; w._refresh_content_ui()
        assert not w.sentence_style.isEnabled() and w._sentence_style_value() == 'normal'
    finally:
        w.close()


# ---------------------------------------------------------------- 언어팩 힌트 오작동 방지
from novel_translator.lexical_guidance import relevant_guidance, exclusions


def test_pack_hints_skip_words_that_only_contain_the_entry():
    pm = ProfileManager(); zh = pm.set_active('zh-ko')
    assert relevant_guidance(zh, '主人公推开门。') == {}                      # 주인공, not 주인
    assert '主人' in relevant_guidance(zh, '主人公对主人说。')                  # a free 主人 still counts
    assert relevant_guidance(zh, '他在下面等着，在下午走了。') == {}            # 아래에서 / 오후에
    assert '在下' in relevant_guidance(zh, '在下姓李。')                        # 저/소생
    assert relevant_guidance(zh, '这个系统性的问题。') == {}
    assert relevant_guidance(zh, '她喝了一杯绿茶。') == {}
    assert '녹차' in relevant_guidance(zh, '你这个绿茶！')['绿茶']             # the hint now names both senses
    assert '弟弟' not in relevant_guidance(zh, '他的小弟弟。') and '弟弟' in relevant_guidance(zh, '我弟弟来了。')
    assert relevant_guidance(zh, '他拜见师父，随后领取任务。') == {'师父': '사부', '任务': '임무'}
    ja = pm.set_active('ja-ko')
    assert relevant_guidance(ja, '某日、騎士団が来た。') == {'騎士団': '기사단'}   # 某日 / 騎士 inside 騎士団
    assert set(relevant_guidance(ja, '騎士団の騎士が言った。')) == {'騎士団', '騎士'}
    assert '某' in relevant_guidance(ja, '某は武士でござる。')


def test_exclusion_list_is_well_formed_and_every_exclusion_contains_its_entry():
    for lang in ('zh', 'ja', 'en'):
        for entry, words in exclusions(lang).items():
            assert words and all(entry in w and w != entry for w in words), (lang, entry)


# ---------------------------------------------------------------- 언어별 설정 탭 스크롤
def test_language_tab_opens_at_the_top_and_wheel_never_hides_or_changes_options(qt_app, sandbox, monkeypatch):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    w = _window(monkeypatch)
    try:
        w.resize(900, 560); w.show(); qt_app.processEvents()
        bar = w.lang_scroll.verticalScrollBar()
        w.tabs.setCurrentWidget(w.lang_scroll); qt_app.processEvents()
        if bar.maximum() == 0:          # tall enough already: nothing can hide, still check the reset
            bar.setMaximum(100)
        bar.setValue(bar.maximum())                                       # scrolled down, as reported
        w.tabs.setCurrentIndex(0); w.tabs.setCurrentWidget(w.lang_scroll); qt_app.processEvents()
        assert bar.value() == 0                                           # back to the top on opening

        def wheel(target, down=True):
            ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120 if down else 120),
                             Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
            QApplication.sendEvent(target, ev); return ev
        ev = wheel(w.user_guide.viewport())
        assert ev.isAccepted()                                            # the guide box keeps its wheel
        before = w.sentence_style.currentIndex()
        w.sentence_style.clearFocus(); wheel(w.sentence_style)
        assert w.sentence_style.currentIndex() == before                  # an unfocused combo is not changed
    finally:
        w.close()


# ---------------------------------------------------------------- "웹에서 받은 파일" 표시 정리
def test_web_marks_are_cleared_only_in_the_program_folder_and_the_exe_last(tmp_path, monkeypatch):
    import os
    from novel_translator import web_mark as wm
    app = tmp_path / 'app'; (app / '_internal' / 'PySide6').mkdir(parents=True); (app / 'config').mkdir()
    files = [app / 'NovelTranslator.exe', app / '_internal' / 'python312.dll', app / '_internal' / 'PySide6' / 'Qt6Core.dll',
             app / 'config' / 'app.json', app / 'README.txt']
    for f in files: f.write_text('x', encoding='utf-8')
    outside = tmp_path / 'book.txt'; outside.write_text('x', encoding='utf-8')
    marked = {str(f) for f in files[:-1]} | {str(outside)}           # README.txt has no mark
    removed = []
    def has_mark(p): return p in marked
    def remove(p):
        assert p.endswith(wm.STREAM); base = p[:-len(wm.STREAM)]
        if base.endswith('Qt6Core.dll'): raise PermissionError('locked')   # errors are ignored
        removed.append(base); marked.discard(base)
    n = wm.clear_marks(app, last=str(files[0]), has_mark=has_mark, remove=remove)
    assert n == 3 and removed[-1] == str(files[0])                   # the exe goes last
    assert str(outside) in marked and str(files[4]) not in removed   # nothing outside the folder; unmarked untouched
    # not the packaged Windows program: nothing happens
    walked = []
    monkeypatch.setattr(wm, 'clear_marks', lambda root, last=None: walked.append((root, last)) or 7)
    assert wm.clear_own_marks_at_start() == 0 and walked == []
    # packaged on Windows but the exe has no mark: no folder walk
    monkeypatch.setattr(wm.os, 'name', 'nt'); monkeypatch.setattr(wm.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(wm.sys, 'executable', str(files[0]))
    monkeypatch.setattr(wm, '_has_mark', lambda p: False)
    assert wm.clear_own_marks_at_start() == 0 and walked == []
    monkeypatch.setattr(wm, '_has_mark', lambda p: True)              # first start: cleared before the window
    assert wm.clear_own_marks_at_start() == 7 and walked == [(str(app), str(files[0]))]


def test_marks_are_cleared_first_thing_at_start_and_never_in_self_tests():
    src = open(Path(__file__).resolve().parents[1] / 'run.py', encoding='utf-8').read()
    main = src[src.index("if __name__ == '__main__':"):]
    assert main.index('clear_own_marks_at_start()') < main.index('runtime_audit') < main.index('from novel_translator.app import main')
    assert 'if not _SELF_TEST:' in main[:main.index('clear_own_marks_at_start()')]


# ---------------------------------------------------------------- 보조 엔진 "미준비" (설정 초기화 뒤)
def test_backup_engines_follow_the_engine_list_even_after_an_old_reset(qt_app, sandbox, monkeypatch):
    w = _window(monkeypatch)
    try:
        gem = {'keys': [{'id': 'g', 'secret': 'dummy-g', 'enabled': True}]}
        engines = [{'id': 'gemini', 'enabled': True}, {'id': 'openai', 'enabled': True}, {'id': 'local', 'enabled': True}]
        # what an earlier "설정 초기화" left behind: engines checked, but the old flags switched off
        w.secrets = {'gemini': gem, 'engines': engines,
                     'openai': {'enabled': False, 'model': 'gpt-6.1-sol', 'keys': [{'id': 'o', 'secret': 'dummy-o', 'enabled': True}]},
                     'local': {'enabled': False, 'model': 'gemma', 'endpoint': 'http://localhost:11434/v1/chat/completions'}}
        assert w._openai_fallback_enabled() and w._local_enabled()
        assert w._openai_worker_settings()['enabled'] and w._local_worker_settings()['enabled']
        w._update_ready_state(); assert 'OpenAI → 로컬 LLM' in w.ready_label.text() and '미준비' not in w.ready_label.text()
        before = dict(w.secrets)
        w._reset_settings()
        assert w.secrets == before and w._openai_fallback_enabled() and w._local_enabled()   # reset keeps the API setup
        # an engine unchecked in the list is not used, whatever its own flag says
        w.secrets = dict(w.secrets, engines=[{'id': 'gemini', 'enabled': True}, {'id': 'openai', 'enabled': False}, {'id': 'local', 'enabled': False}],
                         openai=dict(w.secrets['openai'], enabled=True), local=dict(w.secrets['local'], enabled=True))
        assert not w._openai_fallback_enabled() and not w._local_enabled()
        # old saved settings without an engine list keep using their own flags
        w.secrets = {'gemini': gem, 'openai': {'enabled': True, 'model': 'm', 'keys': [{'id': 'o', 'secret': 'x', 'enabled': True}]}}
        assert w._openai_fallback_enabled() and not w._local_enabled()
    finally:
        w.close()


# ---------------------------------------------------------------- 확정 용어 이름만 바꾸면 다시 번역하지 않음
from novel_translator import name_rename as nr


def test_rename_text_keeps_words_and_fixes_particles():
    t = '좌경은 웃었다. "좌경아!" 그녀가 좌경을 불렀다. 좌경이었다. 좌경으로. 좌경과 좌경에게 좌경의. 흑좌경'
    out, n, _ = nr.apply_counted(t, {'좌경': '쭤지'})
    assert out == '쭤지는 웃었다. "쭤지야!" 그녀가 쭤지를 불렀다. 쭤지였다. 쭤지로. 쭤지와 쭤지에게 쭤지의. 흑좌경' and n == 8
    assert nr.apply_counted('소희였다. 소희가 왔다. 소희는', {'소희': '소현'})[0] == '소현이었다. 소현이 왔다. 소현은'
    assert nr.apply_counted('이훤시를', {'이훤시': '리쉬안스'})[0] == '리쉬안스를'
    assert nr.renames_between({'左京': '좌경', '白颖': '백영'}, {'左京': '쭤징', '白颖': '백영'}) == {'좌경': '쭤징'}
    assert nr.renames_between({'左京': '좌경'}, {'左京': '좌경', '白颖': '백영'}) == {}       # an added term is a real change
    assert nr.renames_between({'左京': '좌'}, {'左京': '쭤'}) == {}                          # one syllable: too risky
    assert nr.renames_between({'甲': '백영', '乙': '백영화'}, {'甲': '바이링', '乙': '백영화'}) == {}   # part of another name


def test_changing_only_a_name_rendering_updates_the_book_without_requests(tmp_path):
    def rule(_prompt, _src, body):
        return '좌경은 창밖을 보았다.' if '窓' in body else '좌경이 웃었다.'
    prov = ScriptedProvider(rule)
    w1, chunks, events = make_worker(tmp_path, '彼は窓の外を見た。\n\n彼は笑った。', prov)
    w1.terms = {'左京': '좌경'}; w1.run()
    first = len(prov.requests); assert first > 0
    out_dir = tmp_path / 'out'
    result = next(p for p in out_dir.iterdir() if p.name.endswith('.ko.txt'))
    assert '좌경' in result.read_text(encoding='utf-8')
    w2 = worker_mod.TranslationWorker(w1.source_path, w1.source_bytes, w1.profile, chunks, 'gemini-3.8-flash',
        [{'id': 'fake', 'secret': 'not-real', 'project_group': 'P1', 'enabled': True}], {'左京': '쭤징'}, 'balanced', out_dir)
    ev2 = []; w2.issue.connect(ev2.append); w2.run()
    assert len(prov.requests) == first                                  # nothing sent again
    text = result.read_text(encoding='utf-8')
    assert '좌경' not in text and '쭤징은 창밖을 보았다.' in text and '쭤징이 웃었다.' in text
    assert any('확정 용어 이름 변경' in m and '좌경→쭤징' in m for m in ev2)
    # any other glossary change is a real change: the book is translated with it
    w3 = worker_mod.TranslationWorker(w1.source_path, w1.source_bytes, w1.profile, chunks, 'gemini-3.8-flash',
        [{'id': 'fake', 'secret': 'not-real', 'project_group': 'P1', 'enabled': True}], {'左京': '쭤징', '白颖': '백영'}, 'balanced', out_dir)
    w3.run(); assert len(prov.requests) > first


# ---------------------------------------------------------------- 속도 우선: 생각 단계 최소
def test_speed_mode_uses_minimal_thinking_and_steps_down_safely(monkeypatch):
    import json as _json
    from novel_translator.config import CONFIG_DIR
    from novel_translator.providers import gemini as gm
    from novel_translator.providers.base import TranslationRequest
    from test_alpha16 import _Resp
    for name in ('app.json', 'defaults.json'):
        modes = _json.loads((CONFIG_DIR / name).read_text(encoding='utf-8'))['modes']
        assert modes['speed']['thinking_level'] == 'minimal'
        assert modes['balanced']['thinking_level'] == 'low' and modes['quality']['thinking_level'] == 'medium'
    ok = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '<P000001>\n번역'}]}}]}
    sent = []; reject_minimal = {'on': False}
    def post(url, headers, json, timeout):
        sent.append(json)
        lvl = (json.get('generationConfig') or {}).get('thinkingConfig', {}).get('thinkingLevel')
        if reject_minimal['on'] and lvl == 'minimal':
            return _Resp({'error': {'message': 'thinking level minimal is not supported for this model'}}, 400)
        return _Resp(ok)
    monkeypatch.setattr(gm.requests, 'post', post)
    g = gm.GeminiProvider()
    g.translate(TranslationRequest('gemini-3.1-flash-lite', 's', 'u', thinking_level='minimal'), 'k')
    assert 'generationConfig' not in sent[-1]                  # Flash Lite: nothing sent (its default is minimal)
    g.translate(TranslationRequest('gemini-3.8-flash', 's', 'u', thinking_level='minimal'), 'k')
    assert sent[-1]['generationConfig']['thinkingConfig']['thinkingLevel'] == 'minimal'
    reject_minimal['on'] = True; sent.clear()
    r = g.translate(TranslationRequest('gemini-3.9-new', 's', 'u', thinking_level='minimal'), 'k')
    assert r.text.endswith('번역') and [s['generationConfig']['thinkingConfig']['thinkingLevel'] for s in sent] == ['minimal', 'low']
    sent.clear(); g.translate(TranslationRequest('gemini-3.9-new', 's', 'u', thinking_level='minimal'), 'k')
    assert [s['generationConfig']['thinkingConfig']['thinkingLevel'] for s in sent] == ['low']   # remembered, never "no setting"
