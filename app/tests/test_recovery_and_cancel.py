import threading
import time
from pathlib import Path
from unittest.mock import patch
import pytest
from novel_translator.validation import normalize_translation_output, validate_translation
from novel_translator.providers.base import TranslationResponse, ProviderError, ErrorKind, TranslationCancelled
from novel_translator.orchestrator import TranslationOrchestrator, KeyState
from novel_translator.profiles import ProfileManager

@pytest.mark.parametrize('raw',['<P1> 한국어 번역입니다.','＜P０００００１＞\n한국어 번역입니다.','**<P000001>**\n한국어 번역입니다.','한국어 번역입니다.'])
def test_single_paragraph_typography_recovery(raw):
    source='<P000001>\nThis is a translation.'
    output=normalize_translation_output(raw,source)
    validate_translation(source,output)
    assert '<P000001>' in output

@pytest.mark.parametrize('raw',['죄송하지만 이 요청은 도와드릴 수 없습니다.','I cannot translate this passage.','이 콘텐츠는 번역할 수 없습니다.'])
def test_untagged_refusal_never_becomes_translation(raw):
    source='<P000001>\nThis is a source paragraph.'
    output=normalize_translation_output(raw,source)
    assert '<P000001>' not in output
    with pytest.raises(ProviderError) as caught:validate_translation(source,output)
    assert caught.value.kind == ErrorKind.REFUSAL

@pytest.mark.parametrize('raw',['한국어 문단 하나입니다.\n\n두 번째 문단입니다.','<P000002>\n순서가 바뀌었습니다.\n<P000001>\n다른 문단입니다.','<P000001>\n한 문단만 있습니다.'])
def test_multiple_paragraphs_never_guess_ids(raw):
    source='<P000001>\nOne.\n<P000002>\nTwo.'
    with pytest.raises(ProviderError):validate_translation(source,normalize_translation_output(raw,source))


def test_quoted_dialogue_is_not_provider_refusal():
    validate_translation('<P000001>\nHe said no.','<P000001>\n“죄송하지만 이 요청은 도와드릴 수 없습니다.” 그가 말했다.')


def test_cancel_pending_http_returns_without_late_write():
    started=threading.Event(); release=threading.Event(); cancelled=threading.Event(); completed=threading.Event()
    class SlowProvider:
        def translate(self,request,key):
            started.set();release.wait(3);completed.set()
            return TranslationResponse('<P000001>\n늦게 돌아온 한국어 번역입니다.')
    orch=TranslationOrchestrator(SlowProvider(),[KeyState('test','dummy')],'fixture',cancel_check=cancelled.is_set)
    threading.Timer(0.1,cancelled.set).start()
    t=time.monotonic()
    try:
        with pytest.raises(TranslationCancelled):orch.translate_chunk(ProfileManager().active,'<P000001>\n日本語の文章です。',{})
        assert started.is_set() and time.monotonic()-t<1.0
        assert not completed.is_set()
    finally:release.set()
    assert completed.wait(1)
    assert orch.keys[0].failures==0


def test_cancel_rate_wait_makes_no_provider_call():
    cancelled=threading.Event();cancelled.set()
    class Provider:
        def translate(self,*args):raise AssertionError('must not request')
    orch=TranslationOrchestrator(Provider(),[KeyState('test','dummy',cooldown_until=time.time()+60)],'fixture',cancel_check=cancelled.is_set)
    with pytest.raises(TranslationCancelled):orch.translate_chunk(ProfileManager().active,'<P000001>\n日本語',{})


