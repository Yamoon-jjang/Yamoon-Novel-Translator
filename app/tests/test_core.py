import tempfile, unittest
from pathlib import Path
from novel_translator.chunking import split_paragraphs,make_chunks,split_chunk_for_retry
from novel_translator.profiles import ProfileManager
from novel_translator.storage import Database
from novel_translator.validation import validate_translation
from novel_translator.providers.base import ProviderError

class CoreTests(unittest.TestCase):
    def test_profiles(self):
        pm=ProfileManager(); ids={p.id for p in pm.all()}
        self.assertEqual(ids,{'ja-ko','zh-ko','en-ko'})
        self.assertEqual(pm.set_active('ja-ko').default_terms,{})
        self.assertTrue(pm.active.term_rules)
    def test_chunk_ids(self):
        ps=split_paragraphs('첫 문단\n\n둘째 문단\n\n셋째 문단')
        cs=make_chunks(ps,'ja',target_tokens=5,min_tokens=1,max_tokens=100)
        self.assertTrue(cs)
        joined='\n'.join(c.source_text for c in cs)
        self.assertIn('<P000001>',joined); self.assertIn('<P000003>',joined)
    def test_retry_split(self):
        ps=split_paragraphs('문장 하나。문장 둘。\n\n다음 문단。')
        c=make_chunks(ps,'ja',target_tokens=100,min_tokens=1,max_tokens=1000)[0]
        self.assertGreaterEqual(len(split_chunk_for_retry(c,'ja')),2)
    def test_validation_ids(self):
        validate_translation('<P000001>\nhello','<P000001>\n안녕하세요')
        with self.assertRaises(ProviderError): validate_translation('<P000001>\nhello','안녕하세요')
    def test_db_resume(self):
        with tempfile.TemporaryDirectory() as td:
            db=Database(Path(td)/'x.db'); pid=db.create_project('a.txt','ja','ja-ko','abc')
            db.upsert_chunk(pid,0,'x'); db.set_chunk_status(pid,0,'COMPLETED',translation='번역')
            self.assertEqual(len(db.pending_chunks(pid)),0)
            self.assertEqual(db.completed_translations(pid)[0]['translation'],'번역')

if __name__=='__main__': unittest.main()

class TextEncodingTests(unittest.TestCase):
    def test_language_aware_legacy_encodings(self):
        from novel_translator.textio import decode_txt_bytes
        t,e=decode_txt_bytes('日本語テスト\n次'.encode('cp932'),'ja')
        self.assertEqual(t,'日本語テスト\n次'); self.assertIn('CP932',e)
        t,e=decode_txt_bytes('中文测试\n下一行'.encode('gb18030'),'zh')
        self.assertEqual(t,'中文测试\n下一行'); self.assertIn('GB18030',e)
        t,e=decode_txt_bytes('繁體中文測試\n下一行'.encode('big5'),'zh')
        self.assertEqual(t,'繁體中文測試\n下一行'); self.assertEqual(e,'Big5')
        t,e=decode_txt_bytes('“English” café — test'.encode('cp1252'),'en')
        self.assertEqual(t,'“English” café — test'); self.assertEqual(e,'Windows-1252')


def test_protocol_like_source_line_is_escaped_inside_chunk():
    from novel_translator.chunking import split_paragraphs, make_chunks
    from novel_translator.validation import paragraph_ids
    src='문단 앞\n<P999999>\n문단 뒤'
    chunks=make_chunks(split_paragraphs(src),'ja',target_tokens=999,min_tokens=1,max_tokens=9999)
    tagged=chunks[0].source_text
    assert '〈P999999〉' in tagged
    assert paragraph_ids(tagged)==['<P000001>','<P000002>','<P000003>']


def test_pathological_long_paragraph_hard_splits_for_retry():
    from novel_translator.chunking import Paragraph, Chunk, split_chunk_for_retry
    text='A'*5000
    parts=split_chunk_for_retry(Chunk(0,[Paragraph(1,text,'\n')],5000),'en')
    assert len(parts)==2
    assert ''.join(p.paragraphs[0].text for p in parts)==text


def test_validation_normalizes_outer_code_fence_and_inline_tag():
    from novel_translator.validation import normalize_translation_output, validate_translation
    raw='```text\n<P000001> 안녕하세요. 이것은 충분히 긴 한국어 번역 문장입니다.\n```'
    norm=normalize_translation_output(raw)
    assert norm.startswith('<P000001>\n')
    validate_translation('<P000001>\nHello there. This is a source sentence.',norm)


