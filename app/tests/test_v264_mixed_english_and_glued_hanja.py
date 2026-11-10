"""v2.6.4: two cases a user found in a finished Chinese novel (".ko.partial.txt").

1. A Chinese paragraph quoting an English line ("Please follow us. ... We'll take you back to
   the apartment immediately." with a Chinese gloss in brackets). A correct translation keeps
   the English line, but the avoidance check counted any 8-word English run copied from the
   source as "a sentence left untranslated" (REFUSAL, 원문 일부 미번역): every retry of the
   paragraph failed, and its Chinese tail stayed as source. English in a Chinese / Japanese
   paragraph is the book's own English and no longer counts; English books, English that is
   not in the source, and Chinese left in the paragraph are checked exactly as before.
2. One Chinese character glued inside a Korean word ("철钳처럼", "윤滑", "암사蛇처럼") was not
   counted: only paragraphs with 2 or more were. Such a paragraph now counts as untranslated
   (shown, and retried with the other leftovers); a gloss in brackets (성(性)) does not.
"""
from __future__ import annotations

import re

import pytest

from novel_translator.chunking import make_chunks, mark_translatable, split_paragraphs
from novel_translator.profiles import ProfileManager
from novel_translator.prompting import REPAIR_SYSTEM
from novel_translator.providers.base import ErrorKind, ProviderError, TranslationResponse
from novel_translator.storage import Database
from novel_translator.validation import (english_kept_from_cjk, leftover_needs_fix, untranslated_residue,
                                         validate_translation)
import novel_translator.ui.worker as worker_mod

ZH_MIXED = ('翻译老师跟在后面，不断用英语安抚，声音里带着明显的颤抖：“Please follow us. It\'s not safe here. '
            'We\'ll take you back to the apartment immediately.”（请跟我们来，这里不安全。我们马上送你们回公寓。）')
KO_MIXED = ('통역 선생님이 뒤따르며 계속 영어로 달랬는데, 목소리가 눈에 띄게 떨리고 있었다. “Please follow us. '
            'It\'s not safe here. We\'ll take you back to the apartment immediately.”(저희를 따라오세요, 여기는 '
            '안전하지 않습니다. 금방 아파트로 모셔다드리겠습니다.)')


# ---------------------------------------------------------------- 1. English quoted in a Chinese / Japanese book
def test_english_line_kept_from_a_chinese_paragraph_is_not_avoidance():
    assert untranslated_residue(ZH_MIXED, KO_MIXED, 'zh') == ''
    validate_translation('<P000001>\n' + ZH_MIXED, '<P000001>\n' + KO_MIXED, source_language='zh')


def test_without_the_language_the_old_check_is_unchanged():
    # every caller in the translation path passes the language; the default keeps the old rule
    assert untranslated_residue(ZH_MIXED, KO_MIXED) == "We'll take you back to the apartment immediately"
    with pytest.raises(ProviderError) as e:
        validate_translation('<P000001>\n' + ZH_MIXED, '<P000001>\n' + KO_MIXED)
    assert e.value.kind == ErrorKind.REFUSAL


def test_japanese_book_quoting_english_is_the_same():
    src = '彼は振り返って叫んだ。「Get out of here right now, before they come back for you!」'
    out = '그는 돌아보며 외쳤다. “Get out of here right now, before they come back for you!”'
    assert english_kept_from_cjk(src, 'ja')
    validate_translation('<P000001>\n' + src, '<P000001>\n' + out, source_language='ja')


def test_english_books_are_checked_as_before():
    src = 'He held her close. "she said that she would never ever go back to that house again" she whispered.'
    out = '그는 그녀를 꼭 끌어안았다. "she said that she would never ever go back to that house again" 그녀가 속삭였다.'
    assert untranslated_residue(src, out, 'en')
    with pytest.raises(ProviderError):
        validate_translation('<P000001>\n' + src, '<P000001>\n' + out, source_language='en')


def test_wrong_language_choice_with_an_english_paragraph_is_still_checked():
    # 원문 언어 중국어로 영어 소설을 연 경우: no Chinese in the paragraph, the old check applies
    src = 'She slowly walked to the window and watched the rain fall for a very long time.'
    out = '그녀는 She slowly walked to the window and watched the rain 했다.'
    assert not english_kept_from_cjk(src, 'zh')
    assert untranslated_residue(src, out, 'zh')


