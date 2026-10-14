import tempfile, unittest
from pathlib import Path

from novel_translator.chunking import Paragraph, Chunk, split_paragraphs, make_chunks, split_chunk_for_retry
from novel_translator.orchestrator import TranslationOrchestrator, KeyState
from novel_translator.providers.base import TranslationProvider, TranslationResponse, ProviderError, ErrorKind
from novel_translator.profiles import ProfileManager
from novel_translator.storage import Database
from novel_translator.validation import validate_translation

class FakeClock:
    def __init__(self): self.now=1000.0
    def time(self): return self.now
    def sleep(self, sec): self.now += max(0, sec)

class ScriptProvider(TranslationProvider):
    name='fake'
    def __init__(self, script): self.script=list(script); self.calls=[]
    def translate(self, request, api_key):
        self.calls.append(api_key)
        event=self.script.pop(0) if self.script else 'ok'
        if isinstance(event, Exception): raise event
        if event=='ok':
            import re
            ids=re.findall(r'<P\d{6}>', request.user_prompt)
            # user prompt also contains context IDs in theory; simulation source uses no context.
            ids=list(dict.fromkeys(ids))
            return TranslationResponse('\n'.join(f'{x}\n번역된 한국어 문장입니다.' for x in ids))
        raise RuntimeError(event)

class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.profile=ProfileManager().set_active('ja-ko')

    def test_single_key_429_waits_and_recovers(self):
        clock=FakeClock(); p=ScriptProvider([ProviderError(ErrorKind.RATE_LIMIT,'429'), 'ok'])
        o=TranslationOrchestrator(p,[KeyState('k1','s1')],'m',retries=2,sleep_fn=clock.sleep,clock_fn=clock.time)
        text,key,retries=o.translate_chunk(self.profile,'<P000001>\n原文',{},'')
        self.assertEqual(key,'k1'); self.assertEqual(retries,1); self.assertGreater(clock.now,1000)

    def test_bad_key_fails_over_to_second_key(self):
        clock=FakeClock(); p=ScriptProvider([ProviderError(ErrorKind.KEY_BLOCKED,'leaked'), 'ok'])
        keys=[KeyState('k1','s1'),KeyState('k2','s2')]
        o=TranslationOrchestrator(p,keys,'m',retries=1,sleep_fn=clock.sleep,clock_fn=clock.time)
        _,key,_=o.translate_chunk(self.profile,'<P000001>\n原文',{},'')
        self.assertEqual(key,'k2'); self.assertFalse(keys[0].enabled)

    def test_safety_is_not_treated_as_bad_key(self):
        clock=FakeClock(); p=ScriptProvider([ProviderError(ErrorKind.SAFETY,'blocked')])
        keys=[KeyState('k1','s1')]
        o=TranslationOrchestrator(p,keys,'m',retries=5,sleep_fn=clock.sleep,clock_fn=clock.time)
        with self.assertRaises(ProviderError) as cm:
            o.translate_chunk(self.profile,'<P000001>\n原文',{},'')
        self.assertEqual(cm.exception.kind,ErrorKind.SAFETY)
        self.assertTrue(keys[0].enabled)

    def test_changed_chunk_invalidates_old_translation(self):
        with tempfile.TemporaryDirectory() as td:
            db=Database(Path(td)/'x.db'); pid=db.create_project('a.txt','ja','ja-ko','sha','fp')
            db.upsert_chunk(pid,0,'old'); db.set_chunk_status(pid,0,'COMPLETED',translation='번역')
            db.upsert_chunk(pid,0,'new')
            row=db.all_chunks(pid)[0]
            self.assertEqual(row['status'],'PENDING'); self.assertIsNone(row['translation'])

    def test_job_fingerprint_separates_term_or_model_changes(self):
        with tempfile.TemporaryDirectory() as td:
            db=Database(Path(td)/'x.db')
            a=db.get_or_create_project('a.txt','ja','ja-ko','sha','fp-a')
            b=db.get_or_create_project('a.txt','ja','ja-ko','sha','fp-b')
            self.assertNotEqual(a,b)

    def test_source_move_still_resumes_same_job(self):
        with tempfile.TemporaryDirectory() as td:
            db=Database(Path(td)/'x.db')
            a=db.get_or_create_project('old/a.txt','ja','ja-ko','sha','fp')
            b=db.get_or_create_project('new/a.txt','ja','ja-ko','sha','fp')
            self.assertEqual(a,b)

    def test_retry_split_preserves_paragraph_boundaries(self):
        ps=split_paragraphs('첫 문장。둘째 문장。셋째 문장。')
        c=make_chunks(ps,'ja',target_tokens=999,min_tokens=1,max_tokens=9999)[0]
        parts=split_chunk_for_retry(c,'ja')
        self.assertGreaterEqual(len(parts),2)
        self.assertTrue(all(part.paragraphs[0].pid==1 for part in parts))

    def test_validation_rejects_tiny_translation(self):
        src='<P000001>\n' + ('very long source sentence. '*100)
        with self.assertRaises(ProviderError): validate_translation(src,'<P000001>\n짧음')

