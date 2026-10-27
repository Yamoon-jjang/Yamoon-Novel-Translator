"""End-to-end, provider-independent Korean literary-edit safeguards."""
import re
import pytest
from novel_translator.chunking import split_paragraphs
from novel_translator.refinement_runtime import (model_reply_to_text, group_paragraphs,
                                                   run_refinement, assemble)


class Dummy:
    name='dummy'
    def __init__(self, bad_group=False):
        self.calls=0
        self.bad_group=bad_group
    def translate(self, request, key):
        from novel_translator.providers.base import TranslationResponse
        self.calls+=1
        assert key=='test'
        hits=list(re.finditer(r'(?m)^<P(\d{6})>\n([^\n]*)',request.user_prompt))
        if self.bad_group and len(hits)>1:
            return TranslationResponse('<P999999>\n실패')
        return TranslationResponse('\n\n'.join('<P'+m.group(1)+'>\n'+m.group(2).replace('어색한','자연스러운')
                                              for m in hits))


def test_reply_rejects_missing_out_of_order_and_short():
    paragraphs=split_paragraphs('첫 문단입니다.\n둘째 문단입니다.')
    assert model_reply_to_text('<P000001>\n좋은 문단입니다.\n<P000002>\n둘째 문단입니다.',paragraphs)==[
        '좋은 문단입니다.','둘째 문단입니다.']
    for bad in ('<P000001>\n한 줄뿐입니다.', '<P000002>\n둘째 문단입니다.\n<P000001>\n첫 문단입니다.',
                '교정 내용입니다.\n<P000001>\n첫 문단입니다.\n<P000002>\n둘째 문단입니다.'):
        with pytest.raises(ValueError):
            model_reply_to_text(bad, paragraphs)


def test_real_text_file_edit_respects_source_and_existing_result(tmp_path):
    src=tmp_path/'novel.ko.txt'
    original='어색한 첫 문단입니다.\r\n\r\n어색한 둘째 문단입니다.\r\n'
    src.write_bytes(original.encode('utf-8'))
    output=tmp_path/'novel.ko.refined.txt'
    mock=Dummy()
    result=run_refinement(src,output,mock,'model','test',checkpoint_dir=tmp_path/'cache')
    assert result['status']=='completed'
    assert result['issues']==0
    assert '자연스러운 첫' in output.read_text(encoding='utf-8-sig')
    assert src.read_bytes()==original.encode('utf-8')
    assert '\n\n' in output.read_text(encoding='utf-8-sig').replace('\r\n','\n')
    with pytest.raises(FileExistsError):
        run_refinement(src,output,Dummy(),'model','test',checkpoint_dir=tmp_path/'cache')
    assert result['report'].endswith('.review.txt')


def test_bad_batch_fallback_to_single_requests(tmp_path):
    src=tmp_path/'a.txt';src.write_text('어색한 첫 문단입니다.\n어색한 둘째 문단입니다.',encoding='utf-8')
    output=tmp_path/'a.ko.refined.txt'; mock=Dummy(bad_group=True)
    result=run_refinement(src,output,mock,'model','test',checkpoint_dir=tmp_path/'cache')
    assert result['done']==2 and result['issues']==0
    assert mock.calls==4  # one work-wide analysis plus 3 editing attempts


def test_cancel_resume_without_repeating_accepted_work(tmp_path):
    src=tmp_path/'large.txt';src.write_text('\n'.join('어색한 문단입니다. '+str(i) for i in range(8)),encoding='utf-8')
    output=tmp_path/'large.ko.refined.txt'; p=Dummy()
    stopped=run_refinement(src,output,p,'model','test',checkpoint_dir=tmp_path/'cache',
                           max_items=2,stop=lambda:p.calls>=2)
    assert stopped['status']=='stopped'
    assert not output.exists()
    n=p.calls
    resume=run_refinement(src,output,p,'model','test',checkpoint_dir=tmp_path/'cache',max_items=2)
    assert resume['status']=='completed'
    assert p.calls==n+3
    assert not list((tmp_path/'cache').glob('*.json'))


