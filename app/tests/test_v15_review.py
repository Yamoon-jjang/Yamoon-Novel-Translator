"""Regression tests for the v1.5 full review (pipeline, providers, GUI)."""
from __future__ import annotations
import json, re
from pathlib import Path

import pytest
from PySide6.QtWidgets import QTableWidgetItem

from novel_translator.providers.base import TranslationRequest, TranslationResponse, ProviderError, ErrorKind
from novel_translator.ui import main_window as ui
import novel_translator.ui.worker as worker_mod
from test_refusal_recovery import ScriptedProvider, make_worker, rows
from test_alpha17 import _window, _fake_worker_class, _wait, qt_app, sandbox  # noqa: F401 (pytest fixtures)


class _Resp:
    def __init__(self, code, body, lines=None):
        self.status_code=code; self._b=body; self.text=json.dumps(body); self.headers={}; self._lines=lines
    def json(self): return self._b
    def iter_lines(self):
        for x in self._lines or []:
            if isinstance(x, Exception): raise x
            yield x
    def close(self): pass


# ---------------------------------------------------------------- pipeline
def test_half_translated_paragraph_is_sent_again_on_resume(tmp_path):
    """One refused sentence kept as source inside a translated paragraph: the next run
    re-sends that paragraph instead of counting it as done."""
    text='\n\n'.join(['　前の段落です。','　普通の文です。BADSENTの文です。もう一つの文。最後の文です。','　後の段落です。'])
    def refuse(prompt,src,body):
        if 'BADSENT' in body: raise ProviderError(ErrorKind.REFUSAL,'refused')
    w,_,_=make_worker(tmp_path,text,ScriptedProvider(refuse),chunk_limits=(500,1,1000)); w.run()
    first=rows(tmp_path)[0]
    assert first['status']=='PARTIAL' and 'BADSENT' in first['translation']
    second=ScriptedProvider()
    w,_,_=make_worker(tmp_path,text,second,chunk_limits=(500,1,1000)); w.run()
    assert len(second.requests)==1
    sent=second.requests[0].user_prompt.split('[SOURCE',1)[1]
    assert 'BADSENT' in sent and '前の段落' not in sent, 'only the half-translated paragraph is sent again'
    r=rows(tmp_path)[0]
    assert r['status']=='COMPLETED' and 'BADSENT' not in r['translation']


def test_kept_paragraphs_are_recorded_and_cleared():
    from novel_translator.storage import Database
    class Row(dict):
        def keys(self): return super().keys()
    assert Database.kept_pid_set(Row(kept_pids='3,7', status='PARTIAL', error_message=''))=={3,7}
    assert Database.kept_pid_set(Row(kept_pids=None, status='PARTIAL', error_message='일부 문단 원문 유지 (2개: P000004, P000009) · REFUSAL'))=={4,9}
    assert Database.kept_pid_set(Row(kept_pids=None, status='COMPLETED', error_message='P000004'))==set()


@pytest.mark.parametrize('src,out,refusal',[
    ('<P000001>\n内容很简单。如果期限内处理不了……','<P000001>\n내용은 간단했다. 기한 안에 처리할 수 없다면…',False),
    ('<P000001>\n「抱歉，这个我帮不了你。」她说。','<P000001>\n죄송합니다만, 그건 도와드릴 수 없습니다, 하고 그녀는 말했다.',False),
    ('<P000001>\n他不能走。'+'他站在窗边。'*40,'<P000001>\n죄송하지만 이 내용은 번역할 수 없습니다.',True),
    ('<P000001>\n他不能走。','<P000001>\n죄송하지만 이 내용은 번역할 수 없습니다.',True),
    ('<P000001>\n他走了。'*3,'<P000001>\n죄송합니다. 도와드릴 수 없습니다.',True),
])
def test_refusal_detection_reads_the_source(src,out,refusal):
    from novel_translator.validation import _is_refusal
    assert _is_refusal(out,src) is refusal


def test_english_dialogue_with_cant_help_is_not_a_refusal():
    from novel_translator.validation import validate_translation
    en='<P000001>\n'+('He looked at her and said quietly. '*12)+'"I can\'t help you," he said.'
    ko='<P000001>\n'+('그는 그녀를 보며 조용히 말했다. '*12)+'"도와드릴 수 없습니다." 그가 말했다.'
    validate_translation(en,ko)


def test_backup_quota_keeps_the_main_engine_translations(tmp_path):
    text='\n\n'.join([f'　段落{i}です。' for i in range(3)]+['　BADPARAの段落。']+[f'　後の段落{i}です。' for i in range(3)])
    def refuse(prompt,src,body):
        if 'BADPARA' in body: raise ProviderError(ErrorKind.REFUSAL,'refused')
    backup=ScriptedProvider(lambda p,s,b: (_ for _ in ()).throw(ProviderError(ErrorKind.QUOTA,'openai insufficient_quota')))
    w,_,events=make_worker(tmp_path,text,ScriptedProvider(refuse),backup=backup,chunk_limits=(500,1,1000))
    paused=[]; w.paused.connect(lambda m,u: paused.append(m))
    w.run()
    r=rows(tmp_path)[0]
    assert r['status']=='PARTIAL' and r['translation'].count('<P')==7 and 'BADPARA' in r['translation']
    assert not paused, 'a backup out of quota does not pause the job'
    assert not any('Gemini 사용 불가' in e for e in events), 'the main engine is not blamed'
    assert any('보조 엔진 OpenAI 사용 불가' in e for e in events)


def test_fullwidth_number_marks_get_no_space():
    from novel_translator.punctuation import korean_punctuation as k
    assert k('3．5킬로미터')=='3.5킬로미터' and k('１，０００명')=='1,000명' and k('10：30에')=='10:30에'
    assert k('그는 웃었다。그리고')=='그는 웃었다. 그리고'


def test_source_line_that_looks_like_an_id_is_translated(tmp_path):
    text='\n\n'.join(['<P1> marked the start of the protocol.','Second line here.'])
    from novel_translator.chunking import split_paragraphs, make_chunks, mark_translatable
    paras=mark_translatable(split_paragraphs(text),'en')
    chunk=make_chunks(paras,'en',target_tokens=500,min_tokens=1,max_tokens=1000)[0]
    assert '〈P1〉 marked' in chunk.source_text
    from novel_translator.validation import normalize_translation_output, validate_translation, parse_tagged_translation
    out='<P000001>\n〈P1〉은 프로토콜의 시작을 표시했다.\n\n<P000002>\n두 번째 줄이다.'
    norm=normalize_translation_output(out, chunk.source_text)
    validate_translation(chunk.source_text, norm)
    assert parse_tagged_translation(norm)[1].startswith('〈P1〉')


# ---------------------------------------------------------------- providers
def test_traditional_chinese_cp950_file_decodes():
    from novel_translator.textio import decode_txt_bytes
    base='第一章 重逢\n林雨推開門，看見蘇晴坐在窗邊，手裡攥著那封信。\n'*40
    for extra in ['裏','恒','碁','€','╔══╗']:
        t=base+extra; raw=t.encode('cp950')
        out,label=decode_txt_bytes(raw,'zh')
        assert out==t and 'Big5' in label
        assert decode_txt_bytes(raw,'zh','big5')[0]==t, 'choosing Big5 by hand works too'
    zh='第一章\n林雨推开门，看见苏晴坐在窗边。'*40
    assert decode_txt_bytes(zh.encode('gb18030'),'zh')[0]==zh


