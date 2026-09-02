"""
Day 4.4 — Semantic proposal validation.

Treats the LLM proposal (MatchProposal) as untrusted external input. Given the
proposal and the candidate set that was actually supplied to the model, this
module decides whether the proposal is semantically usable. It is fully
deterministic, depends only on standard library types plus MatchProposal, and
never calls Groq.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional, Tuple

from reconciliation.proposal import MatchProposal


class ProposalOutcomeType(str, Enum):
    PROPOSAL_VALID = "PROPOSAL_VALID"
    NO_PROPOSAL = "NO_PROPOSAL"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    API_ERROR = "API_ERROR"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True)
class ProposalOutcome:
    """
    The explicit outcome of validating/obtaining a proposal.

    Attributes:
        outcome: One of ProposalOutcomeType.
        proposal: The proposal when it was obtained (valid, no-proposal, or
            validation-failed). None for provider failures (API_ERROR/TIMEOUT)
            where no proposal was produced.
        presented_record_ids: The presented record ID set the proposal was validated against.
        reason: A safe, human-readable explanation. Never contains secrets.
        invalid_ids: Proposed IDs that failed validation (empty unless
            VALIDATION_FAILED due to unknown IDs).
    """

    outcome: ProposalOutcomeType
    proposal: Optional[MatchProposal]
    presented_record_ids: Tuple[str, ...]
    reason: str = ""
    invalid_ids: Tuple[str, ...] = ()
    diagnostic: str = ""
    error_classification: Optional[str] = None

    @property
    def is_safe(self) -> bool:
        """True when no unexpected/external failure occurred."""
        return self.outcome in (
            ProposalOutcomeType.PROPOSAL_VALID,
            ProposalOutcomeType.NO_PROPOSAL,
        )


def _as_tuple(presented_record_ids: Iterable[str]) -> Tuple[str, ...]:
    return tuple(presented_record_ids)


def _check_same_source_duplicate_overinclusion(
    proposed_ids: Tuple[str, ...],
    records_by_id: dict[str, "NormalizedRecord"],
) -> Optional[str]:
    """Check for same-source duplicate over-inclusion.

    When a proposal includes >=2 records from the same source type with
    identical (order_id_hint, amount_paise), those form a "duplicate group".
    The proposal must not also include cross-source records that have a
    different (order_id_hint, amount_paise) than the duplicate group.

    This prevents the model from including an unrelated bank record when
    it correctly identifies same-source duplicate settlements.

    Returns an error reason string if the check fails, else None.
    """
    from reconciliation.domain.models import NormalizedRecord, SourceType

    if len(proposed_ids) < 3:
        # Need at least 3 records for this pattern to apply
        return None

    # Group proposed records by source type
    by_source: dict[str, list[tuple[str, NormalizedRecord]]] = {}
    for rid in proposed_ids:
        rec = records_by_id.get(rid)
        if rec is None:
            continue
        by_source.setdefault(rec.source_type.value, []).append((rid, rec))

    # Check if any source type has >=2 records with identical (order_id, amount)
    for source_type, records in by_source.items():
        if len(records) < 2:
            continue

        # Find groups of records with same (order_id_hint, amount_paise)
        groups: dict[tuple, list[tuple[str, NormalizedRecord]]] = {}
        for rid, rec in records:
            key = (rec.order_id_hint or "", rec.amount_paise)
            groups.setdefault(key, []).append((rid, rec))

        for group_key, group_records in groups.items():
            if len(group_records) < 2:
                continue

            # Found a same-source duplicate group.
            # Check if any cross-source record has a DIFFERENT (order_id, amount).
            group_order_id, group_amount = group_key
            group_rids = {rid for rid, _ in group_records}

            for other_rid in proposed_ids:
                if other_rid in group_rids:
                    continue
                other_rec = records_by_id.get(other_rid)
                if other_rec is None:
                    continue
                if other_rec.source_type.value == source_type:
                    continue

                # Cross-source record found. Check if it differs.
                other_key = (other_rec.order_id_hint or "", other_rec.amount_paise)
                if other_key != group_key:
                    return (
                        f"Proposal contains same-source duplicate group ({source_type}: "
                        f"{len(group_records)} records with order_id={group_order_id!r}, "
                        f"amount={group_amount}) plus cross-source record "
                        f"{other_rid} with different (order_id={other_rec.order_id_hint!r}, "
                        f"amount={other_rec.amount_paise}). Same-source duplicates "
                        f"should not be bundled with unrelated cross-source records."
                    )

    return None


def validate_proposal(
    proposal: MatchProposal,
    presented_record_ids: Iterable[str],
    records_by_id: Optional[dict[str, "NormalizedRecord"]] = None,
) -> ProposalOutcome:
    """
    Deterministically validate a MatchProposal against the presented record set.

    Rules:
      * Empty proposed_match_ids -> NO_PROPOSAL (not an error).
      * Every proposed ID must be in the presented record set, else VALIDATION_FAILED.
      * Proposed IDs must be unique, else VALIDATION_FAILED.
      * Same-source duplicate over-inclusion -> VALIDATION_FAILED.
      * Invalid IDs are never removed or repaired; the proposal is rejected.
      * Otherwise -> PROPOSAL_VALID.

    A VALIDATION_FAILED/PROPOSAL_VALID outcome still carries the proposal so
    callers can inspect it; it is NOT a final reconciliation decision.
    """
    presented = _as_tuple(presented_record_ids)

    if not proposal.proposed_match_ids:
        return ProposalOutcome(
            outcome=ProposalOutcomeType.NO_PROPOSAL,
            proposal=proposal,
            presented_record_ids=presented,
            reason="Model returned no proposed match IDs.",
        )

    proposed = proposal.proposed_match_ids

    if len(set(proposed)) != len(proposed):
        return ProposalOutcome(
            outcome=ProposalOutcomeType.VALIDATION_FAILED,
            proposal=proposal,
            presented_record_ids=presented,
            reason="Proposal contains duplicate proposed match IDs.",
        )

    unknown = [pid for pid in proposed if pid not in presented]
    if unknown:
        return ProposalOutcome(
            outcome=ProposalOutcomeType.VALIDATION_FAILED,
            proposal=proposal,
            presented_record_ids=presented,
            reason="Proposal references IDs outside the supplied presented record set.",
            invalid_ids=tuple(unknown),
        )

    # Same-source duplicate over-inclusion check
    if records_by_id is not None and len(proposed) >= 3:
        overinclusion = _check_same_source_duplicate_overinclusion(
            proposed, records_by_id
        )
        if overinclusion:
            return ProposalOutcome(
                outcome=ProposalOutcomeType.VALIDATION_FAILED,
                proposal=proposal,
                presented_record_ids=presented,
                reason=overinclusion,
            )

    return ProposalOutcome(
        outcome=ProposalOutcomeType.PROPOSAL_VALID,
        proposal=proposal,
        presented_record_ids=presented,
        reason="Proposal validated against presented record set.",
    )
