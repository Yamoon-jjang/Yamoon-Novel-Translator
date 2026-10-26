"""Alpha17 review fixes: folder robustness, status naming, folder cost total, pause on
retries, paste output folder, extraction engine order, Ollama context guard, second
check of scope-excluded paragraphs, automatic glossary extraction before translation."""
from __future__ import annotations
import json, shutil, time
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication, QTableWidgetItem

from novel_translator.providers.base import TranslationRequest, TranslationResponse, ProviderError, ErrorKind
from novel_translator.validation import SKIP_MARKER
from novel_translator import translation_settings as settings
from novel_translator.ui import main_window as ui, advanced_dialog as advanced
import novel_translator.ui.worker as worker_mod
from test_refusal_recovery import ScriptedProvider, rows
from test_alpha16 import _engine_worker, _policy_rule


@pytest.fixture(scope='module')
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def sandbox(tmp_path):
    cfg=tmp_path/'config'; shutil.copytree(ui.CONFIG_DIR,cfg)
    app_cfg=json.loads((cfg/'app.json').read_text(encoding='utf-8')); app_cfg['auto_extract_terms']=False
    app_cfg['unattended'].update(quota_wait_seconds=0,rate_limit_wait_seconds=0,network_wait_seconds=0)
    (cfg/'app.json').write_text(json.dumps(app_cfg,ensure_ascii=False),encoding='utf-8')
    with patch.object(ui,'CONFIG_DIR',cfg),patch.object(settings,'CONFIG_DIR',cfg),patch.object(advanced,'CONFIG_DIR',cfg),patch.object(ui,'DATA_DIR',tmp_path/'data'):
        yield tmp_path,cfg


def _fake_worker_class(started, outcome=lambda p:'completed', usage=None):
    from PySide6.QtCore import QThread, Signal
    class FakeWorker(QThread):
        progress=Signal(int,int,str); failed=Signal(str); paused=Signal(str,bool); finished_ok=Signal(str)
        result_ready=Signal(str,str,int); key_disabled=Signal(str,str,str); issue=Signal(str); api_state=Signal(str)
        def __init__(self,source_path,raw,profile,chunks,model,keys,terms,mode,out_dir,*a,**k):
            super().__init__(k.get('parent')); self.source_path=source_path; self.out_dir=Path(out_dir); self.final_context=''; self.pause_kind=''
            self.terms=dict(terms); self.usage_totals=dict(usage or {}); self.restart_existing=bool(k.get('restart_existing')); started.append(self)
        def _cost(self,row,failed_only=False):
            return 0.5 if not failed_only else 0.1
        def stop(self): pass
        def run(self):
            self.out_dir.mkdir(parents=True,exist_ok=True)
            out=self.out_dir/(self.source_path.stem+'.ko.txt'); out.write_text('번역',encoding='utf-8')
            self.result_ready.emit('completed',str(out),0); self.finished_ok.emit(str(out))
    return FakeWorker


def _wait(qt_app, cond, timeout=15):
    deadline=time.time()+timeout
    while time.time()<deadline and not cond():
        qt_app.processEvents(); time.sleep(0.01)
    return cond()


def _window(monkeypatch):
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k:None)
    monkeypatch.setattr(ui.QMessageBox,'warning',lambda *a,**k:(_ for _ in ()).throw(AssertionError('unexpected warning: '+str(a[2:3]))))
    w=ui.MainWindow()
    w.secrets={'gemini':{'keys':[{'id':'k','secret':'dummy-value','enabled':True}]},'openai':{'enabled':False,'model':'','keys':[]}}
    return w


# ---------------------------------------------------------------- folder robustness + cost total
def test_folder_run_survives_a_file_deleted_mid_run_and_totals_cost(qt_app,sandbox,monkeypatch):
    root,_=sandbox
    books=root/'books'; books.mkdir()
    for n in ('1.txt','2.txt','3.txt'):
        (books/n).write_text('第一章\n\n他静静地看着窗外的雨。',encoding='utf-8')
    started=[]
    Fake=_fake_worker_class(started,usage={'gemini':{'model':'gemini-3.1-flash-lite','requests':2,'failed':0,'input':100,'output':50,'failed_input':0,'failed_output':0}})
    class DeletingFake(Fake):
        def run(self):
            if self.source_path.name=='1.txt': (books/'2.txt').unlink()   # file disappears during the run
            super().run()
    monkeypatch.setattr(ui,'TranslationWorker',DeletingFake)
    w=_window(monkeypatch)
    try:
        w._prepare_new_input(); w.input_mode='folder'; w.batch_root=books
        w._apply_profile('zh-ko'); w._scan_batch_files(); w._update_ready_state()
        w.start_translation()
        assert _wait(qt_app,lambda: w.last_result_path is not None and w.last_result_path.name=='번역_요약.txt'), w.error_view.toPlainText()
        status={Path(k).name:v[0] for k,v in w.batch_results.items()}
        assert status['1.txt']=='완료' and status['3.txt']=='완료' and '건너뜀' in status['2.txt']
        summary=w.last_result_path.read_text(encoding='utf-8')
        assert '폴더 전체 추정 비용: 약 $1.000' in summary and 'Gemini: 요청 4회' in summary
        assert '걸린 시간: ' in summary and '걸린 시간' in w.time_label.text()
        assert ' KB' in w.file_label.text() or ' B ·' in w.file_label.text()
    finally:
        w.close()


def test_job_table_tolerates_missing_files(qt_app,sandbox,monkeypatch):
    root,_=sandbox
    w=_window(monkeypatch)
    try:
        w.input_mode='folder'; w.batch_root=root; w.batch_files=[root/'gone.txt']
        w._refresh_job_table()
        assert w.job_table.item(0,1).text()=='없음'
    finally:
        w.close()


# ---------------------------------------------------------------- labels / output folder / extraction engine
def test_gemini_model_label_shows_when_unused(qt_app,sandbox,monkeypatch):
    w=_window(monkeypatch)
    try:
        w.secrets['engines']=[{'id':'local','enabled':True},{'id':'gemini','enabled':False}]
        w.secrets['local']={'enabled':True,'model':'qwen','endpoint':'http://localhost:11434/v1/chat/completions'}
        w._update_ready_state()
        assert w.model_caption.text()=='로컬 LLM 모델'      # the box follows the main (first) engine
        assert w.model.isHidden() and w._alt_model_id()=='qwen'
        assert 'Gemini 키' not in w.ready_label.text()
        w.secrets['engines']=[{'id':'gemini','enabled':True}]; w._update_ready_state()
        assert w.model_caption.text()=='Gemini 모델'
    finally:
        w.close()


def test_pasted_text_result_goes_to_the_chosen_output_folder(qt_app,sandbox,monkeypatch,tmp_path):
    started=[]; monkeypatch.setattr(ui,'TranslationWorker',_fake_worker_class(started))
    w=_window(monkeypatch)
    try:
        w._prepare_new_input(); w._apply_profile('zh-ko'); w._set_preview_source('')
        out=tmp_path/'my_out'; out.mkdir(); w.custom_output_dir=out
        w.preview.setPlainText('他静静地看着窗外的雨。'); w.start_translation()
        assert _wait(qt_app,lambda: started and not started[0].isRunning() and w._result_text)
        assert started[0].out_dir==out and (out/(started[0].source_path.stem+'.ko.txt')).exists()
    finally:
        w.close()


def test_term_extraction_follows_engine_order(qt_app,sandbox,monkeypatch):
    made=[]
    class FakeThread:
        def __init__(self,provider_id,*a,**k):
            made.append((provider_id,a[0] if a else None,k.get('local_options')))
            from PySide6.QtCore import QObject
        def isRunning(self): return False
        class _Sig:
            def connect(self,*a): pass
        done=_Sig(); finished=_Sig()
        def start(self): pass
    monkeypatch.setattr(ui,'TermExtractThread',FakeThread)
    w=_window(monkeypatch)
    try:
        w.secrets['engines']=[{'id':'local','enabled':True},{'id':'gemini','enabled':True}]
        w.secrets['local']={'enabled':True,'model':'qwen','endpoint':'http://localhost:11434/v1/chat/completions','think':False,'num_ctx':12288}
        w._prepare_new_input(); w._apply_profile('zh-ko'); w._set_preview_source('')
        w.preview.setPlainText('林雨说。林雨笑了。林雨走了。林雨来了。林雨看着。林雨想着。林雨听着。')
        w._extract_terms()
        assert made and made[0][0]=='local' and made[0][2]['model']=='qwen'
    finally:
        w.close()


# ---------------------------------------------------------------- automatic extraction before translation
def test_auto_extraction_runs_once_then_translation_starts(qt_app,sandbox,monkeypatch):
    root,cfg=sandbox
    started=[]; monkeypatch.setattr(ui,'TranslationWorker',_fake_worker_class(started))
    calls=[]
    class FakeThread(ui.QThread):
        done=ui.Signal(bool,object)
        def __init__(self,provider_id,secret,model,endpoint,system,user,cands,parent=None,local_options=None):
            super().__init__(parent); calls.append(user)
        def run(self): self.done.emit(True,{'林雨':'린위'})
    monkeypatch.setattr(ui,'TermExtractThread',FakeThread)
    w=_window(monkeypatch)
    try:
        w.auto_terms.setChecked(True)
        w._prepare_new_input(); w._apply_profile('zh-ko'); w._set_preview_source('')
        w.term_table.setRowCount(1); w.term_table.setItem(0,0,QTableWidgetItem('苏晴')); w.term_table.setItem(0,1,QTableWidgetItem('쑤칭'))
        w.preview.setPlainText('林雨说。林雨笑了。林雨走了。林雨来了。林雨看着。林雨想着。林雨听着。苏晴来了。')
        w.start_translation()
        assert _wait(qt_app,lambda: started and not started[0].isRunning() and w._result_text)
        assert len(calls)==1 and '林雨' in calls[0] and '林雨说' not in calls[0], 'only the candidate list is sent'
        assert started[0].terms=={'苏晴':'쑤칭','林雨':'린위'}, 'existing terms kept, new ones added before translating'
        assert '인명·용어 자동 추출 · 새 항목 1개' in w.error_view.toPlainText()
        # Same source again (e.g. 계속): no second extraction.
        w._show_preview('source'); w._paste_active=True
        w.start_translation()
        assert _wait(qt_app,lambda: len(started)==2 and not started[1].isRunning())
        assert len(calls)==1
        assert json.loads((cfg/'app.json').read_text(encoding='utf-8'))['auto_extract_terms'] is True
    finally:
        w.close()


