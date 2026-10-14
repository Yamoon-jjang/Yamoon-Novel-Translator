"""Alpha16: engine order, local LLM backup, usage/cost, request cap, model-independent
resume, model-aware thinking config, glossary extraction."""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from novel_translator.providers.base import TranslationRequest, TranslationResponse, ProviderError, ErrorKind
from novel_translator.storage import Database
import novel_translator.ui.worker as worker_mod
from test_refusal_recovery import ScriptedProvider, make_worker, rows, _source, TAG_BLOCK


class _Resp:
    def __init__(self, data, status=200):
        self._data=data; self.status_code=status; self.headers={}; self.text=json.dumps(data)
    def json(self): return self._data
    def close(self): pass
    def iter_lines(self):
        # Ollama streaming: content pieces, then a final object with done/counts.
        d=dict(self._data); msg=(d.pop('message',None) or {}); content=str(msg.get('content') or '')
        half=len(content)//2
        for piece in (content[:half],content[half:]):
            if piece: yield json.dumps({'message':{'role':'assistant','content':piece},'done':False}).encode()
        yield json.dumps(dict(d,done=True,message={'role':'assistant','content':''})).encode()


def _policy_rule(word='COREPOLICY'):
    def rule(prompt,src,body):
        if word in src:
            raise ProviderError(ErrorKind.POLICY,'입력 차단: PROHIBITED_CONTENT')
    return rule


def _engine_worker(tmp_path, text, gemini, *, openai=None, local=None, order=None, limits=(500,1,1000), refusal=None):
    if local is not None:
        worker_mod.LocalLLMProvider=lambda *_a,**_k: local
    w,chunks,events=make_worker(tmp_path,text,gemini,backup=openai,chunk_limits=limits,refusal=refusal)
    if local is not None:
        w.local_settings={'enabled':True,'model':'local-model','endpoint':'http://localhost:11434/v1/chat/completions'}
    if order is not None:
        w._engine_order_setting=list(order)
    return w,chunks,events


# ---------------------------------------------------------------- engine order
def test_local_llm_is_tried_before_openai_by_default(tmp_path):
    text='\n\n'.join(['　前の段落です。','　COREPOLICY を含む段落です。','　後の段落です。'])
    gem=ScriptedProvider(_policy_rule()); oai=ScriptedProvider(); loc=ScriptedProvider()
    w,_,_=_engine_worker(tmp_path,text,gem,openai=oai,local=loc)
    assert w.engine_order()==['gemini','local','openai']
    w.run()
    assert (tmp_path/'out'/'novel.ko.txt').exists()
    assert loc.requests and not oai.requests, 'local backup should handle the blocked paragraph before paid OpenAI'


def test_openai_first_order_is_respected(tmp_path):
    text='\n\n'.join(['　前の段落です。','　COREPOLICY を含む段落です。'])
    gem=ScriptedProvider(_policy_rule()); oai=ScriptedProvider(); loc=ScriptedProvider()
    w,_,_=_engine_worker(tmp_path,text,gem,openai=oai,local=loc,order=['gemini','openai','local'])
    w.run()
    assert oai.requests and not loc.requests


def test_local_llm_can_be_the_only_engine(tmp_path):
    text='\n\n'.join(f'　第{i}段落です。' for i in range(6))
    gem=ScriptedProvider(); loc=ScriptedProvider()
    w,_,events=_engine_worker(tmp_path,text,gem,local=loc,order=['local'])
    w.run()
    assert (tmp_path/'out'/'novel.ko.txt').exists()
    assert loc.requests and not gem.requests
    assert all(r['status']=='COMPLETED' for r in rows(tmp_path))


def test_local_server_down_never_pauses_the_job(tmp_path):
    class Down:
        name='local'; last_usage=None
        def translate(self,request,key):
            raise ProviderError(ErrorKind.TRANSIENT,'로컬 LLM 서버에 연결할 수 없습니다.')
    blocks=['　COREPOLICY その%d です。' % i for i in range(5)]
    text='\n\n'.join(blocks)
    gem=ScriptedProvider(_policy_rule()); w,_,events=_engine_worker(tmp_path,text,gem,local=Down(),limits=(30,1,60))
    paused=[]; w.paused.connect(lambda m,u: paused.append(m))
    w.run()
    assert not paused, 'a missing local server must not trip the infrastructure circuit breaker'
    assert (tmp_path/'out'/'novel.ko.partial.txt').exists()
    assert any('로컬 LLM' in e for e in events)


