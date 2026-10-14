"""Review ledger: source evidence and human decisions are authoritative."""
import pytest

from novel_translator.refinement_review import Proposal, ReviewLedger, review_prompt_contract
from novel_translator.refinement_analysis import analyze_source


SOURCE = '임설이 말했다.\n\n"린쉐, 여기로 와."\n\n임설아는 다른 인물이다.'


def _evidence(value):
    return next(x.evidence for x in analyze_source(SOURCE, ['임설', '린쉐', '임설아']) if x.spelling == value)


def test_model_proposal_is_not_approval():
    ledger = ReviewLedger.for_source(SOURCE)
    suggestion = Proposal('name', '린쉐', '임설', _evidence('린쉐'), 'local')
    assert ledger.add_proposal(SOURCE, suggestion) == 'review'
    assert ledger.approved == {}
    ledger.approve('name', '린쉐', '임설')
    assert ledger.add_proposal(SOURCE, suggestion) == 'already-approved'
    assert ledger.add_proposal(SOURCE, Proposal('name', '린쉐', '임설아', _evidence('린쉐'), 'gemini')) == 'conflict'
    assert '린쉐' in review_prompt_contract(ledger)


def test_rejection_cannot_be_overridden():
    ledger = ReviewLedger.for_source(SOURCE)
    ledger.reject('name', '임설아')
    assert ledger.add_proposal(SOURCE, Proposal('name', '임설아', '임설', _evidence('임설아'))) == 'rejected'
    with pytest.raises(ValueError):
        ledger.approve('name', '임설아', '임설')


def test_evidence_and_source_binding():
    ledger = ReviewLedger.for_source(SOURCE)
    suggestion = Proposal('name', '린쉐', '임설', _evidence('린쉐'))
    with pytest.raises(ValueError):
        ledger.add_proposal(SOURCE + '추가', suggestion)
    with pytest.raises(ValueError):
        ledger.add_proposal(SOURCE, Proposal('name', '린쉐', '임설', ()))
    ledger.approve('name', '린쉐', '임설')
    snap = ledger.snapshot()
    assert ReviewLedger.from_snapshot(SOURCE, snap).snapshot() == snap
    with pytest.raises(ValueError):
        ReviewLedger.from_snapshot(SOURCE + '추가', snap)


def test_speech_approval_separate_from_name():
    ledger = ReviewLedger.for_source(SOURCE)
    ledger.approve('speech', 'person1→person2@chapter3', '존댓말')
    ledger.approve('name', '린쉐', '임설')
    assert ledger.approved['speech'] == {'person1→person2@chapter3': '존댓말'}
    assert ledger.approved['name'] == {'린쉐': '임설'}