def test_auto_extraction_failure_never_blocks_translation(qt_app,sandbox,monkeypatch):
    started=[]; monkeypatch.setattr(ui,'TranslationWorker',_fake_worker_class(started))
    class FailThread(ui.QThread):
        done=ui.Signal(bool,object)
        def __init__(self,*a,**k): super().__init__(k.get('parent') or (a[7] if len(a)>7 else None))
        def run(self): self.done.emit(False,'QUOTA · 한도')
    monkeypatch.setattr(ui,'TermExtractThread',FailThread)
    w=_window(monkeypatch)
    try:
        w.auto_terms.setChecked(True)
        w._prepare_new_input(); w._apply_profile('zh-ko'); w._set_preview_source('')
        w.preview.setPlainText('林雨说。林雨笑了。林雨走了。林雨来了。林雨看着。林雨想着。林雨听着。')
        w.start_translation()
        assert _wait(qt_app,lambda: started and not started[0].isRunning() and w._result_text)
        assert '자동 추출 실패 · 용어집 없이 번역을 계속합니다' in w.error_view.toPlainText()
    finally:
        w.close()


# ---------------------------------------------------------------- worker: status, pause, second check
def test_status_line_names_the_backup_engine_while_it_works(tmp_path):
    gem=ScriptedProvider(_policy_rule()); oai=ScriptedProvider()
    text='\n\n'.join(['　前の段落です。','　COREPOLICY を含む段落です。','　後の段落です。'])
    w,_,_=_engine_worker(tmp_path,text,gem,openai=oai,order=['gemini','openai'])
    states=[]; w.api_state.connect(states.append)
    w.run()
    assert any(s.startswith('OpenAI · backup-model') and '보조 번역 중' in s for s in states), states
    assert any(s.startswith('OpenAI · ') and '정상' in s for s in states), states


def test_every_http_request_waits_while_paused():
    from novel_translator.orchestrator import TranslationOrchestrator, KeyState
    from novel_translator.profiles import ProfileManager
    order=[]
    class Flaky:
        name='gemini'; last_usage=None; n=0
        def translate(self,request,key):
            order.append('http'); Flaky.n+=1
            if Flaky.n==1: raise ProviderError(ErrorKind.TRANSIENT,'잠시 오류')
            return TranslationResponse('<P000001>\n번역','STOP',{})
    o=TranslationOrchestrator(Flaky(),[KeyState('k','s','g',True)],'m',retries=2,target_rpm=1e9,min_rpm=1e9,max_rpm=1e9,
                              sleep_fn=lambda s:None,pause_fn=lambda: order.append('pause-check'))
    o.translate_chunk(ProfileManager().set_active('ja-ko'),'<P000001>\nこんにちは',{},'')
    assert order.count('http')==2 and order.index('pause-check')==0
    assert all(order[i-1]=='pause-check' for i,x in enumerate(order) if x=='http'), order


def test_wrongly_excluded_paragraph_gets_a_second_check(tmp_path):
    def gem_rule(prompt,src,body):
        if 'ADULTONLY' in body: return SKIP_MARKER        # main engine over-applies the minors marker
    gem=ScriptedProvider(gem_rule); loc=ScriptedProvider()
    text='\n\n'.join(['　前の段落です。','　ADULTONLY 大人の場面です。','　後の段落です。'])
    w,_,events=_engine_worker(tmp_path,text,gem,local=loc,limits=(500,1,1000))
    w.run()
    r=rows(tmp_path)
    assert all(x['status']=='COMPLETED' for x in r), [dict(x) for x in r]
    assert 'ADULTONLY' not in r[0]['translation'] and loc.requests
    assert any('재확인 후 번역 성공' in e for e in events), events


def test_exclusion_stands_when_the_next_engine_also_excludes(tmp_path):
    def rule(prompt,src,body):
        if 'MINORCASE' in body: return SKIP_MARKER
    gem=ScriptedProvider(rule); loc=ScriptedProvider(rule)
    text='\n\n'.join(['　前の段落です。','　MINORCASE の段落です。'])
    w,_,events=_engine_worker(tmp_path,text,gem,local=loc,limits=(500,1,1000))
    w.run()
    r=rows(tmp_path)
    assert r[0]['status']=='PARTIAL' and 'MINORCASE' in r[0]['translation']
    assert any('로컬 LLM도 범위 제외' in e for e in events), events


def test_no_second_check_without_a_backup_engine(tmp_path):
    def rule(prompt,src,body):
        if 'MINORCASE' in body: return SKIP_MARKER
    gem=ScriptedProvider(rule)
    w,_,_=_engine_worker(tmp_path,'　前です。\n\n　MINORCASE です。',gem,limits=(500,1,1000))
    w.run()
    assert len(gem.requests)==1 and rows(tmp_path)[0]['status']=='PARTIAL'


# ---------------------------------------------------------------- Ollama context guard
def test_ollama_context_grows_for_oversized_requests():
    from novel_translator.providers.local_llm import LocalLLMProvider as L
    p=L()
    small=TranslationRequest('m','s'*12000,'中'*3000)
    assert p.context_for(small)==12288
    big=TranslationRequest('m','s'*12000,'中'*3000+'메'*8000)
    c=p.context_for(big)
    assert c>12288 and c%2048==0 and c<=L.MAX_AUTO_CTX
    assert L(num_ctx=0).context_for(big)==0


def _no_repair():
    """Tests whose fake translations deliberately contain source characters (to tell
    paragraphs apart) are not about the leftover-script repair: switch it off."""
    base=worker_mod.load_json
    worker_mod.load_json=lambda path: dict(base(path), repair_leftover_script=False)


# ---------------------------------------------------------------- resume: only untranslated paragraphs
def _mark(b):
    """Identify a paragraph in a fake Korean translation without copying its Han/kana."""
    return 'U'+'-'.join(f'{ord(c):x}' for c in b[:6])


def _blocked_first_run(tmp_path):
    text='\n\n'.join(['　一つ目の段落です。','　BLOCKED の段落です。','　三つ目の段落です。'])
    gem=ScriptedProvider(lambda p,s,b: (_ for _ in ()).throw(ProviderError(ErrorKind.POLICY,'입력 차단: PROHIBITED_CONTENT')) if 'BLOCKED' in s and 'BLOCKED' in b else '제미니번역 '+_mark(b))
    w,chunks,_=_engine_worker(tmp_path,text,gem,limits=(500,1,1000)); _no_repair()
    w.run()
    r=rows(tmp_path)
    assert len(chunks)==1 and r[0]['status']=='PARTIAL' and 'BLOCKED' in r[0]['translation']
    return text, r[0]['translation']


def test_rerun_with_another_engine_sends_only_the_untranslated_paragraphs(tmp_path):
    text,first=_blocked_first_run(tmp_path)
    loc=ScriptedProvider(lambda p,s,b: '로컬번역 '+_mark(b))
    w,_,events=_engine_worker(tmp_path,text,ScriptedProvider(),local=loc,order=['local'],limits=(500,1,1000)); _no_repair()
    w.run()
    assert len(loc.requests)==1
    sent=loc.requests[0].user_prompt.split('[SOURCE',1)[1]
    assert 'BLOCKED' in sent and '一つ目' not in sent and '三つ目' not in sent, 'only the leftover paragraph is sent'
    r=rows(tmp_path)
    assert r[0]['status']=='COMPLETED'
    assert r[0]['translation'].count('제미니번역')==2 and '로컬번역' in r[0]['translation'], 'earlier translations kept'
    assert any('원문으로 남은 문단만 다시 요청' in e for e in events)


def test_failed_rerun_keeps_the_earlier_partial_translation(tmp_path):
    text,first=_blocked_first_run(tmp_path)
    loc=ScriptedProvider(lambda p,s,b: (_ for _ in ()).throw(ProviderError(ErrorKind.REFUSAL,'거부')))
    w,_,_=_engine_worker(tmp_path,text,ScriptedProvider(),local=loc,order=['local'],limits=(500,1,1000)); _no_repair()
    w.run()
    r=rows(tmp_path)
    assert r[0]['status']=='PARTIAL' and r[0]['translation'].count('제미니번역')==2


def test_auto_extraction_is_skipped_when_resuming_a_translated_file(qt_app,sandbox,monkeypatch,tmp_path):
    root,_=sandbox
    started=[]; monkeypatch.setattr(ui,'TranslationWorker',_fake_worker_class(started))
    calls=[]
    class FakeThread(ui.QThread):
        done=ui.Signal(bool,object)
        def __init__(self,*a,**k): super().__init__(k.get('parent')); calls.append(1)
        def run(self): self.done.emit(True,{})
    monkeypatch.setattr(ui,'TermExtractThread',FakeThread)
    book=tmp_path/'book.txt'; raw='林雨说。林雨笑了。林雨走了。林雨来了。林雨看着。林雨想着。林雨听着。'.encode('utf-8'); book.write_bytes(raw)
    import hashlib
    from novel_translator.storage import Database
    db=Database(root/'data'/'translations.db'); pid=db.get_or_create_project(str(book),'zh','zh-ko',hashlib.sha256(raw).hexdigest(),'fp')
    db.upsert_chunk(pid,0,'x')
    db.set_chunk_status(pid,0,'PARTIAL',translation='<P000001>\n번역')
    monkeypatch.setattr(ui.QFileDialog,'getOpenFileName',lambda *a,**k:(str(book),''))
    w=_window(monkeypatch)
    try:
        w.auto_terms.setChecked(True); w.open_txt()
        assert not w._auto_terms_due(), 'resume must not change the glossary (would re-translate everything)'
        w._force_restart=True
        assert w._auto_terms_due(), '처음부터 다시 may extract'
    finally:
        w.close()


