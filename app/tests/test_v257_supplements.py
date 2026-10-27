"""v2.5.7 보완: 영어 힌트 중복 제거, 義父·義母 역어, 말투표 영어 낱말 경계, 로컬 LLM 컨텍스트 기록."""
from __future__ import annotations

import json

import pytest

from novel_translator.config import CONFIG_DIR
from novel_translator.profiles import ProfileManager
from novel_translator.lexical_guidance import relevant_guidance
from novel_translator.speech_levels import sample_dialogue
from novel_translator.providers.base import TranslationResponse
from novel_translator import orchestrator as orch_mod


# ① English: a shorter hint inside a longer one that also matched is dropped there.
@pytest.mark.parametrize('text,present,absent', [
    ('He told me to jerk off.', ['jerk off'], ['jerk']),
    ('She caught him trying to cheat on her.', ['cheat on'], ['cheat']),
    ('My ex-wife called.', ['ex-wife'], ['ex']),
    ('What the fuck, he said.', ['what the fuck'], ['fuck']),
    ('The demon lord laughed.', ['demon lord'], ['demon']),
])
def test_english_shorter_hint_inside_longer_is_dropped(text, present, absent):
    g = relevant_guidance(ProfileManager().set_active('en-ko'), text)
    for k in present:
        assert k in g, (k, list(g))
    for k in absent:
        assert k not in g, (k, list(g))


@pytest.mark.parametrize('text,both', [
    ('What a jerk. Later he said: jerk off.', ['jerk off', 'jerk']),
    ('My ex-wife and my ex both called.', ['ex-wife', 'ex']),
    ('A demon appeared. The demon lord laughed.', ['demon lord', 'demon']),
])
def test_english_shorter_hint_elsewhere_stays(text, both):
    g = relevant_guidance(ProfileManager().set_active('en-ko'), text)
    for k in both:
        assert k in g, (k, list(g))


def test_english_word_boundary_and_case_unchanged():
    p = ProfileManager().set_active('en-ko')
    assert 'class' not in relevant_guidance(p, 'He studied classics.')
    assert 'Lady' in relevant_guidance(p, 'The Lady smiled.') or 'lady' in str(relevant_guidance(p, 'The Lady smiled.')).lower()


def test_cjk_rules_unchanged():
    zh = ProfileManager().set_active('zh-ko')
    g = relevant_guidance(zh, '主人公走进房间。')
    assert '主人' not in g
    g = relevant_guidance(zh, '主人回来了。')
    assert '主人' in g


# ② 義父・義母
def test_ja_in_law_entries_include_step_and_adoptive():
    data = json.loads((CONFIG_DIR / 'lexicon_extra.json').read_text(encoding='utf-8'))['ja']
    assert '의붓아버지' in data['義父'] and '양아버지' in data['義父'] and '시아버지' in data['義父'] and '장인' in data['義父']
    assert '의붓어머니' in data['義母'] and '양어머니' in data['義母'] and '시어머니' in data['義母'] and '장모' in data['義母']
    g = relevant_guidance(ProfileManager().set_active('ja-ko'), '義父は笑った。')
    assert '의붓아버지' in g.get('義父', '')


# ③ Speech-table sampling: English cues are whole words.
def _pick(lines, cues, limit):
    return sample_dialogue('\n'.join(lines), {'Tom': '톰', 'Ann': '앤'}, limit=limit, cues=cues)


def test_english_cue_inside_another_word_no_longer_counts():
    filler = ['Narration line number %d goes on for a while.' % i for i in range(40)]
    noise = '"I desire a cocktail," said Tom to Ann at the long table tonight.'
    real = '"Yes, sir, right away," said Ann to Tom at the long table tonight.'
    out = _pick(filler + [noise, real], ['sir', 'cock'], limit=80)
    assert 'Yes, sir' in out and 'desire' not in out


def test_cjk_cue_matching_unchanged():
    filler = ['旁白第%d行，继续说下去。' % i for i in range(40)]
    plain = '“今天天气不错，我们出去走走吧。”小明对小红说。'
    cued = '“妈，我回来了，今天好累啊。”小明对小红说。'
    out = sample_dialogue('\n'.join(filler + [plain, cued]), {'小明': '샤오밍', '小红': '샤오훙'}, limit=30, cues=['妈'])
    assert '妈，我回来了' in out and '天气不错' not in out


# Local LLM context growth is written to the work log once.
def test_context_growth_is_logged_once():
    events = []

    class P:
        last_usage = None
        num_ctx = 12288
        def translate(self, req, key):
            self.num_ctx = 16384 if self.num_ctx == 12288 else self.num_ctx
            return TranslationResponse('<P000001>\n그가 웃었다.')

    prof = ProfileManager().set_active('zh-ko')
    o = orch_mod.TranslationOrchestrator(P(), [orch_mod.KeyState('k', 's')], 'gemma4', sleep_fn=lambda s: None,
                                         event_fn=events.append)
    o.translate_chunk(prof, '<P000001>\n他笑了。', {})
    o.translate_chunk(prof, '<P000001>\n他笑了。', {})
    grown = [e for e in events if '컨텍스트 12,288 → 16,384' in e]
    assert len(grown) == 1, events


def test_no_context_log_for_cloud_providers():
    events = []

    class P:
        last_usage = None
        def translate(self, req, key):
            return TranslationResponse('<P000001>\n그가 웃었다.')

    prof = ProfileManager().set_active('zh-ko')
    o = orch_mod.TranslationOrchestrator(P(), [orch_mod.KeyState('k', 's')], 'gemini', sleep_fn=lambda s: None,
                                         event_fn=events.append)
    o.translate_chunk(prof, '<P000001>\n他笑了。', {})
    assert not [e for e in events if '컨텍스트' in e]
