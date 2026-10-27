"""v2.5.7: English sound effects / system words in brackets are translated everywhere.

v2.5.6 sent <SPANK> inside a sentence to the model, but
- a line made ONLY of such words in a Chinese / Japanese novel (<WHACK>, “<Slurp, slurp>”)
  was still treated as "not source language" and copied unchanged, and
- results saved by earlier builds (resume, translation memory, paragraph memory) were
  reused as they were, so <SPANK> stayed in English there.
"""
from __future__ import annotations

import pytest

from novel_translator.protected_literals import bracket_prose, stale_bracket_prose
from novel_translator.chunking import split_paragraphs, mark_translatable, make_chunks, should_translate_text
from novel_translator.providers.base import TranslationResponse
from novel_translator.profiles import ProfileManager
from novel_translator import orchestrator as orch_mod
from novel_translator.ui.worker import TranslationWorker


LONG = ('<I will never forgive you for what you did to my family, never, not in this life '
        'and not in the next one either, do you hear me>')
LINES = [LONG, '[' + LONG[1:-1] + ']', '<WHACK>', '<SPANK>', '<SPANK!>', '“<Slurp, slurp>”', '「<Ding!>」', '[System]', '[Quest Complete]',
         '<WHACK> <WHACK>', '＜BANG＞', '【Level Up】']


@pytest.mark.parametrize('lang', ['zh', 'ja'])
@pytest.mark.parametrize('line', LINES)
def test_a_line_of_only_bracket_words_is_translated(lang, line):
    assert should_translate_text(line, lang)
    ps = mark_translatable(split_paragraphs('他走了。\n\n' + line + '\n\n她笑了。'), lang)
    assert [p.translatable for p in ps] == [True, True, True]


@pytest.mark.parametrize('lang', ['zh', 'ja'])
@pytest.mark.parametrize('line', ['<b>', '<br>', '</i>', '[b]', '[/url]', 'Chapter 3', 'OK', 'https://x.com/a',
                                  '***', '<P000001>', '—', '…'])
def test_other_latin_only_lines_keep_their_old_decision(lang, line):
    assert should_translate_text(line, lang) is False


def test_korean_lines_are_untouched():
    for lang in ('zh', 'ja'):
        assert should_translate_text('<퍽>', lang) is False
        assert should_translate_text('[시스템] 레벨 업', lang) is False


@pytest.mark.parametrize('text,found', [
    ('<WHACK> 他倒下了。', ['<WHACK>']),
    ('“<Slurp, slurp>”', ['<Slurp, slurp>']),
    ('[System] Level up!', ['[System]']),
    ('<b>粗体</b> <br> [b]x[/b] <a href="u">', []),
    ('<P000001>\n本文', []),
    ('a < b > c', []),
    ('', []),
])
def test_bracket_prose(text, found):
    assert bracket_prose(text) == found


def test_stale_bracket_prose():
    assert stale_bracket_prose('<WHACK> 他倒下了。', '<WHACK> 그가 쓰러졌다.')
    assert not stale_bracket_prose('<WHACK> 他倒下了。', '<퍽> 그가 쓰러졌다.')
    assert not stale_bracket_prose('<b>粗</b>他笑了。', '<b>굵게</b> 그가 웃었다.')
    assert not stale_bracket_prose('<WHACK>', '')


class _Chunk:
    def __init__(self, paras):
        self.paragraphs = paras


def test_saved_results_with_english_bracket_words_are_sent_again():
    ps = mark_translatable(split_paragraphs('<SPANK> 她叫了一声。\n\n他笑了。\n\n[System] 升级了。'), 'zh')
    saved = '<P000001>\n<SPANK> 그녀가 비명을 질렀다.\n\n<P000002>\n그가 웃었다.\n\n<P000003>\n[시스템] 레벨이 올랐다.'
    assert TranslationWorker._bracket_pids(_Chunk(ps), saved) == {1}
    fixed = saved.replace('<SPANK>', '<찰싹>')
    assert TranslationWorker._bracket_pids(_Chunk(ps), fixed) == set()


def test_prompt_asks_to_translate_bracket_words_only_when_present():
    seen = []

    class P:
        last_usage = None
        def translate(self, req, key):
            seen.append(req)
            return TranslationResponse('<P000001>\n<퍽>')

    prof = ProfileManager().set_active('zh-ko')
    o = orch_mod.TranslationOrchestrator(P(), [orch_mod.KeyState('k', 's')], 'gemma4', sleep_fn=lambda s: None)
    assert o.translate_chunk(prof, '<P000001>\n<WHACK>', {})[0].strip().endswith('<퍽>')
    assert 'Bracketed English sound effects' in seen[-1].user_prompt
    sys_prompt = seen[-1].system_prompt

    class Q(P):
        def translate(self, req, key):
            seen.append(req)
            return TranslationResponse('<P000001>\n그가 웃었다.')
    o2 = orch_mod.TranslationOrchestrator(Q(), [orch_mod.KeyState('k', 's')], 'gemma4', sleep_fn=lambda s: None)
    o2.translate_chunk(prof, '<P000001>\n他笑了。', {})
    assert 'Bracketed English sound effects' not in seen[-1].user_prompt
    assert seen[-1].system_prompt == sys_prompt   # job identity / memory key unchanged


def test_paragraph_numbers_do_not_change():
    """Making a bracket-only line translatable never renumbers paragraphs, so a job in
    progress finds every paragraph it already translated (paragraph memory)."""
    text = '她转过身。\n\n<WHACK>\n\n“<Slurp, slurp>”\n\n<b>粗体</b>他笑了。\n\n第二章'
    ps = mark_translatable(split_paragraphs(text), 'zh')
    assert [(p.pid, p.text) for p in ps] == [(1, '她转过身。'), (2, '<WHACK>'), (3, '“<Slurp, slurp>”'),
                                            (4, '<b>粗体</b>他笑了。'), (5, '第二章')]
    joined = '\n\n'.join(c.source_text for c in make_chunks(ps, 'zh'))
    assert '<WHACK>' in joined and '“<Slurp, slurp>”' in joined
