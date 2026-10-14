"""Offline evidence-only refinement checks, independent of the translation worker."""
import pytest
from novel_translator.refinement_analysis import (
    analyze_source, dialogue_evidence, verify_evidence, propose_alias, Evidence
)


def test_span_evidence_and_paragraphs():
    text = '임설이 등장했다. 真气를 운용했다.\n\n"린쉐, 기다려!" 임설이 말했다.\n\n修炼 중이다.'
    rows = analyze_source(text, ['임설', '린쉐'])
    names = {(c.kind, c.spelling): c for c in rows}
    assert names['name', '임설'].occurrences == 2
    assert names['name', '린쉐'].occurrences == 1
    assert names['han', '真气'].occurrences == 1
    assert names['han', '修炼'].evidence[0].paragraph_id == 3
    assert all(verify_evidence(text, e, c.spelling) for c in rows for e in c.evidence)


def test_only_candidates_not_automatic_merges():
    text = '임설이 말했다.\n\n임설아는 다른 사람이다.'
    rows = analyze_source(text, ['임설', '임설아'])
    x = {(r.kind, r.spelling): r.occurrences for r in rows}
    assert x['name', '임설'] == 1
    assert x['name', '임설아'] == 1
    assert propose_alias('린쉐', '임설', {})['status'] == 'review'
    assert propose_alias('린쉐', '임설', {'린쉐': '임쉐'})['status'] == 'conflict'


def test_dialogue_is_not_attributed_to_anyone():
    text = '그는 말했다. "안녕하세요."\n\n“반말로 하자.”'
    rows = dialogue_evidence(text)
    assert len(rows) == 2 and rows[0].paragraph_id == 1 and rows[1].paragraph_id == 2
    assert all(verify_evidence(text, e) for e in rows)


def test_bounded_evidence_and_bad_input():
    text = '\n\n'.join(['임설이 修炼한다.'] * 30)
    rows = analyze_source(text, ['임설'], max_evidence=2)
    c = next(x for x in rows if x.spelling == '임설')
    assert c.occurrences == 30 and len(c.evidence) == 2
    with pytest.raises(ValueError):
        analyze_source(text, ['한'])
    with pytest.raises(ValueError):
        dialogue_evidence(text, 0)
    assert not verify_evidence(text, Evidence(999, 'x', 0, 1), 'x')