_RPM={"error":{"code":429,"message":"You exceeded your current quota, please check your plan and billing details.\n* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 10\nPlease retry in 17.3s.","status":"RESOURCE_EXHAUSTED","details":[{"@type":"type.googleapis.com/google.rpc.QuotaFailure","violations":[{"quotaId":"GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},{"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"17s"}]}}
_DAY={"error":{"code":429,"message":"You exceeded your current quota, please check your plan and billing details.","status":"RESOURCE_EXHAUSTED","details":[{"@type":"type.googleapis.com/google.rpc.QuotaFailure","violations":[{"quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
_BADKEY={"error":{"code":400,"message":"API key not valid. Please pass a valid API key.","status":"INVALID_ARGUMENT","details":[{"reason":"API_KEY_INVALID"}]}}


@pytest.mark.parametrize('code,body,kind',[(429,_RPM,ErrorKind.RATE_LIMIT),(429,_DAY,ErrorKind.QUOTA),(400,_BADKEY,ErrorKind.AUTH),
                                           (400,{"error":{"code":400,"message":"API key expired. Please renew the API key.","details":[{"reason":"API_KEY_INVALID"}]}},ErrorKind.AUTH),
                                           (400,{"error":{"code":400,"message":"Invalid model name"}},ErrorKind.FATAL)])
def test_gemini_error_kinds(monkeypatch,code,body,kind):
    from novel_translator.providers import gemini as G
    monkeypatch.setattr(G.requests,'post',lambda *a,**k: _Resp(code,body))
    with pytest.raises(ProviderError) as e:
        G.GeminiProvider().translate(TranslationRequest('gemini-2.5-flash','s','u'),'K')
    assert e.value.kind==kind


def test_gemini_bad_key_moves_to_the_next_key(monkeypatch):
    from novel_translator.providers import gemini as G
    from novel_translator.orchestrator import TranslationOrchestrator, KeyState
    from novel_translator.profiles import ProfileManager
    seq=[_Resp(400,_BADKEY),_Resp(200,{"candidates":[{"content":{"parts":[{"text":"<P000001>\n안녕"}]},"finishReason":"STOP"}]})]
    used=[]
    def post(*a,**k): used.append(k['headers']['x-goog-api-key']); return seq.pop(0)
    monkeypatch.setattr(G.requests,'post',post)
    o=TranslationOrchestrator(G.GeminiProvider(),[KeyState('k1','BAD'),KeyState('k2','GOOD')],'gemini-2.5-flash',sleep_fn=lambda s:None,target_rpm=1e9,min_rpm=1e9,max_rpm=1e9)
    text,key,_=o.translate_chunk(ProfileManager().set_active('zh-ko'),'<P000001>\n你好',{})
    assert used==['BAD','GOOD'] and key=='k2' and '안녕' in text


def test_openai_flagged_prompt_is_a_content_outcome(monkeypatch):
    from novel_translator.providers import openai as O
    body={"error":{"message":"Invalid prompt: your prompt was flagged as potentially violating our usage policy.","type":"invalid_request_error","code":"invalid_prompt"}}
    monkeypatch.setattr(O.requests,'post',lambda *a,**k: _Resp(400,body))
    with pytest.raises(ProviderError) as e:
        O.OpenAIProvider().translate(TranslationRequest('gpt-5','s','u'),'k')
    assert e.value.kind==ErrorKind.POLICY


@pytest.mark.parametrize('reply,expected',[
    ('{"林雨":"린위","苏晴":"쑤칭"}',{'林雨':'린위','苏晴':'쑤칭'}),
    ('<think>먼저 후보를 본다 {예시}</think>\n```json\n{"林雨":"린위"}\n```',{'林雨':'린위'}),
    ('{"林雨":"린위",}\n참고: {公司}는 일반 단어라 뺐습니다.',{'林雨':'린위'}),
    ('Okay, the user wants names.\n</think>{"林雨":"린위"}',{'林雨':'린위'}),
    ('{"林雨":{"korean":"린위"}}',{}),
    ('{}',{}),
])
def test_glossary_reply_parsing(reply,expected):
    from novel_translator.term_extraction import parse_glossary
    cands=[('林雨',9),('苏晴',7),('公司',8)]
    assert parse_glossary(reply,cands,strict=True)==expected


def test_unreadable_glossary_reply_is_a_failure():
    from novel_translator.term_extraction import parse_glossary, GlossaryParseError
    with pytest.raises(GlossaryParseError):
        parse_glossary('죄송하지만 목록을 만들 수 없습니다.',[('林雨',9)],strict=True)
    assert parse_glossary('설명만 있음',[('林雨',9)])=={}


def test_ollama_read_timeout_inside_the_stream_gets_the_timeout_message(monkeypatch):
    import requests
    from urllib3.exceptions import ReadTimeoutError
    from novel_translator.providers import local_llm as LL
    lines=[b'{"message":{"content":"\\uc548"},"done":false}', requests.ConnectionError(ReadTimeoutError(None,None,'Read timed out.'))]
    monkeypatch.setattr(LL.requests,'post',lambda *a,**k: _Resp(200,{},lines))
    with pytest.raises(ProviderError) as e:
        LL.LocalLLMProvider().translate(TranslationRequest('m','s','u'),'local')
    assert e.value.kind==ErrorKind.TRANSIENT and '한 글자도' in e.value.message and 'ollama ps' in e.value.message


def test_ollama_in_stream_errors(monkeypatch):
    from novel_translator.providers import local_llm as LL
    for msg,kind in (('CUDA error: out of memory',ErrorKind.TRANSIENT),("model 'x' not found",ErrorKind.FATAL)):
        monkeypatch.setattr(LL.requests,'post',lambda *a,_m=msg,**k: _Resp(200,{},[json.dumps({'error':_m}).encode()]))
        with pytest.raises(ProviderError) as e:
            LL.LocalLLMProvider().translate(TranslationRequest('m','s','u'),'local')
        assert e.value.kind==kind


def test_reasoning_without_an_opening_tag_is_removed(monkeypatch):
    from novel_translator.providers import local_llm as LL
    lines=[json.dumps({'message':{'content':'Okay, translate it.\n</think>\n\n<P000001>\n린위가 웃었다.'},'done':False}).encode(),
           json.dumps({'message':{'content':''},'done':True,'done_reason':'stop'}).encode()]
    monkeypatch.setattr(LL.requests,'post',lambda *a,**k: _Resp(200,{},lines))
    r=LL.LocalLLMProvider().translate(TranslationRequest('m','s','u'),'local')
    assert r.text=='<P000001>\n린위가 웃었다.'


def test_thinking_gets_room_in_the_output_budget(monkeypatch):
    from novel_translator.providers import local_llm as LL
    sent=[]
    lines=[json.dumps({'message':{'content':'<P000001>\n안녕'},'done':True,'done_reason':'stop'}).encode()]
    def post(url,headers=None,json=None,timeout=None,stream=False): sent.append(json); return _Resp(200,{},lines)
    monkeypatch.setattr(LL.requests,'post',post)
    LL.LocalLLMProvider(think=False).translate(TranslationRequest('m','s','[SOURCE - q]\n你好'),'local')
    LL.LocalLLMProvider(think=True).translate(TranslationRequest('m','s','[SOURCE - q]\n你好'),'local')
    assert sent[1]['options']['num_predict']-sent[0]['options']['num_predict']==8192


# ---------------------------------------------------------------- GUI: auto term extraction
def _gated_extraction(monkeypatch, result=(True,{'林雨':'린위'})):
    import threading
    gate=threading.Event(); calls=[]
    class T(ui.QThread):
        done=ui.Signal(bool,object)
        def __init__(self,*a,**k): super().__init__(k.get('parent')); calls.append(1)
        def run(self): gate.wait(10); self.done.emit(*result)
    monkeypatch.setattr(ui,'TermExtractThread',T)
    return gate, calls


def _paste_window(monkeypatch, text='林雨说。林雨笑了。林雨走了。林雨来了。林雨看着。林雨想着。林雨听着。'):
    w=_window(monkeypatch)
    w.auto_terms.setChecked(True)
    w._prepare_new_input(); w._apply_profile('zh-ko'); w._set_preview_source('')
    w.preview.setPlainText(text)
    return w


def test_everything_is_locked_while_terms_are_extracted(qt_app,sandbox,monkeypatch):
    started=[]; monkeypatch.setattr(ui,'TranslationWorker',_fake_worker_class(started))
    gate,calls=_gated_extraction(monkeypatch)
    w=_paste_window(monkeypatch)
    try:
        w.start_translation()
        assert calls and w._extracting()
        w._update_ready_state()
        assert not w.start.isEnabled() and not w.restart_btn.isEnabled() and not w.single_btn.isEnabled() and w.preview.isReadOnly()
        w.start_translation(); w._restart_translation()
        assert not started, 'nothing starts twice while extracting'
        gate.set()
        assert _wait(qt_app,lambda: started and not started[0].isRunning() and w._result_text)
        assert len(started)==1 and started[0].terms=={'林雨':'린위'}
    finally:
        gate.set(); w.close()


def test_closing_during_extraction_waits_for_it(qt_app,sandbox,monkeypatch):
    started=[]; monkeypatch.setattr(ui,'TranslationWorker',_fake_worker_class(started))
    gate,_=_gated_extraction(monkeypatch)
    w=_paste_window(monkeypatch)
    w.show(); w.start_translation()
    assert w._extracting()
    w.close()
    assert w.isVisible(), 'close is postponed while the request runs'
    gate.set()
    assert _wait(qt_app,lambda: not w.isVisible())
    assert not started, 'no translation starts after the user closed the window'


def test_start_is_enabled_again_after_a_check_stops_the_start(qt_app,sandbox,monkeypatch):
    started=[]; monkeypatch.setattr(ui,'TranslationWorker',_fake_worker_class(started))
    gate,_=_gated_extraction(monkeypatch,(True,{}))
    w=_paste_window(monkeypatch)
    warned=[]; monkeypatch.setattr(ui.QMessageBox,'warning',lambda *a,**k: warned.append(a[1]))
    try:
        for r,(a,b) in enumerate([('苏晴','쑤칭'),('苏晴','수청')]):
            w.term_table.insertRow(r); w.term_table.setItem(r,0,QTableWidgetItem(a)); w.term_table.setItem(r,1,QTableWidgetItem(b))
        w.start_translation(); gate.set()
        assert _wait(qt_app,lambda: not w._extracting() and w.start.isEnabled())
        assert warned==['확정 용어 충돌'] and not started and w.start.text()=='번역 시작 / 계속'
    finally:
        w.close()


def test_restart_chosen_for_terms_falls_back_to_resume_when_none_were_added(qt_app,sandbox,monkeypatch,tmp_path):
    from test_alpha17 import _resume_window
    w,started,calls,asked=_resume_window(qt_app,sandbox,monkeypatch,tmp_path,'extract')
    class Failing(ui.QThread):
        done=ui.Signal(bool,object)
        def __init__(self,*a,**k): super().__init__(k.get('parent'))
        def run(self): self.done.emit(False,'QUOTA · 한도 소진')
    monkeypatch.setattr(ui,'TermExtractThread',Failing)
    try:
        w.start_translation()
        assert _wait(qt_app,lambda: started and not started[0].isRunning() and w._result_text)
        assert asked and not started[0].restart_existing, 'no terms were added: resume instead of paying for the whole book again'
        assert '이어서 번역합니다' in w.error_view.toPlainText()
    finally:
        w.close()


def test_continue_without_glossary_is_remembered(qt_app,sandbox,monkeypatch,tmp_path):
    from test_alpha17 import _resume_window
    w,started,calls,asked=_resume_window(qt_app,sandbox,monkeypatch,tmp_path,'resume')
    try:
        w.start_translation()
        assert _wait(qt_app,lambda: started and not started[0].isRunning() and w._result_text)
        w.start_translation()
        assert _wait(qt_app,lambda: len(started)==2 and not started[1].isRunning())
        assert len(asked)==1, 'asked once per file, not after every stop / continue'
    finally:
        w.close()


def test_reset_settings_updates_the_checkboxes(qt_app,sandbox,monkeypatch):
    w=_window(monkeypatch)
    try:
        w.korean_punct.setChecked(False); w.auto_terms.setChecked(False); w.quote_style.setCurrentIndex(w.quote_style.findData('keep'))
        w._reset_settings()
        assert w.korean_punct.isChecked() and w.auto_terms.isChecked() and w.quote_style.currentData()=='straight'
    finally:
        w.close()


# ---------------------------------------------------------------- independent re-check findings
@pytest.mark.parametrize('src,out',[
    ('<P000001>\n他抓住她的手，说道："你不能走。"她挣扎着，却无法挣脱。','<P000001>\n죄송합니다. 이 요청은 도와드릴 수 없습니다.'),
    ('<P000001>\n彼は彼女の腕をつかんだ。「行かせない、できないよ」','<P000001>\n죄송하지만 도와드릴 수 없습니다.'),
    ('<P000001>\nHe said he can\'t go.','<P000001>\nI\'m sorry, but I can\'t help with that.'),
    ('<P000001>\n他不能走。她说。','<P000001>\n죄송하지만 도와드릴 수 없습니다. 다른 문장을 주시면 도와드릴게요.'),
])
def test_short_source_with_cant_words_never_lets_a_refusal_through(src,out):
    from novel_translator.validation import normalize_translation_output, validate_translation
    with pytest.raises(ProviderError) as e:
        validate_translation(src, normalize_translation_output(out,src))
    assert e.value.kind==ErrorKind.REFUSAL


def test_one_transient_backup_error_does_not_block_whole_chunk_failover(tmp_path):
    """Chunk 1: main refuses a paragraph and the backup times out on it. Chunk 2+: the
    main engine is out of quota -> the backup still takes the whole chunks."""
    state={'n':0}
    def main(prompt,src,body):
        if 'QUOTA_NOW' in src: raise ProviderError(ErrorKind.QUOTA,'daily quota')
        if 'BADPARA' in body: raise ProviderError(ErrorKind.REFUSAL,'refused')
    def backup(prompt,src,body):
        if 'BADPARA' in body: raise ProviderError(ErrorKind.TRANSIENT,'timeout')
        return '보조 '+body[:4]
    text='\n\n'.join(['　一つ目です。','　BADPARAの段落。','　QUOTA_NOW 二つ目の章です。','　QUOTA_NOW 三つ目の章です。'])
    w,chunks,_=make_worker(tmp_path,text,ScriptedProvider(main),backup=ScriptedProvider(backup),chunk_limits=(8,1,12))
    assert len(chunks)>=3
    paused=[]; w.paused.connect(lambda m,u: paused.append(m))
    w.run()
    r=rows(tmp_path)
    assert not paused, 'the job keeps going on the backup'
    assert [x['status'] for x in r[-2:]]==['COMPLETED','COMPLETED'] and all('보조' in x['translation'] for x in r[-2:])


def test_literal_think_tag_inside_the_novel_is_kept(monkeypatch):
    from novel_translator.providers import local_llm as LL
    body='<P000001>\n그는 화면에 "</think>"라는 글자를 쳐 넣고는 웃었다.'
    lines=[json.dumps({'message':{'content':body},'done':True,'done_reason':'stop'}).encode()]
    monkeypatch.setattr(LL.requests,'post',lambda *a,**k: _Resp(200,{},lines))
    assert LL.LocalLLMProvider().translate(TranslationRequest('m','s','u'),'local').text==body


# ---------------------------------------------------------------- Japanese source
def test_euc_jp_file_is_not_read_as_chinese():
    from novel_translator.textio import decode_txt_bytes
    from novel_translator.language_detection import detect_language_bytes
    ja='俺の名前は佐藤一郎。異世界に転生した。彼女は「本当に？」と言った。\n'*30
    raw=ja.encode('euc_jp')
    for lang in (None,'ja'):
        out,label=decode_txt_bytes(raw,lang)
        assert out==ja and label=='EUC-JP'
    assert detect_language_bytes(raw)[0]=='ja'
    assert decode_txt_bytes(raw,'ja','euc_jp')[0]==ja


def test_elongation_marks_do_not_trigger_a_repair_and_are_tidied():
    from novel_translator.validation import leftover_script
    from novel_translator.punctuation import korean_punctuation as k
    assert leftover_script('“앗, 안 돼ー!” 아리아・폰・로젠')==[] and leftover_script('“안 돼一!”')==[]
    assert leftover_script('그는 一人으로 갔다')==['一','人'], 'a real leftover 一 is still caught'
    assert k('“앗, 안 돼ー!” 아리아・폰・로젠')=='"앗, 안 돼~!" 아리아·폰·로젠'


def test_repair_keeps_the_paragraph_indent(tmp_path):
    from test_alpha17 import _single_para_worker, _RepairProvider
    class P(_RepairProvider):
        # The first translation is indented (with leftover Han); the repair answer is not.
        def translate(self, request, key):
            r=super().translate(request,key)
            if '林雨는' in r.text:
                return TranslationResponse(r.text.replace('<P000001>\n','<P000001>\n　'),'STOP',{})
            return r
    prov=P()
    w,_,_=_single_para_worker(tmp_path,prov); w.run()
    t=rows(tmp_path)[0]['translation']
    assert '린위는 창밖의' in t and t.split('\n',1)[1].startswith('　'), 'the full-width indent survives the repair'


def test_polite_japanese_refusal_in_dialogue_is_translated():
    from novel_translator.validation import validate_translation
    src='<P000001>\n'+'受付の女性は書類を見て、困ったように眉を寄せた。'*8+'「そのご依頼は、当ギルドではお引き受けいたしかねます」'
    out='<P000001>\n'+'접수처 여성은 서류를 보고 곤란한 듯 눈썹을 찌푸렸다. '*8+'"그 의뢰는 저희 길드에서 도와드릴 수 없습니다."'
    validate_translation(src,out)


def test_half_width_katakana_and_aozora_notes():
    from novel_translator.chunking import should_translate_text
    assert should_translate_text('ﾄﾞｶｰﾝ!!','ja') and should_translate_text('ｱﾘｼｱ','ja')
    assert not should_translate_text('［＃改ページ］','ja')
    from novel_translator.protected_literals import protect_literals
    assert '［＃' not in protect_literals('彼は笑った［＃「笑った」に傍点］。').text


def test_japanese_name_candidates_skip_one_kanji_words():
    from novel_translator.term_extraction import candidates
    t=('お兄さんが言った。皆さん、同様に。彼氏と王様。'*30)+('佐藤一郎さんが笑った。アリシア・フォン・ローゼンベルク様が来た。'*12)
    names=[k for k,_ in candidates(t,'ja')]
    assert '佐藤一郎' in names and 'アリシア・フォン・ローゼンベルク' in names
    assert not {'兄','皆','同','彼','王'} & set(names)


def test_retry_split_keeps_indent_and_abbreviations():
    from novel_translator.chunking import split_chunk_for_retry, Chunk, Paragraph
    p=Paragraph(1,'　あいうえお。かきくけこ。さしすせそ。','\n','',True)
    assert split_chunk_for_retry(Chunk(0,[p],10,10),'ja')[0].paragraphs[0].text.startswith('　')
    p=Paragraph(1,'He met Mr. Smith and Dr. Jones at 3.14 p.m. sharp. Then they left. It was late.','\n\n','',True)
    first=split_chunk_for_retry(Chunk(0,[p],10,10),'en')[0].paragraphs[0].text
    assert first=='He met Mr. Smith and Dr. Jones at 3.14 p.m. sharp.'


# ---------------------------------------------------------------- English source
_GUTENBERG="""It is a truth universally acknowledged, that a single man in possession
of a good fortune, must be in want of a wife.

However little known the feelings or views of such a man may be on his
first entering a neighbourhood, this truth is so well fixed in the minds
of the surrounding families, that he is considered as the rightful neigh-
bourhood property of some one or other of their daughters.

Roses are red,
Violets are blue.
"""


def test_hard_wrapped_english_is_joined_into_paragraphs():
    from novel_translator.chunking import split_paragraphs, mark_translatable
    ps=mark_translatable(split_paragraphs(_GUTENBERG),'en')
    texts=[p.text for p in ps]
    assert texts[0]=='It is a truth universally acknowledged, that a single man in possession of a good fortune, must be in want of a wife.'
    assert 'rightful neighbourhood property' in texts[1]
    assert texts[2:]==['Roses are red,','Violets are blue.'], 'short lines (poems, lists) stay separate'
    web='He walked into the room and looked around for a while.\nShe was there, sitting by the window as always.\n'
    assert len(mark_translatable(split_paragraphs(web),'en'))==2, 'one paragraph per line (web novels) is kept'


def test_refusal_in_one_paragraph_is_caught_even_with_cant_elsewhere():
    from novel_translator.validation import validate_translation
    src=('<P000001>\nThey ran.\n\n<P000002>\n"They can\'t follow us," she said.\n\n<P000003>\n'
         'The rain fell hard on the old roof of the barn, and the horses stamped nervously in their dark stalls.')
    out='<P000001>\n그들은 달렸다.\n\n<P000002>\n"그들은 우릴 따라올 수 없어." 그녀가 말했다.\n\n<P000003>\n죄송하지만 이 부분은 번역할 수 없습니다.'
    with pytest.raises(ProviderError) as e: validate_translation(src,out)
    assert e.value.kind==ErrorKind.REFUSAL


def test_omission_note_check_uses_whole_words_per_paragraph():
    from novel_translator.validation import has_unsourced_omission_note
    assert has_unsourced_omission_note('<P000001>\nShe was cute and he smiled.','<P000001>\n그녀는 귀여웠다(성적인 묘사 생략).')
    assert not has_unsourced_omission_note('<P000001>\n[scene cut]','<P000001>\n[장면 생략]')


def test_english_title_kept_in_english_and_code_are_accepted():
    from novel_translator.validation import validate_translation, untranslated_residue
    assert untranslated_residue('He was reading Harry Potter and the Order of the Phoenix again.','그는 Harry Potter and the Order of the Phoenix를 다시 읽고 있었다.')==''
    assert untranslated_residue('He said that he would never come back to this town again.','그는 he would never come back to this town again 라고 했다.')
    code='def main():    x = compute(a, b)  # sum    return {"k": x}'
    validate_translation('<P000001>\nRun the following code:\n\n<P000002>\n'+code,'<P000001>\n다음 코드를 실행하세요:\n\n<P000002>\n'+code)
    prose='<P000001>\n'+'The rain fell hard on the old roof of the barn, and the horses stamped. '*3
    with pytest.raises(ProviderError): validate_translation(prose,prose)


def test_english_encodings():
    from novel_translator.textio import decode_txt_bytes
    en='She said, “It’s fine — really,” and smiled at the café owner.\n'*40
    out,label=decode_txt_bytes(en.encode('utf-8')+b'\x92s\n','en'); assert out.startswith(en) and label.startswith('UTF-8')
    out,label=decode_txt_bytes(en.encode('cp1252')+b'\x9d','en'); assert out.startswith(en)
    out,_=decode_txt_bytes('Hello there, my friend.\n'.encode('utf-16-le')*20,'en'); assert out.startswith('Hello there')
    assert decode_txt_bytes(en.encode('cp1252'),'ja')[0]==en, 'a wrong language setting still reads the file'


def test_english_name_candidates():
    from novel_translator.term_extraction import candidates
    t=('"My dear Mr. Darcy," said Elizabeth. Lady Catherine frowned at Miss Bennet. Dr. Watson nodded. '
       'Lord Voldemort laughed while Professor McGonagall and Sean O\'Brien watched. Suddenly, nothing moved. '
       'On Monday in March, Sir Walter Elliot arrived. Everyone stared. ')*5
    names={k for k,_ in candidates(t,'en')}
    assert {'Darcy','Elizabeth','Catherine','Bennet','Watson','Voldemort','McGonagall',"Sean O'Brien",'Walter Elliot'} <= names
    assert not names & {'Mr','Suddenly','Everyone','Monday','March','Lord','Lady'}


# ---------------------------------------------------------------- second re-check findings
def test_english_name_extraction_is_fast_on_big_files():
    import time
    from novel_translator.term_extraction import candidates
    t=('"My dear Mr. Darcy," said Elizabeth. Lady Catherine frowned. "Alice," said Bob to Alice and Jean-Luc. ')
    big=t*int(1_000_000/len(t))
    t0=time.monotonic(); names={k for k,_ in candidates(big,'en')}; took=time.monotonic()-t0
    assert took < 3, f'{took:.1f}s for 1 MB'
    assert {'Darcy','Elizabeth','Catherine','Alice','Bob','Jean-Luc'} <= names


def test_one_inside_a_korean_word_is_not_an_elongation():
    from novel_translator.punctuation import korean_punctuation as k
    from novel_translator.validation import leftover_script
    assert k('제一장 시작')=='제一장 시작' and k('균一한')=='균一한' and leftover_script('제一장')==['一']
    assert k('안 돼一!')=='안 돼~!' and leftover_script('안 돼一!')==[]


@pytest.mark.parametrize('name,text',[
    ('verse',"I celebrate myself, and sing myself, and what I assume you shall assume,\nFor every atom belonging to me as good belongs to you, and all of it, yes,\nI loafe and invite my soul, I lean and loafe at my ease observing a spear,\nMy tongue, every atom of my blood, form'd from this soil, this air.\n"),
    ('dialogue',"“I told you already, you cannot simply walk into the hall like that—”\n“Watch me,” she said, and she pushed the heavy door open with both hands—\n“Fine.”\n"),
    ('list',"- First item that is fairly long and goes on for a while without end\n- Second item that is fairly long and goes on for a while without end\n- Third item.\n"),
    ('status',"[System] You have gained a new skill called Shadow Step at level one\n[System] Your stamina has increased by ten points and your speed by five\n[System] Done.\n"),
])
def test_unwrap_leaves_verse_dialogue_lists_and_markup_alone(name,text):
    from novel_translator.chunking import split_paragraphs, mark_translatable
    assert len(mark_translatable(split_paragraphs(text),'en'))==len(split_paragraphs(text))


def test_quoted_refusal_like_dialogue_is_accepted():
    from novel_translator.validation import validate_translation
    validate_translation('<P000001>\n"I\'m afraid that is not something I am permitted to help you with, my lord."',
                         '<P000001>\n"유감스럽게도 그건 제가 도와드릴 수 없습니다, 경."')


def test_cjk_file_with_one_bad_byte_keeps_its_encoding():
    from novel_translator.textio import decode_txt_bytes
    zh='第一章\n林雨推开门，看见苏晴坐在窗边。\n'*30
    out,label=decode_txt_bytes(zh.encode('gb18030')+b'\xb0','zh'); assert out.startswith(zh) and label.startswith('GB18030')
    ja='彼女は窓の外を見つめていた。\n'*30
    for enc,name in (('cp932','CP932'),('euc_jp','EUC-JP')):
        out,label=decode_txt_bytes(ja.encode(enc)+b'\x82' if enc=='cp932' else ja.encode(enc)+b'\xa4','ja')
        assert out.startswith(ja) and label.startswith(name)


def test_rerun_retranslates_paragraphs_with_leftover_han(tmp_path):
    from test_alpha17 import _no_repair
    text='\n\n'.join(['　前の段落です。','　真ん中の段落です。','　後の段落です。'])
    first=ScriptedProvider(lambda p,s,b: '중간 段落은 그대로 남았다' if '真ん中' in b else '번역된 문단')
    w,_,events=make_worker(tmp_path,text,first,chunk_limits=(500,1,1000)); _no_repair(); w.run()
    r=rows(tmp_path)[0]
    assert r['status']=='COMPLETED' and '段落' in r['translation']
    assert any('한자·가나가 남은 문단 1개' in e for e in events), 'the summary tells the user'
    second=ScriptedProvider(lambda p,s,b: '중간 문단은 깨끗하다')
    w,_,events=make_worker(tmp_path,text,second,chunk_limits=(500,1,1000)); _no_repair(); w.run()
    assert len(second.requests)==1
    sent=second.requests[0].user_prompt.split('[SOURCE',1)[1]
    assert '真ん中' in sent and '前の段落' not in sent, 'only the paragraph with leftover Han is sent again'
    t=rows(tmp_path)[0]['translation']
    assert '段落' not in t and '중간 문단은 깨끗하다' in t and t.count('번역된 문단')==2


# ---------------------------------------------------------------- rerun: only the untranslated sentences of a big paragraph
def test_rerun_sends_only_the_sentences_left_in_source(tmp_path):
    big='　'+''.join(f'これは{i}番目の普通の文です。' for i in range(12))+'BADSENTの文です。'+''.join(f'その後{i}番目の文が続きます。' for i in range(12))
    text='\n\n'.join(['　前の段落です。', big, '　後の段落です。'])
    def refuse(prompt,src,body):
        if 'BADSENT' in body: raise ProviderError(ErrorKind.REFUSAL,'refused')
        return '번역문장'
    w,_,_=make_worker(tmp_path,text,ScriptedProvider(refuse),chunk_limits=(2000,1,4000)); w.run()
    first=rows(tmp_path)[0]
    assert first['status']=='PARTIAL' and 'BADSENT' in first['translation'] and '번역문장' in first['translation']
    second=ScriptedProvider(lambda p,s,b: '고친문장')
    w,_,events=make_worker(tmp_path,text,second,chunk_limits=(2000,1,4000)); w.run()
    sent=[r.user_prompt.split('[SOURCE',1)[1] for r in second.requests]
    assert sent and all('BADSENT' in x for x in sent), 'only the sentences still in Japanese are sent'
    assert not any('これは' in x for x in sent), 'the translated sentences are not sent again'
    r=rows(tmp_path)[0]
    assert r['status']=='COMPLETED' and 'BADSENT' not in r['translation'] and '번역문장' in r['translation'] and '고친문장' in r['translation']
    assert any('원문으로 남았던 문장' in e for e in events)


class _LeftoverProvider:
    """Translations keep two Han characters; the repair answer is clean when `fix` is set."""
    name='gemini'
    def __init__(self, fix=False): self.fix=fix; self.requests=[]
    def translate(self, request, key):
        from novel_translator.prompting import REPAIR_SYSTEM
        from test_refusal_recovery import _source, TAG_BLOCK
        self.requests.append(request)
        if request.system_prompt==REPAIR_SYSTEM:
            body='한국어 문장이 길게 이어진다. '*30+('끝.' if self.fix else '窗外 끝.')
            return TranslationResponse('<P000002>\n'+body,'STOP',{})
        rows_=[]
        for pid,b in TAG_BLOCK.findall(_source(request.user_prompt)):
            rows_.append(f'<P{pid}>\n'+('한국어 문장이 길게 이어진다. '*30+'窗外 끝.' if len(b)>400 else '짧은 번역.'))
        return TranslationResponse('\n\n'.join(rows_),'STOP',{})


def test_rerun_repairs_leftover_han_in_a_long_paragraph_without_retranslating(tmp_path):
    long='　'+'林雨看着窗外的雨，很久没有说话。'*30
    text='\n\n'.join(['　前の段落です。', long, '　後の段落です。'])
    w,_,_=make_worker(tmp_path,text,_LeftoverProvider(),chunk_limits=(2000,1,4000)); w.run()
    assert '窗外' in rows(tmp_path)[0]['translation']
    second=_LeftoverProvider(fix=True)
    w,_,_=make_worker(tmp_path,text,second,chunk_limits=(2000,1,4000)); w.run()
    from novel_translator.prompting import REPAIR_SYSTEM
    assert [r.system_prompt==REPAIR_SYSTEM for r in second.requests]==[True], 'one repair request, no full re-translation'
    t=rows(tmp_path)[0]['translation']
    assert '窗外' not in t and t.count('짧은 번역.')==2


# ---------------------------------------------------------------- targeted retry: re-check findings
def _partial_big(tmp_path, extra='', terms=None):
    big='　'+''.join(f'これは{i}番目の普通の文です。' for i in range(12))+'BADSENTの文です。'+extra+''.join(f'その後{i}番目の文が続きます。' for i in range(12))
    text='\n\n'.join(['　前の段落です。', big, '　後の段落です。'])
    def refuse(prompt,src,body):
        if 'BADSENT' in body: raise ProviderError(ErrorKind.REFUSAL,'refused')
        if body.strip()=='林雨！': return body.strip()
        return '번역문장 林雨！' if '林雨' in body else '번역문장'
    w,_,_=make_worker(tmp_path,text,ScriptedProvider(refuse),chunk_limits=(2000,1,4000))
    if terms: w.terms.update(terms)
    w.run()
    assert rows(tmp_path)[0]['status']=='PARTIAL'
    return text


def test_stop_during_the_sentence_retry_is_a_stop_not_an_error(tmp_path):
    text=_partial_big(tmp_path)
    holder={}
    def rule(p,s,b):
        holder['w']._stop=True; return '고친문장'
    w,_,_=make_worker(tmp_path,text,ScriptedProvider(rule),chunk_limits=(2000,1,4000)); holder['w']=w
    failed=[]; w.failed.connect(failed.append)
    w.run()
    assert not failed and rows(tmp_path)[0]['status']=='PARTIAL', 'paused like any stop; the earlier result is kept'


def test_a_sentence_kept_in_han_on_purpose_does_not_keep_the_chunk_partial(tmp_path):
    text=_partial_big(tmp_path,'林雨！',{'林雨':'林雨'})
    counts=[]
    for _ in range(3):
        p=ScriptedProvider(lambda p,s,b: '林雨！' if b.strip()=='林雨！' else '고친문장')
        w,_,_=make_worker(tmp_path,text,p,chunk_limits=(2000,1,4000)); w.terms.update({'林雨':'林雨'}); w.run()
        counts.append(len(p.requests))
    assert rows(tmp_path)[0]['status']=='COMPLETED' and counts[1:]==[0,0], counts


def test_scattered_leftover_sentences_go_in_one_request(tmp_path):
    import sqlite3
    big='　'+''.join(f'これは{i}番目の普通の文です。BAD{i}の文です。' for i in range(20))
    text='\n\n'.join(['　前の段落です。', big, '　後の段落です。'])
    w,_,_=make_worker(tmp_path,text,ScriptedProvider(lambda p,s,b:'번역문장 '*max(1,len(b)//5)),chunk_limits=(4000,1,8000)); w.run()
    body='　'+''.join(f'번역{i}입니다. BAD{i}の文です。' for i in range(20))
    con=sqlite3.connect(tmp_path/'data'/'translations.db')
    con.execute("UPDATE chunks SET status='PARTIAL', translation=?, kept_pids='2', error_type='REFUSAL'",
                (f'<P000001>\n번역문장\n\n<P000002>\n{body}\n\n<P000003>\n번역문장',)); con.commit(); con.close()
    p=ScriptedProvider(lambda p,s,b: '고친문장')
    w,_,events=make_worker(tmp_path,text,p,chunk_limits=(4000,1,8000)); w.run()
    r=rows(tmp_path)[0]
    assert len(p.requests)==1 and r['status']=='COMPLETED' and 'BAD' not in r['translation'] and r['translation'].count('번역')>=20
    assert not any('상한' in e for e in events)


# ---------------------------------------------------------------- drag & drop
def _drop(w, paths, target=None):
    from PySide6.QtCore import QMimeData, QUrl, QPointF, Qt as _Qt
    from PySide6.QtGui import QDropEvent, QDragEnterEvent
    from PySide6.QtWidgets import QApplication
    mime=QMimeData(); mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    target=target or w
    enter=QDragEnterEvent(target.rect().center(), _Qt.CopyAction, mime, _Qt.LeftButton, _Qt.NoModifier)
    QApplication.sendEvent(target, enter)
    drop=QDropEvent(QPointF(target.rect().center()), _Qt.CopyAction, mime, _Qt.LeftButton, _Qt.NoModifier)
    QApplication.sendEvent(target, drop)
    return enter.isAccepted(), drop.isAccepted()


def test_drop_a_txt_file_or_a_folder_opens_it(qt_app,sandbox,monkeypatch,tmp_path):
    book=tmp_path/'book.txt'; book.write_text('林雨看着窗外的雨，很久没有说话。','utf-8')
    folder=tmp_path/'shelf'; folder.mkdir(); (folder/'a.txt').write_text('他笑了。','utf-8'); (folder/'b.txt').write_text('她哭了。','utf-8')
    w=_window(monkeypatch)
    try:
        assert w.acceptDrops()
        assert _drop(w,[book])==(True,True)
        assert w.input_mode=='single' and w.source_path==book and '林雨' in w.preview.toPlainText()
        _drop(w,[folder])
        assert w.input_mode=='folder' and w.batch_root==folder and len(w.batch_files)==2
        # dropped on the source pane: the file opens, its path is not pasted as text
        accepted=_drop(w,[book],target=w.preview.viewport())
        assert accepted[1] and w.input_mode=='single' and w.preview.toPlainText().startswith('林雨')
    finally:
        w.close()


def test_drop_is_refused_politely(qt_app,sandbox,monkeypatch,tmp_path):
    a=tmp_path/'a.txt'; b=tmp_path/'b.txt'; img=tmp_path/'c.png'
    for f in (a,b): f.write_text('他笑了。','utf-8')
    img.write_bytes(b'\x89PNG')
    w=_window(monkeypatch)
    infos=[]; monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k: infos.append(a[1]))
    try:
        _drop(w,[img]); _drop(w,[tmp_path, tmp_path])
        assert infos==['TXT 파일만 열 수 있음','폴더는 하나씩'] and w.input_mode!='single'
        class Busy:
            def isRunning(self): return True
        w.worker=Busy()
        _drop(w,[a])
        assert w.source_path!=a and '번역' in w.status.text(), 'nothing changes while a job runs'
        w.worker=None
    finally:
        w.close()


def test_several_dropped_files_are_translated_as_a_batch_of_only_those_files(qt_app,sandbox,monkeypatch,tmp_path):
    shelf=tmp_path/'shelf'; shelf.mkdir(); (shelf/'sub').mkdir()
    picked=[shelf/'b.txt', shelf/'a.txt', shelf/'sub'/'c.txt']
    for f in picked+[shelf/'not_me.txt']: f.write_text('他笑了。','utf-8')
    w=_window(monkeypatch)
    try:
        _drop(w,picked)
        assert w.input_mode=='folder' and w.batch_root==shelf
        assert [p.name for p in w.batch_files]==['a.txt','b.txt','c.txt'], 'only the dropped files, never the rest of the folder'
        assert not w.include_sub.isEnabled() and '끌어다 놓은 파일' in w.file_label.text()
        w.include_sub.setChecked(True); w._rescan_all_batch_files()
        assert len(w.batch_files)==3
        assert w._output_dir_for(shelf/'sub'/'c.txt')==shelf/'한국어_번역'/'sub'
        _drop(w,[shelf])   # a whole folder again: the selection is gone
        assert 'not_me.txt' in [p.name for p in w.batch_files]
    finally:
        w.close()


def test_new_keys_get_their_own_project_group(qt_app,monkeypatch):
    from novel_translator.ui import api_dialog as ad
    t=ad._KeyTable('gemini',[],lambda:'gemini-3.1-flash-lite')
    answers=iter([('KEY-AAA',True),('',True),(None,True),('KEY-BBB',True),('',True),(None,True),('KEY-CCC',True),('',True),('P1',True)])
    def fake(parent,title,label,*a,**k):
        val,ok=next(answers)
        return (k.get('text','') if val is None else val), ok
    monkeypatch.setattr(ad.QInputDialog,'getText',fake)
    for _ in range(3): t._add_key()
    assert [r.project_group for r in t._records]==['P1','P2','P1'], 'new keys default to separate groups; the user can say a key shares a project'


def test_group_typed_in_the_table_survives_adding_and_deleting_keys(qt_app,monkeypatch):
    from novel_translator.ui import api_dialog as ad
    from novel_translator.api_keys import ApiKeyRecord
    t=ad._KeyTable('gemini',[ApiKeyRecord('GEM-A','key-a','', 'default',True,'').to_dict(),ApiKeyRecord('GEM-B','key-b','', 'default',True,'').to_dict()],lambda:'m')
    assert t.table.rowCount()==2
    ida,idb=[t.table.item(r,0).data(ad.Qt.UserRole) for r in range(2)]
    t.table.item(0,2).setText('P7'); t.table.item(1,1).setText('두번째')
    answers=iter([('key-c',True),('',True),('P9',True)])
    monkeypatch.setattr(ad.QInputDialog,'getText',lambda *a,**k: next(answers))
    t._add_key()
    assert t.table.item(0,2).text()=='P7' and t.table.item(1,1).text()=='두번째'
    monkeypatch.setattr(ad.QMessageBox,'question',lambda *a,**k: ad.QMessageBox.Yes)
    t.table.item(1,2).setText('P8')
    t._delete_key(ida)
    assert [(r['label'],r['project_group']) for r in t.records()]==[('두번째','P8'),('','P9')]


def test_new_key_name_and_group_are_kept_with_the_real_dialog_wiring(qt_app,monkeypatch):
    """The API dialog listens to table changes (engine list refresh) and reads the table back;
    that read happened while a new row was half built and erased the name and group."""
    from novel_translator.ui import api_dialog as ad
    t=ad._KeyTable('gemini',[{'id':'GEM-A','secret':'key-a','label':'','project_group':'P1','enabled':True}],lambda:'m')
    t.on_change=lambda: t.enabled_count()   # what ApiDialog._refresh_engines does
    answers=iter([('key-b',True),('Comfyui 프로젝트',True),('P2',True)])
    monkeypatch.setattr(ad.QInputDialog,'getText',lambda *a,**k: next(answers))
    t._add_key()
    assert [(r['label'],r['project_group']) for r in t.records()]==[('','P1'),('Comfyui 프로젝트','P2')]
    assert t.table.item(1,1).text()=='Comfyui 프로젝트' and t.table.item(1,2).text()=='P2'


# ---------------------------------------------------------------- name reading, personal guidelines, live view
def test_chinese_names_default_to_sino_korean_reading():
    from novel_translator.language_options import option_spec, prompt_instructions
    spec=option_spec('zh-ko')
    assert spec['choices'][0][1]=='sino_korean_names'
    rule=' '.join(prompt_instructions({'중국 인명 표기':'sino_korean_names'}))
    assert '좌경' in rule and '쭤징' in rule, 'the rule shows the wanted and the unwanted reading'


def test_personal_guidelines_go_into_requests_but_not_into_the_job_identity(tmp_path):
    from novel_translator.user_guidelines import save_user_guidelines
    text='\n\n'.join(['　前の段落です。','　後の段落です。'])
    prov=ScriptedProvider()
    w,_,_=make_worker(tmp_path,text,prov,chunk_limits=(500,1,1000))
    before=(w._fingerprint(), w._settings_key())
    save_user_guidelines(w.profile.id,'대사는 반말로, 의성어는 한국식으로',worker_mod.DATA_DIR)
    after=(w._fingerprint(), w._settings_key())
    assert before==after, 'editing the guidelines never restarts a half-translated book'
    w.run()
    assert '대사는 반말로' in prov.requests[0].user_prompt and 'USER TRANSLATION PREFERENCES' in prov.requests[0].user_prompt
    save_user_guidelines(w.profile.id,'',worker_mod.DATA_DIR)


def test_name_option_in_the_glossary_tab_mirrors_the_language_setting(qt_app,sandbox,monkeypatch):
    w=_window(monkeypatch)
    try:
        w._apply_profile('zh-ko')
        assert w.name_option.count()==w.lang_option.count() and w.name_option_label.text()=='중국 인명 표기'
        w.name_option.setCurrentIndex(2)
        assert w.lang_option.currentData()=='mandarin_names'
        w.lang_option.setCurrentIndex(0)
        assert w.name_option.currentIndex()==0
    finally:
        w.close()


def test_changing_the_name_reading_offers_to_reextract_and_replaces_old_readings(qt_app,sandbox,monkeypatch):
    calls=[]
    class FakeThread(ui.QThread):
        done=ui.Signal(bool,object)
        def __init__(self,provider_id,secret,model,endpoint,system,user,cands,parent=None,local_options=None):
            super().__init__(parent); calls.append(user)
        def run(self): self.done.emit(True,{'林雨':'임우'})
    monkeypatch.setattr(ui,'TermExtractThread',FakeThread)
    w=_window(monkeypatch)
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k: None)
    try:
        w._prepare_new_input(); w._apply_profile('zh-ko'); w._set_preview_source('')
        w.preview.setPlainText('林雨说。林雨笑了。林雨走了。林雨来了。林雨看着。林雨想着。林雨听着。')
        w.term_table.insertRow(0); w.term_table.setItem(0,0,QTableWidgetItem('林雨')); w.term_table.setItem(0,1,QTableWidgetItem('린위'))
        monkeypatch.setattr(ui.QMessageBox,'exec',lambda self: None)
        monkeypatch.setattr(ui.QMessageBox,'clickedButton',lambda self: self.buttons()[0])   # "새 방식으로 다시 추출"
        w.lang_option.setCurrentIndex(w.lang_option.findData('contextual_names'))
        assert _wait(qt_app,lambda: calls and w._term_thread is None)
        assert 'ALREADY CONFIRMED' not in calls[0], 'the old readings are not handed to the model as fixed'
        assert w._terms()=={'林雨':'임우'}, 'the old reading is replaced'
        assert '확정 용어집 (1)' in w.tabs.tabText(w.tabs.indexOf(w.terms_tab))
    finally:
        w.close()


def test_live_translation_view_grows_chunk_by_chunk(qt_app,sandbox,monkeypatch):
    w=_window(monkeypatch)
    try:
        w._result_text=''; w._live_started=False
        w.on_chunk_translated(1,3,'첫 번째 청크의 번역')
        assert w.view_result_btn.isEnabled() and w.view_result_btn.text()=='번역 결과 (진행 중)'
        w._show_preview('result')
        w.on_chunk_translated(2,3,'두 번째 청크의 번역')
        shown=w.preview.toPlainText()
        assert shown.startswith('[진행 중인 번역') and '첫 번째' in shown and shown.rstrip().endswith('두 번째 청크의 번역')
        w._set_running_ui(False)
        assert w.view_result_btn.text()=='번역 결과'
    finally:
        w.close()


def test_worker_emits_each_translated_chunk(tmp_path):
    text='\n\n'.join(f'　第{i}段落です。' for i in range(4))
    w,chunks,_=make_worker(tmp_path,text,ScriptedProvider(lambda p,s,b: '번역 '+str(len(b))),chunk_limits=(10,1,20))
    got=[]; w.chunk_translated.connect(lambda i,t,x: got.append((i,t,x)))
    w.run()
    assert len(got)==len(chunks) and got[0][1]==len(chunks) and all(x.startswith('번역') for _,_,x in got)


# ---------------------------------------------------------------- model box follows the main engine
def _engine_window(monkeypatch, engines, **extra):
    w=_window(monkeypatch)
    w.secrets.update(extra); w.secrets['engines']=[{'id':n,'enabled':on} for n,on in engines]
    saved=[]; w.secret_store.save=lambda data: saved.append(dict(data))
    w._update_ready_state()
    return w,saved


def test_model_box_shows_gemini_when_gemini_is_first(qt_app,sandbox,monkeypatch):
    w,_=_engine_window(monkeypatch,[('gemini',True),('openai',True),('local',False)],
                       openai={'enabled':True,'model':'gpt-6.1-sol','keys':[{'id':'o','secret':'x','enabled':True}]})
    try:
        assert w.model_caption.text()=='Gemini 모델'
        assert not w.model.isHidden() and w.alt_model.isHidden()
    finally: w.close()


def test_model_box_follows_openai_first_and_saves_choice(qt_app,sandbox,monkeypatch):
    w,saved=_engine_window(monkeypatch,[('openai',True),('gemini',True),('local',False)],
                           openai={'enabled':True,'model':'gpt-6.1-sol','keys':[{'id':'o','secret':'x','enabled':True}]})
    try:
        assert w.model_caption.text()=='OpenAI 모델'
        assert w.model.isHidden() and not w.alt_model.isHidden()
        assert w._alt_model_id()=='gpt-6.1-sol'
        assert w.alt_model.findData('gpt-6-luna')>=0          # the OpenAI models are listed
        assert 'Gemini(보조) 모델' in w.model_caption.toolTip()
        gem_before=w._selected_model_id()
        w.alt_model.setCurrentIndex(w.alt_model.findData('gpt-6-luna')); w._alt_model_changed()
        assert w.secrets['openai']['model']=='gpt-6-luna' and saved and saved[-1]['openai']['model']=='gpt-6-luna'
        assert w._openai_worker_settings()['model']=='gpt-6-luna'   # the worker gets the new model
        assert w._selected_model_id()==gem_before                  # Gemini's model is untouched
        w._update_ready_state(); assert w._alt_model_id()=='gpt-6-luna'   # a refresh keeps the choice
    finally: w.close()


def test_model_box_follows_local_first_and_typed_name_is_saved(qt_app,sandbox,monkeypatch):
    w,saved=_engine_window(monkeypatch,[('local',True),('gemini',False),('openai',False)],
                           local={'enabled':True,'model':'gemma3:12b','endpoint':'http://localhost:11434'})
    try:
        assert w.model_caption.text()=='로컬 LLM 모델' and w._alt_model_id()=='gemma3:12b'
        assert 'Gemini(보조)' not in w.model_caption.toolTip()
        w.alt_model.setEditText('qwen3:14b'); w._alt_model_changed()
        assert w.secrets['local']['model']=='qwen3:14b' and saved[-1]['local']['model']=='qwen3:14b'
        w.alt_model.setEditText(''); w._alt_model_changed()        # an empty box never wipes the saved model
        assert w.secrets['local']['model']=='qwen3:14b'
    finally: w.close()


def test_model_box_switches_back_after_engine_order_changes(qt_app,sandbox,monkeypatch):
    w,_=_engine_window(monkeypatch,[('openai',True),('gemini',True),('local',False)],
                       openai={'enabled':True,'model':'gpt-6.1-sol','keys':[{'id':'o','secret':'x','enabled':True}]})
    try:
        assert w.model_caption.text()=='OpenAI 모델'
        w.secrets['engines']=[{'id':'gemini','enabled':True},{'id':'openai','enabled':True},{'id':'local','enabled':False}]
        w._update_ready_state()
        assert w.model_caption.text()=='Gemini 모델' and not w.model.isHidden() and w.alt_model.isHidden()
        w.secrets['engines']=[{'id':'gemini','enabled':False},{'id':'openai','enabled':False},{'id':'local','enabled':False}]
        w._update_ready_state()
        assert w.model_caption.text()=='Gemini 모델 (사용 안 함)'
    finally: w.close()


def test_model_box_is_locked_while_running(qt_app,sandbox,monkeypatch):
    w,_=_engine_window(monkeypatch,[('openai',True),('gemini',False),('local',False)],
                       openai={'enabled':True,'model':'gpt-6.1-sol','keys':[{'id':'o','secret':'x','enabled':True}]})
    try:
        w._lock_inputs(True); assert not w.alt_model.isEnabled()
        w._lock_inputs(False); assert w.alt_model.isEnabled()
    finally: w.close()


def test_model_box_lists_installed_local_models(qt_app,sandbox,monkeypatch):
    calls=[]
    def fake_list(self, timeout=4.0):
        calls.append(self.endpoint); return ['gemma3:12b','qwen3:14b']
    from novel_translator.ui import api_dialog as ad
    monkeypatch.setattr(ad.LocalLLMProvider,'list_models',fake_list)
    w,_=_engine_window(monkeypatch,[('local',True),('gemini',False),('openai',False)],
                       local={'enabled':True,'model':'gemma3:12b','endpoint':'http://localhost:11434'})
    try:
        assert _wait(qt_app,lambda: w.alt_model.findData('qwen3:14b')>=0)
        assert w._alt_model_id()=='gemma3:12b' and len(calls)==1
        w._update_ready_state(); qt_app.processEvents(); assert len(calls)==1   # fetched once, not on every refresh
    finally:
        _wait(qt_app,lambda: w._local_models_thread is None); w.close()


def test_api_dialog_has_gemini_model_and_main_box_takes_it(qt_app,sandbox,monkeypatch):
    from novel_translator.ui import api_dialog as ad
    dlg=ad.ApiDialog([{'id':'k','secret':'dummy-value','enabled':True}],'gemini-3.1-flash-lite',{'enabled':False,'model':'gpt-6.1-sol','keys':[]},'')
    try:
        assert dlg.gemini_model.text()=='gemini-3.1-flash-lite'
        assert dlg.gemini_model.findText('gemini-3.8-flash')>=0
        dlg.gemini_model.setText('gemini-3.8-flash'); dlg._save_and_accept()
        assert dlg.settings_payload()['gemini_model']=='gemini-3.8-flash'
    finally: dlg.close()
    w,saved=_engine_window(monkeypatch,[('gemini',True),('openai',False),('local',False)])
    try:
        class FakeDlg:
            def __init__(self,*a,**k):
                self.local_model=ad._ModelCombo([],''); self.local_endpoint=ad.QLineEdit('')
            def exec(self): return True
            def settings_payload(self): return {'gemini':{'keys':[{'id':'k','secret':'dummy-value','enabled':True}]},'engines':[{'id':'gemini','enabled':True}],'gemini_model':'gemini-3.8-flash'}
        monkeypatch.setattr(ui,'ApiDialog',FakeDlg)
        w.open_api()
        assert w._selected_model_id()=='gemini-3.8-flash'
        assert 'gemini_model' not in saved[-1] and 'gemini_model' not in w.secrets
    finally: w.close()


# ---------------------------------------------------------------- v1.57: retry the untranslated parts
def _scripted_worker_class(started, script):
    """Each new worker takes the next (status, failed, leftover) from `script`."""
    Base=_fake_worker_class(started)
    class Scripted(Base):
        def run(self):
            status,failed,leftover=script[min(len(started)-1,len(script)-1)]
            self.out_dir.mkdir(parents=True,exist_ok=True)
            out=self.out_dir/(self.source_path.stem+('.ko.txt' if status=='completed' else '.ko.partial.txt'))
            out.write_text('번역',encoding='utf-8'); self.leftover_count=leftover
            self.result_ready.emit(status,str(out),failed); self.finished_ok.emit(str(out))
    return Scripted


def _single_file(root, name='book.txt'):
    p=root/name; p.write_text('第一章\n\n他静静地看着窗外的雨。',encoding='utf-8'); return p


def test_single_file_partial_asks_and_retries_only_on_yes(qt_app,sandbox,monkeypatch):
    root,_=sandbox; started=[]
    monkeypatch.setattr(ui,'TranslationWorker',_scripted_worker_class(started,[('partial',2,0),('completed',0,0)]))
    w=_window(monkeypatch); asked=[]; infos=[]
    monkeypatch.setattr(w,'_ask_retry_untranslated',lambda msg:(asked.append(w._untranslated_summary()) or True))
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k: infos.append(a[1]))
    try:
        w._open_txt_path(str(_single_file(root))); w.start_translation()
        assert _wait(qt_app,lambda: len(started)==2 and infos), (started,infos)
        assert len(asked)==1 and '2개' in asked[0]          # asked once, then the retry run finished cleanly
        assert infos==['번역 완료'] and w._retry_rounds==0
    finally:
        _wait(qt_app,lambda: not (w.worker and w.worker.isRunning())); w.close()


def test_single_file_no_retry_when_user_says_later(qt_app,sandbox,monkeypatch):
    root,_=sandbox; started=[]
    monkeypatch.setattr(ui,'TranslationWorker',_scripted_worker_class(started,[('partial',1,0)]))
    w=_window(monkeypatch); asked=[]
    monkeypatch.setattr(w,'_ask_retry_untranslated',lambda msg:(asked.append(1) or False))
    try:
        w._open_txt_path(str(_single_file(root))); w.start_translation()
        assert _wait(qt_app,lambda: asked)
        for _ in range(30): qt_app.processEvents()
        assert len(started)==1 and w.start.text()=='번역 시작 / 계속'
    finally: w.close()


def test_completed_with_leftover_script_also_asks(qt_app,sandbox,monkeypatch):
    root,_=sandbox; started=[]
    monkeypatch.setattr(ui,'TranslationWorker',_scripted_worker_class(started,[('completed',0,3)]))
    w=_window(monkeypatch); asked=[]
    monkeypatch.setattr(w,'_ask_retry_untranslated',lambda msg:(asked.append(w._untranslated_summary()) or False))
    try:
        w._open_txt_path(str(_single_file(root))); w.start_translation()
        assert _wait(qt_app,lambda: asked)
        assert '한자·가나가 남은 문단 3개' in asked[0] and '청크' not in asked[0]
    finally: w.close()


def test_clean_completion_does_not_ask(qt_app,sandbox,monkeypatch):
    root,_=sandbox; started=[]
    monkeypatch.setattr(ui,'TranslationWorker',_scripted_worker_class(started,[('completed',0,0)]))
    w=_window(monkeypatch); infos=[]
    monkeypatch.setattr(w,'_ask_retry_untranslated',lambda msg:(_ for _ in ()).throw(AssertionError('asked')))
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k: infos.append(a[1]))
    try:
        w._open_txt_path(str(_single_file(root))); w.start_translation()
        assert _wait(qt_app,lambda: infos==['번역 완료'])
    finally: w.close()


def test_retry_prompt_dialog_text(qt_app,sandbox,monkeypatch):
    w=_window(monkeypatch)
    try:
        seen={}
        def fake_exec(box):
            seen['text']=box.text(); seen['buttons']=[b.text() for b in box.buttons()]; return 0
        monkeypatch.setattr(ui.QMessageBox,'exec',fake_exec)
        w._last_failed=2; w._last_leftover=1
        assert w._ask_retry_untranslated('detail') is False
        assert '미번역 부분을 재시도할까요?' in seen['text'] and '재시도' in seen['buttons'] and '나중에' in seen['buttons']
    finally: w.close()


def test_folder_retries_each_partial_file_once_then_moves_on(qt_app,sandbox,monkeypatch):
    root,_=sandbox; books=root/'books'; books.mkdir()
    for n in ('1.txt','2.txt'): (books/n).write_text('第一章\n\n他静静地看着窗外的雨。',encoding='utf-8')
    started=[]
    # 1.txt: partial, retry still partial → move on;  2.txt: completed with no leftovers
    monkeypatch.setattr(ui,'TranslationWorker',_scripted_worker_class(started,[('partial',1,0),('partial',1,0),('completed',0,0)]))
    w=_window(monkeypatch)
    try:
        w._prepare_new_input(); w.input_mode='folder'; w.batch_root=books
        w._apply_profile('zh-ko'); w._scan_batch_files(); w._update_ready_state()
        w.start_translation()
        assert _wait(qt_app,lambda: w.last_result_path is not None and w.last_result_path.name=='번역_요약.txt')
        assert [s.source_path.name for s in started]==['1.txt','1.txt','2.txt']
        assert '미번역 부분 자동 재시도' in w.error_view.toPlainText()
    finally: w.close()


def test_folder_continuous_retries_before_stopping(qt_app,sandbox,monkeypatch):
    root,_=sandbox; books=root/'books'; books.mkdir()
    for n in ('1.txt','2.txt'): (books/n).write_text('第一章\n\n他静静地看着窗外的雨。',encoding='utf-8')
    started=[]
    # the retry fixes 1.txt, so the continuous queue goes on to 2.txt
    monkeypatch.setattr(ui,'TranslationWorker',_scripted_worker_class(started,[('partial',1,0),('completed',0,0),('completed',0,0)]))
    w=_window(monkeypatch)
    try:
        w._prepare_new_input(); w.input_mode='folder'; w.batch_root=books
        w._apply_profile('zh-ko'); w._scan_batch_files()
        w.batch_relation.setCurrentIndex(w.batch_relation.findData('continuous')); w._update_ready_state()
        w.start_translation()
        assert _wait(qt_app,lambda: w.last_result_path is not None and w.last_result_path.name=='번역_요약.txt')
        assert [s.source_path.name for s in started]==['1.txt','1.txt','2.txt']
    finally: w.close()


# ---------------------------------------------------------------- v1.57: finer retry of chunks that failed before
def _two_chunk_text():
    a='\n\n'.join(f'彼は窓の外の雨を静かに見ていた。その日は長い一日だった{i}。' for i in range(4))
    b='\n\n'.join(f'彼女は扉の前でしばらく立ち止まっていた。風が冷たかった{i}。' for i in range(4))
    return a+'\n\n'+b


def _target_chunk(chunks):
    multi=[c for c in chunks if len(c.paragraphs)>1]
    assert multi and multi[-1].index>0
    return multi[-1]


def _run_failed_then_retry(tmp_path, first_kind):
    from test_refusal_recovery import ScriptedProvider
    text=_two_chunk_text()
    # run 1: every request touching the target chunk fails with `first_kind`
    holder={}
    def fail(prompt,src,body):
        if any(f'<P{p.pid:06d}>' in src for p in holder['t'].paragraphs):
            raise ProviderError(first_kind,'blocked for the test')
    p1=ScriptedProvider(fail)
    w1,chunks,_=make_worker(tmp_path,text,p1)
    holder['t']=t=_target_chunk(chunks)
    w1.run()
    st={r['chunk_index']:(r['status'],r['error_type']) for r in rows(tmp_path)}
    # run 2: the engine works now; record what is asked
    p2=ScriptedProvider()
    w2,chunks2,events=make_worker(tmp_path,text,p2)
    w2.run()
    asked=[r.user_prompt for r in p2.requests if any(f'<P{p.pid:06d}>' in _src(r.user_prompt) for p in t.paragraphs)]
    return st[t.index],t,asked,events,rows(tmp_path)


def _src(prompt):
    from test_refusal_recovery import _source
    return _source(prompt)


def test_retry_of_content_blocked_chunk_starts_fine_grained(tmp_path):
    (status,kind),t,asked,events,final=_run_failed_then_retry(tmp_path,ErrorKind.REFUSAL)
    assert status=='FAILED' and kind=='REFUSAL'
    first=asked[0]
    n_first=len(re.findall(r'<P\d{6}>',_src(first)))
    assert n_first < len(t.paragraphs)                    # split from the first request
    assert _ctx(first).split(']',1)[1].strip().startswith('(none)')   # no earlier scene
    assert '[RECOVERY NOTE]' in first
    assert any('더 세밀하게 다시 요청' in e for e in events)
    assert all(r['status']=='COMPLETED' for r in final)


def test_retry_after_infrastructure_failure_keeps_the_normal_path(tmp_path):
    (status,kind),t,asked,events,final=_run_failed_then_retry(tmp_path,ErrorKind.TRANSIENT)
    first=asked[0]
    assert len(re.findall(r'<P\d{6}>',_src(first)))==len(t.paragraphs)   # whole chunk, as usual
    assert not _ctx(first).split(']',1)[1].strip().startswith('(none)')   # with the earlier scene, as usual
    assert not any('더 세밀하게 다시 요청' in e for e in events)
    assert all(r['status']=='COMPLETED' for r in final)


def _ctx(prompt):
    from test_refusal_recovery import _context
    return _context(prompt)


def test_fine_retry_reason_parsing():
    W=worker_mod.TranslationWorker
    assert W._fine_retry_reason('POLICY')=='content' and W._fine_retry_reason('REFUSAL/POLICY')=='content'
    assert W._fine_retry_reason('FORMAT')=='format' and W._fine_retry_reason('FORMAT/SAFETY')=='content'
    assert W._fine_retry_reason('QUOTA')=='' and W._fine_retry_reason(None)==''


# ---------------------------------------------------------------- v1.57+: open results in 야문 클린리더
def test_cleanreader_opens_single_result_and_remembers_location(qt_app,sandbox,monkeypatch):
    root,_=sandbox; started=[]
    monkeypatch.setattr(ui,'TranslationWorker',_scripted_worker_class(started,[('completed',0,0)]))
    w=_window(monkeypatch)
    exe=root/'tools'/'YamoonCleanReader.exe'; exe.parent.mkdir(); exe.write_bytes(b'MZ')
    picks=[]; launched=[]
    monkeypatch.setattr(ui.QFileDialog,'getOpenFileName',lambda *a,**k:(picks.append(1) or (str(exe),'')))
    monkeypatch.setattr(ui.subprocess,'Popen',lambda args,**k: launched.append(args))
    try:
        assert not w.cleanreader_btn.isEnabled()
        w._open_txt_path(str(_single_file(root))); w.start_translation()
        assert _wait(qt_app,lambda: w.cleanreader_btn.isEnabled() and not (w.worker and w.worker.isRunning()))
        w._open_in_cleanreader(); w._open_in_cleanreader()
        assert len(picks)==1                                    # asked once, then remembered
        assert launched[0][0]==str(exe) and launched[0][1].endswith('book.ko.txt') and len(launched)==2
    finally: w.close()


def test_cleanreader_folder_passes_all_results_not_the_summary(qt_app,sandbox,monkeypatch):
    root,_=sandbox; books=root/'books'; books.mkdir()
    for n in ('1.txt','2.txt'): (books/n).write_text('第一章\n\n他静静地看着窗外的雨。',encoding='utf-8')
    started=[]; monkeypatch.setattr(ui,'TranslationWorker',_scripted_worker_class(started,[('completed',0,0)]))
    w=_window(monkeypatch)
    exe=root/'YamoonCleanReader.exe'; exe.write_bytes(b'MZ')
    monkeypatch.setattr(w,'_find_cleanreader',lambda: exe)
    launched=[]; monkeypatch.setattr(ui.subprocess,'Popen',lambda args,**k: launched.append(args))
    try:
        w._prepare_new_input(); w.input_mode='folder'; w.batch_root=books
        w._apply_profile('zh-ko'); w._scan_batch_files(); w._update_ready_state(); w.start_translation()
        assert _wait(qt_app,lambda: w.last_result_path is not None and w.last_result_path.name=='번역_요약.txt')
        w._open_in_cleanreader()
        names=sorted(Path(a).name for a in launched[0][1:])
        assert names==['1.ko.txt','2.ko.txt']
    finally: w.close()


def test_cleanreader_cancelled_pick_does_nothing(qt_app,sandbox,monkeypatch):
    w=_window(monkeypatch)
    monkeypatch.setattr(w,'_find_cleanreader',lambda: None)
    monkeypatch.setattr(ui.QFileDialog,'getOpenFileName',lambda *a,**k:('',''))
    monkeypatch.setattr(ui.subprocess,'Popen',lambda *a,**k:(_ for _ in ()).throw(AssertionError('launched')))
    try: w._open_in_cleanreader()
    finally: w.close()


# ---------------------------------------------------------------- 인물 말투표
from novel_translator import speech_levels as sl

_NAMES={'左京':'좌경','李萱诗':'이훤시','白颖':'백영'}
_BOOK=('第一章\n\n'+'\n\n'.join(
    ['左京推门进来。','“妈，我回来了。”','李萱诗笑着说：“京京，快坐。”','白颖看着左京。','“老公，你吃饭了吗？”','左京点头：“吃了，你别担心。”']*30))


def test_sample_dialogue_prefers_named_dialogue_and_is_capped():
    s=sl.sample_dialogue(_BOOK,_NAMES,limit=600)
    assert 0 < len(s) <= 600
    assert '“' in s and ('左京' in s or '李萱诗' in s)
    assert sl.sample_dialogue('没有对话的文本。',_NAMES)=='' and sl.sample_dialogue(_BOOK,{})==''


def test_parse_table_is_strict():
    text='\n'.join(['<think>음</think>',
        '좌경\t이훤시\t해요체\t어머니',
        '이훤시 | 좌경 | 반말 | 아들',
        '白颖\t左京\t반말(해체)\t아내',          # original names map to Korean
        '좌경\t좌경\t반말\t자기 자신',            # self pair
        '좌경\t왕대장\t합쇼체\t상관',             # unknown person
        '좌경\t백영\t존댓말?\t',                  # alias → 해요체
        '좌경\t이훤시\t반말\t중복',               # duplicate pair
        '백영\t이훤시\t아무말\t'])                # bad level
    rows=sl.parse_table(text,_NAMES)
    assert [(r['speaker'],r['listener'],r['level']) for r in rows]==[
        ('좌경','이훤시','해요체'),('이훤시','좌경','반말'),('백영','좌경','반말'),('좌경','백영','해요체')]


def test_speech_table_reaches_the_prompt_and_not_the_job_identity(tmp_path):
    from novel_translator.language_options import prompt_instructions
    rows=[{'speaker':'좌경','listener':'이훤시','level':'해요체','note':'어머니'}]
    text='\n'.join(prompt_instructions({sl.SPEECH_KEY:rows}))
    assert 'CHARACTER SPEECH LEVELS' in text and '좌경 → 이훤시: 해요체 (어머니)' in text
    w,chunks,_=make_worker(tmp_path,'彼は窓の外を見た。\n\n「行こう」と彼女は言った。',ScriptedProvider())
    fp=w._fingerprint(); key=w._settings_key()
    w.speech_table=sl.clean_rows(rows)
    assert w._fingerprint()==fp and w._settings_key()==key      # editing the table never restarts a book
    (tmp_path/'b').mkdir(); p=ScriptedProvider(); w2,_,_=make_worker(tmp_path/'b','彼は窓の外を見た。',p)
    w2.speech_table=sl.clean_rows(rows); w2.run()
    assert p.requests and '좌경 → 이훤시: 해요체' in p.requests[0].system_prompt+p.requests[0].user_prompt


def _speech_window(monkeypatch, root, reply):
    """Window with a file open, names in the glossary, and a fake extraction thread."""
    w=_window(monkeypatch)
    calls=[]
    class FakeProvider:
        def __init__(self,pid): self.pid=pid
        def translate(self,req,secret):
            calls.append((self.pid,req.user_prompt))
            out=reply(self.pid,req) if callable(reply) else reply
            if isinstance(out,Exception): raise out
            return TranslationResponse(out,'STOP',{})
    monkeypatch.setattr(ui,'_make_provider',lambda pid,*a,**k: FakeProvider(pid))
    engines=[('gemini','k','m','',None)]
    monkeypatch.setattr(w,'_extraction_engines',lambda: list(engines))
    w._test_engines=engines
    p=root/'book.txt'; p.write_text(_BOOK,encoding='utf-8')
    w._open_txt_path(str(p)); w._apply_profile('zh-ko')
    for a,b in _NAMES.items():
        r=w.term_table.rowCount(); w.term_table.insertRow(r); w.term_table.setItem(r,0,QTableWidgetItem(a)); w.term_table.setItem(r,1,QTableWidgetItem(b))
    return w,calls


def test_speech_table_is_built_before_the_first_translation_and_used(qt_app,sandbox,monkeypatch):
    root,_=sandbox; started=[]
    monkeypatch.setattr(ui,'TranslationWorker',_scripted_worker_class(started,[('completed',0,0)]))
    got={}
    Orig=ui.TranslationWorker
    class Capture(Orig):
        def __init__(self,*a,**k): got['speech']=k.get('speech_table'); super().__init__(*a,**k)
    monkeypatch.setattr(ui,'TranslationWorker',Capture)
    w,calls=_speech_window(monkeypatch,root,'좌경\t이훤시\t해요체\t어머니\n이훤시\t좌경\t반말\t아들')
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k:None)
    try:
        w.start_translation()
        assert _wait(qt_app,lambda: started and not (w.worker and w.worker.isRunning()))
        assert calls and all('[DIALOGUE EXCERPTS' in u for _,u in calls)
        assert [(r['speaker'],r['level']) for r in got['speech']]==[('좌경','해요체'),('이훤시','반말')]
        assert w.tabs.tabText(w.tabs.indexOf(w.speech_tab))=='인물 말투 (2)'
        assert '인물 말투표 자동 생성 · 2쌍' in w.error_view.toPlainText()
        # a second start does not ask again
        n=len(calls); w.start_translation(); assert _wait(qt_app,lambda: len(started)==2 and not w.worker.isRunning()); assert len(calls)==n
    finally: w.close()


def test_speech_failure_never_blocks_the_translation(qt_app,sandbox,monkeypatch):
    root,_=sandbox; started=[]
    monkeypatch.setattr(ui,'TranslationWorker',_scripted_worker_class(started,[('completed',0,0)]))
    w,calls=_speech_window(monkeypatch,root,ProviderError(ErrorKind.POLICY,'blocked'))
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k:None)
    try:
        w.start_translation()
        assert _wait(qt_app,lambda: started and not (w.worker and w.worker.isRunning()))
        assert '말투표 만들기 실패' in w.error_view.toPlainText() and w._speech_rows()==[]
    finally: w.close()


def test_speech_table_is_saved_per_book_and_cleared_for_a_new_one(qt_app,sandbox,monkeypatch):
    root,_=sandbox
    w,_=_speech_window(monkeypatch,root,'')
    try:
        w._set_speech_rows([{'speaker':'좌경','listener':'이훤시','level':'합쇼체','note':''}]); w._save_workspace()
        other=root/'other.txt'; other.write_text('另一本书。',encoding='utf-8')
        w._open_txt_path(str(other)); assert w._speech_rows()==[]
        w._open_txt_path(str(root/'book.txt'))
        assert [(r['speaker'],r['level']) for r in w._speech_rows()]==[('좌경','합쇼체')]
    finally: w.close()


def test_manual_generate_needs_names(qt_app,sandbox,monkeypatch):
    w=_window(monkeypatch); infos=[]
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k: infos.append(a[1]))
    try:
        w._generate_speech(); assert infos==['인물 이름 필요']
    finally: w.close()


# ---------------------------------------------------------------- 내 공용 용어집 · 언어팩 보충
from novel_translator import common_terms as ct


def test_common_terms_accumulate_without_overwriting(tmp_path):
    a=ct.add('zh',{'左京':'좌경','青云门':'청운문','公司':'회사','郝长华':'郝장화','王':'왕','李':'x'},tmp_path)
    assert a==[('左京','좌경'),('青云门','청운문')]                     # common word / Han left / 1 char skipped
    assert ct.add('zh',{'左京':'쭤징','白颖':'백영'},tmp_path)==[('白颖','백영')]
    assert ct.load('zh',tmp_path)=={'左京':'좌경','青云门':'청운문','白颖':'백영'}   # first rendering kept
    assert ct.load('ja',tmp_path)=={}
    assert ct.present_in('青云门来了。青云门走了。左京',ct.load('zh',tmp_path))=={'青云门':'청운문'}


def test_two_character_shared_entries_need_five_occurrences(tmp_path):
    ct.add('zh',{'左京':'좌경','青云门':'청운문'},tmp_path); e=ct.load('zh',tmp_path)
    assert ct.present_in('左京'*4+'青云门'*2,e)=={'青云门':'청운문'}       # 2 chars x4: not yet; 3 chars x2: yes
    assert ct.present_in('左京'*5+'青云门',e)=={'左京':'좌경'}            # 2 chars x5: yes; 3 chars x1: no
    ct.add('ja',{'春日':'가스가','山田太郎':'야마다 타로'},tmp_path)
    assert ct.present_in('春日。春日。山田太郎。山田太郎。',ct.load('ja',tmp_path))=={'山田太郎':'야마다 타로'}


def test_full_shared_glossary_stops_growing_and_reports_what_is_left(tmp_path,monkeypatch):
    monkeypatch.setattr(ct,'MAX_PER_LANG',3)
    terms={'左京':'좌경','青云门':'청운문','白颖':'백영','李萱诗':'이훤시','郝江化':'학강화'}
    wanted=ct.new_candidates('zh',terms,tmp_path); assert len(wanted)==5
    added=ct.add('zh',wanted,tmp_path)
    assert len(added)==3 and ct.is_full('zh',tmp_path) and not ct.is_full('ja',tmp_path)
    assert ct.add('zh',{'郝江化':'학강화'},tmp_path)==[]
    assert ct.new_candidates('zh',terms,tmp_path)=={'李萱诗':'이훤시','郝江化':'학강화'}


def test_supplement_pack_and_shared_hints_reach_the_prompt_without_changing_identity(tmp_path):
    import copy
    from novel_translator.lexical_guidance import relevant_guidance
    from novel_translator.profiles import ProfileManager
    p=copy.copy(ProfileManager().set_active('zh-ko')); object.__setattr__(p,'lexical_extra',{'青云门':'청운문','公公':'틀린값'})
    g=relevant_guidance(p,'公公说青云门的岳父来了')
    assert g['青云门']=='청운문' and g['岳父']=='장인' and '환관' in g['公公']     # pack wins over the shared list
    w1,_,_=make_worker(tmp_path,'彼は窓の外を見た。',ScriptedProvider())
    fp=w1._fingerprint(); key=w1._settings_key()
    (tmp_path/'b').mkdir(); prov=ScriptedProvider()
    w2,chunks,_=make_worker(tmp_path/'b','彼は窓の外を見た。',prov)
    w3=worker_mod.TranslationWorker(w2.source_path,w2.source_bytes,w2.profile,chunks,'gemini-3.8-flash',
        [{'id':'fake','secret':'not-real','project_group':'P1','enabled':True}],{},'balanced',tmp_path/'b'/'out',common_terms={'窓':'창문(공용)'})
    assert w3._fingerprint()==w1._fingerprint() and w3._settings_key()==w1._settings_key()==key
    w3.run()
    assert any('창문(공용)' in r.user_prompt for r in prov.requests)


def test_finished_book_grows_the_shared_glossary_and_next_book_pulls_it(qt_app,sandbox,monkeypatch):
    root,_=sandbox; started=[]
    monkeypatch.setattr(ui,'TranslationWorker',_scripted_worker_class(started,[('completed',0,0)]))
    w,calls=_speech_window(monkeypatch,root,'')          # glossary has 左京/李萱诗/白颖
    w.auto_speech.setChecked(False)
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k:None)
    try:
        w.start_translation()
        assert _wait(qt_app,lambda: '작업 끝' in w.error_view.toPlainText())   # the finished signals were handled
        assert {'左京':'좌경','李萱诗':'이훤시','白颖':'백영'}.items() <= ct.load('zh',ui.DATA_DIR).items()
        # a new book (sequel) that mentions 左京 twice: the extraction pulls it in first
        seq=root/'seq.txt'; seq.write_text('左京回家了。\n\n左京笑了。\n\n李萱诗来了。\n\n李萱诗走了。\n\n白颖笑了。\n\n他们很好。',encoding='utf-8')
        w._open_txt_path(str(seq)); assert w._terms()=={}
        pulled=w._pull_common_terms(w.source_text)   # 2-char 左京 x2 stays out; 3-char 李萱诗 x2 comes in
        assert pulled==[('李萱诗','이훤시')] and w._terms()=={'李萱诗':'이훤시'}
        seq.write_text('左京回家了。\n\n'*5,encoding='utf-8'); w._open_txt_path(str(seq))
        assert w._pull_common_terms(w.source_text)==[('左京','좌경')]
    finally: w.close()


def test_shared_glossary_off_neither_grows_nor_pulls(qt_app,sandbox,monkeypatch):
    root,_=sandbox; started=[]
    monkeypatch.setattr(ui,'TranslationWorker',_scripted_worker_class(started,[('completed',0,0)]))
    w,_=_speech_window(monkeypatch,root,''); w.auto_speech.setChecked(False); w.use_common.setChecked(False)
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k:None)
    try:
        w.start_translation()
        assert _wait(qt_app,lambda: '작업 끝' in w.error_view.toPlainText())   # the finished signals were handled
        assert ct.load('zh',ui.DATA_DIR)=={} and w._pull_common_terms('左京左京')==[] and w._common_terms_for_job()=={}
    finally: w.close()


def test_speech_blocked_batch_moves_to_the_next_engine(qt_app,sandbox,monkeypatch):
    """Gemini refuses every batch; the local LLM (next in the order) builds the table."""
    root,_=sandbox; started=[]
    monkeypatch.setattr(ui,'TranslationWorker',_scripted_worker_class(started,[('completed',0,0)]))
    def reply(pid,req):
        if pid=='gemini': return ProviderError(ErrorKind.POLICY,'입력 차단: PROHIBITED_CONTENT')
        return '좌경\t이훤시\t해요체\t어머니'
    w,calls=_speech_window(monkeypatch,root,reply)
    w._test_engines.append(('local','local','gemma','',{}))
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**k:None)
    try:
        w.start_translation()
        assert _wait(qt_app,lambda: '작업 끝' in w.error_view.toPlainText())
        assert [r['speaker'] for r in w._speech_rows()]==['좌경']
        assert '로컬 LLM' in w.error_view.toPlainText()
        assert {p for p,_ in calls}=={'gemini','local'} and len([1 for p,_ in calls if p=='gemini'])==1   # full context tried once; local then succeeded
    finally: w.close()


def test_speech_samples_are_split_into_batches_and_prefer_address_terms():
    s=sl.sample_dialogue(_BOOK,_NAMES,cues=['妈','老公'])
    parts=sl.split_samples(s)
    assert 1 < len(parts) <= 3 and all(len(p) < len(s) for p in parts)
    assert '妈' in s and '老公' in s


def test_speech_table_stays_scrollable_while_locked(qt_app,sandbox,monkeypatch):
    w=_window(monkeypatch)
    try:
        w._set_speech_rows([{'speaker':'좌경','listener':'이훤시','level':'해요체','note':''}])
        w._lock_inputs(True)
        assert w.speech_table.isEnabled() and not w.speech_table.cellWidget(0,2).isEnabled()
        assert not w.speech_add.isEnabled()
        w._lock_inputs(False)
        assert w.speech_table.cellWidget(0,2).isEnabled() and w.speech_add.isEnabled()
    finally: w.close()


def test_zh_candidates_keep_prefixed_and_four_character_names():
    from novel_translator.term_extraction import candidates, parse_glossary
    t=('小雪笑着说：“好。”欧阳明月看着小雪。老王点头。左京说：“走吧。”阿强问：“去哪？”白颖低声说。'*10)
    c=[k for k,_ in candidates(t,'zh')]
    for n in ('小雪','欧阳明月','老王','左京','阿强','白颖'): assert n in c, n
    assert not any(x in c for x in ('白颖低','老王点','小雪笑','颖低'))
    # a full name returned for a broken candidate is kept
    assert parse_glossary('{"欧阳明月":"구양명월"}',[('阳明月',9)])=={'欧阳明月':'구양명월'}


def test_a_broken_profile_file_does_not_stop_the_program(tmp_path,monkeypatch):
    import shutil
    from novel_translator import profiles as pr
    d=tmp_path/'profiles'; shutil.copytree(pr.PROFILE_DIR,d)
    (d/'ja-ko.json').write_text('{"id": "ja-ko", "display_name": "x",,}',encoding='utf-8')
    monkeypatch.setattr(pr,'PROFILE_DIR',d)
    pm=pr.ProfileManager()
    assert 'zh-ko' in {p.id for p in pm.all()} and 'ja-ko' not in {p.id for p in pm.all()}
    assert pm.load_errors and 'ja-ko.json' in pm.load_errors[0] and '번째 줄' in pm.load_errors[0]


def test_guidelines_big_editor_saves(qt_app,sandbox,monkeypatch):
    from PySide6.QtWidgets import QDialog, QPlainTextEdit
    w=_window(monkeypatch)
    def fake_exec(d):
        d.findChild(QPlainTextEdit).setPlainText('- 대사는 구어체로'); return 1
    monkeypatch.setattr(QDialog,'exec',fake_exec)
    try:
        assert w.user_guide.minimumHeight()>=170
        w._edit_guide_big()
        assert w.user_guide.toPlainText()=='- 대사는 구어체로'
        from novel_translator.user_guidelines import load_user_guidelines
        assert load_user_guidelines(w.pm.active.id, ui.DATA_DIR)=='- 대사는 구어체로'
    finally: w.close()


def test_shared_glossary_dialog_previews_and_adds_now(qt_app,sandbox,monkeypatch):
    from PySide6.QtWidgets import QDialog, QPushButton
    root,_=sandbox
    w,_=_speech_window(monkeypatch,root,'')
    seen={}
    def fake_exec(d):
        btn=[b for b in d.findChildren(QPushButton) if b.text()=='지금 추가'][0]
        seen['enabled']=btn.isEnabled(); btn.click(); return 1
    monkeypatch.setattr(QDialog,'exec',fake_exec)
    try:
        w._open_common_terms()
        assert seen['enabled'] and ct.load('zh',ui.DATA_DIR)=={'左京':'좌경','李萱诗':'이훤시','白颖':'백영'}
        assert w.common_btn.text()=='공용 용어집 (3)'
    finally: w.close()


def test_full_shared_glossary_tells_the_user_to_tidy_up(qt_app,sandbox,monkeypatch):
    from PySide6.QtWidgets import QDialog, QLabel, QPushButton
    root,_=sandbox
    w,_=_speech_window(monkeypatch,root,'')          # glossary has 左京/李萱诗/白颖
    monkeypatch.setattr(ct,'MAX_PER_LANG',2)
    seen={}
    def fake_exec(d):
        seen['note']=[l.text() for l in d.findChildren(QLabel) if l.objectName()=='commonFullNote']
        seen['add']=[b for b in d.findChildren(QPushButton) if b.text()=='지금 추가'][0].isEnabled()
        return 0
    monkeypatch.setattr(QDialog,'exec',fake_exec)
    try:
        w._grow_common_terms()
        log=w.error_view.toPlainText()
        assert len(ct.load('zh',ui.DATA_DIR))==2 and '가득 찼습니다' in log and '1개를 쌓지 못함' in log and '정리하세요' in log
        assert w.common_btn.text()=='공용 용어집 (2 · 가득 참)' and '정리' in w.common_btn.toolTip()
        w._open_common_terms()
        assert seen['note'] and '2 / 2개' in seen['note'][0] and seen['add'] is False
        monkeypatch.setattr(ct,'MAX_PER_LANG',5000); w._update_common_btn()
        assert w.common_btn.text()=='공용 용어집 (2)' and w.common_btn.toolTip()==''
    finally: w.close()