# ---------------------------------------------------------------- short source lines are translated
@pytest.mark.parametrize('lang,text,expected',[
    ('zh','偏楼版1',True),('zh','第一章',True),('zh','林雨',True),('zh','嗯。',True),('zh','偏楼版12（下）',True),
    ('zh','한국어 이름 王 입니다.',False),('zh','이미 번역된 문장입니다.',False),('zh','***',False),('zh','2024',False),
    ('ja','え？',True),('ja','第一章',True),('ja','はい',True),('ja','한국어 文 입니다',False),
    ('en','Chapter 3',True),('en','No.',True),('en','What?',True),('en','한국어 OK 입니다',False),
])
def test_short_lines_without_korean_are_source_text(lang,text,expected):
    from novel_translator.chunking import should_translate_text, split_paragraphs, mark_translatable
    assert should_translate_text(text,lang) is expected
    assert any(p.translatable for p in mark_translatable(split_paragraphs(text),lang)) is expected


def _worker_for(tmp_path, text, paragraphs, provider, lang_profile='zh-ko', **kw):
    from novel_translator.profiles import ProfileManager
    from novel_translator.chunking import make_chunks
    profile=ProfileManager().set_active(lang_profile)
    chunks=make_chunks(paragraphs,'zh',target_tokens=500,min_tokens=1,max_tokens=1000)
    src=tmp_path/'novel.txt'; raw=text.encode('utf-8'); src.write_bytes(raw)
    cfg={'modes':{'balanced':{'thinking_level':'medium'}},'rate_limiter':{'start_rpm':1e9,'min_rpm':1e9,'max_rpm':1e9},
         'refusal_recovery':{'reframe_once':True,'split_chunk':True,'fallback_provider':True,'fallback_on_policy':True}}
    worker_mod.DATA_DIR=tmp_path/'data'; worker_mod.GeminiProvider=lambda: provider; worker_mod.load_json=lambda _p: cfg
    w=worker_mod.TranslationWorker(src,raw,profile,chunks,'gemini-3.8-flash',[{'id':'fake','secret':'not-real','project_group':'P1','enabled':True}],{},'balanced',tmp_path/'out',
                                   all_paragraphs=paragraphs,**kw)
    events=[]; w.issue.connect(events.append)
    return w,chunks,events


def test_rerun_after_rule_change_translates_only_the_short_lines(tmp_path):
    from dataclasses import replace
    from novel_translator.chunking import split_paragraphs, mark_translatable
    text='第一章\n\n偏楼版1\n\n他静静地看着窗外的雨，很久没有说话。\n\n偏楼版2\n\n她也沉默着，轻轻叹了口气。'
    new_marks=mark_translatable(split_paragraphs(text),'zh')
    assert all(p.translatable for p in new_marks)
    # First run as the old rule did it: short lines kept as source, long lines translated.
    old_marks=[replace(p,translatable=len(p.text)>=4 and not p.text.startswith('偏楼') and p.text!='第一章') for p in new_marks]
    mark=lambda b: 'U'+'-'.join(f'{ord(c):x}' for c in b[:4])   # identifies the paragraph without leaving Han in the Korean
    first=ScriptedProvider(lambda p,s,b: '이전번역 '+mark(b))
    w,_,_=_worker_for(tmp_path,text,old_marks,first); _no_repair(); w.run()
    assert len(first.requests)==1
    # Re-run with the new rule: only the three short lines are requested.
    second=ScriptedProvider(lambda p,s,b: '새번역 '+b)
    w,chunks,events=_worker_for(tmp_path,text,new_marks,second); _no_repair(); w.run()
    sent='\n'.join(r.user_prompt.split('[SOURCE',1)[1] for r in second.requests)
    assert '偏楼版1' in sent and '偏楼版2' in sent and '第一章' in sent
    assert '他静静地' not in sent and '她也沉默' not in sent, 'already translated paragraphs are not paid for again'
    out=(tmp_path/'out'/'novel.ko.txt').read_text(encoding='utf-8-sig')
    assert '새번역 偏楼版1' in out and '이전번역 '+mark('他静静地') in out and '偏楼版2' not in out.replace('새번역 偏楼版2','')
    assert any('문단 단위로 재사용' in e for e in events)


# ---------------------------------------------------------------- Korean punctuation in results
@pytest.mark.parametrize('src,expected',[
    ('그는 말했다。그녀는 웃었다。','그는 말했다. 그녀는 웃었다.'),
    ('왜？어째서？','왜? 어째서?'),
    ('하나，둘、셋：끝；','하나, 둘, 셋: 끝;'),
    ('“좋아。”','"좋아."'),
    ('말했다。“가자”','말했다. "가자"'),
    ('「안녕」하고 말했다。','"안녕"하고 말했다.'),
    ("‘생각’했다。","'생각'했다."),
    ('『검은 숲』을 읽었다。','『검은 숲』을 읽었다.'),
    ('３층의 ＡＢ실，５０％。','3층의 AB실, 50%.'),
    ('그래。。。','그래...'),
    ('“。。。。。”','"....."'),                    # punctuation-only line
    ('그는（웃으며）말했다。','그는(웃으며)말했다.'),
    ('그는　웃었다～','그는 웃었다~'),
    ('　들여쓰기 문단이다。','　들여쓰기 문단이다.'),
    ('……','……'),
    ('존・스미스가 왔다〜','존·스미스가 왔다~'),
    ('그건‥','그건…'),
    ('【스킬】을 얻었다。※주의','【스킬】을 얻었다. ※주의'),
    ("It’s 그의 것이다。","It's 그의 것이다."),
    ('他静静地看着。','他静静地看着。'),            # Chinese source kept as is
    ('Hello！World？','Hello！World？'),            # foreign text without Hangul kept as is
    ('이미 한국식이다. 문제없다!','이미 한국식이다. 문제없다!'),
])
def test_korean_punctuation(src,expected):
    from novel_translator.punctuation import korean_punctuation
    assert korean_punctuation(src)==expected


def test_quote_styles():
    from novel_translator.punctuation import korean_punctuation as k
    assert k('“가자。” 「응」','curly')=='“가자.” “응”'
    assert k('“가자。” 「응」','keep')=='“가자.” 「응」'
    assert k('“가자。” 「응」','straight')=='"가자." "응"'


def test_punctuation_only_source_line_is_cleaned_in_the_result(tmp_path):
    from novel_translator.chunking import split_paragraphs, mark_translatable
    text='　他静静地看着窗外的雨。\n\n“。。。。。”\n\n　她也沉默着。'
    paras=mark_translatable(split_paragraphs(text),'zh')
    assert [p.translatable for p in paras]==[True,False,True]
    w,_,_=_worker_for(tmp_path,text,paras,ScriptedProvider(lambda p,s,b:'그는 말했다。'))
    w.run()
    out=(tmp_path/'out'/'novel.ko.txt').read_text(encoding='utf-8-sig')
    assert '"....."' in out and '。' not in out


def test_result_file_uses_korean_punctuation_and_keeps_source_paragraphs(tmp_path):
    def rule(prompt,src,body):
        if 'BLOCKED' in body: raise ProviderError(ErrorKind.POLICY,'입력 차단: PROHIBITED_CONTENT')
        return '그는 말했다。그녀는 웃었다！'
    text='\n\n'.join(['　一つ目の段落です。','　BLOCKED の段落です。'])
    w,_,_=_engine_worker(tmp_path,text,ScriptedProvider(rule),limits=(500,1,1000))
    w.run()
    out=(tmp_path/'out'/'novel.ko.partial.txt').read_text(encoding='utf-8-sig')
    assert '그는 말했다. 그녀는 웃었다!' in out
    assert 'BLOCKED の段落です。' in out, 'a paragraph kept as source is not altered'
    # Stored translation is unchanged (only the written file is cleaned).
    assert '。' in rows(tmp_path)[0]['translation']


def test_korean_punctuation_can_be_turned_off(tmp_path):
    text='　一つ目の段落です。'
    w,_,_=_engine_worker(tmp_path,text,ScriptedProvider(lambda p,s,b:'그는 말했다。'),limits=(500,1,1000))
    worker_mod.load_json=lambda _p: {'korean_punctuation':False,'modes':{'balanced':{}},'rate_limiter':{'start_rpm':1e9,'min_rpm':1e9,'max_rpm':1e9},
                                     'refusal_recovery':{'reframe_once':True,'split_chunk':True,'fallback_provider':True,'fallback_on_policy':True}}
    w.run()
    assert '그는 말했다。' in (tmp_path/'out'/'novel.ko.txt').read_text(encoding='utf-8-sig')


# ---------------------------------------------------------------- v1.5: version, About, manual
def test_version_label_and_about_dialog(qt_app,sandbox,monkeypatch):
    import novel_translator as nt
    from novel_translator.ui.about_dialog import AboutDialog, app_icon
    assert nt.__version__=='2.5.4' and nt.VERSION_LABEL=='v2.5.4'
    w=_window(monkeypatch)
    try:
        assert w.windowTitle()=='야문 Novel Translator v2.5.4' and 'Alpha' not in w.windowTitle()
        assert w.about_btn.text()=='정보' and w.manual_btn.text()=='사용 설명서'
        assert not app_icon().isNull()
        d=AboutDialog(w)
        texts=' '.join(d.texts)
        assert d.width()==480 and d.height()==460, '빌드 정보 행을 포함한 정보 창 크기'
        assert '야문 Novel Translator' in texts and '버전 2.5' in texts and '야문 짱' in texts and '무료 프로그램' in texts
        assert 'Alpha' not in texts
        d.close()
    finally:
        w.close()