def test_provider_outage_moves_to_next_engine_and_returns(tmp_path):
    state={'down':True}
    class Flaky(ScriptedProvider):
        def translate(self,request,key):
            if state['down']:
                self.requests.append(request)
                # Project access denied: unavailable now, but no cooldown, so it can be re-probed.
                raise ProviderError(ErrorKind.FATAL,'permission denied for project',403)
            return super().translate(request,key)
    gem=Flaky(); oai=ScriptedProvider()
    text='\n\n'.join(f'　段落{i}です。'+'あ'*30 for i in range(30))
    w,chunks,events=_engine_worker(tmp_path,text,gem,openai=oai,limits=(30,1,60),
                                   refusal={'reframe_once':True,'split_chunk':True,'fallback_provider':True,'fallback_on_policy':True,'gemini_probe_every_chunks':3})
    real=w._translate_with_engines
    calls={'n':0}
    def counting(engines,chunk,prev):
        calls['n']+=1
        if calls['n']==4: state['down']=False     # Gemini recovers
        return real(engines,chunk,prev)
    w._translate_with_engines=counting
    w.run()
    assert (tmp_path/'out'/'novel.ko.txt').exists()
    assert any('사용 불가' in e and 'OpenAI' in e for e in events)
    assert any('정상 응답 확인' in e for e in events), events
    assert w.provider_chunks.get('gemini',0)>0 and w.provider_chunks.get('openai',0)>0


# ---------------------------------------------------------------- cost / cap
def test_usage_and_cost_summary_counts_failed_calls(tmp_path):
    class Metered(ScriptedProvider):
        def translate(self,request,key):
            self.last_usage={'input':1000,'output':500,'thinking':0}
            return super().translate(request,key)
    def rule(prompt,src,body):
        if 'REFUSEME' in src and '[RECOVERY NOTE]' not in prompt:
            raise ProviderError(ErrorKind.REFUSAL,'refused')
    p=Metered(rule)
    w,_,_=_engine_worker(tmp_path,'\n\n'.join(['　REFUSEME です。','　普通です。']),p)
    w.model='gemini-3.1-flash-lite'
    w.run()
    row=w.usage_totals['gemini']
    assert row['requests']>=2 and row['failed']>=1
    summary=w.usage_summary()
    assert '추정 비용' in summary and '$' in summary and '실패' not in summary


def test_request_cap_per_chunk_stops_runaway_retries(tmp_path):
    def rule(prompt,src,body):
        raise ProviderError(ErrorKind.REFUSAL,'always refused')
    p=ScriptedProvider(rule)
    text='　'+'。'.join(f'文{i}' for i in range(200))+'。'
    w,_,_=_engine_worker(tmp_path,text,p,limits=(5000,1,9000),
                         refusal={'reframe_once':True,'split_chunk':True,'fallback_provider':True,'max_requests_per_chunk':6})
    w.run()
    assert len(p.requests)<=6+3, f'cap ignored: {len(p.requests)} requests'


# ---------------------------------------------------------------- resume with another model
def test_switching_model_resumes_instead_of_restarting(tmp_path):
    text='\n\n'.join(['　前の段落です。','　COREPOLICY を含む段落です。']+[f'　段落{i}です。' for i in range(8)])
    w,_,_=_engine_worker(tmp_path,text,ScriptedProvider(_policy_rule()),limits=(20,1,40)); w.run()
    first=rows(tmp_path); done=sum(r['status']=='COMPLETED' for r in first)
    assert done>=2 and any(r['status']!='COMPLETED' for r in first)
    retry=ScriptedProvider()
    w2,_,_=_engine_worker(tmp_path,text,retry,limits=(20,1,40)); w2.model='gemini-3.8-flash'; w2.run()
    assert len(retry.requests)==len(first)-done, 'completed chunks were re-translated after a model switch'
    assert (tmp_path/'out'/'novel.ko.txt').exists()


