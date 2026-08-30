"""
Day 4.6 — Routing layer for Layer 2 proposal outcomes.

Consumes a validated ProposalOutcome and deterministically maps it to a routing
decision. This module has no knowledge of evaluation/ground truth; it only
enforces the safety rules described below.
"""
from __future__ import annotations

from enum import Enum

from reconciliation.proposal_validation import ProposalOutcome, ProposalOutcomeType

AUTO_ACCEPT_THRESHOLD = 0.90
REVIEW_THRESHOLD = 0.60


class ProposalVerdict(str, Enum):
    """Scenario-level verdict for a Layer 2 proposal outcome.

    This is deliberately distinct from layer3.RoutingDecision, which is
    the per-record routing model combining Layer 1 and Layer 2 results.
    """

    AUTO_ACCEPT = "AUTO_ACCEPT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    EXCEPTION = "EXCEPTION"


def route(outcome: ProposalOutcome) -> ProposalVerdict:
    if outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID:
        confidence = outcome.proposal.confidence if outcome.proposal else 0.0
        if confidence >= AUTO_ACCEPT_THRESHOLD:
            return ProposalVerdict.AUTO_ACCEPT
        if confidence >= REVIEW_THRESHOLD:
            return ProposalVerdict.NEEDS_REVIEW
        return ProposalVerdict.EXCEPTION

    return ProposalVerdict.EXCEPTION