def test_manual_is_found_and_opened(qt_app,sandbox,monkeypatch):
    from novel_translator.config import manual_path
    path=manual_path()
    assert path is not None and path.name=='사용설명서.html'
    html=path.read_text(encoding='utf-8')
    assert 'v2.5.4' in html and 'Alpha' not in html
    for heading in ('API 설정','번역하기','이름·용어','거부·차단','결과 파일','로컬 LLM','자주 묻는 질문'):
        assert heading in html
    opened=[]
    monkeypatch.setattr('PySide6.QtGui.QDesktopServices.openUrl',lambda url: opened.append(url.toLocalFile()) or True)
    w=_window(monkeypatch)
    try:
        w.open_manual()
        assert opened and opened[0].endswith('사용설명서.html')
    finally:
        w.close()


def test_manual_mentions_only_buttons_that_exist(qt_app,sandbox,monkeypatch):
    """Every [button] named in the manual exists in the app (main window or API dialog)."""
    import re
    from novel_translator.config import manual_path
    from PySide6.QtWidgets import QPushButton
    from novel_translator.ui.api_dialog import ApiDialog
    html=manual_path().read_text(encoding='utf-8')
    named=set(re.findall(r'<span class="btn">([^<]+)</span>',html))
    w=_window(monkeypatch)
    try:
        d=ApiDialog([],'gemini-3.1-flash-lite',{},'',w,local_settings={},engines=None)
        from novel_translator.ui.refinement_run_dialog import RefinementRunDialog
        r=RefinementRunDialog(w)
        have={b.text() for b in w.findChildren(QPushButton)}|{b.text() for b in d.findChildren(QPushButton)}|{b.text() for b in r.findChildren(QPushButton)}
        r.close()
        have|={'다시 진행','번역 중지','추가 정보','실행','결과 복사','결과 저장','번역 결과','원문','말투표 건너뛰고 번역 시작','말투표 생성 취소'}   # state labels / Windows dialog
        have|={'이어서 번역','처음부터 다시 번역'}   # buttons of the "이전 번역 이어 쓰기" question (v2.5.2, checked in test_v252)
        from novel_translator.ui.name_collision_dialog import NameCollisionDialog
        c=NameCollisionDialog([('스잔',['Susan','Suzanne'])],{},set(),parent=w)
        have|={b.text() for b in c.findChildren(QPushButton)}; c.close()   # 한글 이름 겹침 확인 (v2.5.2 보완)
        missing=sorted(n for n in named if n not in have)
        assert not missing, missing
    finally:
        w.close()


def test_exe_icon_matches_the_about_icon():
    """assets/app.ico is the About icon drawing at every Windows size, and the build uses it."""
    from pathlib import Path
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImageReader, QImage
    QApplication.instance() or QApplication([])
    from novel_translator.ui.about_dialog import app_pixmap
    root=Path(__file__).resolve().parent.parent
    assert "icon='assets/app.ico'" in (root/'NovelTranslator.spec').read_text(encoding='utf-8')
    reader=QImageReader(str(root/'assets'/'app.ico'),b'ico')
    frames={}
    for i in range(reader.imageCount()):
        reader.jumpToImage(i); img=reader.read()
        frames[img.width()]=img.convertToFormat(QImage.Format_ARGB32)
    assert {16,32,48,256} <= set(frames), sorted(frames)
    def channels(v): return ((v>>24)&255,(v>>16)&255,(v>>8)&255,v&255)
    for n in (16,24):   # text-free sizes ('가' in larger ones depends on the local font)
        drawn=app_pixmap(n).toImage().convertToFormat(QImage.Format_ARGB32)
        stored=frames[n]
        diffs=[max(abs(a-b) for a,b in zip(channels(drawn.pixel(x,y)),channels(stored.pixel(x,y)))) for y in range(n) for x in range(n)]
        # Anti-aliasing differs slightly between platforms; a different drawing differs a lot.
        assert sum(diffs)/len(diffs) < 6 and sum(d>96 for d in diffs) <= n//4, \
            f'app.ico {n}px is not the About icon drawing: regenerate assets/app.ico'


def test_local_model_list_from_ollama_and_lm_studio(monkeypatch):
    from novel_translator.providers import local_llm as ll
    calls=[]
    class R:
        def __init__(self,data,code=200): self._d=data; self.status_code=code
        def json(self): return self._d
    def get(url,timeout=None,headers=None):
        calls.append(url)
        if url.endswith('/api/tags'): return R({'models':[{'name':'qwen27b:latest'},{'name':'gemma3:12b'}]})
        if url.endswith('/v1/models'): return R({'data':[{'id':'lmstudio-model'}]})
        return R({},404)
    monkeypatch.setattr(ll.requests,'get',get)
    # "localhost" is asked as 127.0.0.1 first (no slow IPv6 attempt on Windows), v2.5
    assert ll.LocalLLMProvider().list_models()==['gemma3:12b','qwen27b:latest'] and calls[-1]=='http://127.0.0.1:11434/api/tags'
    assert ll.LocalLLMProvider(ll.LocalLLMProvider.LMSTUDIO_ENDPOINT).list_models()==['lmstudio-model'] and calls[-1]=='http://127.0.0.1:1234/v1/models'
    tried=[]
    def ipv6_only(url,timeout=None,headers=None):
        tried.append(url)
        if '127.0.0.1' in url: raise ll.requests.ConnectionError('refused')
        return get(url,timeout,headers)
    monkeypatch.setattr(ll.requests,'get',ipv6_only)
    assert ll.LocalLLMProvider().list_models()==['gemma3:12b','qwen27b:latest']
    assert tried==['http://127.0.0.1:11434/api/tags','http://localhost:11434/api/tags']   # falls back to the address as written
    def down(*a,**k): raise ll.requests.ConnectionError('refused')
    monkeypatch.setattr(ll.requests,'get',down)
    with pytest.raises(ProviderError) as caught: ll.LocalLLMProvider().list_models()
    assert 'Ollama' in caught.value.message


def test_local_model_dropdown_is_filled_from_the_server(qt_app,monkeypatch):
    import time
    from novel_translator.ui import api_dialog as ad
    monkeypatch.setattr(ad.LocalLLMProvider,'list_models',lambda self,timeout=4.0: ['gemma3:12b','qwen27b:latest'])
    d=ad.ApiDialog([],'gemini-3.1-flash-lite',{},'',None,local_settings={'model':'qwen27b:latest','endpoint':'http://localhost:11434/v1/chat/completions'},engines=None)
    try:
        tabs=d.findChild(ad.QTabWidget); tabs.setCurrentIndex(d._tab_of['local'])   # first visit loads the list
        deadline=time.time()+5
        while time.time()<deadline and d.local_model.count()<2:
            qt_app.processEvents(); time.sleep(0.01)
        assert [d.local_model.itemText(i) for i in range(d.local_model.count())]==['gemma3:12b','qwen27b:latest']
        assert d.local_model.text()=='qwen27b:latest', 'the saved model stays selected'
        assert '등록된 모델 2개' in d.local_models_status.text()
        d.local_model.setCurrentIndex(0); assert d.local_model.text()=='gemma3:12b'
        d.local_model.setCurrentText('직접입력모델'); assert d.local_model.text()=='직접입력모델', 'typing a name still works'
    finally:
        d.done(0)


# ---------------------------------------------------------------- v1.5 fixes: scrolling while running, glossary quality
def test_panes_stay_scrollable_but_read_only_while_translating(qt_app,sandbox,monkeypatch):
    import time
    from PySide6.QtWidgets import QAbstractItemView
    state={'release':False}
    Base=_fake_worker_class([])
    class Hold(Base):
        def stop(self): state['release']=True
        def run(self):
            while not state['release']: time.sleep(0.01)
            super().run()
    monkeypatch.setattr(ui,'TranslationWorker',Hold)
    w=_window(monkeypatch)
    try:
        w._prepare_new_input(); w._apply_profile('zh-ko'); w._set_preview_source('')
        w.term_table.setRowCount(40)
        for r in range(40):
            w.term_table.setItem(r,0,QTableWidgetItem(f'名{r}')); w.term_table.setItem(r,1,QTableWidgetItem(f'이름{r}'))
        w.preview.setPlainText('他静静地看着窗外的雨。\n'*200); w.start_translation()
        assert w.worker and w.worker.isRunning()
        for pane in (w.term_table,w.book_memory,w.preview):
            assert pane.isEnabled(), 'must stay enabled so it can scroll'
        assert w.term_table.editTriggers()==QAbstractItemView.NoEditTriggers
        assert w.book_memory.isReadOnly() and w.preview.isReadOnly()
        w.tabs.setCurrentIndex(w.tabs.indexOf(w.term_table.parentWidget())); w.resize(1000,700); w.show(); qt_app.processEvents()
        sb=w.term_table.verticalScrollBar(); assert sb.isEnabled() and sb.maximum()>0
        sb.setValue(sb.maximum()); assert sb.value()==sb.maximum()
        assert not w.term_add.isEnabled() and not w.term_extract.isEnabled()
        state['release']=True
        assert _wait(qt_app,lambda: not (w.worker and w.worker.isRunning()) and w.term_add.isEnabled())
        assert w.term_table.editTriggers()!=QAbstractItemView.NoEditTriggers and not w.book_memory.isReadOnly()
    finally:
        state['release']=True; w.close()


