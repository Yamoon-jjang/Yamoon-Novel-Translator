"""v2.5.6: "이게 용어인가" 판단을 한곳(term_extraction)에서. 추출 · 빨간 표시 · 공용 용어집 ·
말투표 인물 명단이 같은 규칙을 쓰는지, 예전 판단이 바뀌지 않았는지 확인."""
from novel_translator import term_extraction as te
from novel_translator import common_terms as ct


def test_review_reason_keeps_old_order_and_tips():
    names = {'Alan', '左京'}
    assert te.review_reason('左京', '좌경', names) == ''
    assert te.review_reason('As Alan', '애즈 앨런', names) == 'glued'
    assert te.review_reason('He\'s', '히즈', names) == 'ordinary'
    assert te.review_reason('Sister', '시스터', names) == 'doubtful'
    assert te.review_reason('林雨', '林위', names) == 'rendering'
    assert te.review_reason('', '좌경', names) == '' and te.review_reason('左京', '', names) == ''
    assert "'Alan'" in te.review_tip('As Alan', 'glued')


def test_address_on_its_own_is_now_marked_like_the_extraction_leaves_it_out():
    # The extraction never adds 二姐 (who calls her decides the Korean); a row of it left
    # from an older version or typed by hand is now shown light red too.
    assert te.is_address_only('二姐', '둘째 언니')
    assert te.review_reason('二姐', '둘째 언니', set()) == 'address'
    assert '호칭' in te.review_tip('二姐', 'address')
    # a name with an address is split by the extraction, not marked as an address
    assert not te.is_address_only('丹丹姐', '단단 언니', {'丹丹'})


def test_shared_glossary_rules_unchanged():
    assert ct.acceptable('左京', '좌경')
    for bad in ('公司', "He's", 'Son', '二姐'):
        assert not ct.acceptable(bad, '좌경')
    assert ct.usable({'Son': '손', '二姐': '이저', "He's": '히즈'}) == {'Son': '손'}, 'a doubtful word typed on purpose stays usable'


def test_one_definition_used_everywhere():
    import inspect
    from novel_translator.ui import main_window
    src = inspect.getsource(main_window)
    assert 'COMMON_WORDS' not in src and 'is_never_term' not in src and 'is_doubtful_term' not in src
