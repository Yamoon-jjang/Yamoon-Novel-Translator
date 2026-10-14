"""Immutable source-bound review decisions for Korean novel refinement.

Never automatically merge people or rewrite the input. Model suggestions cannot
override approvals, rejections, or explicit scene-specific speech exceptions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Mapping
import json

from .refinement_analysis import Evidence, verify_evidence


@dataclass(frozen=True)
class Proposal:
    kind: str
    value: str
    proposed_value: str
    evidence: tuple[Evidence, ...]
    engine: str = ''


@dataclass
class ReviewLedger:
    source_sha256: str
    approved: dict[str, dict[str, str]] = field(default_factory=dict)
    rejected: dict[str, set[str]] = field(default_factory=dict)
    proposals: list[Proposal] = field(default_factory=list)

    @classmethod
    def for_source(cls, source: str) -> 'ReviewLedger':
        return cls(sha256(source.encode('utf-8', 'surrogatepass')).hexdigest())

    def _assert_source(self, source: str) -> None:
        if sha256(source.encode('utf-8', 'surrogatepass')).hexdigest() != self.source_sha256:
            raise ValueError('원문이 변경되어 이전 근거를 적용할 수 없습니다.')

    def add_proposal(self, source: str, proposal: Proposal) -> str:
        """Require source evidence; never promote model suggestions into approvals."""
        self._assert_source(source)
        if proposal.kind not in ('name', 'term', 'speech'):
            raise ValueError('잘못된 후보 종류입니다.')
        if not proposal.value.strip() or not proposal.proposed_value.strip():
            raise ValueError('빈 후보 값은 허용하지 않습니다.')
        if not proposal.evidence or not all(verify_evidence(source, e, proposal.value if proposal.kind in ('name', 'term') else '') for e in proposal.evidence):
            raise ValueError('원문으로 확인되지 않은 근거입니다.')
        if proposal.value in self.rejected.get(proposal.kind, set()):
            return 'rejected'
        previous = self.approved.get(proposal.kind, {}).get(proposal.value)
        if previous is not None:
            return 'already-approved' if previous == proposal.proposed_value else 'conflict'
        if proposal not in self.proposals:
            self.proposals.append(proposal)
        return 'review'

    def approve(self, kind: str, value: str, replacement: str) -> None:
        """Explicit user-facing approval action; not called by model import."""
        if kind not in ('name', 'term', 'speech') or not value.strip() or not replacement.strip():
            raise ValueError('확정값을 확인하세요.')
        if value in self.rejected.get(kind, set()):
            raise ValueError('거절한 후보는 거절 해제 후 확정하세요.')
        old = self.approved.setdefault(kind, {}).get(value)
        if old is not None and old != replacement:
            raise ValueError('이미 확정된 값을 변경하려면 명시적 수정 절차가 필요합니다.')
        self.approved[kind][value] = replacement

    def reject(self, kind: str, value: str) -> None:
        if kind not in ('name', 'term', 'speech') or not value.strip():
            raise ValueError('거절할 후보를 확인하세요.')
        if value in self.approved.get(kind, {}):
            raise ValueError('확정된 항목은 먼저 명시적으로 해제해야 합니다.')
        self.rejected.setdefault(kind, set()).add(value)

    def snapshot(self) -> dict:
        """Serializable human-review state. Proposals are discarded on re-analysis."""
        return {
            'schema': 1,
            'source_sha256': self.source_sha256,
            'approved': {k: dict(sorted(v.items())) for k, v in sorted(self.approved.items())},
            'rejected': {k: sorted(v) for k, v in sorted(self.rejected.items())},
        }

    @classmethod
    def from_snapshot(cls, source: str, state: Mapping) -> 'ReviewLedger':
        if state.get('schema') != 1:
            raise ValueError('지원하지 않는 검토 데이터입니다.')
        ledger = cls.for_source(source)
        if state.get('source_sha256') != ledger.source_sha256:
            raise ValueError('이 검토 데이터는 다른 원문에서 작성되었습니다.')
        approved, rejected = state.get('approved', {}), state.get('rejected', {})
        if not isinstance(approved, dict) or not isinstance(rejected, dict):
            raise ValueError('검토 데이터 구조가 잘못되었습니다.')
        for kind, rows in approved.items():
            if kind not in ('name', 'term', 'speech') or not isinstance(rows, dict):
                raise ValueError('잘못된 확정 항목입니다.')
            for key, value in rows.items():
                ledger.approve(kind, key, value)
        for kind, rows in rejected.items():
            if kind not in ('name', 'term', 'speech') or not isinstance(rows, list):
                raise ValueError('잘못된 거절 항목입니다.')
            for key in rows:
                ledger.reject(kind, key)
        return ledger


def review_prompt_contract(ledger: ReviewLedger) -> str:
    """Provide only user-approved facts to the correction model."""
    approved = ledger.snapshot()['approved']
    return json.dumps(approved, ensure_ascii=False, sort_keys=True)
