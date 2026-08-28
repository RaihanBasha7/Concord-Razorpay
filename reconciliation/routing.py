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


class RoutingDecision(str, Enum):
    AUTO_ACCEPT = "AUTO_ACCEPT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    EXCEPTION = "EXCEPTION"


def route(outcome: ProposalOutcome) -> RoutingDecision:
    if outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID:
        confidence = outcome.proposal.confidence if outcome.proposal else 0.0
        if confidence >= AUTO_ACCEPT_THRESHOLD:
            return RoutingDecision.AUTO_ACCEPT
        if confidence >= REVIEW_THRESHOLD:
            return RoutingDecision.NEEDS_REVIEW
        return RoutingDecision.EXCEPTION

    return RoutingDecision.EXCEPTION