def test_chinese_left_in_the_mixed_paragraph_is_still_caught():
    half = KO_MIXED.split('.', 1)[0] + '. 他慢慢走到窗边看着外面下个不停的雨 “Please follow us.”'
    src = ZH_MIXED + '他慢慢走到窗边看着外面下个不停的雨'
    assert untranslated_residue(src, half, 'zh') == '他慢慢走到窗边看着外面下个不停的雨'
    assert leftover_needs_fix(KO_MIXED[:40] + '翻译老师跟在后面')


def test_an_answer_in_english_instead_of_korean_is_still_rejected():
    src = '他慢慢走到窗边，看着外面下个不停的雨，很久都没有说话。' * 3
    out = ('He slowly walked to the window and watched the rain that would not stop, '
           'and for a long time he said nothing at all. ') * 3
    with pytest.raises(ProviderError):
        validate_translation(f'<P000001>\n{src}\n\n<P000002>\n{src}', f'<P000001>\n{out}\n\n<P000002>\n{out}',
                             source_language='zh')


def test_mostly_english_quote_with_a_short_chinese_tag_has_enough_korean():
    eng = 'Please follow us quietly. It is not safe here anymore, and we will take you back home immediately.'
    src = f'他说：“{eng}”'
    out = f'그가 말했다. “{eng}”'
    validate_translation(f'<P000001>\n{src}\n\n<P000002>\n{src}', f'<P000001>\n{out}\n\n<P000002>\n{out}',
                         source_language='zh')


# ---------------------------------------------------------------- 2. one character glued inside a Korean word
@pytest.mark.parametrize('body', ['강호의 손이 철钳처럼 그녀의 다리를 붙잡았다.', '애액의 윤滑 덕분에 쉽게 들어갔다.',
                                  '허리는 발정 난 암사蛇처럼 뒤틀렸다.', '선배様가 웃었다.'])
def test_one_glued_character_needs_a_fix(body):
    assert leftover_needs_fix(body)


@pytest.mark.parametrize('body', ['권력과 성(性)이 뒤섞인 거래였다.', '저 성(曾) 씨 계집애한테만 눈이 가 있던데.',
                                  '안 돼一! 그녀가 외쳤다.', '모두 한국어로만 된 문단이다.', ''])
def test_glosses_and_clean_korean_need_nothing(body):
    assert not leftover_needs_fix(body)


def test_glossary_characters_are_allowed():
    assert not leftover_needs_fix('林씨가 웃었다.', allowed='林')
    assert leftover_needs_fix('林씨가 웃었다.')


def test_two_or_more_characters_count_as_before():
    assert leftover_needs_fix('그는 窗 밖을 보았다. 雨가 내렸다.')
    assert leftover_needs_fix('성(性)과 성(曾)')   # two glosses in one paragraph: unchanged rule (2 or more)


# ---------------------------------------------------------------- through the real worker
def _worker(tmp_path, text, provider):
    profile = ProfileManager().set_active('zh-ko')
    paragraphs = mark_translatable(split_paragraphs(text), 'zh')
    chunks = make_chunks(paragraphs, 'zh', target_tokens=500, min_tokens=1, max_tokens=1000)
    src = tmp_path / 'novel.txt'; raw = text.encode('utf-8'); src.write_bytes(raw)
    cfg = {'modes': {'balanced': {'thinking_level': 'medium'}}, 'rate_limiter': {'start_rpm': 1e9, 'min_rpm': 1e9, 'max_rpm': 1e9},
           'refusal_recovery': {'reframe_once': True, 'split_chunk': True, 'fallback_provider': True, 'fallback_on_policy': True}}
    worker_mod.DATA_DIR = tmp_path / 'data'; worker_mod.GeminiProvider = lambda: provider; worker_mod.load_json = lambda _p: cfg
    w = worker_mod.TranslationWorker(src, raw, profile, chunks, 'gemini-3.8-flash',
                                     [{'id': 'fake', 'secret': 'not-real', 'project_group': 'P1', 'enabled': True}], {},
                                     'balanced', tmp_path / 'out', all_paragraphs=paragraphs)
    events = []; w.issue.connect(events.append)
    return w, events