def test_source_output_same_path_rejected(tmp_path):
    p=tmp_path/'same.txt';p.write_text('본문',encoding='utf-8')
    with pytest.raises(ValueError):
        run_refinement(p,p,Dummy(),'model','test')

def test_refusal_stops_without_repeating_paid_requests(tmp_path):
    from novel_translator.providers.base import ProviderError, ErrorKind
    class Refusing:
        name='refusal-test'
        def __init__(self): self.calls=0
        def translate(self,request,key):
            self.calls+=1
            raise ProviderError(ErrorKind.REFUSAL,'요청 거부')
    source=tmp_path/'refusal.txt'
    source.write_text('첫 문단입니다.\n둘째 문단입니다.',encoding='utf-8')
    p=Refusing()
    analysis=Dummy()
    result=run_refinement(source,tmp_path/'refusal.ko.refined.txt',p,'m','test',checkpoint_dir=tmp_path/'cp',analysis_engine=(analysis,'analysis-model','test'))
    assert p.calls==1
    assert result['done']==0 and result['issues']==2
    assert (tmp_path/'refusal.ko.refined.txt').read_text(encoding='utf-8-sig')==source.read_text(encoding='utf-8')
    assert '모델 정책 거부' in (tmp_path/'refusal.ko.refined.txt.review.txt').read_text(encoding='utf-8-sig')


def test_scene_chunks_group_dialogue_without_four_paragraph_cap():
    text='\n'.join('“대사는 장면의 흐름을 유지해야 합니다.” '+str(i) for i in range(120))
    paragraphs=split_paragraphs(text)
    groups=group_paragraphs(paragraphs)
    assert sum(map(len,groups))==120
    assert len(groups)<10
    assert any(len(group)>4 for group in groups)
    assert [p.pid for group in groups for p in group]==list(range(1,121))


def test_large_source_is_rejected_before_model_or_input_read(tmp_path):
    from novel_translator.refinement_runtime import MAX_REFINEMENT_BYTES
    src=tmp_path/'too_large.txt'
    with src.open('wb') as out:
        out.truncate(MAX_REFINEMENT_BYTES)
    provider=Dummy()
    with pytest.raises(ValueError,match='10MB'):
        run_refinement(src,tmp_path/'out.txt',provider,'model','test')
    assert provider.calls==0


def test_long_group_format_fallback_bisects_not_one_request_per_line(tmp_path):
    src=tmp_path/'long_scene.txt'
    src.write_text('\n'.join('어색한 한국어 소설 장면입니다 '+str(i) for i in range(16)),encoding='utf-8')
    class FailsLarge(Dummy):
        def translate(self,request,key):
            self.calls+=1
            hits=list(re.finditer(r'(?m)^<P(\d{6})>\n([^\n]*)',request.user_prompt))
            if len(hits)>4:
                from novel_translator.providers.base import TranslationResponse
                return TranslationResponse('<P999999>\n무효')
            from novel_translator.providers.base import TranslationResponse
            return TranslationResponse('\n\n'.join('<P'+m.group(1)+'>\n'+m.group(2).replace('어색한','자연스러운') for m in hits))
    provider=FailsLarge()
    result=run_refinement(src,tmp_path/'out.txt',provider,'model','test',checkpoint_dir=tmp_path/'jobs',max_chars=5000)
    assert result['done']==16 and result['issues']==0
    assert provider.calls==8  # one work-wide analysis plus binary editing recovery


def test_checkpoint_rejects_prior_small_chunk_pipeline(tmp_path):
    from novel_translator.refinement_runtime import checkpoint_path
    from novel_translator.config import save_json
    src=tmp_path/'book.txt'
    src.write_text('어색한 한국어입니다.\n어색한 한국어입니다.',encoding='utf-8')
    cp=checkpoint_path(src,tmp_path/'jobs')
    save_json(cp,{'schema':1,'identity':'old-rule-id','completed':{'1':'변조된 내용'},'issues':{}})
    result=run_refinement(src,tmp_path/'done.txt',Dummy(),'model','test',checkpoint_dir=tmp_path/'jobs')
    assert result['done']==2
    assert '변조된 내용' not in (tmp_path/'done.txt').read_text(encoding='utf-8-sig')