if __name__=='__main__': unittest.main()

class KeyAndPromptHardeningTests(unittest.TestCase):
    def setUp(self):
        self.profile=ProfileManager().set_active('ja-ko')

    def test_project_group_429_uses_independent_group(self):
        clock=FakeClock(); p=ScriptProvider([ProviderError(ErrorKind.RATE_LIMIT,'429'), 'ok'])
        keys=[KeyState('k1','s1','project-a'),KeyState('k2','s2','project-b')]
        o=TranslationOrchestrator(p,keys,'m',retries=2,sleep_fn=clock.sleep,clock_fn=clock.time)
        _,key,_=o.translate_chunk(self.profile,'<P000001>\n原文',{},'')
        self.assertEqual(key,'k2')
        self.assertEqual(p.calls[:2],['s1','s2'])

    def test_same_project_keys_share_cooldown(self):
        clock=FakeClock(); p=ScriptProvider([ProviderError(ErrorKind.RATE_LIMIT,'429'), 'ok'])
        keys=[KeyState('k1','s1','project-a'),KeyState('k2','s2','project-a')]
        o=TranslationOrchestrator(p,keys,'m',retries=2,sleep_fn=clock.sleep,clock_fn=clock.time)
        _,_,_=o.translate_chunk(self.profile,'<P000001>\n原文',{},'')
        self.assertGreater(clock.now,1000)

    def test_blocked_key_does_not_disable_sibling(self):
        clock=FakeClock(); p=ScriptProvider([ProviderError(ErrorKind.KEY_BLOCKED,'leaked'), 'ok'])
        keys=[KeyState('k1','s1','project-a'),KeyState('k2','s2','project-a')]
        o=TranslationOrchestrator(p,keys,'m',retries=1,sleep_fn=clock.sleep,clock_fn=clock.time)
        _,key,_=o.translate_chunk(self.profile,'<P000001>\n原文',{},'')
        self.assertEqual(key,'k2'); self.assertFalse(keys[0].enabled); self.assertTrue(keys[1].enabled)

    def test_language_prompts_are_detailed_and_distinct(self):
        from novel_translator.prompting import build_system_prompt
        pm=ProfileManager()
        texts={pid:build_system_prompt(pm.set_active(pid)) for pid in ('ja-ko','zh-ko','en-ko')}
        self.assertEqual(len(set(texts.values())),3)
        self.assertIn('zero pronouns', texts['ja-ko'])
        self.assertIn('成语', texts['zh-ko'])
        self.assertIn('free indirect', texts['en-ko'].lower())
        for text in texts.values():
            self.assertIn('adult sexual content', text)
            self.assertIn('Never invent', text)
            self.assertGreater(len(text),2500)

    def test_default_terms_do_not_force_context_sensitive_titles(self):
        pm=ProfileManager()
        for pid in ('ja-ko','zh-ko','en-ko'):
            self.assertEqual(pm.set_active(pid).default_terms,{})

    def test_key_record_migrates_legacy_and_deduplicates(self):
        from novel_translator.api_keys import normalize_key_records
        recs=normalize_key_records(['abc123456789','abc123456789',{'secret':'xyz987654321','project_group':'p2'}])
        self.assertEqual(len(recs),2)
        self.assertEqual(recs[1].project_group,'p2')
        self.assertNotIn('abc123456789', recs[0].masked())