# ---------------------------------------------------------------- providers
def test_gemini_thinking_is_sent_only_where_supported(monkeypatch):
    from novel_translator.providers import gemini as gm
    sent=[]
    ok={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'<P000001>\n번역'}]}}],'usageMetadata':{'promptTokenCount':10,'candidatesTokenCount':5,'thoughtsTokenCount':2}}
    monkeypatch.setattr(gm.requests,'post',lambda url,headers,json,timeout:(sent.append(json),_Resp(ok))[1])
    g=gm.GeminiProvider()
    g.translate(TranslationRequest('gemini-3.1-flash-lite','s','u',thinking_level='medium'),'k')
    assert 'generationConfig' not in sent[-1]
    g.translate(TranslationRequest('gemini-3.8-flash','s','u',thinking_level='medium'),'k')
    assert sent[-1]['generationConfig']['thinkingConfig']['thinkingLevel']=='medium'
    assert g.last_usage=={'input':10,'output':7,'thinking':2}


def test_gemini_retries_without_thinking_on_400(monkeypatch):
    from novel_translator.providers import gemini as gm
    sent=[]
    ok={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'<P000001>\n번역'}]}}]}
    bad={'error':{'message':'Unknown name "thinkingLevel": thinking is not supported for this model'}}
    def post(url,headers,json,timeout):
        sent.append(json)
        return _Resp(bad,400) if 'generationConfig' in json else _Resp(ok)
    monkeypatch.setattr(gm.requests,'post',post)
    g=gm.GeminiProvider()
    r=g.translate(TranslationRequest('gemini-3.9-experimental','s','u',thinking_level='medium'),'k')
    assert r.text.endswith('번역') and len(sent)==2
    g.translate(TranslationRequest('gemini-3.9-experimental','s','u',thinking_level='medium'),'k')
    assert 'generationConfig' not in sent[-1], 'rejected thinking setting must be remembered'


def test_local_provider_strips_reasoning_and_maps_errors(monkeypatch):
    from novel_translator.providers import local_llm as ll
    data={'choices':[{'finish_reason':'stop','message':{'content':'<think>고민 중...</think>\n<P000001>\n번역문'}}],'usage':{'prompt_tokens':9,'completion_tokens':4}}
    monkeypatch.setattr(ll.requests,'post',lambda *a,**k:_Resp(data))
    p=ll.LocalLLMProvider(ll.LocalLLMProvider.LMSTUDIO_ENDPOINT)
    assert p.native_url is None
    r=p.translate(TranslationRequest('m','s','u'),'local')
    assert r.text=='<P000001>\n번역문' and p.last_usage['input']==9
    def refused(*a,**k): raise ll.requests.ConnectionError('refused')
    monkeypatch.setattr(ll.requests,'post',refused)
    with pytest.raises(ProviderError) as caught: p.translate(TranslationRequest('m','s','u'),'local')
    assert caught.value.kind==ErrorKind.TRANSIENT and 'Ollama' in caught.value.message
    monkeypatch.setattr(ll.requests,'post',lambda *a,**k:_Resp({'error':'model "x" not found'},404))
    with pytest.raises(ProviderError) as caught: p.translate(TranslationRequest('m','s','u'),'local')
    assert caught.value.kind==ErrorKind.FATAL