def test_dual_engine_guidance_is_forwarded_into_editing_prompt(tmp_path):
    from novel_translator.providers.base import TranslationResponse
    class Analysis:
        name='analysis'
        def __init__(self):self.calls=0
        def translate(self,req,key):
            self.calls+=1
            assert '인물' in req.system_prompt
            return TranslationResponse('은지 → 민수: 반말. 이름은 은지, 민수.')
    class Editing(Dummy):
        name='editing'
        def translate(self,req,key):
            assert '은지 → 민수: 반말' in req.user_prompt
            return super().translate(req,key)
    src=tmp_path/'novel.txt'
    src.write_text('어색한 은지의 문장입니다.\n어색한 민수의 문장입니다.',encoding='utf-8')
    a=Analysis();e=Editing()
    result=run_refinement(src,tmp_path/'result.txt',e,'editing-model','test',
                          analysis_engine=(a,'analysis-model','test'),checkpoint_dir=tmp_path/'cp')
    assert result['done']==2 and a.calls==1
    assert '자연스러운' in (tmp_path/'result.txt').read_text(encoding='utf-8-sig')

def test_natural_language_refusal_is_not_saved_as_novel_content(tmp_path):
    from novel_translator.providers.base import TranslationResponse
    from novel_translator.refinement_runtime import is_refusal_notice, RefinementRefused
    notice='가족 간의 노골적인 성적 묘사가 포함된 문장은 다듬어 드릴 수 없습니다. 성적 묘사를 제외한 부분의 문장 교정은 도와드릴 수 있습니다.'
    assert is_refusal_notice(notice)
    class RefusalText:
        name='refusal-text'
        def __init__(self): self.calls=0
        def translate(self, req, key):
            self.calls+=1
            return TranslationResponse(notice)
    source=tmp_path/'book.txt'
    original='일반적인 첫 문단입니다.\n일반적인 둘째 문단입니다.'
    source.write_text(original,encoding='utf-8')
    p=RefusalText()
    result=run_refinement(source,tmp_path/'out.txt',p,'model','key',checkpoint_dir=tmp_path/'jobs',
                          analysis_engine=(Dummy(),'analysis','test'))
    assert p.calls==1
    assert result['issues']==2 and result['done']==0
    assert (tmp_path/'out.txt').read_text(encoding='utf-8-sig')==original
    assert notice not in (tmp_path/'out.txt').read_text(encoding='utf-8-sig')


def test_refusal_inside_valid_paragraph_id_is_rejected():
    from novel_translator.refinement_runtime import RefinementRefused, is_refusal_notice
    paragraphs=split_paragraphs('일반적인 문단입니다.')
    with pytest.raises(RefinementRefused):
        model_reply_to_text('<P000001>\n죄송하지만 이 내용을 교정해 드릴 수 없습니다.',paragraphs)
    assert not is_refusal_notice('“저는 이 내용을 교정해 드릴 수 없습니다.”라고 극중 인물이 말했다.')


def test_provider_review_failure_does_not_trigger_recursive_reprocessing(tmp_path):
    from novel_translator.providers.base import TranslationResponse, ProviderError, ErrorKind
    class ReviewFailure:
        name='review-failure'
        def __init__(self):self.calls=0
        def translate(self,req,key):
            self.calls+=1
            if '두 한국어 장면을 비교' in req.system_prompt:
                raise ProviderError(ErrorKind.TRANSIENT,'일시적 검토 오류')
            import re
            hits=list(re.finditer(r'(?m)^<P(\d{6})>\n([^\n]*)',req.user_prompt))
            return TranslationResponse('\n\n'.join('<P'+m.group(1)+'>\n'+(m.group(2)[:max(10,int(len(m.group(2))*.60))]) for m in hits))
    src=tmp_path/'review.txt';original='한 문장이 아주 길게 작성된 예시입니다. '*8
    src.write_text(original,encoding='utf-8')
    p=ReviewFailure()
    res=run_refinement(src,tmp_path/'result.txt',p,'model','key',
                       checkpoint_dir=tmp_path/'jobs',analysis_engine=(Dummy(),'analysis','test'))
    # v2.5: a needed review that could not be done keeps the original (never an unreviewed,
    # possibly shortened edit), without asking again in a loop; it can be retried later.
    assert res['issues']==1 and res['done']==0 and res['changed']==0 and res['preserved']==1 and p.calls==2
    assert (tmp_path/'result.txt').read_text(encoding='utf-8-sig')==original
    assert '연속성 검토 실패(분량 변화) · 원문 보존' in (tmp_path/'result.txt.review.txt').read_text(encoding='utf-8-sig')
    assert res['retry_available']==1