def test_glossary_rejects_leftover_chinese_and_ordinary_words():
    from novel_translator.term_extraction import parse_glossary, candidates, valid_rendering
    c=[(k,9) for k in ('左京','白颖','李萱诗','郝江化','公司','何晓月','岑筱薇')]
    got=parse_glossary('{"左京":"좌경","白颖":"백영","李萱诗":"리환시","郝江化":"郝장화","公司":"회사","何晓月":"허효월","岑筱薇":"岑소薇"}',c)
    assert got=={'左京':'좌경','白颖':'백영','李萱诗':'리환시','何晓月':'허효월'}
    assert not valid_rendering('林雨','林雨') and valid_rendering('Alice','앨리스') and valid_rendering('爱丽丝','Alice')
    names=[k for k,_ in candidates(('公司里，林雨说话。在公司，苏晴笑了。老师来了，岑筱薇走了。郝江化来了。')*10,'zh')]
    assert '林雨' in names and '岑筱薇' in names and '郝江化' in names
    assert not any(x in names for x in ('公司','公司里','在公司','司里','老师'))


def test_suspicious_glossary_rows_are_marked(qt_app,sandbox,monkeypatch):
    w=_window(monkeypatch)
    try:
        rows=[('左京','좌경'),('郝江化','郝장화'),('岑筱薇','岑소薇'),('林雨','林雨'),('公司','회사')]
        w.term_table.setRowCount(0)
        for a,b in rows:
            r=w.term_table.rowCount(); w.term_table.insertRow(r); w.term_table.setItem(r,0,QTableWidgetItem(a)); w.term_table.setItem(r,1,QTableWidgetItem(b))
        bad=[a for _,a,_ in w._suspicious_terms()]
        assert bad==['郝江化','岑筱薇','林雨','公司']
        assert '일반 단어' in w.term_table.item(4,1).toolTip()
        assert w.term_table.item(1,1).toolTip().startswith('확인 필요') and not w.term_table.item(0,1).toolTip()
        w.term_table.item(1,1).setText('학강화')
        assert not w.term_table.item(1,1).toolTip(), 'fixing the row clears the mark'
    finally:
        w.close()



# ---------------------------------------------------------------- leftover Chinese/kana repair, prompt v12
class _RepairProvider:
    """First call: Korean with leftover Han; repair call: fixed Korean (or a bad answer)."""
    name='local'
    def __init__(self, repair_answer=None):
        self.requests=[]; self.repair_answer=repair_answer
    def translate(self, request, key):
        from novel_translator.prompting import REPAIR_SYSTEM
        self.requests.append(request)
        if request.system_prompt==REPAIR_SYSTEM:
            if self.repair_answer is not None:
                return TranslationResponse(self.repair_answer,'STOP',{})
            return TranslationResponse('<P000001>\n린위는 창밖의 비를 오래 바라보았다. 아무 말도 하지 않았다.','STOP',{})
        return TranslationResponse('<P000001>\n林雨는 窗外의 비를 오래 바라보았다. 아무 말도 하지 않았다.','STOP',{})


def _single_para_worker(tmp_path, provider):
    from novel_translator.profiles import ProfileManager
    from novel_translator.chunking import split_paragraphs, mark_translatable, make_chunks
    text='林雨看着窗外的雨，很久没有说话。'
    paras=mark_translatable(split_paragraphs(text),'zh')
    return _worker_for(tmp_path,text,paras,provider)


def test_leftover_chinese_is_repaired_with_one_extra_request(tmp_path):
    prov=_RepairProvider()
    w,_,events=_single_para_worker(tmp_path,prov)
    w.run()
    from novel_translator.prompting import REPAIR_SYSTEM
    assert len(prov.requests)==2 and prov.requests[1].system_prompt==REPAIR_SYSTEM
    assert '[SOURCE]\n林雨看着' in prov.requests[1].user_prompt and '林雨는 窗外의' in prov.requests[1].user_prompt
    out=rows(tmp_path)[0]['translation']
    assert '린위는 창밖의' in out and '窗' not in out


@pytest.mark.parametrize('bad',['<P000001>\n짧다.','<P000001>\n林雨는 窗外의 비를 오래 바라보았다. 아무 말도 하지 않았다.','죄송하지만 번역할 수 없습니다.'])
def test_bad_repair_keeps_the_original_translation(tmp_path,bad):
    prov=_RepairProvider(repair_answer=bad)
    w,_,_=_single_para_worker(tmp_path,prov)
    w.run()
    r=rows(tmp_path)[0]
    assert r['status']=='COMPLETED' and '林雨는 窗外의 비를' in r['translation'], 'a failed repair never loses or fails the chunk'


def test_no_repair_request_for_clean_korean_or_english_source(tmp_path):
    gem=ScriptedProvider()
    w,_,_=_single_para_worker(tmp_path,gem)
    w.run()
    assert len(gem.requests)==1, 'pure Hangul output needs no repair'


def test_prompt_v12_has_hangul_only_rules_for_each_language():
    from novel_translator.profiles import ProfileManager
    from novel_translator.prompting import build_system_prompt, build_user_prompt, PROMPT_VERSION
    assert PROMPT_VERSION=='12'
    pm=ProfileManager()
    for pid,marker in (('zh-ko','한자를 한 글자도'),('ja-ko','한자·가나를 한 글자도'),('en-ko','일반 영어 단어를')):
        sp=build_system_prompt(pm.set_active(pid),'novel')
        assert 'OUTPUT SCRIPT (KOREAN HANGUL ONLY)' in sp and marker in sp and 'STYLE EXAMPLE' in sp
        assert '<P000001>' not in sp.split('STYLE EXAMPLE')[1].split('SOURCE-LANGUAGE ROLE')[0], 'example must not carry paragraph IDs'
    up=build_user_prompt('<P000001>\n测试',{},'')
    assert up.rstrip().endswith('render names and Sino-Korean words in Hangul.')


def test_rejection_reasons_are_named_and_local_llm_is_not_capped(tmp_path):
    from novel_translator.providers.base import ProviderError as PE
    w,_,_=_single_para_worker(tmp_path,ScriptedProvider())
    assert w._kind_ko(PE(ErrorKind.REFUSAL,'청크당 요청 상한(16회) 도달 · 비용 보호를 위해 원문 유지'))=='요청 상한 도달(보내지 않음)'
    assert w._kind_ko(PE(ErrorKind.REFUSAL,'모든 문단이 제외 표식으로 반환되었습니다.'))=='범위 제외 표시'
    assert w._kind_ko(PE(ErrorKind.REFUSAL,'원문에 없는 생략/검열 표기가 번역문에 들어 있습니다'))=='생략 표기 삽입'
    assert w._kind_ko(PE(ErrorKind.REFUSAL,'문단 P000001에 원문 문장이 번역되지 않고 남아 있습니다'))=='원문 일부 미번역'
    assert w._kind_ko(PE(ErrorKind.REFUSAL,'API가 번역문 대신 거부 안내를 반환했습니다.'))=='거부'
    # Cap: paid engines stop at the limit, the free local LLM is still asked.
    w._chunk_requests=0
    for _ in range(w.max_requests_per_chunk): w._on_usage('gemini','m',None,False)
    w._on_usage('local','m',None,False)
    assert w._chunk_requests==w.max_requests_per_chunk
    class O:
        provider_id='gemini'; last_excluded=[]
        def translate_chunk(self,*a,**k): return ('<P000001>\n번역','k',0)
    from novel_translator.chunking import Chunk, Paragraph
    c=Chunk(0,[Paragraph(1,'x')],1,1)
    with pytest.raises(PE) as caught: w._call_orch(O(),c,'',False)
    assert '요청 상한' in caught.value.message
    o=O(); o.provider_id='local'
    assert w._call_orch(o,c,'',False)[0].startswith('<P000001>')


def test_sentence_left_in_chinese_is_repaired_instead_of_rejected(tmp_path):
    from novel_translator.prompting import REPAIR_SYSTEM
    class P:
        name='local'
        def __init__(self): self.requests=[]
        def translate(self,request,key):
            self.requests.append(request)
            if request.system_prompt==REPAIR_SYSTEM:
                return TranslationResponse('<P000001>\n린위는 창밖을 보았다. 그는 아주 오랫동안 아무 말도 하지 않았다.','STOP',{})
            return TranslationResponse('<P000001>\n린위는 창밖을 보았다. 他很久很久都没有说一句话那样。','STOP',{})
    prov=P()
    text='林雨看着窗外。他很久很久都没有说一句话那样。'
    from novel_translator.chunking import split_paragraphs, mark_translatable
    w,_,events=_worker_for(tmp_path,text,mark_translatable(split_paragraphs(text),'zh'),prov)
    w.run()
    r=rows(tmp_path)[0]
    assert r['status']=='COMPLETED' and '他很久' not in r['translation'] and '아무 말도' in r['translation']
    assert len(prov.requests)==2, 'one translation request + one repair, no rejection/retry cycle'



# ---------------------------------------------------------------- file size, elapsed / remaining / total time
def test_file_label_shows_size(qt_app,sandbox,monkeypatch,tmp_path):
    book=tmp_path/'book.txt'; book.write_bytes(('第一章\n\n'+'她也沉默着。'*30000).encode('utf-8'))
    monkeypatch.setattr(ui.QFileDialog,'getOpenFileName',lambda *a,**k:(str(book),''))
    w=_window(monkeypatch)
    try:
        w.open_txt()
        size=book.stat().st_size
        assert f'{size/1024:.2f} KB' in w.file_label.text() and '자' in w.file_label.text()
        assert w._size_text(500)=='500 B' and w._size_text(533043)=='520.55 KB' and w._size_text(3*1024**2)=='3.00 MB'
    finally:
        w.close()


