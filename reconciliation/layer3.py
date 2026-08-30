"""
Day 5.1 — Layer 3 deterministic guardrail routing.

Reads the immutable Layer 1 and Layer 2 outputs and PRODUCES a new, separate
per-record routing result. Layer 3 is 100% deterministic Python and never calls
an LLM or any provider. It is independent of FastAPI, persistence, and the
evaluation/ground-truth packages (which it must never import).

This module is intentionally distinct from reconciliation.routing (Day 4.6),
which remains the narrower Layer 2 single-outcome helper. Layer 3 projects the
Layer 2 scenario/group outcomes onto individual records so that exactly one
routing decision is produced per record.

Projection rules (records are keyed by record_id):
  * A record that participates in a Layer 1 decision wins deterministically
    (DETERMINISTIC_MATCH) and is never re-routed by Layer 2.
  * A valid Layer 2 proposal routes ONLY its proposed_match_ids by confidence:
        confidence >= 0.90            -> AI_AUTO_ACCEPTED
        confidence >= 0.60 (< 0.90)  -> HUMAN_REVIEW
        confidence <  0.60            -> EXCEPTION / LOW_CONFIDENCE
    Other records merely presented to Layer 2 (candidates or members the model
    did not propose) are NOT invented as matches; they fall to EXCEPTION /
    NO_CANDIDATE rather than a false positive claim.
  * NO_PROPOSAL (model responded, proposed nothing) -> EXCEPTION / NO_CANDIDATE.
  * VALIDATION_FAILED / API_ERROR / TIMEOUT -> EXCEPTION / AI_RESPONSE_INVALID.
  * A record with neither a Layer 1 decision nor any Layer 2 outcome (a true
    residual with no Layer 2 attempt) -> EXCEPTION / NO_CANDIDATE. This is not
    an AI failure, so AI_RESPONSE_INVALID is deliberately avoided.

When a record is presented by more than one Layer 2 outcome (e.g. as a member of
one scenario and a retrieved candidate of another), the first outcome in input
order assigns the record and later overlaps are ignored. Residual scenarios are
expected to be disjoint on their member records, so this is deterministic and
never produces two decisions for one record.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, List, Optional, Tuple

from reconciliation.domain.models import NormalizedRecord, ReconciliationDecision
from reconciliation.proposal_validation import ProposalOutcome, ProposalOutcomeType

AUTO_ACCEPT_THRESHOLD = 0.90
REVIEW_THRESHOLD = 0.60


class RoutingBucket(str, Enum):
    DETERMINISTIC_MATCH = "DETERMINISTIC_MATCH"
    AI_AUTO_ACCEPTED = "AI_AUTO_ACCEPTED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    EXCEPTION = "EXCEPTION"


class RoutingReason(str, Enum):
    LAYER1_DETERMINISTIC = "LAYER1_DETERMINISTIC"
    AI_CONFIDENT = "AI_CONFIDENT"
    AI_NEEDS_REVIEW = "AI_NEEDS_REVIEW"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    AI_RESPONSE_INVALID = "AI_RESPONSE_INVALID"
    NO_CANDIDATE = "NO_CANDIDATE"


@dataclass(frozen=True)
class RoutingDecision:
    """
    A per-record Layer 3 routing verdict.

    bucket and reason are kept separate on purpose: the bucket is the routing
    destination, while reason explains *why* (e.g. which exception class fired).
    source_decision_id / source_outcome carry only identifiers for audit
    traceability and never expose provider secrets or raw payloads.
    """

    record_id: str
    bucket: RoutingBucket
    reason: RoutingReason
    source_decision_id: Optional[str] = None
    source_outcome: Optional[ProposalOutcomeType] = None
    confidence: Optional[float] = None


def route(
    layer1_decisions: Iterable[ReconciliationDecision],
    layer2_outcomes: Iterable[ProposalOutcome],
    all_records: Iterable[NormalizedRecord],
) -> List[RoutingDecision]:
    """
    Deterministically route every input record to exactly one RoutingDecision.

    The provided collections are never mutated. Inputs are read only; a brand
    new list is returned in the same order as all_records.
    """
    layer1 = list(layer1_decisions)
    outcomes = list(layer2_outcomes)
    records = list(all_records)

    decisions: dict[str, RoutingDecision] = {}

    _apply_layer1(layer1, decisions)
    _apply_layer2(outcomes, decisions)

    for record in records:
        if record.record_id not in decisions:
            decisions[record.record_id] = RoutingDecision(
                record_id=record.record_id,
                bucket=RoutingBucket.EXCEPTION,
                reason=RoutingReason.NO_CANDIDATE,
                confidence=None,
            )

    return [decisions[record.record_id] for record in records]


def _apply_layer1(
    layer1: List[ReconciliationDecision],
    decisions: dict[str, RoutingDecision],
) -> None:
    for decision in layer1:
        for record_id in decision.member_record_ids:
            decisions[record_id] = RoutingDecision(
                record_id=record_id,
                bucket=RoutingBucket.DETERMINISTIC_MATCH,
                reason=RoutingReason.LAYER1_DETERMINISTIC,
                source_decision_id=decision.decision_id,
                confidence=decision.confidence,
            )


def _apply_layer2(
    outcomes: List[ProposalOutcome],
    decisions: dict[str, RoutingDecision],
) -> None:
    for outcome in outcomes:
        proposed_ids = (
            set(outcome.proposal.proposed_match_ids)
            if outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID
            and outcome.proposal is not None
            else set()
        )

        for record_id in outcome.presented_record_ids:
            if record_id in decisions:
                continue

            if outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID:
                if record_id in proposed_ids:
                    bucket, reason, confidence = _accepted_bucket(outcome.proposal.confidence)
                else:
                    bucket, reason, confidence = (
                        RoutingBucket.EXCEPTION,
                        RoutingReason.NO_CANDIDATE,
                        None,
                    )
            elif outcome.outcome == ProposalOutcomeType.NO_PROPOSAL:
                bucket, reason, confidence = (
                    RoutingBucket.EXCEPTION,
                    RoutingReason.NO_CANDIDATE,
                    None,
                )
            else:
                bucket, reason, confidence = (
                    RoutingBucket.EXCEPTION,
                    RoutingReason.AI_RESPONSE_INVALID,
                    None,
                )

            decisions[record_id] = RoutingDecision(
                record_id=record_id,
                bucket=bucket,
                reason=reason,
                source_outcome=outcome.outcome,
                confidence=confidence,
            )


def _accepted_bucket(confidence: float) -> Tuple[RoutingBucket, RoutingReason, float]:
    if confidence >= AUTO_ACCEPT_THRESHOLD:
        return RoutingBucket.AI_AUTO_ACCEPTED, RoutingReason.AI_CONFIDENT, confidence
    if confidence >= REVIEW_THRESHOLD:
        return RoutingBucket.HUMAN_REVIEW, RoutingReason.AI_NEEDS_REVIEW, confidence
    return RoutingBucket.EXCEPTION, RoutingReason.LOW_CONFIDENCE, confidence
