"""
Day 5.1 tests: Layer 3 deterministic guardrail routing.

These tests exercise reconciliation.layer3.route only. The Day 4.6
single-outcome helper (reconciliation.routing) is intentionally untouched and
covered separately in test_routing.py.
"""
from __future__ import annotations

from datetime import date
from typing import List

import pytest

from reconciliation.domain.models import (
    NormalizedRecord,
    ReconciliationDecision,
    ResolutionLayer,
    SourceType,
)
from reconciliation.layer3 import (
    RoutingBucket,
    RoutingReason,
    RoutingDecision,
    route,
)
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_validation import ProposalOutcome, ProposalOutcomeType

from tests.conftest import make_record


def _l1_decision(*member_ids: str) -> ReconciliationDecision:
    return ReconciliationDecision(
        decision_id=f"L1-{'_'.join(member_ids)}",
        member_record_ids=tuple(member_ids),
        resolution_layer=ResolutionLayer.LAYER_1,
        rule_or_rationale="EXACT_ID",
        confidence=1.0,
    )


def _outcome(
    outcome_type: ProposalOutcomeType,
    *,
    confidence: float | None = None,
    proposed: List[str] | None = None,
    presented: List[str] | None = None,
) -> ProposalOutcome:
    proposal = None
    if outcome_type == ProposalOutcomeType.PROPOSAL_VALID and confidence is not None:
        proposal = MatchProposal(
            proposed_match_ids=proposed or [],
            confidence=confidence,
            rationale="test",
        )
    return ProposalOutcome(
        outcome=outcome_type,
        proposal=proposal,
        presented_record_ids=tuple(presented or []),
        reason="test",
    )


def _decisions_by_id(results: List[RoutingDecision]) -> dict[str, RoutingDecision]:
    return {r.record_id: r for r in results}


class TestDeterministicMatch:
    def test_layer1_record_routes_deterministic(self):
        records = [make_record(record_id="R1", source_type="SETTLEMENT",
                               source_native_id="s1", amount_paise=100, date=date(2024, 1, 1))]
        results = route([_l1_decision("R1", "RX")], [], records)
        by_id = _decisions_by_id(results)
        assert by_id["R1"].bucket == RoutingBucket.DETERMINISTIC_MATCH
        assert by_id["R1"].reason == RoutingReason.LAYER1_DETERMINISTIC
        assert by_id["R1"].source_decision_id is not None

    def test_layer1_precedence_over_layer2(self):
        records = [make_record(record_id="R1", source_type="SETTLEMENT",
                               source_native_id="s1", amount_paise=100, date=date(2024, 1, 1))]
        l2 = _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.99,
                      proposed=["R1"], presented=["R1"])
        results = route([_l1_decision("R1", "RX")], [l2], records)
        assert _decisions_by_id(results)["R1"].bucket == RoutingBucket.DETERMINISTIC_MATCH


class TestAiBuckets:
    def test_high_confidence_auto_accepted(self):
        records = [make_record(record_id="R1", source_type="BANK",
                               source_native_id="b1", amount_paise=100, date=date(2024, 1, 1))]
        l2 = _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.95,
                      proposed=["R1"], presented=["R1"])
        results = route([], [l2], records)
        by_id = _decisions_by_id(results)
        assert by_id["R1"].bucket == RoutingBucket.AI_AUTO_ACCEPTED
        assert by_id["R1"].reason == RoutingReason.AI_CONFIDENT

    def test_mid_confidence_human_review(self):
        records = [make_record(record_id="R1", source_type="BANK",
                               source_native_id="b1", amount_paise=100, date=date(2024, 1, 1))]
        l2 = _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.75,
                      proposed=["R1"], presented=["R1"])
        results = route([], [l2], records)
        assert _decisions_by_id(results)["R1"].bucket == RoutingBucket.HUMAN_REVIEW
        assert _decisions_by_id(results)["R1"].reason == RoutingReason.AI_NEEDS_REVIEW

    def test_threshold_exactly_0_90_auto_accepted(self):
        records = [make_record(record_id="R1", source_type="BANK",
                               source_native_id="b1", amount_paise=100, date=date(2024, 1, 1))]
        l2 = _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.90,
                      proposed=["R1"], presented=["R1"])
        results = route([], [l2], records)
        assert _decisions_by_id(results)["R1"].bucket == RoutingBucket.AI_AUTO_ACCEPTED

    def test_threshold_exactly_0_60_human_review(self):
        records = [make_record(record_id="R1", source_type="BANK",
                               source_native_id="b1", amount_paise=100, date=date(2024, 1, 1))]
        l2 = _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.60,
                      proposed=["R1"], presented=["R1"])
        results = route([], [l2], records)
        assert _decisions_by_id(results)["R1"].bucket == RoutingBucket.HUMAN_REVIEW