def test_job_clock_counts_elapsed_excludes_pause_and_reports_total(qt_app,sandbox,monkeypatch):
    import time
    state={'release':False}; infos=[]
    Base=_fake_worker_class([])
    class Hold(Base):
        def pause(self): pass
        def resume(self): pass
        def run(self):
            while not state['release']: time.sleep(0.01)
            super().run()
    monkeypatch.setattr(ui,'TranslationWorker',Hold)
    w=_window(monkeypatch)
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k:infos.append(a[2]))
    try:
        w._prepare_new_input(); w._apply_profile('zh-ko'); w._set_preview_source('')
        w.preview.setPlainText('他静静地看着窗外的雨。'); w.start_translation()
        assert w._clock.isActive() and w.time_label.text().startswith('경과 0:00:0')
        w._job_t0-=100                                   # pretend 100 s have passed
        w.on_progress(1,4,'완료'); w.on_progress(2,4,'완료')
        w._tick_clock()
        assert w.time_label.text().startswith('경과 0:01:4') and '남은 예상 약' in w.time_label.text()
        w.pause_btn.click(); w._job_t0-=50; w._job_paused_at-=50; w._tick_clock()   # then 50 s spent paused
        assert '일시정지' in w.time_label.text()
        w.pause_btn.click()
        assert 99 <= w._job_elapsed() <= 103, w._job_elapsed()
        state['release']=True
        assert _wait(qt_app,lambda: not w._clock.isActive() and infos)
        assert w.time_label.text().startswith('걸린 시간 0:01:4')
        assert '걸린 시간: 1분 4' in infos[-1]
        assert '작업 끝 · 걸린 시간 1분 4' in w.error_view.toPlainText()
    finally:
        state['release']=True; w.close()


def test_duration_formats():
    assert ui.MainWindow._human_duration(5)=='5초' and ui.MainWindow._human_duration(125)=='2분 5초'
    assert ui.MainWindow._human_duration(3725)=='1시간 2분 5초' and ui.MainWindow._clock_text(3725)=='1:02:05'



# ---------------------------------------------------------------- local LLM: streaming, idle timeout, stable context
def test_ollama_stream_is_assembled_and_idle_timeout_is_per_piece(monkeypatch):
    import json as _json
    from novel_translator.providers import local_llm as ll
    seen={}
    class S:
        status_code=200
        def __init__(self, lines): self.lines=lines
        def iter_lines(self):
            for x in self.lines:
                if isinstance(x,Exception): raise x
                yield x
        def close(self): seen['closed']=True
    lines=[_json.dumps({'message':{'content':'<P000001>\n'},'done':False}).encode(), b'',
           _json.dumps({'message':{'content':'린위가 '},'done':False}).encode(),
           _json.dumps({'message':{'content':'웃었다.'},'done':False}).encode(),
           _json.dumps({'message':{'content':''},'done':True,'done_reason':'stop','prompt_eval_count':900,'eval_count':12,'eval_duration':2_000_000_000}).encode()]
    def post(url,headers=None,json=None,timeout=None,stream=False):
        seen.update(stream=stream,timeout=timeout); return S(lines)
    monkeypatch.setattr(ll.requests,'post',post)
    p=ll.LocalLLMProvider(timeout=300)
    r=p.translate(TranslationRequest('m','s','u'),'local')
    assert r.text=='<P000001>\n린위가 웃었다.' and p.last_usage['input']==900 and p.last_speed==6.0
    assert seen['stream'] is True and seen['timeout']==(15,300) and seen['closed']
    # Silence longer than the limit in the middle of the answer -> clear TRANSIENT message.
    lines[:]=[_json.dumps({'message':{'content':'<P000001>\n'},'done':False}).encode(), ll.requests.exceptions.ReadTimeout('idle')]
    with pytest.raises(ProviderError) as caught: p.translate(TranslationRequest('m','s','u'),'local')
    assert caught.value.kind==ErrorKind.TRANSIENT and '300초 동안 한 글자도' in caught.value.message and 'ollama ps' in caught.value.message
    # An error object inside the stream is reported like an HTTP error.
    lines[:]=[_json.dumps({'error':"model 'x' not found"}).encode()]
    with pytest.raises(ProviderError) as caught: p.translate(TranslationRequest('x','s','u'),'local')
    assert caught.value.kind==ErrorKind.FATAL


def test_ollama_context_length_only_grows():
    from novel_translator.providers.local_llm import LocalLLMProvider as L
    p=L()
    small=TranslationRequest('m','s'*12000,'中'*3000)
    big=TranslationRequest('m','s'*12000,'中'*3000+'메'*8000)
    assert p.context_for(small)==12288
    grown=p.context_for(big); assert grown>12288
    assert p.context_for(small)==grown, 'never shrinks back (each change reloads the model in Ollama)'


# ---------------------------------------------------------------- Windows dark mode
def test_light_theme_is_forced_over_a_dark_system_palette(qt_app,sandbox,monkeypatch):
    import os, sys
    from PySide6.QtGui import QPalette, QColor
    from novel_translator import theme
    original=qt_app.palette()
    dark=QPalette()
    for role,col in ((QPalette.Window,'#202020'),(QPalette.WindowText,'#ffffff'),(QPalette.Base,'#2b2b2b'),(QPalette.Text,'#ffffff'),(QPalette.Button,'#2d2d2d'),(QPalette.ButtonText,'#ffffff')):
        dark.setColor(role,QColor(col))
    try:
        qt_app.setPalette(dark)
        theme.force_light(qt_app)
        pal=qt_app.palette()
        assert pal.color(QPalette.Base).lightness()>240 and pal.color(QPalette.Text).lightness()<60
        assert pal.color(QPalette.Window).lightness()>230 and pal.color(QPalette.ButtonText).lightness()<60
        w=_window(monkeypatch)
        try:
            assert w.preview.palette().color(QPalette.Base).lightness()>240, 'text boxes are white, not black'
            assert w.status.palette().color(QPalette.WindowText).lightness()<60, 'labels are dark text, not white'
        finally:
            w.close()
    finally:
        qt_app.setPalette(original)
    # Before QApplication: the Windows platform plugin is told not to follow dark mode.
    monkeypatch.delenv('QT_QPA_PLATFORM',raising=False)
    monkeypatch.setattr(sys,'platform','win32')
    theme.prepare_environment()
    assert os.environ['QT_QPA_PLATFORM']=='windows:darkmode=0'
    monkeypatch.setenv('QT_QPA_PLATFORM','offscreen'); theme.prepare_environment()
    assert os.environ['QT_QPA_PLATFORM']=='offscreen', 'an explicit platform choice is respected'
    src=(Path(theme.__file__).parent/'app.py').read_text(encoding='utf-8')
    assert src.count('force_light(app)')>=2 and 'prepare_environment()' in src


# ---------------------------------------------------------------- leaked model reasoning (</think>, "The user provided…")
_LEAK='<P000001>\n야야야야야\n</think>\n\nOkay, let\'s see. The user provided a very long string of the letter \'y\'.\n$$\\boxed{1}$$'


def test_stray_think_prefix_is_stripped():
    from novel_translator.validation import strip_reasoning, normalize_translation_output
    assert strip_reasoning('Okay, so the user wants…\n</think>\n\n<P000001>\n린위가 웃었다.').strip()=='<P000001>\n린위가 웃었다.'
    from novel_translator.validation import parse_tagged_translation as parse
    assert parse(normalize_translation_output('<think>hmm</think>\n<P000001>\n린위가 웃었다.'))=={1:'린위가 웃었다.'}
    assert parse(normalize_translation_output('생각\n</thinl>\n<P000001>\n린위가 웃었다.'))=={1:'린위가 웃었다.'}


@pytest.mark.parametrize('body',['린위가 웃었다.\n</think>','린위가 웃었다. $$\\boxed{1}$$','The user provided a long text. 린위가 웃었다.',
                                 "Okay, let's see. 린위가 웃었다.",'린위가 웃었다.</thinl>'])
def test_reasoning_inside_a_paragraph_is_rejected(body):
    from novel_translator.validation import validate_translation
    from novel_translator.providers.base import ProviderError, ErrorKind
    with pytest.raises(ProviderError) as e:
        validate_translation('<P000001>\n林雨笑了。', '<P000001>\n'+body)
    assert e.value.kind==ErrorKind.FORMAT and '생각' in str(e.value)


def test_phrase_present_in_the_source_is_allowed():
    from novel_translator.validation import meta_leak, validate_translation
    assert meta_leak('He did it step by step.','그는 step by step 해냈다')==''
    validate_translation('<P000001>\nThe user provided the final answer step by step.','<P000001>\n사용자는 the user provided 의 최종 답을 단계별로 제시했다.')


def test_leaked_answer_is_retried_and_the_result_is_clean(tmp_path):
    calls=[]
    def rule(prompt,src,body):
        calls.append(1)
        return _LEAK.split('\n',1)[1] if len(calls)==1 else '린위는 창밖의 비를 오래 바라보았다.'
    prov=ScriptedProvider(rule)
    w,_,_=_single_para_worker(tmp_path,prov); _no_repair(); w.run()
    r=rows(tmp_path)[0]
    assert r['status']=='COMPLETED' and len(prov.requests)>=2
    assert 'think' not in r['translation'] and 'user provided' not in r['translation'] and '린위는 창밖의' in r['translation']


def test_old_leaky_result_is_translated_again(tmp_path):
    first=ScriptedProvider(lambda p,s,b: '린위가 웃었다.')
    w,_,_=_single_para_worker(tmp_path,first); _no_repair(); w.run()
    import sqlite3
    dbfile=tmp_path/'data'/'translations.db'
    con=sqlite3.connect(dbfile); con.execute("UPDATE chunks SET translation=?", (_LEAK,)); con.commit(); con.close()
    second=ScriptedProvider(lambda p,s,b: '린위는 창밖의 비를 오래 바라보았다.')
    w,_,events=_single_para_worker(tmp_path,second); _no_repair(); w.run()
    assert len(second.requests)==1
    assert any('생각·설명이 섞인' in e for e in events)
    out=(tmp_path/'out'/'novel.ko.txt').read_text(encoding='utf-8-sig')
    assert 'think' not in out and 'boxed' not in out and '린위는 창밖의' in out


