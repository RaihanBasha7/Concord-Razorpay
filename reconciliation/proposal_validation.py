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

    @property
    def is_safe(self) -> bool:
        """True when no unexpected/external failure occurred."""
        return self.outcome in (
            ProposalOutcomeType.PROPOSAL_VALID,
            ProposalOutcomeType.NO_PROPOSAL,
        )


def _as_tuple(presented_record_ids: Iterable[str]) -> Tuple[str, ...]:
    return tuple(presented_record_ids)


def validate_proposal(
    proposal: MatchProposal,
    presented_record_ids: Iterable[str],
) -> ProposalOutcome:
    """
    Deterministically validate a MatchProposal against the presented record set.

    Rules:
      * Empty proposed_match_ids -> NO_PROPOSAL (not an error).
      * Every proposed ID must be in the presented record set, else VALIDATION_FAILED.
      * Proposed IDs must be unique, else VALIDATION_FAILED.
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

    return ProposalOutcome(
        outcome=ProposalOutcomeType.PROPOSAL_VALID,
        proposal=proposal,
        presented_record_ids=presented,
        reason="Proposal validated against presented record set.",
    )
