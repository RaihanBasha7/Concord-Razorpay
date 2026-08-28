"""
Day 4.6 tests: Routing layer for Layer 2 proposal outcomes.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

import pytest

from reconciliation.groq_provider import StructuredCompletionProvider
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_orchestration import ProposalOutcome
from reconciliation.proposal_validation import ProposalOutcomeType
from reconciliation.routing import RoutingDecision, route


class _FakeProvider(StructuredCompletionProvider):
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload

    def complete_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._payload


def _make_outcome(
    outcome_type: ProposalOutcomeType,
    confidence: Optional[float] = None,
    match_ids: Optional[list[str]] = None,
) -> ProposalOutcome:
    proposal = None
    if outcome_type == ProposalOutcomeType.PROPOSAL_VALID and confidence is not None:
        proposal = MatchProposal(
            proposed_match_ids=match_ids or [],
            confidence=confidence,
            rationale="test",
        )
    return ProposalOutcome(
        outcome=outcome_type,
        proposal=proposal,
        presented_record_ids=("A", "B"),
        reason="test",
    )


class TestRoutingThresholds:
    def test_high_confidence_auto_accepts(self):
        outcome = _make_outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.95)
        assert route(outcome) == RoutingDecision.AUTO_ACCEPT

    def test_at_auto_accept_threshold_auto_accepts(self):
        outcome = _make_outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.90)
        assert route(outcome) == RoutingDecision.AUTO_ACCEPT

    def test_mid_confidence_needs_review(self):
        outcome = _make_outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.75)
        assert route(outcome) == RoutingDecision.NEEDS_REVIEW

    def test_at_review_threshold_needs_review(self):
        outcome = _make_outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.60)
        assert route(outcome) == RoutingDecision.NEEDS_REVIEW

    def test_below_review_threshold_exception(self):
        outcome = _make_outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.59)
        assert route(outcome) == RoutingDecision.EXCEPTION

    def test_zero_confidence_exception(self):
        outcome = _make_outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.0)
        assert route(outcome) == RoutingDecision.EXCEPTION


class TestFailureOutcomesNeverAutoAccept:
    def test_no_proposal_routes_to_exception(self):
        outcome = _make_outcome(ProposalOutcomeType.NO_PROPOSAL)
        assert route(outcome) == RoutingDecision.EXCEPTION

    def test_validation_failed_routes_to_exception(self):
        outcome = _make_outcome(ProposalOutcomeType.VALIDATION_FAILED)
        assert route(outcome) == RoutingDecision.EXCEPTION

    def test_api_error_routes_to_exception_even_with_stray_confidence(self):
        outcome = ProposalOutcome(
            outcome=ProposalOutcomeType.API_ERROR,
            proposal=MatchProposal(
                proposed_match_ids=["A"],
                confidence=0.99,
                rationale="stray",
            ),
            presented_record_ids=("A", "B"),
            reason="API error",
        )
        assert route(outcome) == RoutingDecision.EXCEPTION

    def test_timeout_routes_to_exception_even_with_stray_confidence(self):
        outcome = ProposalOutcome(
            outcome=ProposalOutcomeType.TIMEOUT,
            proposal=MatchProposal(
                proposed_match_ids=["A"],
                confidence=0.99,
                rationale="stray",
            ),
            presented_record_ids=("A", "B"),
            reason="Timeout",
        )
        assert route(outcome) == RoutingDecision.EXCEPTION