def test_repair_answer_with_reasoning_is_not_used(tmp_path):
    prov=_RepairProvider(repair_answer='<P000001>\n린위는 창밖의 비를 오래 바라보았다. 아무 말도 하지 않았다.\n</think> The user provided text.')
    w,_,_=_single_para_worker(tmp_path,prov); w.run()
    r=rows(tmp_path)[0]
    assert r['status']=='COMPLETED' and 'think' not in r['translation'] and '林雨는 窗外의' in r['translation']


# ---------------------------------------------------------------- auto extraction never leaves the glossary silently empty
def _resume_window(qt_app,sandbox,monkeypatch,tmp_path,answer):
    root,_=sandbox
    started=[]; monkeypatch.setattr(ui,'TranslationWorker',_fake_worker_class(started))
    calls=[]
    class FakeThread(ui.QThread):
        done=ui.Signal(bool,object)
        def __init__(self,*a,**k): super().__init__(k.get('parent')); calls.append(1)
        def run(self): self.done.emit(True,{'林雨':'린위'})
    monkeypatch.setattr(ui,'TermExtractThread',FakeThread)
    book=tmp_path/'book.txt'; raw='林雨说。林雨笑了。林雨走了。林雨来了。林雨看着。林雨想着。林雨听着。'.encode('utf-8'); book.write_bytes(raw)
    import hashlib
    from novel_translator.storage import Database
    db=Database(root/'data'/'translations.db'); pid=db.get_or_create_project(str(book),'zh','zh-ko',hashlib.sha256(raw).hexdigest(),'fp')
    db.upsert_chunk(pid,0,'x'); db.set_chunk_status(pid,0,'COMPLETED',translation='<P000001>\n번역')
    monkeypatch.setattr(ui.QFileDialog,'getOpenFileName',lambda *a,**k:(str(book),''))
    w=_window(monkeypatch)
    asked=[]
    monkeypatch.setattr(w,'_ask_extract_on_resume',lambda: asked.append(1) or answer)
    w.auto_terms.setChecked(True); w.open_txt(); w.term_table.setRowCount(0)
    return w,started,calls,asked


def test_resume_with_empty_glossary_asks_and_extracts_then_restarts(qt_app,sandbox,monkeypatch,tmp_path):
    w,started,calls,asked=_resume_window(qt_app,sandbox,monkeypatch,tmp_path,'extract')
    try:
        w.start_translation()
        assert _wait(qt_app,lambda: started and not started[0].isRunning() and w._result_text)
        assert asked and calls, 'empty glossary on a resumed file: the user is asked, then terms are extracted'
        assert w._terms()=={'林雨':'린위'} and started[0].restart_existing, 'restarts from the beginning with the new glossary'
        assert '확정 용어집에 추가' in w.error_view.toPlainText()
    finally:
        w.close()


def test_resume_with_empty_glossary_can_continue_without_terms(qt_app,sandbox,monkeypatch,tmp_path):
    w,started,calls,asked=_resume_window(qt_app,sandbox,monkeypatch,tmp_path,'resume')
    try:
        w.start_translation()
        assert _wait(qt_app,lambda: started and not started[0].isRunning() and w._result_text)
        assert asked and not calls and not started[0].restart_existing
        assert '용어집 없이 이어서' in w.error_view.toPlainText(), 'the skip is written in the log'
    finally:
        w.close()


def test_resume_cancel_starts_nothing(qt_app,sandbox,monkeypatch,tmp_path):
    w,started,calls,asked=_resume_window(qt_app,sandbox,monkeypatch,tmp_path,'cancel')
    try:
        w.start_translation(); qt_app.processEvents()
        assert asked and not calls and not started
    finally:
        w.close()


def test_resume_with_a_glossary_keeps_it_and_says_so(qt_app,sandbox,monkeypatch,tmp_path):
    w,started,calls,asked=_resume_window(qt_app,sandbox,monkeypatch,tmp_path,'extract')
    try:
        w.term_table.insertRow(0); w.term_table.setItem(0,0,QTableWidgetItem('林雨')); w.term_table.setItem(0,1,QTableWidgetItem('임우'))
        w.start_translation()
        assert _wait(qt_app,lambda: started and not started[0].isRunning() and w._result_text)
        assert not asked and not calls
        assert '현재 확정 용어집 1개를 그대로' in w.error_view.toPlainText()
    finally:
        w.close()


def test_already_extracted_but_emptied_glossary_is_extracted_again(qt_app,sandbox,monkeypatch,tmp_path):
    w,started,calls,asked=_resume_window(qt_app,sandbox,monkeypatch,tmp_path,'extract')
    try:
        w._force_restart=True; w._auto_terms_sha=w._source_sha()
        assert w._auto_terms_plan()[0]=='extract', 'same text, but the glossary was cleared: extract again'
        w.term_table.insertRow(0); w.term_table.setItem(0,0,QTableWidgetItem('林雨')); w.term_table.setItem(0,1,QTableWidgetItem('린위'))
        plan,note=w._auto_terms_plan()
        assert plan=='skip' and '이미 추출함' in note
    finally:
        w.close()


# ---------------------------------------------------------------- local LLM: endless repetition loop
def _stream_provider(monkeypatch, pieces, sent=None):
    import json as _json
    from novel_translator.providers import local_llm as ll
    state={'yielded':0}
    class S:
        status_code=200
        def iter_lines(self):
            for x in pieces:
                state['yielded']+=1
                yield _json.dumps({'message':{'content':x},'done':False}).encode()
            yield _json.dumps({'message':{'content':''},'done':True,'done_reason':'stop'}).encode()
        def close(self): state['closed']=True
    def post(url,headers=None,json=None,timeout=None,stream=False):
        if sent is not None: sent.append(json)
        return S()
    monkeypatch.setattr(ll.requests,'post',post)
    return ll.LocalLLMProvider(timeout=300), state


def test_endless_repetition_is_stopped_early(monkeypatch):
    import itertools
    endless=itertools.chain(['<P000001>\n'], itertools.repeat('야', 100000))
    p,state=_stream_provider(monkeypatch, endless)
    src='<P000001>\n呀，林雨笑了。'
    with pytest.raises(ProviderError) as e:
        p.translate(TranslationRequest('m','s','[SOURCE - quoted]\n'+src),'local')
    assert e.value.kind==ErrorKind.FORMAT and '반복' in e.value.message
    assert state['yielded']<1000 and state.get('closed'), 'stopped within a few hundred pieces, connection closed'


def test_phrase_loop_is_stopped_and_normal_text_is_not(monkeypatch):
    import itertools
    p,_=_stream_provider(monkeypatch, itertools.chain(['<P000001>\n'], itertools.repeat('그는 웃었다. ', 5000)))
    with pytest.raises(ProviderError): p.translate(TranslationRequest('m','s','[SOURCE - q]\n<P000001>\n他笑了。'),'local')
    # A separator the source itself has, and ordinary long text, are fine.
    sep='-'*300
    p,_=_stream_provider(monkeypatch, ['<P000001>\n', *(['-']*300), '\n\n<P000002>\n', *['린위가 창밖을 보았다. 비가 왔다. 그녀는 웃었다. ']*20])
    p.structured=False   # this checks loop detection on tagged text (structured JSON output is tested separately)
    r=p.translate(TranslationRequest('m','s','[SOURCE - q]\n<P000001>\n'+sep+'\n\n<P000002>\n林雨看着窗外。'*1),'local')
    assert r.text.count('-')==300


def test_output_budget_is_sent_and_scales_with_the_source(monkeypatch):
    sent=[]
    p,_=_stream_provider(monkeypatch, ['<P000001>\n린위가 웃었다.'], sent)
    p.translate(TranslationRequest('m','s','[BOOK MEMORY]\n'+'메'*5000+'\n[SOURCE - q]\n<P000001>\n林雨笑了。'),'local')
    assert sent[0]['options']['num_predict']==1024, 'budget from the source part only, never from the memo/context'
    p.translate(TranslationRequest('m','s','[SOURCE - q]\n'+'中'*2000),'local')
    assert sent[1]['options']['num_predict']>=6000


def test_loop_gets_one_varied_retry_inside_the_provider(monkeypatch):
    import itertools, json as _json
    from novel_translator.providers import local_llm as ll
    sent=[]
    class S:
        status_code=200
        def __init__(self, pieces): self.pieces=pieces
        def iter_lines(self):
            for x in self.pieces: yield _json.dumps({'message':{'content':x},'done':False}).encode()
            yield _json.dumps({'message':{'content':''},'done':True,'done_reason':'stop'}).encode()
        def close(self): pass
    answers=[itertools.chain(['<P000001>\n'], itertools.repeat('야',5000)), ['<P000001>\n린위가 웃었다.']]
    def post(url,headers=None,json=None,timeout=None,stream=False):
        sent.append(json); return S(answers[len(sent)-1])
    monkeypatch.setattr(ll.requests,'post',post)
    p=ll.LocalLLMProvider()
    r=p.translate(TranslationRequest('m','s','[SOURCE - q]\n<P000001>\n林雨笑了。'),'local')
    assert r.text=='<P000001>\n린위가 웃었다.' and len(sent)==2 and p.loops==1
    assert 'repeat_penalty' not in sent[0]['options'] and sent[1]['options']['repeat_penalty']>1.1, 'the retry uses anti-repetition sampling'
    # Loops again: exactly two requests, then a marked loop error.
    sent.clear(); answers[:]=[itertools.repeat('야',5000), itertools.repeat('야',5000)]
    from novel_translator.providers.base import is_loop_error
    with pytest.raises(ProviderError) as e: p.translate(TranslationRequest('m','s','[SOURCE - q]\n<P000001>\n林雨笑了。'),'local')
    assert len(sent)==2 and is_loop_error(e.value)


