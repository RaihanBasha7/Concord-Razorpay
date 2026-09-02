"""
Day 4.4 — Orchestration / service boundary for safe proposal outcomes.

Wraps the Day 4.3 ProposalService so that:
  * Provider/API failures -> API_ERROR
  * Timeouts -> TIMEOUT
  * Semantic validation failures -> VALIDATION_FAILED
  * No proposal -> NO_PROPOSAL
  * Valid proposal -> PROPOSAL_VALID

No exception path ever invents or substitutes a match. No automatic retries are
added. Secrets are never surfaced in the resulting outcome's reason.
"""
from __future__ import annotations

from typing import Iterable

from reconciliation.groq_provider import (
    GroqProviderError,
    GroqTimeoutError,
    safe_diagnostic,
)
from reconciliation.layer2 import Layer2Case
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_service import ProposalService
from reconciliation.proposal_validation import (
    ProposalOutcome,
    ProposalOutcomeType,
    validate_proposal,
)
from reconciliation.retrieval import RetrievalResult


class ProposalOrchestrator:
    """
    Boundaries the LLM proposal flow into explicit, safe outcomes.

    Accepts a ProposalService (which owns the mockable provider boundary). The
    orchestrator is the only place that maps provider exceptions to outcomes,
    keeping validation deterministic and independent of Groq.
    """

    def __init__(self, service: ProposalService) -> None:
        self._service = service

    @staticmethod
    def _presented_record_ids(
        case: Layer2Case, retrieval_result: RetrievalResult
    ) -> Iterable[str]:
        member_ids = [r.record_id for r in case.member_records]
        candidate_ids = [c.record.record_id for c in retrieval_result.candidates]
        seen: set[str] = set()
        presented: list[str] = []
        for rid in member_ids + candidate_ids:
            if rid not in seen:
                seen.add(rid)
                presented.append(rid)
        return presented

    def resolve(
        self,
        case: Layer2Case,
        retrieval_result: RetrievalResult,
    ) -> ProposalOutcome:
        presented_ids = list(self._presented_record_ids(case, retrieval_result))

        # Build records_by_id for structural validation
        records_by_id = {r.record_id: r for r in case.member_records}
        for c in retrieval_result.candidates:
            records_by_id.setdefault(c.record.record_id, c.record)

        try:
            proposal: MatchProposal = self._service.propose(case, retrieval_result)
        except GroqTimeoutError as exc:
            return ProposalOutcome(
                outcome=ProposalOutcomeType.TIMEOUT,
                proposal=None,
                presented_record_ids=tuple(presented_ids),
                reason="LLM provider request timed out.",
                diagnostic=safe_diagnostic(exc),
                error_classification=getattr(exc, "classification", None),
            )
        except GroqProviderError as exc:
            return ProposalOutcome(
                outcome=ProposalOutcomeType.API_ERROR,
                proposal=None,
                presented_record_ids=tuple(presented_ids),
                reason="LLM provider returned an API error.",
                diagnostic=safe_diagnostic(exc),
                error_classification=getattr(exc, "classification", None),
            )

        return validate_proposal(proposal, presented_ids, records_by_id=records_by_id)