def test_worker_cancel_persists_pending_and_ignores_late_result(tmp_path):
    from novel_translator.ui import worker as wm
    from novel_translator.chunking import split_paragraphs,mark_translatable,make_chunks
    from novel_translator.storage import Database
    started=threading.Event();release=threading.Event();done=threading.Event()
    class Provider:
        def translate(self,*args):
            started.set();release.wait(3);done.set()
            return TranslationResponse('<P000001>\n늦은 응답이 파일에 들어가면 안 됩니다.')
    src=tmp_path/'book.txt';text='他站在窗边看着外面的雨。';raw=text.encode();src.write_bytes(raw)
    ps=mark_translatable(split_paragraphs(text),'zh');cs=make_chunks(ps,'zh',target_tokens=1000,min_tokens=1,max_tokens=4000)
    with patch.object(wm,'DATA_DIR',tmp_path/'data'),patch.object(wm,'GeminiProvider',Provider):
        w=wm.TranslationWorker(src,raw,ProfileManager().set_active('zh-ko'),cs,'fixture',[{'id':'k','secret':'dummy','enabled':True}],{},output_dir=tmp_path/'out',all_paragraphs=ps)
        outcomes=[];errors=[]
        w.result_ready.connect(lambda *a:outcomes.append(a));w.failed.connect(errors.append)
        def stop():
            if started.wait(30):w.stop()
        stopper=threading.Thread(target=stop);stopper.start()
        try:
            w.run();stopper.join(1)
            assert not errors and outcomes[0][0]=='stopped'
            output=Path(outcomes[0][1]);before=output.read_bytes()
            assert text in before.decode('utf-8-sig')
            release.set();assert done.wait(1)
            assert output.read_bytes()==before
            import sqlite3
            db=sqlite3.connect(tmp_path/'data'/'translations.db')
            assert db.execute('select status from chunks').fetchone()[0]=='PENDING'
        finally:release.set()


def test_split_recovery_preserves_distinct_paragraph_id_order():
    """Combining independently successful split parts never guesses or moves IDs."""
    from types import SimpleNamespace
    from novel_translator.ui.worker import TranslationWorker
    from novel_translator.chunking import Paragraph, Chunk

    source_parts = [
        Chunk(0, [Paragraph(14, '첫 원문')], 10),
        Chunk(1, [Paragraph(15, '다음 원문')], 10),
    ]
    outputs = {
        14: '<P000014>\n첫 번역입니다.',
        15: '<P000015>\n다음 번역입니다.',
    }
    calls = []
    def translate(orch, part, previous, depth):
        pid = part.paragraphs[0].pid
        calls.append((pid, depth))
        return outputs[pid], 'key', 0
    worker = SimpleNamespace(
        profile=SimpleNamespace(source_language='zh'),
        _chunk_exclusions=[], _pending_traces=[],
        _translate_recover=translate,
    )
    combined, _, _ = TranslationWorker._translate_parts(worker, None, source_parts, '', 0)
    assert combined == '<P000014>\n첫 번역입니다.\n\n<P000015>\n다음 번역입니다.'
    assert calls == [(14, 1), (15, 1)]


def test_split_recovery_same_paragraph_id_combines_only_bodies():
    """Sentence subdivisions of one long paragraph must keep one original ID."""
    from types import SimpleNamespace
    from novel_translator.ui.worker import TranslationWorker
    from novel_translator.chunking import Paragraph, Chunk
    from novel_translator.validation import paragraph_ids

    parts = [
        Chunk(0, [Paragraph(14, '원문 앞부분')], 10),
        Chunk(1, [Paragraph(14, '원문 뒷부분')], 10),
    ]
    answers = iter([
        '<P000014>\n앞부분 번역.',
        '<P000014>\n뒷부분 번역.',
    ])
    def translate(orch, part, previous, depth):
        return next(answers), 'key', 0
    worker = SimpleNamespace(
        profile=SimpleNamespace(source_language='zh'),
        _chunk_exclusions=[], _pending_traces=[],
        _translate_recover=translate,
    )
    combined, _, _ = TranslationWorker._translate_parts(worker, None, parts, '', 0)
    assert paragraph_ids(combined) == ['<P000014>']
    assert combined == '<P000014>\n앞부분 번역.뒷부분 번역.'