def test_ollama_uses_native_api_with_thinking_off_and_enough_context(monkeypatch):
    from novel_translator.providers import local_llm as ll
    sent=[]
    native={'model':'m','message':{'role':'assistant','content':'<P000001>\n번역문','thinking':''},'done':True,'done_reason':'stop',
            'prompt_eval_count':8000,'eval_count':120,'eval_duration':4_000_000_000}
    def post(url,headers=None,json=None,timeout=None,**kw):
        sent.append((url,json)); return _Resp(native)
    monkeypatch.setattr(ll.requests,'post',post)
    p=ll.LocalLLMProvider()      # default Ollama address (OpenAI-compatible path) -> native /api/chat
    r=p.translate(TranslationRequest('m','SYSTEM','USER'),'local')
    url,body=sent[-1]
    assert url=='http://localhost:11434/api/chat'
    assert body['think'] is False and body['stream'] is True, 'streamed, so long chunks never time out while writing'
    assert body['options']['num_ctx']==ll.LocalLLMProvider.DEFAULT_NUM_CTX>=12288
    assert body['messages'][0]=={'role':'system','content':'SYSTEM'}
    assert r.text=='<P000001>\n번역문' and p.last_usage=={'input':8000,'output':120,'thinking':0} and p.last_speed==30.0
    # A custom context length and an explicit "thinking on" are passed through.
    p2=ll.LocalLLMProvider('http://127.0.0.1:11434/v1/chat/completions',think=True,num_ctx=16384)
    p2.translate(TranslationRequest('m','s','u'),'local')
    assert sent[-1][0]=='http://127.0.0.1:11434/api/chat' and sent[-1][1]['think'] is True and sent[-1][1]['options']['num_ctx']==16384
    # Truncated output is reported as a format error that names the context setting.
    native.update(done_reason='length')
    with pytest.raises(ProviderError) as caught: p.translate(TranslationRequest('m','s','u'),'local')
    assert caught.value.kind==ErrorKind.FORMAT and '컨텍스트' in caught.value.message


def test_ollama_model_without_thinking_switch_and_non_ollama_server(monkeypatch):
    from novel_translator.providers import local_llm as ll
    sent=[]
    replies=[_Resp({'error':'"m" does not support thinking'},400),
             _Resp({'message':{'content':'번역'},'done_reason':'stop'})]
    monkeypatch.setattr(ll.requests,'post',lambda url,headers=None,json=None,timeout=None,**kw:(sent.append((url,dict(json))),replies.pop(0))[1])
    p=ll.LocalLLMProvider()
    assert p.translate(TranslationRequest('m','s','u'),'local').text=='번역'
    assert 'think' in sent[0][1] and 'think' not in sent[1][1]
    # Remembered: the next request does not send the switch again.
    replies.append(_Resp({'message':{'content':'둘째'},'done_reason':'stop'}))
    p.translate(TranslationRequest('m','s','u'),'local'); assert 'think' not in sent[-1][1]
    # Some other server on port 11434 without /api/chat -> OpenAI-compatible path.
    sent.clear()
    replies[:]=[_Resp({'error':'404 page not found'},404),
                _Resp({'choices':[{'finish_reason':'stop','message':{'content':'호환 번역'}}]})]
    q=ll.LocalLLMProvider()
    assert q.translate(TranslationRequest('m','s','u'),'local').text=='호환 번역'
    assert sent[0][0].endswith('/api/chat') and sent[1][0]=='http://localhost:11434/v1/chat/completions' and q.native_url is None
    # A missing model on the native API is a settings error, not a fallback.
    replies[:]=[_Resp({'error':"model 'qwen-x' not found"},404)]
    with pytest.raises(ProviderError) as caught: ll.LocalLLMProvider().translate(TranslationRequest('qwen-x','s','u'),'local')
    assert caught.value.kind==ErrorKind.FATAL


def test_worker_passes_local_speed_settings(tmp_path):
    made=[]
    class Rec:
        name='local'; last_usage=None
        def __init__(self,*a,**k): made.append((a,k))
        def translate(self,request,key): return TranslationResponse(text='')
    worker_mod.LocalLLMProvider=Rec
    try:
        gem=ScriptedProvider(); w,_,_=make_worker(tmp_path,'　一つ目です。',gem)
        w.local_settings={'enabled':True,'model':'qwen','endpoint':'http://localhost:11434/v1/chat/completions','think':False,'num_ctx':16384}
        w._engine_order_setting=['gemini','local']
        w.run()
        assert made and made[0][1]=={'think':False,'num_ctx':16384,'structured':True,'sampling':'recommended'}
    finally:
        from novel_translator.providers.local_llm import LocalLLMProvider
        worker_mod.LocalLLMProvider=LocalLLMProvider


