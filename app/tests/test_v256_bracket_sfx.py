"""v2.5.6: bracketed sound effects / system words are prose, not markup.

<SPANK>, <Slurp, slurp>, [System] used to be protected as HTML / BBCode tags, so the
model never saw them and they stayed in the source language. Only real markup is
protected now; everything else reaches the model and is translated (<찰싹>, [시스템]).
"""
from __future__ import annotations

import pytest

from novel_translator.protected_literals import protect_literals, restore_literals, literal_spans
from novel_translator.providers.base import TranslationResponse
from novel_translator.profiles import ProfileManager
from novel_translator.chunking import split_paragraphs, mark_translatable, make_chunks
from novel_translator import orchestrator as orch_mod


PROSE = ['<SPANK>', '<Slurp, slurp>', '<SPANK!>', '<Spank spank>', '<Ding!>', '<System Notice>',
         '<I hate you>', '<Em, em>', '<Bang>', '[System]', '[Quest Complete]', '[Level Up!]', '[Status]']

MARKUP = ['<br>', '<br/>', '<BR />', '<b>', '</b>', '<i>', '</i>', '<a href="x">', '</a>', '<p>', '</p>',
          '<code value="中文测试">', '<ruby>', '<rt>', '</rt>', '</ruby>', '<font color=red>', '</font>',
          "<span class='x' id=\"y\">", '<img src="a.png" />', '<custom-tag data="1">', '<think>', '</think>',
          '[b]', '[/b]', '[i]', '[color=red]', '[/color]', '[size=3]', '[quote]', '[/quote]', '[*]']


@pytest.mark.parametrize('word', PROSE)
@pytest.mark.parametrize('frame', ['{} 她叫了一声。', '彼女は{}と声を上げた。', '{} she cried.', '{}', '“{}”'])
def test_bracketed_sound_effects_reach_the_model(word, frame):
    src = frame.format(word)
    p = protect_literals(src)
    assert p.mapping == {} and p.text == src, (src, p.mapping)


@pytest.mark.parametrize('tag', MARKUP)
def test_real_markup_is_still_protected_byte_for_byte(tag):
    src = f'他看着{tag}窗外的雨。'
    p = protect_literals(src)
    assert list(p.mapping.values()) == [tag]
    assert tag not in p.text and restore_literals(p.text, p.mapping) == src


def test_other_protected_literals_are_unchanged():
    src = ('主页 https://example.com/a?q=1 邮箱 test@example.com '
           '`code()` ＊＊＊ ［＃改ページ］ 12345678-1234-1234-1234-123456789abc 路径 C:\\Users\\a.txt')
    p = protect_literals(src)
    assert sorted(p.mapping.values()) == sorted(['https://example.com/a?q=1', 'test@example.com',
                                                 '`code()`', '＊＊＊', '［＃改ページ］',
                                                 '12345678-1234-1234-1234-123456789abc', 'C:\\Users\\a.txt'])
    assert restore_literals(p.text, p.mapping) == src


def test_paragraph_ids_are_never_protected():
    for s in ('<P000001>', '本文提到 <P999999> 字符串', '<p000001>'):
        assert literal_spans(s) == []


@pytest.mark.parametrize('lang,src,ko', [
    ('zh-ko', '<SPANK> 她叫了一声。<Slurp, slurp>', '<찰싹> 그녀가 비명을 질렀다. <후릅, 후릅>'),
    ('ja-ko', '<SPANK>　彼女は声を上げた。', '<찰싹>　그녀가 소리를 질렀다.'),
    ('en-ko', '<Slurp, slurp> [System] She cried.', '<후릅, 후릅> [시스템] 그녀가 울었다.'),
])
def test_orchestrator_sends_the_words_and_keeps_the_translation(lang, src, ko):
    seen = []

    class P:
        last_usage = None
        def translate(self, req, key):
            seen.append(req)
            return TranslationResponse('<P000001>\n' + ko)

    prof = ProfileManager().set_active(lang)
    o = orch_mod.TranslationOrchestrator(P(), [orch_mod.KeyState('k', 's')], 'gemma4', sleep_fn=lambda s: None)
    out = o.translate_chunk(prof, '<P000001>\n' + src, {})[0]
    assert 'NTLIT' not in seen[0].user_prompt
    assert '<SPANK>' in seen[0].user_prompt or '<Slurp, slurp>' in seen[0].user_prompt
    assert ko in out


def test_chunk_identity_of_a_book_does_not_change():
    """Protection is applied at request time only: chunk source text (resume identity) is
    the same as before, so a job in progress keeps every finished chunk."""
    text = '她转过身。\n\n<SPANK>\n\n<Slurp, slurp> 她低下头。\n\n<b>粗体</b>他笑了。'
    ps = mark_translatable(split_paragraphs(text), 'zh')
    chunks = make_chunks(ps, 'zh')
    joined = '\n\n'.join(c.source_text for c in chunks)
    assert '<Slurp, slurp> 她低下头。' in joined and '<b>粗体</b>他笑了。' in joined