def test_adaptive_rpm_paces_and_slows_after_429():
    clock=FakeClock(); p=ScriptProvider(['ok', ProviderError(ErrorKind.RATE_LIMIT,'429'), 'ok'])
    o=TranslationOrchestrator(p,[KeyState('k1','s1')],'m',retries=2,sleep_fn=clock.sleep,clock_fn=clock.time,target_rpm=12,min_rpm=2,max_rpm=30)
    o.translate_chunk(ProfileManager().set_active('ja-ko'),'<P000001>\n原文',{},'')
    before=clock.now
    o.translate_chunk(ProfileManager().set_active('ja-ko'),'<P000002>\n原文',{},'')
    assert clock.now>before
    assert o._adaptive_rpm < 12


def test_healthy_primary_key_is_sticky_not_round_robin():
    clock=FakeClock(); p=ScriptProvider(['ok','ok'])
    keys=[KeyState('k1','s1','project-a'),KeyState('k2','s2','project-a')]
    o=TranslationOrchestrator(p,keys,'m',retries=1,sleep_fn=clock.sleep,clock_fn=clock.time,target_rpm=1000000,min_rpm=1000000,max_rpm=1000000)
    profile=ProfileManager().set_active('ja-ko')
    o.translate_chunk(profile,'<P000001>\n原文',{},'')
    o.translate_chunk(profile,'<P000002>\n原文',{},'')
    assert p.calls==['s1','s1']


def test_hard_quota_does_not_rotate_sibling_key_in_same_project():
    clock=FakeClock(); p=ScriptProvider([ProviderError(ErrorKind.QUOTA,'daily quota')])
    keys=[KeyState('k1','s1','project-a'),KeyState('k2','s2','project-a')]
    o=TranslationOrchestrator(p,keys,'m',retries=5,sleep_fn=clock.sleep,clock_fn=clock.time,target_rpm=1000000,min_rpm=1000000,max_rpm=1000000)
    with unittest.TestCase().assertRaises(ProviderError) as cm:
        o.translate_chunk(ProfileManager().set_active('ja-ko'),'<P000001>\n原文',{},'')
    assert cm.exception.kind==ErrorKind.QUOTA
    assert p.calls==['s1']
    assert keys[1].enabled


def test_key_pool_constant_is_ten_without_silent_normalize_truncation():
    from novel_translator.api_keys import MAX_KEYS_PER_PROVIDER, normalize_key_records
    assert MAX_KEYS_PER_PROVIDER==10
    rows=[{'secret':f'dummy-key-{i:02d}-abcdefghijkl','project_group':'p'} for i in range(12)]
    assert len(normalize_key_records(rows))==12  # old stores are preserved; UI prevents new additions >10.



def test_refusal_recovery_switches_are_wired_in_worker_source():
    from pathlib import Path
    src=(Path(__file__).resolve().parents[1]/'novel_translator'/'ui'/'worker.py').read_text(encoding='utf-8')
    assert "self.allow_reframe=bool(self._refusal_cfg.get('reframe_once',True))" in src
    assert "self.allow_split_recovery=bool(self._refusal_cfg.get('split_chunk',True))" in src
    assert "self.allow_provider_fallback=bool(self._refusal_cfg.get('fallback_provider',True))" in src
    assert 'not self.allow_split_recovery' in src
    assert 'not self.allow_provider_fallback' in src