def test_pause_holds_new_requests_until_resume(tmp_path):
    import threading, time
    gem=ScriptedProvider()
    text='\n\n'.join(f'　第{i}段落です。' for i in range(4))
    w,chunks,events=make_worker(tmp_path,text,gem,chunk_limits=(10,1,20))
    assert len(chunks)>=3
    calls=[]
    real=gem.translate
    def counted(request,key):
        calls.append(time.monotonic())
        if len(calls)==1: w.pause()          # user presses 일시정지 during the first request
        return real(request,key)
    gem.translate=counted
    t0=time.monotonic(); t=threading.Thread(target=w.run); t.start()
    deadline=time.monotonic()+20
    while not w._waiting_in_pause and time.monotonic()<deadline: time.sleep(0.02)   # slow CI machines
    assert w._waiting_in_pause, 'worker never reached the pause'
    time.sleep(0.6)
    assert len(calls)==1 and w.is_paused(), 'no new request may be sent while paused'
    w.resume(); t.join(20); wall=time.monotonic()-t0
    assert not t.is_alive() and len(calls)==len(chunks)
    assert all(r['status']=='COMPLETED' for r in rows(tmp_path))
    assert w._paused_seconds>=0.4 and w._timing['seconds']<=wall-w._paused_seconds+0.05, 'paused time is not counted as translation time'


def test_stop_while_paused_saves_and_ends(tmp_path):
    import threading, time
    gem=ScriptedProvider()
    text='\n\n'.join(f'　第{i}段落です。' for i in range(4))
    w,chunks,events=make_worker(tmp_path,text,gem,chunk_limits=(10,1,20))
    real=gem.translate; calls=[]
    def counted(request,key):
        calls.append(1)
        if len(calls)==1: w.pause()
        return real(request,key)
    gem.translate=counted
    t=threading.Thread(target=w.run); t.start()
    deadline=time.monotonic()+20
    while not w._waiting_in_pause and time.monotonic()<deadline: time.sleep(0.02)
    assert w._waiting_in_pause
    w.stop(); t.join(20)
    assert not t.is_alive() and len(calls)==1
    st=[r['status'] for r in rows(tmp_path)]
    assert st.count('COMPLETED')==1 and 'REQUESTING' not in st


# ---------------------------------------------------------------- glossary extraction
def test_glossary_candidates_contain_no_prose_and_parse_is_strict():
    from novel_translator.term_extraction import candidates, build_prompts, parse_glossary
    zh=('林雨看着苏晴。苏晴笑了。林雨说道：“青云宗的长老来了。”'*8)
    cands=candidates(zh,'zh'); names=[k for k,_ in cands]
    assert '林雨' in names and '苏晴' in names and '青云宗' in names
    assert not any('的' in k or '了' in k for k in names)
    system,user=build_prompts(cands,'zh','',{'林雨':'임우'})
    assert '苏晴笑了' not in user and '“' not in user, 'prose must never be sent'
    got=parse_glossary('```json\n{"林雨":"린위","苏晴":"쑤칭","不存在":"없음","青云宗":"靑雲宗"}\n```',cands)
    assert got=={'林雨':'린위','苏晴':'쑤칭'}


def test_engine_order_migrates_legacy_settings():
    from novel_translator.ui.api_dialog import initial_engine_order
    assert initial_engine_order(None,{'enabled':True},{'enabled':False}) == [('gemini',True),('local',False),('openai',True)]
    assert initial_engine_order(None,{'enabled':True},{'enabled':True,'order':'openai_first'})[:3] == [('gemini',True),('openai',True),('local',True)]
    saved=[{'id':'local','enabled':True},{'id':'gemini','enabled':False}]
    assert initial_engine_order(saved,{},{}) == [('local',True),('gemini',False),('openai',False)]


