"""Kinship addresses (姐 → 누나/언니, 哥 → 형/오빠) follow who speaks: the glossary fixes only
the name, the speech table and the translation instruction follow the narrator's gender,
and names that disagree with that gender are reported."""
import pytest
from novel_translator.term_extraction import split_address, parse_glossary
from novel_translator.speech_levels import (clean_rows, narrator_gender, address_mismatches,
                                            render_instruction)
from novel_translator import common_terms


@pytest.mark.parametrize('src,ko,expected', [
    ('丹丹姐', '단단 언니', ('丹丹', '단단')),
    ('丹丹姐姐', '단단언니', ('丹丹', '단단')),
    ('阿强哥', '아창 형', ('阿强', '아창')),
    ('美咲姉さん', '미사키 언니', ('美咲', '미사키')),
    ('李小妹', '이소매', None),        # a transliterated name, not name + address
    ('林姐', '린 언니', None),          # a bare surname would match everywhere: kept as is
    ('姐姐', '언니', None),
    ('张伟', '장웨이', None),
])
def test_split_address(src, ko, expected):
    assert split_address(src, ko) == expected


def test_glossary_keeps_only_the_name_and_counts_it():
    rep = {}
    got = parse_glossary('{"丹丹姐":"단단 언니","秦岚":"진란"}', [('丹丹姐', 9), ('秦岚', 20)], report=rep)
    assert got == {'丹丹': '단단', '秦岚': '진란'} and rep['address'] == 1
    # The name already confirmed: the address entry is dropped, the confirmed name stays.
    got = parse_glossary('{"丹丹姐":"단단 언니"}', [('丹丹姐', 9)], known={'丹丹': '단단'})
    assert got == {}


def test_shared_glossary_never_spreads_an_address(tmp_path):
    assert not common_terms.acceptable('丹丹姐', '단단 언니')
    assert common_terms.acceptable('丹丹', '단단')
    assert common_terms.usable({'丹丹姐': '단단 언니', '秦岚': '진란'}) == {'秦岚': '진란'}


MOTHER_SON = [{'speaker': '나', 'listener': '엄마', 'level': '반말', 'note': '모자'},
              {'speaker': '나', 'listener': '언니·누나', 'level': '반말', 'note': ''},
              {'speaker': '언니·누나', 'listener': '나', 'level': '반말', 'note': ''},
              {'speaker': '오빠·형', 'listener': '나', 'level': '반말', 'note': ''}]


def test_narrator_gender_from_the_narrators_own_rows():
    assert narrator_gender(MOTHER_SON) == 'male'
    assert narrator_gender([{'speaker': '엄마', 'listener': '나', 'level': '반말', 'note': '모녀'}]) == 'female'
    assert narrator_gender([{'speaker': '나', 'listener': '오빠', 'level': '반말', 'note': ''}]) == 'female'
    # Rows of other people, or a contradiction, decide nothing.
    assert narrator_gender([{'speaker': '엄마', 'listener': '소매', 'level': '반말', 'note': '모녀'}]) == ''
    assert narrator_gender([{'speaker': '나', 'listener': '엄마', 'level': '반말', 'note': '모자'},
                            {'speaker': '나', 'listener': '아빠', 'level': '반말', 'note': '부녀'}]) == ''
    # "아는 언니" is someone else's relation word, not the narrator's gender.
    assert narrator_gender([{'speaker': '단단 언니', 'listener': '나', 'level': '반말', 'note': '아는 언니'}]) == ''


def test_combined_roles_become_the_narrators_word():
    rows = clean_rows(MOTHER_SON)
    pairs = {(r['speaker'], r['listener']) for r in rows}
    assert ('나', '누나') in pairs and ('누나', '나') in pairs and ('형', '나') in pairs
    assert not any('·' in r['speaker'] + r['listener'] for r in rows)
    daughter = clean_rows([{'speaker': '나', 'listener': '엄마', 'level': '해요체', 'note': '모녀'},
                           {'speaker': '나', 'listener': '언니·누나', 'level': '반말', 'note': ''}])
    assert ('나', '언니') in {(r['speaker'], r['listener']) for r in daughter}
    unknown = clean_rows([{'speaker': '나', 'listener': '언니·누나', 'level': '반말', 'note': ''}])
    assert unknown[0]['listener'] == '언니·누나'       # not decided: left for the translator


def test_translation_instruction_names_the_narrators_address():
    text = render_instruction(MOTHER_SON)
    assert '나 is a man' in text and '누나' in text and '〇〇 형' in text
    assert 'is a woman' not in text
    assert 'is a man' not in render_instruction([{'speaker': '엄마', 'listener': '소매', 'level': '반말', 'note': '모녀'}])


def test_address_mismatches():
    assert address_mismatches('male', ['단단 언니', '엄마', '오빠', '진란', '단단 누나']) == ['단단 언니']
    assert address_mismatches('female', ['단단 누나', '아창 형', '단단 언니']) == ['단단 누나', '아창 형']
    assert address_mismatches('', ['단단 언니']) == []


def test_window_reports_the_narrator_and_wrong_addresses(monkeypatch):
    from novel_translator.ui.main_window import MainWindow

    class Fake:
        def __init__(self, rows, terms):
            self.speech_table = object(); self.term_table = object()
            self._rows, self._t = rows, terms
        def _speech_rows(self): return clean_rows(self._rows)
        def _terms(self): return dict(self._t)

    note = MainWindow._address_note(Fake(MOTHER_SON + [{'speaker': '단단 언니', 'listener': '나', 'level': '반말', 'note': ''}],
                                         {'丹丹姐': '단단 언니', '秦岚': '진란'}))
    assert '1인칭 화자(나): 남자' in note and '姐는 누나' in note
    assert '단단 언니(丹丹姐)' in note
    # A glossary entry that fixes an address is reported even before the narrator is known.
    early = MainWindow._address_note(Fake([], {'丹丹姐': '단단 언니', '老姨': '노이'}))
    assert '이름만 단단(丹丹)' in early and '노이(老姨) → 호칭이라 빼기' in early and '1인칭 화자' not in early
    assert MainWindow._address_note(Fake([], {'秦岚': '진란'})) == ''


def test_an_answer_may_name_either_half_of_a_combined_role():
    from novel_translator.speech_levels import role_parse_names, parse_table
    names = role_parse_names({'姐姐': '언니·누나', '我 (1인칭 화자)': '나', '妈妈': '엄마'})
    rows = parse_table('나\t엄마\t반말\t모자\n나\t누나\t반말\t남매', names)
    assert ('나', '언니·누나') in {(r['speaker'], r['listener']) for r in rows}
    assert ('나', '누나') in {(r['speaker'], r['listener']) for r in clean_rows(rows)}