class TestExceptions:
    def test_low_confidence_exception(self):
        records = [make_record(record_id="R1", source_type="BANK",
                               source_native_id="b1", amount_paise=100, date=date(2024, 1, 1))]
        l2 = _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.50,
                      proposed=["R1"], presented=["R1"])
        results = route([], [l2], records)
        by_id = _decisions_by_id(results)
        assert by_id["R1"].bucket == RoutingBucket.EXCEPTION
        assert by_id["R1"].reason == RoutingReason.LOW_CONFIDENCE

    def test_no_candidate_exception(self):
        records = [make_record(record_id="R1", source_type="BANK",
                               source_native_id="b1", amount_paise=100, date=date(2024, 1, 1))]
        l2 = _outcome(ProposalOutcomeType.NO_PROPOSAL, presented=["R1"])
        results = route([], [l2], records)
        by_id = _decisions_by_id(results)
        assert by_id["R1"].bucket == RoutingBucket.EXCEPTION
        assert by_id["R1"].reason == RoutingReason.NO_CANDIDATE

    @pytest.mark.parametrize(
        "outcome_type",
        [
            ProposalOutcomeType.API_ERROR,
            ProposalOutcomeType.TIMEOUT,
            ProposalOutcomeType.VALIDATION_FAILED,
        ],
    )
    def test_ai_response_invalid_exception(self, outcome_type):
        records = [make_record(record_id="R1", source_type="BANK",
                               source_native_id="b1", amount_paise=100, date=date(2024, 1, 1))]
        l2 = _outcome(outcome_type, presented=["R1"])
        results = route([], [l2], records)
        by_id = _decisions_by_id(results)
        assert by_id["R1"].bucket == RoutingBucket.EXCEPTION
        assert by_id["R1"].reason == RoutingReason.AI_RESPONSE_INVALID
        assert by_id["R1"].source_outcome == outcome_type

    def test_true_residual_with_no_layer2_attempt_not_dropped(self):
        records = [make_record(record_id="R1", source_type="LEDGER",
                               source_native_id="l1", amount_paise=100, date=date(2024, 1, 1))]
        results = route([], [], records)
        by_id = _decisions_by_id(results)
        assert by_id["R1"].bucket == RoutingBucket.EXCEPTION
        assert by_id["R1"].reason == RoutingReason.NO_CANDIDATE


class TestCoverageAndInvariants:
    def test_exactly_one_decision_per_record(self):
        records = [
            make_record(record_id=f"R{i}", source_type="BANK",
                        source_native_id=f"b{i}", amount_paise=100, date=date(2024, 1, 1))
            for i in range(5)
        ]
        l1 = [_l1_decision("R0", "RX")]
        l2 = [
            _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.95,
                     proposed=["R1"], presented=["R1"]),
            _outcome(ProposalOutcomeType.NO_PROPOSAL, presented=["R2"]),
            _outcome(ProposalOutcomeType.API_ERROR, presented=["R3"]),
        ]
        results = route(l1, l2, records)
        assert len(results) == len(records)
        assert {r.record_id for r in results} == {f"R{i}" for i in range(5)}
        assert len({r.record_id for r in results}) == len(results)

    def test_inputs_not_mutated(self):
        records = [make_record(record_id="R1", source_type="BANK",
                               source_native_id="b1", amount_paise=100, date=date(2024, 1, 1))]
        l1 = [_l1_decision("R1", "RX")]
        l2_in = [
            _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.99,
                     proposed=["R1"], presented=["R1"]),
        ]
        l2_snapshot = [
            (o.outcome, o.proposal, tuple(o.presented_record_ids)) for o in l2_in
        ]
        records_snapshot = [(r.record_id, r.amount_paise) for r in records]

        route(l1, l2_in, records)

        assert [(o.outcome, o.proposal, tuple(o.presented_record_ids)) for o in l2_in] == l2_snapshot
        assert [(r.record_id, r.amount_paise) for r in records] == records_snapshot
        assert l1[0].member_record_ids == ("R1", "RX")
