"""v2.41: an address word alone (老姨, 姨姐, 李哥, 小妹, お姉さん) is never extracted as a name;
a name with an address keeps only the name (丹丹姐 -> 丹丹); entries the user adds stay."""
import pytest
from novel_translator.term_extraction import is_address_term, parse_glossary
from novel_translator import common_terms


@pytest.mark.parametrize('word', ['姨姐', '李哥', '老姨', '小妹', '阿姨', '二舅妈', '王小妹', '姐姐', '表哥',
                                  '妈咪', '老公', 'お姉さん', '姉さん', '叔母さん', 'ママ', 'おばさん',
                                  '二哥', '二姐', '老二', '老李', '老大', '哥哥', '妹妹', '嫂子', '叔叔'])
def test_address_words(word):
    assert is_address_term(word)


@pytest.mark.parametrize('word', ['丹丹姐', '李萱诗', '秦岚', '左京', '白颖', '单单', '美咲', '美咲さん',
                                  '陈屿明', '主人公', '台湾', '小雪', '小葵', '老张家'])
def test_names_are_not_address_words(word):
    assert not is_address_term(word)


def test_extraction_leaves_address_words_out_and_reports_them():
    rep = {}
    answer = '{"老姨":"노이","姨姐":"이저","李哥":"이가","秦岚":"진란","丹丹姐":"단단 언니"}'
    cands = [(k, 9) for k in ('老姨', '姨姐', '李哥', '秦岚', '丹丹姐')]
    got = parse_glossary(answer, cands, report=rep)
    assert got == {'秦岚': '진란', '丹丹': '단단'}
    assert rep['address_only'] == 3 and rep['address'] == 1


def test_shared_glossary_never_takes_or_hands_out_address_words():
    assert not common_terms.acceptable('老姨', '노이')
    assert common_terms.usable({'老姨': '노이', '秦岚': '진란'}) == {'秦岚': '진란'}


@pytest.mark.parametrize('word', ['三婶', '王叔', '表姐夫', '大伯母', '小舅', '婶子', '小姑子', '张婶', '四妹',
                                  '老三', '二嫂', '堂哥', '叔母さん', 'お祖母さん'])
def test_similar_unlisted_address_words_follow_the_same_rule(word):
    assert is_address_term(word)


@pytest.mark.parametrize('word', ['美子', '花子', '儿子', '小雪', '王子', '公子哥儿'])
def test_words_that_only_look_similar(word):
    # 美子/花子 (names), 王子 (prince), 公子哥儿: not built from a kinship word
    assert not is_address_term(word)


def test_translation_instruction_states_the_general_principle():
    from novel_translator.speech_levels import render_instruction
    text = render_instruction([{'speaker': '진란', 'listener': '단단', 'level': '반말', 'note': ''}])
    assert 'have no fixed Korean rendering' in text and 'not a list' in text


def test_one_chinese_character_is_dropped_but_one_kanji_japanese_name_is_kept():
    rep = {}
    got = parse_glossary('{"李":"이","剑":"검","秦岚":"진란"}', [('李', 9), ('剑', 9), ('秦岚', 9)], report=rep, lang='zh')
    assert got == {'秦岚': '진란'} and rep['single'] == 2
    assert parse_glossary('{"誠":"마코토","美咲":"미사키"}', [('誠', 9), ('美咲', 9)], lang='ja') == {'誠': '마코토', '美咲': '미사키'}


def test_extraction_instruction_draws_the_line():
    from novel_translator.term_extraction import build_prompts
    zh, _ = build_prompts([('秦岚', 9)], 'zh')
    ja, _ = build_prompts([('美咲', 9)], 'ja')
    assert 'EVERY character name' in zh and 'even common-sounding ones' in zh        # names: all of them
    assert '少林' in zh and 'may be selected' in zh                                   # known sects/places stay
    assert 'generic genre vocabulary' in zh and 'adjectives or verbs' in zh          # soft guidance only
    assert 'Do not select forms of address' in zh and 'treat every similar word the same way' in zh
    assert 'single-character' in zh and 'single-character' not in ja
    assert '학 아저씨' not in zh                                                      # no longer asked to render addresses