def test_a_passage_that_always_loops_is_bounded_and_kept_as_source(tmp_path):
    from novel_translator.providers.base import LOOP_MARK
    text='\n\n'.join(['　一つ目の段落です。長い文がここにあります。さらに続きます。','　LOOPS の段落です。ここでも文が続きます。まだ続きます。','　三つ目の段落です。'])
    loc=ScriptedProvider(lambda p,s,b: (_ for _ in ()).throw(ProviderError(ErrorKind.FORMAT,f'로컬 LLM {LOOP_MARK} · 반복')) if 'LOOPS' in b else '로컬번역 '+b[:6])
    w,_,events=_engine_worker(tmp_path,text,ScriptedProvider(),local=loc,order=['local'],limits=(500,1,1000)); _no_repair()
    w.run()
    looping=[r for r in loc.requests if 'LOOPS' in r.user_prompt.split('[SOURCE',1)[1]]
    assert 1<=len(looping)<=w.MAX_LOOPS_PER_CHUNK, f'never asked again and again ({len(looping)} requests)'
    r=rows(tmp_path)[0]
    assert r['status']=='PARTIAL' and '로컬번역' in r['translation'] and 'LOOPS' in r['translation'], 'other paragraphs kept, looping one stays source'
    assert any('같은 말을 반복해 끊음' in e for e in events) and any('반복 루프' in e for e in events)


def test_loop_moves_to_the_next_engine(tmp_path):
    from novel_translator.providers.base import LOOP_MARK
    text='\n\n'.join(['　一つ目の段落です。','　LOOPS の段落です。'])
    loc=ScriptedProvider(lambda p,s,b: (_ for _ in ()).throw(ProviderError(ErrorKind.FORMAT,f'로컬 LLM {LOOP_MARK}')) if 'LOOPS' in b else '로컬번역 '+b[:6])
    oai=ScriptedProvider(lambda p,s,b: '오픈번역 '+b[:6])
    w,_,_=_engine_worker(tmp_path,text,ScriptedProvider(),openai=oai,local=loc,order=['local','openai'],limits=(500,1,1000)); _no_repair()
    w.run()
    r=rows(tmp_path)[0]
    assert r['status']=='COMPLETED' and '오픈번역' in r['translation']


# ---------------------------------------------------------------- cancellable glossary/speech analysis
def test_analysis_cancel_controls_remain_clickable(qt_app,sandbox,monkeypatch):
    class FakeTerm:
        def __init__(self): self.cancelled=False
        def isRunning(self): return True
        def requestInterruption(self): self.cancelled=True
        def isInterruptionRequested(self): return self.cancelled
    class FakeSpeech(FakeTerm): pass
    monkeypatch.setattr(ui,'TermExtractThread',FakeTerm)
    monkeypatch.setattr(ui,'SpeechExtractThread',FakeSpeech)
    w=_window(monkeypatch)
    try:
        term=FakeTerm()
        w._term_thread=term
        w._lock_inputs(True)
        assert w.term_extract.isEnabled(), 'term cancel remains available while locked'
        w._extract_terms()
        assert term.cancelled and not w.term_extract.isEnabled()
        assert '취소 요청' in w.status.text()

        speech=FakeSpeech()
        w._term_thread=speech
        w._lock_inputs(True)
        assert w.speech_generate.isEnabled(), 'speech cancel remains available while locked'
        w._generate_speech()
        assert speech.cancelled and not w.speech_generate.isEnabled()
        assert '취소 요청' in w.status.text()
    finally:
        w._term_thread=None
        w._lock_inputs(False)
        w.close()


def test_manual_term_cancel_does_not_apply_results(qt_app,sandbox,monkeypatch):
    w=_window(monkeypatch)
    try:
        w._term_thread=None
        w._term_result=(False,'사용자 취소')
        w._term_auto=False
        w._term_replace=True
        before=w._terms()
        w._extract_terms_finished()
        assert w._terms()==before
        assert not w._term_replace
        assert '취소됨' in w.status.text()
    finally:
        w.close()


def test_term_worker_cancel_after_provider_error_skips_fallback(qt_app,monkeypatch):
    names=[]
    thread=ui.TermExtractThread('gemini','fake','m','', 'system','user',[],engines=[
        ('gemini','fake','m','',{}),('openai','fake','m','',{})])
    class Failing:
        def translate(self,*args):
            names.append('called')
            thread.requestInterruption()
            raise RuntimeError('simulated timeout')
    monkeypatch.setattr(ui,'_make_provider',lambda *args: Failing())
    result=[]
    thread.done.connect(lambda ok,message:result.append((ok,message)))
    thread.run()
    assert names==['called']
    assert result==[(False,'사용자 취소')]


def test_speech_worker_cancel_after_provider_error_skips_fallback(qt_app,monkeypatch):
    names=[]
    thread=ui.SpeechExtractThread(
        [('gemini','fake','m','',{}),('openai','fake','m','',{})],
        [('system','dialogue'),('system2','dialogue2')],
        lambda text: [])
    class Failing:
        def translate(self,*args):
            names.append('called')
            thread.requestInterruption()
            raise RuntimeError('simulated timeout')
    monkeypatch.setattr(ui,'_make_provider',lambda *args: Failing())
    result=[]
    thread.done.connect(lambda ok,message:result.append((ok,message)))
    thread.run()
    assert names==['called']
    assert result==[(False,'사용자 취소')]


def test_speech_full_context_success_uses_only_one_request(qt_app,monkeypatch):
    from novel_translator.providers.base import TranslationResponse
    calls=[]
    class Provider:
        def translate(self,request,secret):
            calls.append(request.user_prompt)
            return TranslationResponse('린위\t쑤칭\t반말\t친구')
    monkeypatch.setattr(ui,'_make_provider',lambda *args:Provider())
    from novel_translator.speech_levels import parse_table
    names={'林雨':'린위','苏晴':'쑤칭'}
    t=ui.SpeechExtractThread([('gemini','dummy','test','',{})],
          [('system','ALL_CONTEXT')], lambda text:parse_table(text,names),
          fallback_prompts=[('system','PART1'),('system','PART2'),('system','PART3')])
    result=[]
    t.done.connect(lambda ok,data:result.append((ok,data)))
    t.run()
    assert calls==['ALL_CONTEXT']
    assert result[0][0] and result[0][1]['parts']==1
    assert result[0][1]['requests']==1


def test_speech_fallback_only_after_failed_whole_and_excludes_conflicting_levels(qt_app,monkeypatch):
    from novel_translator.providers.base import TranslationResponse
    calls=[]
    replies={'ALL':'', 'P1':'린위\t쑤칭\t반말\t친구',
             'P2':'린위\t쑤칭\t해요체\t친구', 'P3':'쑤칭\t린위\t반말\t친구'}
    class Provider:
        def translate(self,req,secret):
            calls.append(req.user_prompt)
            return TranslationResponse(replies[req.user_prompt])
    monkeypatch.setattr(ui,'_make_provider',lambda *args:Provider())
    from novel_translator.speech_levels import parse_table
    names={'林雨':'린위','苏晴':'쑤칭'}
    t=ui.SpeechExtractThread([('gemini','dummy','test','',{})],
         [('system','ALL')],lambda text:parse_table(text,names),
         fallback_prompts=[('system','P1'),('system','P2'),('system','P3')])
    result=[]
    t.done.connect(lambda ok,data:result.append((ok,data)))
    t.run()
    assert calls==['ALL','P1','P2','P3']
    assert result[0][0]
    assert result[0][1]['conflicts']==1
    assert [(x['speaker'],x['listener']) for x in result[0][1]['rows']]==[('쑤칭','린위')]


def test_refinement_launch_and_main_menu_hand_cursors(qt_app,sandbox,monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QComboBox,QTabBar,QAbstractButton
    w=_window(monkeypatch)
    try:
        w._update_ready_state()
        assert w.refinement_run_btn.isEnabled(), 'no selected foreign-language source is needed'
        assert w.refinement_run_btn.cursor().shape()==Qt.PointingHandCursor
        assert w.api_btn.cursor().shape()==Qt.PointingHandCursor
        assert w.manual_btn.cursor().shape()==Qt.PointingHandCursor
        assert w.tabs.tabBar().cursor().shape()==Qt.PointingHandCursor
        for child in w.findChildren(QAbstractButton):
            assert child.cursor().shape()==Qt.PointingHandCursor
        for child in w.findChildren(QComboBox):
            assert child.cursor().shape()==Qt.PointingHandCursor
        for child in w.findChildren(QTabBar):
            assert child.cursor().shape()==Qt.PointingHandCursor
    finally:
        w.close()


def test_refinement_dialog_initializes_without_keys_or_translation_input(qt_app,sandbox,monkeypatch):
    from novel_translator.ui.refinement_run_dialog import RefinementRunDialog
    w=_window(monkeypatch)
    try:
        w.secrets={'gemini':{'keys':[]},'openai':{'enabled':False,'keys':[]},
                   'local':{'enabled':False},'engines':[]}
        dlg=RefinementRunDialog(w)
        assert not dlg._routes_ready
        assert not hasattr(dlg, 'engines')
        assert not dlg.start.isEnabled()
        assert dlg.pick.isEnabled()
        dlg.close()
        # A prepared online provider cannot enable local-only refinement.
        w.secrets={'gemini':{'keys':[{'secret':'test-gemini-key-for-ui','enabled':True}]},
                   'openai':{'enabled':False,'keys':[]},'local':{'enabled':False},
                   'engines':[{'id':'gemini','enabled':True}],
                   'refinement_editing':{'provider':'gemini','model':'gemini-3.1-flash-lite'},
                   'extraction_engines':{'terms':'gemini','speech':'gemini','model':'gemini-3.1-flash-lite'}}
        monkeypatch.setattr(w,'_selected_model_id',lambda:'gemini-3.1-flash-lite')
        dlg=RefinementRunDialog(w)
        assert not dlg._routes_ready and not dlg.start.isEnabled()
        assert '로컬 LLM' in dlg._route_error
        dlg.close()
    finally:
        w.close()