def test_editorial_system_prompt_is_shared_and_preserves_paragraph_contract():
    from novel_translator.refinement_runtime import refinement_prompt
    paragraphs=split_paragraphs('첫 장면입니다.\n둘째 장면입니다.')
    system,user=refinement_prompt(paragraphs,previous='이전 장면',following='다음 장면',
                                  guidance='인물 A는 인물 B에게 존댓말')
    for rule in ('번역투','조사','어미','서술 시점','반말','존댓말','호칭',
                 '문장 구조','줄거리','<P000001>','문단 내부'):
        assert rule in system
    assert '<P000001>' in user and '<P000002>' in user
    assert '이전 장면' in user and '다음 장면' in user
    assert '인물 A는 인물 B에게 존댓말' in user

def test_partial_tagged_refusal_keeps_successful_edit(tmp_path):
    from novel_translator.providers.base import TranslationResponse
    class Mixed:
        name='mixed'
        def __init__(self): self.calls=0
        def translate(self,request,key):
            self.calls+=1
            return TranslationResponse(
                '<P000001>\n자연스럽게 정돈한 첫 문단입니다.\n\n'
                '<P000002>\n죄송하지만 이 문장은 교정해 드릴 수 없습니다.')
    src=tmp_path/'mixed.txt'
    src.write_text('어색한 첫 문단입니다.\n어색한 둘째 문단입니다.',encoding='utf-8')
    mock=Mixed()
    res=run_refinement(src,tmp_path/'mixed.out.txt',mock,'model','key',
                       analysis_engine=(Dummy(),'analysis','test'),checkpoint_dir=tmp_path/'jobs')
    assert mock.calls==1
    assert res['changed']==1 and res['preserved']==1 and res['done']==1
    content=(tmp_path/'mixed.out.txt').read_text(encoding='utf-8-sig')
    assert '자연스럽게 정돈한 첫 문단' in content
    assert '어색한 둘째 문단' in content
    assert '죄송하지만' not in content


def test_refinement_prompt_prioritizes_fluent_rewriting_over_exact_words():
    from novel_translator.refinement_runtime import refinement_prompt
    paragraph=split_paragraphs('그는 자신의 시선을 그녀를 향하여 이동시켰다.')
    system,_=refinement_prompt(paragraph)
    assert '최소한의 교정이 아니라' in system
    assert '문장 구조' in system
    assert '문단 안에서는 문장을 합치거나 나눌 수' in system
    assert '줄거리' in system