def test_http200_style_refusal_is_rejected_for_long_source():
    from novel_translator.validation import validate_translation
    from novel_translator.providers.base import ProviderError, ErrorKind
    src='<P000001>\n'+('This is explicit source fiction text. '*40)
    out='<P000001>\n죄송하지만 이 요청은 도와드릴 수 없습니다.'
    try: validate_translation(src,out)
    except ProviderError as e: assert e.kind == ErrorKind.REFUSAL
    else: assert False


def test_secret_payload_migrates_legacy_provider_shapes():
    from novel_translator.api_keys import normalize_secret_payload
    raw={'gemini':['g-key-1','g-key-2'],'openai':['o-key-1'],'note':'keep'}
    out=normalize_secret_payload(raw)
    assert out['note']=='keep'
    assert len(out['gemini']['keys'])==2
    assert out['gemini']['keys'][0]['secret']=='g-key-1'
    assert len(out['openai']['keys'])==1
    assert out['openai']['keys'][0]['secret']=='o-key-1'
    assert out['openai']['enabled'] is False
    assert out['openai']['model']==''


def test_secret_payload_migrates_top_level_legacy_keys():
    from novel_translator.api_keys import normalize_secret_payload
    out=normalize_secret_payload({'keys':['legacy-gemini']})
    assert 'keys' not in out
    assert out['gemini']['keys'][0]['secret']=='legacy-gemini'
    assert out['openai']['keys']==[]


def test_openai_response_refusal_is_not_core_policy(monkeypatch):
    from novel_translator.providers.openai import OpenAIProvider
    from novel_translator.providers.base import TranslationRequest, ProviderError, ErrorKind
    class Resp:
        status_code=200
        headers={}
        text=''
        def json(self):
            return {'status':'completed','output':[{'content':[{'type':'refusal','refusal':'I cannot translate this passage.'}]}]}
    monkeypatch.setattr('novel_translator.providers.openai.requests.post',lambda *a,**k:Resp())
    try:
        OpenAIProvider().translate(TranslationRequest('gpt-6-astra','sys','user',timeout=10),'dummy')
    except ProviderError as e:
        assert e.kind == ErrorKind.REFUSAL
    else:
        raise AssertionError('refusal must raise ProviderError')


def test_mixed_korean_source_passes_through_and_only_foreign_paragraphs_are_chunked():
    from novel_translator.chunking import split_paragraphs, mark_translatable, make_chunks
    src='이미 번역된 한국어 문단입니다.\n\n他慢慢走到窗边，看着外面的雨。\n\n다시 한국어 문단입니다.\n\n她没有回答，只是低下了头。'
    paras=mark_translatable(split_paragraphs(src),'zh')
    assert [p.translatable for p in paras] == [False,True,False,True]
    chunks=make_chunks(paras,'zh',target_tokens=1200,min_tokens=100,max_tokens=2000,target_chars=800,min_chars=200,max_chars=1100)
    sent='\n'.join(c.source_text for c in chunks)
    assert '이미 번역된 한국어 문단' not in sent
    assert '다시 한국어 문단' not in sent
    assert '他慢慢走到窗边' in sent and '她没有回答' in sent


def test_character_budget_keeps_novel_chunks_short():
    from novel_translator.chunking import Paragraph, make_chunks
    ps=[Paragraph(i+1,'中'*310,'\n\n') for i in range(6)]
    cs=make_chunks(ps,'zh',target_tokens=5000,min_tokens=1,max_tokens=9999,target_chars=800,min_chars=250,max_chars=1150)
    assert len(cs) >= 2
    assert all(c.estimated_chars <= 1150 or len(c.paragraphs)==1 for c in cs)


def test_protected_literals_round_trip_and_do_not_mask_normal_prose():
    from novel_translator.protected_literals import protect_literals, restore_literals
    src='주소는 https://example.com/a?q=1 이고 메일은 test@example.com 이다. 普通中文句子。'
    p=protect_literals(src)
    assert 'https://example.com/a?q=1' not in p.text
    assert 'test@example.com' not in p.text
    assert '普通中文句子' in p.text
    assert restore_literals(p.text,p.mapping)==src


def test_language_lexical_guidance_is_relevant_only_and_user_terms_remain_separate():
    from novel_translator.profiles import ProfileManager
    from novel_translator.lexical_guidance import relevant_guidance
    prof=ProfileManager().set_active('zh-ko')
    g=relevant_guidance(prof,'他拜见师父，随后领取任务。')
    assert '师父' in g and '任务' in g
    assert '直播' not in g