def _rows(tmp_path):
    db = Database(tmp_path / 'data' / 'translations.db')
    with db.connect() as con:
        return con.execute('SELECT chunk_index,status,translation FROM chunks ORDER BY chunk_index').fetchall()


class _Provider:
    """Answers by source paragraph text; repair requests get `repair` (or the same answer)."""
    name = 'gemini'

    def __init__(self, answers, repair=None):
        self.answers = answers; self.repair = repair; self.requests = []

    def translate(self, request, key):
        self.requests.append(request)
        src = request.user_prompt
        if request.system_prompt == REPAIR_SYSTEM:
            return TranslationResponse(self.repair or '<P000001>\n(고치지 못함)', 'STOP', {})
        rows = []
        for pid, body in re.findall(r'(?ms)^<P(\d{6})>\s*\n(.*?)(?=^<P\d{6}>\s*\n|\Z)', src.split('[SOURCE', 1)[1]):
            body = body.split('\n\nReturn the same <P######>', 1)[0].strip()
            rows.append(f'<P{pid}>\n' + next(v for k, v in self.answers.items() if k in body))
        return TranslationResponse('\n\n'.join(rows), 'STOP', {})


LEAD_ZH = '侯俊远点了点头，脸色同样不好看。他立刻带上翻译老师，几乎是半拖半拽地把还愣在原地的外宾们带向侧门。'
LEAD_KO = '호준원이 고개를 끄덕였으나 안색은 마찬가지로 좋지 않았다. 그는 곧바로 통역 선생님을 데리고 외국인 귀빈들을 옆문 쪽으로 안내했다.'


def test_the_reported_paragraph_is_translated_in_one_request(tmp_path):
    text = LEAD_ZH + ZH_MIXED + '\n\n他们很快就消失在走廊尽头。'
    prov = _Provider({'侯俊远': LEAD_KO + KO_MIXED, '他们很快': '그들은 곧 복도 끝으로 사라졌다.'})
    w, events = _worker(tmp_path, text, prov)
    w.run()
    rows = _rows(tmp_path)
    assert [r['status'] for r in rows] == ['COMPLETED']
    assert 'We\'ll take you back to the apartment immediately' in rows[0]['translation']
    assert len(prov.requests) == 1, 'a correct answer is accepted the first time (was: REFUSAL and retries)'
    assert not any('원문 일부 미번역' in e or '번역되지 않고 남아' in e for e in events)
    assert w.leftover_count == 0


def test_a_glued_character_the_repair_cannot_fix_is_reported(tmp_path):
    text = '江浩的手像铁钳一样紧紧抓住她的右腿，她完全动弹不得。'
    bad = '강호의 손이 철钳처럼 그녀의 오른쪽 다리를 꽉 붙잡고 있어 전혀 움직일 수 없었다.'
    prov = _Provider({'江浩': bad})
    w, events = _worker(tmp_path, text, prov)
    w.run()
    assert w.leftover_count == 1
    assert any('한자·가나가 남은 문단 1개' in e for e in events)
    assert '철钳처럼' in _rows(tmp_path)[0]['translation'], 'the translation itself is kept as it was'


def test_a_glued_character_is_repaired_once(tmp_path):
    text = '江浩的手像铁钳一样紧紧抓住她的右腿，她完全动弹不得。'
    bad = '강호의 손이 철钳처럼 그녀의 오른쪽 다리를 꽉 붙잡고 있어 전혀 움직일 수 없었다.'
    good = '강호의 손이 쇠집게처럼 그녀의 오른쪽 다리를 꽉 붙잡고 있어 전혀 움직일 수 없었다.'
    prov = _Provider({'江浩': bad}, repair='<P000001>\n' + good)
    w, _ = _worker(tmp_path, text, prov)
    w.run()
    assert w.leftover_count == 0
    assert _rows(tmp_path)[0]['translation'].endswith(good)


def test_a_gloss_in_brackets_is_not_reported(tmp_path):
    text = '他独自承担了学校所有腐败以及权力与性交织的交易的责任。'
    ok = '그는 학교의 모든 비리와 권력과 성(性)이 뒤섞인 거래의 책임을 홀로 짊어졌다.'
    prov = _Provider({'他独自': ok}, repair='<P000001>\n' + ok)
    w, events = _worker(tmp_path, text, prov)
    w.run()
    assert w.leftover_count == 0
    assert not any('한자·가나가 남은 문단' in e for e in events)