def test_stop_during_a_slow_request_ends_at_once_and_resumes(tmp_path):
    """v2.5.7 보완: 재정비 중지도 번역처럼 진행 중인 요청을 기다리지 않고 바로 멈추며,
    다듬은 문단은 저장되어 같은 TXT·같은 엔진으로 다시 시작하면 이어진다."""
    import threading, time
    from novel_translator.providers.base import TranslationResponse
    src=tmp_path/'slow.txt';src.write_text('\n'.join('어색한 문단입니다. '+str(i) for i in range(8)),encoding='utf-8')
    output=tmp_path/'slow.ko.refined.txt'
    flag={'stop':False}
    class Slow(Dummy):
        def __init__(self):
            super().__init__(); self.cancels=[]; self.release=threading.Event()
        def translate(self, request, key):
            self.cancels.append(getattr(request,'cancel',None))
            if request.purpose=='edit' and self.calls>=2:
                flag['stop']=True              # the user presses stop while this one runs
                self.release.wait(30)          # a long local generation / API wait
            return Dummy.translate(self, request, key)
    p=Slow()
    started=time.monotonic()
    stopped=run_refinement(src,output,p,'model','test',checkpoint_dir=tmp_path/'cache',
                           max_items=2,stop=lambda:flag['stop'])
    took=time.monotonic()-started
    p.release.set()
    assert stopped['status']=='stopped' and took < 5          # no waiting for the slow reply
    assert stopped['done']==2                                   # the finished group is kept
    assert all(c is not None for c in p.cancels)                # local LLM streams see the stop
    assert not output.exists()
    flag['stop']=False
    q=Dummy()
    resume=run_refinement(src,output,q,'model','test',checkpoint_dir=tmp_path/'cache',max_items=2)
    assert resume['status']=='completed' and resume['done']==8
    assert q.calls==3                                           # only the 3 unfinished groups


def test_another_editing_model_is_reported_before_starting_over(tmp_path):
    from novel_translator.refinement_runtime import resume_conflict
    src=tmp_path/'m.txt';src.write_text('\n'.join('어색한 문단입니다. '+str(i) for i in range(6)),encoding='utf-8')
    output=tmp_path/'m.ko.refined.txt'; p=Dummy(); p.name='local'
    run_refinement(src,output,p,'model-a','test',checkpoint_dir=tmp_path/'cache',max_items=2,stop=lambda:p.calls>=2)
    assert resume_conflict(src,'local','model-a',checkpoint_dir=tmp_path/'cache',max_items=2)==(0,'')
    done,engine=resume_conflict(src,'local','model-b',checkpoint_dir=tmp_path/'cache',max_items=2)
    assert done==2 and engine=='로컬 LLM model-a'
    # the same model still resumes without a question
    q=Dummy(); q.name='local'
    r=run_refinement(src,output,q,'model-a','test',checkpoint_dir=tmp_path/'cache',max_items=2)
    assert r['status']=='completed' and q.calls==2
    assert resume_conflict(src,'local','model-b',checkpoint_dir=tmp_path/'cache',max_items=2)==(0,'')


def test_dialog_asks_before_starting_over_with_another_editing_model(tmp_path, monkeypatch):
    """윤문 모델을 바꾼 뒤 시작하면 처음부터 하기 전에 먼저 묻고, [아니요]면 시작하지 않는다."""
    from PySide6.QtWidgets import QApplication, QMessageBox
    import novel_translator.ui.refinement_run_dialog as rd
    from test_v20_refinement_api_integration import Host
    app=QApplication.instance() or QApplication([])
    src=tmp_path/'책.txt'; src.write_text('\n'.join('어색한 문단입니다. '+str(i) for i in range(6)),encoding='utf-8')
    d=rd.RefinementRunDialog(Host())
    try:
        d._set_source(src)
        asked=[]
        monkeypatch.setattr(rd,'resume_conflict',lambda *a,**k:(12,'로컬 LLM old:latest'))
        monkeypatch.setattr(QMessageBox,'question',staticmethod(lambda *a,**k:(asked.append(a[2]),QMessageBox.No)[1]))
        started=[]
        monkeypatch.setattr(rd.RefinementWorker,'start',lambda self: started.append(self))
        d._start()
        assert asked and '로컬 LLM old:latest' in asked[0] and '12문단' in asked[0] and 'editor:latest' in asked[0]
        assert not started and d.worker is None
        monkeypatch.setattr(QMessageBox,'question',staticmethod(lambda *a,**k:QMessageBox.Yes))
        d._start()
        assert started                                      # [예]: 지금 모델로 처음부터
        d.worker=None
        monkeypatch.setattr(rd,'resume_conflict',lambda *a,**k:(0,''))
        monkeypatch.setattr(QMessageBox,'question',staticmethod(lambda *a,**k:pytest.fail('같은 모델이면 묻지 않음')))
        started.clear(); d._start()
        assert started
        d.worker=None
    finally:
        d.close()
