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

from reconciliation.config import DEFAULT_TOLERANCES
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
    LOW_EVIDENCE = "LOW_EVIDENCE"
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


def _check_financial_evidence(
    proposal: "MatchProposal",
    all_records_by_id: dict[str, NormalizedRecord],
    amount_tolerance_paise: int = DEFAULT_TOLERANCES.amount_tolerance_paise,
    date_window_days: int = DEFAULT_TOLERANCES.date_window_days,
) -> bool:
    """Check whether a proposal has sufficient deterministic financial evidence.

    This is the core safety gate that prevents auto-acceptance of proposals
    lacking deterministic financial/structural evidence.  It operates purely
    on record structural properties — never on scenario labels, ground truth,
    or evaluation metadata.

    Evidence rules by proposal cardinality:

    **Exactly 2 records (pair):**
      - Amounts must be within tolerance, AND
      - At least one stronger signal:
        * Order IDs match exactly (both non-None and equal), OR
        * Dates are within the configured window.
      - This applies to both cross-source and same-source pairs.
      - Same-source pairs (legitimate duplicates) require the same evidence
        standard as cross-source pairs: amount + (order_id OR date).

    **3+ records (multi-record):**
      Must satisfy the split-settlement invariant:
      - Exactly two distinct source types among proposed records.
      - One side has exactly 1 record (the "whole"), the other has N ("parts").
      - Sum of parts' amounts is within tolerance of the whole.
      - All parts' dates are within the date window of the whole.
      - Otherwise return False → HUMAN_REVIEW, not AI_AUTO_ACCEPTED.

    **Fewer than 2 records:**
      Return False (insufficient to establish any relationship).

    Returns True if evidence is sufficient for auto-acceptance.
    """
    proposed_ids = proposal.proposed_match_ids
    n = len(proposed_ids)

    if n < 2:
        return False

    records = [all_records_by_id.get(pid) for pid in proposed_ids]
    if any(r is None for r in records):
        return True  # Validation already caught unknown IDs

    if n == 2:
        return _check_pair_evidence(
            records[0], records[1], amount_tolerance_paise, date_window_days,
        )

    # n >= 3
    return _check_multi_record_evidence(
        records, amount_tolerance_paise, date_window_days,
    )


def _check_pair_evidence(
    r1: NormalizedRecord,
    r2: NormalizedRecord,
    amount_tolerance_paise: int,
    date_window_days: int,
) -> bool:
    """Evidence check for exactly 2 proposed records.

    Both same-source and cross-source pairs use the same evidence standard:
    amounts within tolerance AND at least one stronger signal (order ID match
    or date compatibility).
    """
    if abs(r1.amount_paise - r2.amount_paise) > amount_tolerance_paise:
        return False

    order_id_match = (
        r1.order_id_hint is not None
        and r2.order_id_hint is not None
        and r1.order_id_hint == r2.order_id_hint
    )
    date_ok = abs((r1.date - r2.date).days) <= date_window_days

    return order_id_match or date_ok


def _check_multi_record_evidence(
    records: list[NormalizedRecord],
    amount_tolerance_paise: int,
    date_window_days: int,
) -> bool:
    """Evidence check for 3+ proposed records.

    The only legitimate multi-record reconciliation pattern in Concord is
    split settlement: one record from source type A (the "whole") and N
    records from source type B (the "parts") whose amounts sum to the
    whole within tolerance.

    Deterministic invariants:
      - Exactly two distinct source types.
      - One side has exactly 1 record, the other has N >= 2.
      - sum(parts.amount_paise) within tolerance of whole.amount_paise.
      - All part dates within date_window_days of the whole's date.

    If the system cannot deterministically establish these invariants,
    returns False → HUMAN_REVIEW rather than trusting model confidence.
    """
    from reconciliation.domain.models import SourceType

    # Group by source type
    by_source: dict[SourceType, list[NormalizedRecord]] = {}
    for r in records:
        by_source.setdefault(r.source_type, []).append(r)

    source_types = list(by_source.keys())

    # Must have exactly 2 distinct source types for a split relationship
    if len(source_types) != 2:
        return False

    groups = sorted(by_source.values(), key=len)
    whole_group, part_group = groups[0], groups[1]

    # The "whole" side must have exactly 1 record
    if len(whole_group) != 1:
        return False

    whole = whole_group[0]
    parts = part_group

    # Sum of parts must be within tolerance of whole
    parts_sum = sum(p.amount_paise for p in parts)
    if abs(parts_sum - whole.amount_paise) > amount_tolerance_paise:
        return False

    # All part dates must be compatible with the whole's date
    for p in parts:
        if abs((p.date - whole.date).days) > date_window_days:
            return False

    return True


def route(
    layer1_decisions: Iterable[ReconciliationDecision],
    layer2_outcomes: Iterable[ProposalOutcome],
    all_records: Iterable[NormalizedRecord],
    amount_tolerance_paise: int = DEFAULT_TOLERANCES.amount_tolerance_paise,
    date_window_days: int = DEFAULT_TOLERANCES.date_window_days,
) -> List[RoutingDecision]:
    """
    Deterministically route every input record to exactly one RoutingDecision.

    The provided collections are never mutated. Inputs are read only; a brand
    new list is returned in the same order as all_records.
    """
    layer1 = list(layer1_decisions)
    outcomes = list(layer2_outcomes)
    records = list(all_records)

    records_by_id = {r.record_id: r for r in records}

    decisions: dict[str, RoutingDecision] = {}

    _apply_layer1(layer1, decisions)
    _apply_layer2(outcomes, decisions, records_by_id, amount_tolerance_paise, date_window_days)

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
    records_by_id: dict[str, NormalizedRecord],
    amount_tolerance_paise: int = DEFAULT_TOLERANCES.amount_tolerance_paise,
    date_window_days: int = DEFAULT_TOLERANCES.date_window_days,
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
                    # Financial evidence pre-check before auto-acceptance
                    evidence_ok = _check_financial_evidence(
                        outcome.proposal, records_by_id,
                        amount_tolerance_paise, date_window_days,
                    )
                    if evidence_ok:
                        bucket, reason, confidence = _accepted_bucket(outcome.proposal.confidence)
                    else:
                        # Downgrade auto-accept to review when evidence is weak
                        conf = outcome.proposal.confidence
                        if conf >= AUTO_ACCEPT_THRESHOLD:
                            bucket = RoutingBucket.HUMAN_REVIEW
                            reason = RoutingReason.LOW_EVIDENCE
                        elif conf >= REVIEW_THRESHOLD:
                            bucket = RoutingBucket.HUMAN_REVIEW
                            reason = RoutingReason.AI_NEEDS_REVIEW
                        else:
                            bucket = RoutingBucket.EXCEPTION
                            reason = RoutingReason.LOW_CONFIDENCE
                        confidence = conf
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
