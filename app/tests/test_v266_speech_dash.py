"""v2.6.6: Korean punctuation for speech that trails off in a dash, and for ・・・.

Japanese and Chinese novels end or open a line of speech with a dash (「実は――」 “我——”), and a
model usually copies it into the translation ("사실은――"), which does not read as Korean. The
result file's punctuation step (문장부호 정리) now writes "……" for a dash that sits right
inside a quotation mark. Everything else keeps its dash: a dash in the middle of a sentence, a
scene-break line, a dash outside the quotes, a line whose straight quotes do not pair up, and
untranslated source paragraphs. Japanese middle-dot ellipses (・・・) become "……" instead of
"···"; a single ・ between names still becomes ·.

Like the rest of the punctuation step this is applied only when a result file is written: the
stored translation, the request and the job identity do not change.
"""
from __future__ import annotations

import pytest

from novel_translator.punctuation import korean_punctuation as k


@pytest.mark.parametrize('src,expected', [
    # a dash that ends or opens a line of speech -> ……
    ('"게이코가, 사실은――"', '"게이코가, 사실은……"'),
    ('“게이코가, 사실은――”', '"게이코가, 사실은……"'),
    ('「사실은――」', '"사실은……"'),
    ('"――저기, 좀 더 상냥하게 해 주세요."', '"……저기, 좀 더 상냥하게 해 주세요."'),
    ('「――――」', '"……"'),                                   # a silent line of speech
    ('"잠깐――"이라고 말하다 멈췄다.', '"잠깐……"이라고 말하다 멈췄다.'),
    ('"그건――――!"', '"그건……!"'),                           # ! ? ♡ ~ may follow the dash
    ('"싫어――!!"', '"싫어……!!"'),
    ('"아앗――♡"', '"아앗……♡"'),
    ('"너도――?"', '"너도……?"'),
    ('"사실은―"', '"사실은……"'),
    ('"사실은——"', '"사실은……"'),                             # Chinese double em dash
    ('"사실은──"', '"사실은……"'),                             # box-drawing dash
    ('"잠깐–" 그가 말했다.', '"잠깐……" 그가 말했다.'),           # en dash
    ('"하나――" "둘――" 그는 셌다.', '"하나……" "둘……" 그는 셌다.'),
    # kept as they are
    ('――야마자키 탐정 사무소―― 이곳의 소장은 친구다.', '――야마자키 탐정 사무소―― 이곳의 소장은 친구다.'),
    ('"뜬금없이 무슨 일입니까. ――자, 진정하세요."', '"뜬금없이 무슨 일입니까. ――자, 진정하세요."'),
    ('"그건—그러니까—말이야."', '"그건—그러니까—말이야."'),
    ('"좋아"――그는 웃었다.', '"좋아"――그는 웃었다.'),          # outside the quotes
    ('그는――"좋아" 하고 웃었다.', '그는――"좋아" 하고 웃었다.'),
    ('"싫어, 그것만은――', '"싫어, 그것만은――'),               # straight quotes do not pair up
    ('계속된 대사――" 하고 끝났다.', '계속된 대사――" 하고 끝났다.'),
    ('그것은――', '그것은――'),
    ('――――', '――――'),                                       # scene break
    ('◇――◇――◇', '◇――◇――◇'),
    ('「実は――」', '「実は――」'),                             # untranslated source kept as is
    ('“我——”', '“我——”'),
    ('"Wait–"', '"Wait–"'),
    # Japanese middle-dot ellipsis
    ('"그래・・・"', '"그래……"'),
    ('"음・・"', '"음……"'),
    ('그는・・・ 말이 없었다.', '그는…… 말이 없었다.'),
    ('"그래･･･"', '"그래……"'),                                # half-width ･
    ('존・스미스가 왔다〜', '존·스미스가 왔다~'),               # a single ・ is still a name separator
    ('「そう・・・」', '「そう・・・」'),
])
def test_speech_dash_and_middle_dot_ellipsis(src, expected):
    assert k(src) == expected


def test_quote_styles_keep_their_quotes():
    assert k('「사실은――」', 'curly') == '“사실은……”'
    assert k('「사실은――」', 'keep') == '「사실은……」'
    assert k('“사실은――”', 'keep') == '“사실은……”'
    assert k('「――――」', 'keep') == '「……」'


def test_earlier_results_are_unchanged():
    """Lines with no dash next to a quotation mark and no ・・ come out exactly as in v2.6.5."""
    assert k('그는 말했다。그녀는 웃었다。') == '그는 말했다. 그녀는 웃었다.'
    assert k('"안 돼ー!"') == '"안 돼~!"'
    assert k('그래。。。') == '그래...'
    assert k('“。。。。。”') == '"....."'
    assert k('그건‥') == '그건…'


def test_multiline_paragraph_each_line_on_its_own():
    text = '"하나――"\n"둘, 그리고――\n셋――"'
    # line 2 has one straight quote (speech goes on), line 3 too: neither is guessed
    assert k(text) == '"하나……"\n"둘, 그리고――\n셋――"'


def test_refinement_counts_a_dash_only_edit_as_unchanged():
    """재정비: an edit that only turns the dash into …… is the same once the result file's own
    tidying is done, like a quotation-mark shape change (v2.6.2 rule)."""
    from novel_translator.refinement_runtime import same_result, _speech_parts, finalize_body
    assert same_result('"게이코가, 사실은――"', '"게이코가, 사실은……"')
    assert same_result('"그래・・・"', '"그래……"')
    assert not same_result('"게이코가, 사실은――"', '"게이코가 말이야, 사실은――"')
    # the paragraph guard sees the same speech before and after
    assert _speech_parts('"사실은――" 그가 말했다.') == _speech_parts('"사실은……" 그가 말했다.')
    assert finalize_body('"사실은――"', '"사실은――"', True, 'straight') == '"사실은……"'
    assert finalize_body('"사실은――"', '"사실은――"', False, 'straight') == '"사실은――"'


def test_hidden_marks_are_kept():
    from novel_translator.refinement_runtime import finalize_body
    zw = '​'
    original = f'"게이{zw}코가, 사실은――"'
    out = finalize_body(original, '"게이코가, 사실은――"', True, 'straight')
    assert out.replace(zw, '') == '"게이코가, 사실은……"' and zw in out
