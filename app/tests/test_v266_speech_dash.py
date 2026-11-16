"""v2.6.6: Korean punctuation for speech that trails off in a dash, and for ・・・.

Japanese and Chinese novels end or open a line of speech with a dash (「実は――」 “我——”), and a
model usually copies it into the translation ("사실은――"), which does not read as Korean. The
result file's punctuation step (문장부호 정리) now writes "……" for a dash that sits right
inside a quotation mark, or that opens a new sentence inside the same speech ("무슨 일입니까.
――자,"). Everything else keeps its dash: a dash in the middle of a sentence, a
scene-break line, a dash outside the quotes, a line whose straight quotes do not pair up, and
untranslated source paragraphs. Japanese middle-dot ellipses (・・・) become "……" instead of
"···"; a single ・ between names still becomes ·.

Like the rest of the punctuation step this is applied when a result file is written and to the
live 번역 결과 (진행 중) view, which now shows exactly what the file will hold: the stored
translation, the request and the job identity do not change.
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
    # a dash that opens a new sentence inside the same speech -> ……
    ('“갑자기 무슨 일입니까. ――자, 일단 진정하고 말씀하세요.”', '"갑자기 무슨 일입니까. ……자, 일단 진정하고 말씀하세요."'),
    ('"무슨 일이야! ――잠깐, 저건 뭐지?"', '"무슨 일이야! ……잠깐, 저건 뭐지?"'),
    ('"그래요? ――그럼 가죠."', '"그래요? ……그럼 가죠."'),
    ('"그만해. ――"', '"그만해. ……"'),
    ('"그건 말이지. ――그러니까――말이야."', '"그건 말이지. ……그러니까――말이야."'),   # the second dash is mid-sentence
    ('그는 말했다. ――그런데 그녀는 없었다.', '그는 말했다. ――그런데 그녀는 없었다.'),   # narration
    ('"좋아." 그는 웃었다. ――그리고 "가자"라고 했다.', '"좋아." 그는 웃었다. ――그리고 "가자"라고 했다.'),
    ('"끝났어…… ――이제 됐어."', '"끝났어…… ――이제 됐어."'),                 # no "…… ……"
    ('"하나. ― 둘."', '"하나. ― 둘."'),                                     # a spaced dash is not a sentence start
    ('"싫어, 그건. ――그만', '"싫어, 그건. ――그만'),                         # quotes do not pair up
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


# ---------------------------------------------------------------- live view = result file
def _live_run(tmp_path, punct=None):
    import re
    from test_refusal_recovery import ScriptedProvider, make_worker
    from novel_translator.ui import worker as worker_mod
    text = '\n\n'.join(f'　第{i}の段落です。' + 'あ' * 60 for i in range(6))
    lines = {0: '“갑자기 무슨 일입니까. ――자, 일단 진정하고 말씀하세요.”', 1: '“게이코가, 사실은――”',
             2: '――야마자키 탐정 사무소―― 이곳의 소장은 친구다.', 3: '“그래・・・”', 4: '그는 말했다。', 5: '“아앗――♡”'}
    def rule(prompt, src, body):
        m = re.search(r'第(\d+)の', body)
        return lines[int(m.group(1))] if m else None
    w, chunks, _ = make_worker(tmp_path, text, ScriptedProvider(rule))
    if punct is not None:
        w._punct_settings = staticmethod(lambda: punct)
    shown = []
    w.chunk_translated.connect(lambda pos, total, t: shown.append(t))
    w.run()
    out = (tmp_path / 'out' / 'novel.ko.txt').read_text(encoding='utf-8-sig')
    return [x for t in shown for x in t.split('\n') if x.strip()], out, len(chunks)


def test_live_view_shows_what_the_result_file_holds(tmp_path):
    live, out, n = _live_run(tmp_path)
    assert n >= 2 and len(live) == 6
    file_lines = [x.strip() for x in out.split('\n') if x.strip()]
    assert [x.strip() for x in live] == file_lines           # same text, line for line
    assert '"갑자기 무슨 일입니까. ……자, 일단 진정하고 말씀하세요."' in live
    assert '"게이코가, 사실은……"' in live and '"그래……"' in live and '"아앗……♡"' in live and '그는 말했다.' in live
    assert any(x.startswith('――야마자키 탐정 사무소――') for x in live)
    assert not any('“' in x or '”' in x for x in live)       # 따옴표 setting (곧은) applies too


def test_live_view_follows_punctuation_off(tmp_path):
    live, out, _ = _live_run(tmp_path, punct=(False, 'straight'))
    assert '“게이코가, 사실은――”' in live and '그는 말했다。' in live


def test_live_view_keeps_hidden_marks(tmp_path):
    import re
    from test_refusal_recovery import ScriptedProvider, make_worker
    zw = '\u200b'
    text = f'　第0の段{zw}落です。' + 'あ' * 60
    w, _, _ = make_worker(tmp_path, text, ScriptedProvider(lambda p, s, b: '“사실은――”' if '第0の' in b else None))
    shown = []
    w.chunk_translated.connect(lambda pos, total, t: shown.append(t))
    w.run()
    live = '\n'.join(shown)
    assert zw in live and live.replace(zw, '').strip() == '"사실은……"'
    out = (tmp_path / 'out' / 'novel.ko.txt').read_text(encoding='utf-8-sig')
    assert out.strip() == live.strip()