def test_backup_engine_receives_previous_chunk_context(tmp_path):
    # Chunk 1 introduces a name; chunk 2 has a blocked paragraph that goes to the
    # local backup. The backup must see chunk 1's Korean context (name continuity).
    def gem_rule(prompt,src,body):
        if 'COREPOLICY' in src:
            raise ProviderError(ErrorKind.POLICY,'입력 차단: PROHIBITED_CONTENT')
        if 'NAMEINTRO' in body:
            return '린위가 등장했다. 한국어번역문장'
        return None
    text='\n\n'.join(['　NAMEINTRO の段落です。'+'あ'*40,'　次の段落です。'+'い'*40,'　COREPOLICY の段落です。'+'う'*40])
    gem=ScriptedProvider(gem_rule); loc=ScriptedProvider()
    w,chunks,_=_engine_worker(tmp_path,text,gem,local=loc,limits=(60,1,120))
    assert len(chunks)>=2
    w.run()
    assert loc.requests
    ctx=loc.requests[0].user_prompt.split('[PREVIOUS KOREAN CONTEXT',1)[1].split('[SOURCE',1)[0]
    assert '린위' in ctx, 'backup engine lost the story context'


def test_timing_summary_reports_recovery_share(tmp_path):
    def rule(prompt,src,body):
        if 'REFUSEME' in src and '[RECOVERY NOTE]' not in prompt:
            raise ProviderError(ErrorKind.REFUSAL,'refused')
    text='\n\n'.join(['　REFUSEME です。'+'あ'*40]+[f'　段落{i}です。'+'い'*40 for i in range(4)])
    w,_,events=_engine_worker(tmp_path,text,ScriptedProvider(rule),limits=(40,1,80))
    w.run()
    summary=w.run_summary()
    assert '소요 시간' in summary and '청크당 평균' in summary and '재시도·복구가 필요했던 청크 1개' in summary


def test_pacing_wait_is_visible():
    from novel_translator.orchestrator import TranslationOrchestrator, KeyState
    seen=[]; clock={'t':0.0}
    o=TranslationOrchestrator(ScriptedProvider(),[KeyState('k','s')],'m',sleep_fn=lambda x:clock.__setitem__('t',clock['t']+x),
                              clock_fn=lambda:clock['t'],target_rpm=6,min_rpm=2,max_rpm=6,wait_fn=seen.append)
    o._pace(); o._pace()
    assert seen and '속도 조절 대기' in seen[0] and '6.0회/분' in seen[0]


@pytest.mark.parametrize('flag',['--self-test','--gui-self-test'])
def test_self_test_output_survives_legacy_console_encoding(flag):
    # The Windows CI console used cp1252; printing the Korean window title raised
    # UnicodeEncodeError and the crash handler then opened a modal box that hung
    # the build. Self-tests must print safely and never show a modal box.
    import os, subprocess, sys
    root=Path(__file__).resolve().parents[1]
    env=dict(os.environ, PYTHONIOENCODING='cp1252', QT_QPA_PLATFORM='offscreen')
    proc=subprocess.run([sys.executable,str(root/'run.py'),flag],cwd=root,env=env,capture_output=True,timeout=120)
    assert proc.returncode==0, proc.stderr.decode('utf-8','replace')[-800:]
    assert b'PASS' in proc.stdout


def test_run_py_never_opens_a_modal_box_in_self_test_mode():
    src=(Path(__file__).resolve().parents[1]/'run.py').read_text(encoding='utf-8')
    assert 'if not _SELF_TEST:' in src and 'MessageBoxW' in src


def test_api_dialog_engine_list_and_tabs_are_linked():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    app=QApplication.instance() or QApplication([])
    from novel_translator.ui.api_dialog import ApiDialog
    keys=[{'id':f'GEM-{i}','secret':f'AQ.dummy-key-{i}-abcdef','enabled':True} for i in range(5)]
    d=ApiDialog(keys,'gemini-3.1-flash-lite',{'enabled':True,'model':'gpt-x','keys':[{'id':'OAI','secret':'sk-dummy-1234','enabled':True}]},'',None,
                local_settings={'model':'qwen-local','endpoint':'http://localhost:11434/v1/chat/completions'},
                engines=[{'id':'gemini','enabled':True},{'id':'local','enabled':True},{'id':'openai','enabled':False}])
    try:
        texts=[d.engine_list.item(i).text() for i in range(3)]
        assert texts[0].startswith('1. Gemini') and '주 엔진' in texts[0] and '키 5개' in texts[0]
        assert texts[1].startswith('2. 로컬 LLM') and '보조 1' in texts[1]
        assert '사용 안 함' in texts[2]
        # list -> tab
        d.engine_list.setCurrentRow(1); app.processEvents()
        assert d.tabs.currentIndex()==2 and d.tabs.tabText(2)=='로컬 LLM (보조 1)'
        # tab -> list
        d.tabs.setCurrentIndex(1); app.processEvents()
        assert d.engine_list.currentItem().data(Qt.UserRole)=='openai'
        # check OpenAI, move it to the top: roles and tab titles follow
        d.engine_list.item(2).setCheckState(Qt.Checked); d.engine_list.setCurrentRow(2)
        d._move_engine(-1); d._move_engine(-1); app.processEvents()
        assert d.tabs.tabText(1)=='OpenAI (주 엔진)' and d.tabs.tabText(0)=='Gemini (보조 1)'
        assert [r['id'] for r in d.engine_rows()]==['openai','gemini','local']
        # no clipped/scrolling engine list
        assert d.engine_list.height() >= sum(d.engine_list.sizeHintForRow(i) for i in range(3))
    finally:
        d.close()


def test_translation_memory_reuses_chunks_of_an_earlier_job(tmp_path):
    base='\n\n'.join(f'　第{i}段落です。'+'あ'*40 for i in range(6))
    first=ScriptedProvider()
    w,chunks,_=_engine_worker(tmp_path,base,first,limits=(40,1,80)); w.run()
    assert len(first.requests)==len(chunks)
    # Same text plus an appended paragraph, as a different source file.
    extended=base+'\n\n　追加で貼り付けた段落です。'+'い'*40
    second=ScriptedProvider()
    (tmp_path/'second').mkdir()
    w2,chunks2,events=_engine_worker(tmp_path/'second',extended,second,limits=(40,1,80))
    worker_mod.DATA_DIR=tmp_path/'data'      # same translation database as the first job
    w2.run()
    assert len(second.requests)==len(chunks2)-len(chunks), 'already translated chunks were paid for again'
    assert any('번역 기억' in e for e in events)
    assert (tmp_path/'second'/'out'/'novel.ko.txt').exists()


def test_translation_memory_is_skipped_on_restart_and_when_terms_change(tmp_path):
    base='\n\n'.join(f'　第{i}段落です。'+'あ'*40 for i in range(4))
    w,chunks,_=_engine_worker(tmp_path,base,ScriptedProvider(),limits=(40,1,80)); w.run()
    (tmp_path/'b').mkdir(); (tmp_path/'c').mkdir()
    p2=ScriptedProvider(); w2,_,_=_engine_worker(tmp_path/'b',base+'\n\n　別の段落。',p2,limits=(40,1,80))
    worker_mod.DATA_DIR=tmp_path/'data'
    w2.terms={'第':'제'}; w2.run()
    assert len(p2.requests)>=len(chunks), 'different confirmed terms must not reuse old translations'
    p3=ScriptedProvider(); w3,c3,_=_engine_worker(tmp_path/'c',base+'\n\n　別の段落。',p3,limits=(40,1,80))
    worker_mod.DATA_DIR=tmp_path/'data'
    w3.restart_existing=True; w3.run()
    assert len(p3.requests)==len(c3)


def test_status_names_the_engine_actually_used(tmp_path):
    """Only the local LLM selected: status/DB must say 로컬 LLM + its model, never OpenAI/Gemini."""
    gem=ScriptedProvider(); oai=ScriptedProvider(); loc=ScriptedProvider()
    w,_,_=_engine_worker(tmp_path,'　一つ目です。\n\n　二つ目です。',gem,openai=oai,local=loc,order=['local'])
    states=[]; w.api_state.connect(states.append)
    w.run()
    asking=[s for s in states if '요청 중' in s]
    assert asking and all(s.startswith('로컬 LLM · local-model') for s in asking), asking
    db=Database(tmp_path/'data'/'translations.db')
    with db.connect() as con:
        provs={r[0] for r in con.execute('SELECT provider FROM chunks')}
    assert provs=={'local'} and not gem.requests and not oai.requests
