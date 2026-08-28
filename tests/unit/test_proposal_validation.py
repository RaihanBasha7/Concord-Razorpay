"""
Day 4.4 tests: Semantic proposal validation & safe failure outcomes.

All tests run without network access or an API key. The provider boundary is
mocked; no Groq SDK calls occur.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Mapping

import pytest

from reconciliation.domain.models import SourceType
from reconciliation.groq_provider import (
    GroqProviderError,
    GroqTimeoutError,
    StructuredCompletionProvider,
)
from reconciliation.layer2 import Layer2Case
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_orchestration import ProposalOrchestrator
from reconciliation.proposal_service import ProposalService
from reconciliation.proposal_validation import (
    ProposalOutcome,
    ProposalOutcomeType,
    validate_proposal,
)
from reconciliation.retrieval import CandidateRecord, RetrievalResult
from tests.conftest import make_record


class FakeProvider(StructuredCompletionProvider):
    def __init__(
        self,
        payload: Mapping[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._payload = payload
        self._error = error

    def complete_structured(self, *, system_prompt, user_prompt, json_schema):
        if self._error is not None:
            raise self._error
        return self._payload


def _case_and_retrieval(candidate_ids=("BANK-1", "BANK-2")) -> tuple[Layer2Case, RetrievalResult]:
    member = make_record(
        record_id="M1",
        source_type=SourceType.SETTLEMENT,
        source_native_id="SET-1",
        order_id_hint="ORD-1",
        amount_paise=100000,
        date=date(2026, 8, 25),
    )
    case = Layer2Case(scenario_id="FEE-001", member_records=(member,), record_count=1)
    candidates = []
    for i, cid in enumerate(candidate_ids, start=1):
        rec = make_record(
            record_id=cid,
            source_type=SourceType.BANK,
            source_native_id=f"BNK-{i}",
            order_id_hint="ORD-1",
            amount_paise=99500,
            date=date(2026, 8, 25),
        )
        candidates.append(
            CandidateRecord(
                record=rec,
                score=-3.0,
                rank=i,
                match_signals=("order_id_exact_match",),
                source_type=SourceType.BANK,
            )
        )
    retrieval = RetrievalResult(
        scenario_id="DUP-001",
        candidates=tuple(candidates),
        candidate_count=len(candidates),
        max_candidates=10,
        retrieval_signals_used=("order_id_exact_match",),
    )
    return case, retrieval


class TestValidateProposalDeterministic:
    def test_valid_proposal_using_presented_record_ids(self):
        proposal = MatchProposal(
            proposed_match_ids=["BANK-1"], confidence=0.8, rationale="x"
        )
        outcome = validate_proposal(proposal, ["BANK-1", "BANK-2"])
        assert outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID
        assert outcome.proposal is proposal
        assert outcome.is_safe
        assert outcome.presented_record_ids == ("BANK-1", "BANK-2")

    def test_empty_proposal_is_no_proposal_not_error(self):
        proposal = MatchProposal(proposed_match_ids=[], confidence=0.1, rationale="weak")
        outcome = validate_proposal(proposal, ["BANK-1", "BANK-2"])
        assert outcome.outcome == ProposalOutcomeType.NO_PROPOSAL
        assert outcome.is_safe

    def test_unknown_id_is_validation_failed(self):
        proposal = MatchProposal(
            proposed_match_ids=["BANK-1", "GHOST"], confidence=0.9, rationale="x"
        )
        outcome = validate_proposal(proposal, ["BANK-1", "BANK-2"])
        assert outcome.outcome == ProposalOutcomeType.VALIDATION_FAILED
        assert outcome.invalid_ids == ("GHOST",)
        assert not outcome.is_safe

    def test_duplicate_ids_is_validation_failed(self):
        proposal = MatchProposal(
            proposed_match_ids=["BANK-1", "BANK-1"], confidence=0.9, rationale="x"
        )
        outcome = validate_proposal(proposal, ["BANK-1", "BANK-2"])
        assert outcome.outcome == ProposalOutcomeType.VALIDATION_FAILED
        assert "duplicate" in outcome.reason.lower()

    def test_validation_is_independent_of_groq(self):
        proposal = MatchProposal(
            proposed_match_ids=["BANK-1"], confidence=0.8, rationale="x"
        )
        outcome = validate_proposal(proposal, ["BANK-1"])
        assert outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID

    def test_member_records_are_valid_proposal_targets(self):
        proposal = MatchProposal(
            proposed_match_ids=["M1"], confidence=0.9, rationale="member match"
        )
        outcome = validate_proposal(proposal, ["M1", "BANK-1", "BANK-2"])
        assert outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID
        assert outcome.presented_record_ids == ("M1", "BANK-1", "BANK-2")

    def test_deterministic_deduplication_preserves_member_order(self):
        member_ids = ["M1", "M2"]
        candidate_ids = ["M2", "BANK-1"]
        presented = member_ids + candidate_ids
        outcome = validate_proposal(
            MatchProposal(proposed_match_ids=["M2"], confidence=0.8, rationale="x"),
            presented,
        )
        assert outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID
        assert list(outcome.presented_record_ids) == ["M1", "M2", "M2", "BANK-1"]

    def test_orchestrator_deduplicates_and_preserves_member_order(self):
        from reconciliation.retrieval import CandidateRecord, RetrievalResult
        from reconciliation.proposal_orchestration import ProposalOrchestrator

        member = make_record(
            record_id="M1",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-1",
            order_id_hint="ORD-1",
            amount_paise=100000,
            date=date(2026, 8, 25),
        )
        member2 = make_record(
            record_id="M2",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-2",
            order_id_hint="ORD-1",
            amount_paise=100000,
            date=date(2026, 8, 25),
        )
        case = Layer2Case(
            scenario_id="DUP-001", member_records=(member, member2), record_count=2
        )
        candidate = make_record(
            record_id="M2",
            source_type=SourceType.BANK,
            source_native_id="BNK-1",
            order_id_hint="ORD-1",
            amount_paise=99500,
            date=date(2026, 8, 25),
        )
        cand_rec = CandidateRecord(
            record=candidate,
            score=-3.0,
            rank=1,
            match_signals=("order_id_exact_match",),
            source_type=SourceType.BANK,
        )
        retrieval = RetrievalResult(
            scenario_id="DUP-001",
            candidates=(cand_rec,),
            candidate_count=1,
            max_candidates=10,
            retrieval_signals_used=("order_id_exact_match",),
        )
        orch = ProposalOrchestrator.__new__(ProposalOrchestrator)
        orch._service = None  # type: ignore[assignment]
        presented = orch._presented_record_ids(case, retrieval)
        assert list(presented) == ["M1", "M2"]

    def test_validation_rejects_ids_outside_presented_set(self):
        proposal = MatchProposal(
            proposed_match_ids=["M1", "GHOST"], confidence=0.9, rationale="x"
        )
        outcome = validate_proposal(proposal, ["M1", "BANK-1"])
        assert outcome.outcome == ProposalOutcomeType.VALIDATION_FAILED
        assert outcome.invalid_ids == ("GHOST",)

    def test_evaluation_metadata_not_in_presented_set(self):
        presented = ["M1", "BANK-1"]
        outcome = validate_proposal(
            MatchProposal(proposed_match_ids=["M1"], confidence=0.9, rationale="x"),
            presented,
        )
        assert "category" not in str(outcome.presented_record_ids)
        assert "has_valid_relationship" not in str(outcome.presented_record_ids)
        assert "is_true_exception" not in str(outcome.presented_record_ids)


class TestOrchestratorOutcomes:
    def test_valid_proposal_flow(self):
        provider = FakeProvider(
            {"proposed_match_ids": ["BANK-1"], "confidence": 0.8, "rationale": "x"}
        )
        orch = ProposalOrchestrator(ProposalService(provider))
        case, retrieval = _case_and_retrieval()
        outcome = orch.resolve(case, retrieval)
        assert outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID

    def test_member_record_proposal_is_valid_through_orchestrator(self):
        provider = FakeProvider(
            {"proposed_match_ids": ["M1"], "confidence": 0.8, "rationale": "member"}
        )
        orch = ProposalOrchestrator(ProposalService(provider))
        case, retrieval = _case_and_retrieval()
        outcome = orch.resolve(case, retrieval)
        assert outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID
        assert "M1" in outcome.presented_record_ids

    def test_no_proposal_flow(self):
        provider = FakeProvider(
            {"proposed_match_ids": [], "confidence": 0.1, "rationale": "weak"}
        )
        orch = ProposalOrchestrator(ProposalService(provider))
        case, retrieval = _case_and_retrieval()
        outcome = orch.resolve(case, retrieval)
        assert outcome.outcome == ProposalOutcomeType.NO_PROPOSAL

    def test_unknown_id_flow_is_validation_failed(self):
        provider = FakeProvider(
            {
                "proposed_match_ids": ["BANK-1", "GHOST"],
                "confidence": 0.9,
                "rationale": "x",
            }
        )
        orch = ProposalOrchestrator(ProposalService(provider))
        case, retrieval = _case_and_retrieval()
        outcome = orch.resolve(case, retrieval)
        assert outcome.outcome == ProposalOutcomeType.VALIDATION_FAILED
        assert outcome.invalid_ids == ("GHOST",)

    def test_duplicate_ids_flow_is_validation_failed(self):
        provider = FakeProvider(
            {
                "proposed_match_ids": ["BANK-1", "BANK-1"],
                "confidence": 0.9,
                "rationale": "x",
            }
        )
        orch = ProposalOrchestrator(ProposalService(provider))
        case, retrieval = _case_and_retrieval()
        outcome = orch.resolve(case, retrieval)
        assert outcome.outcome == ProposalOutcomeType.VALIDATION_FAILED

    def test_api_error_flow(self):
        provider = FakeProvider(error=GroqProviderError("boom"))
        orch = ProposalOrchestrator(ProposalService(provider))
        case, retrieval = _case_and_retrieval()
        outcome = orch.resolve(case, retrieval)
        assert outcome.outcome == ProposalOutcomeType.API_ERROR
        assert outcome.proposal is None
        assert "API" in outcome.reason

    def test_timeout_flow(self):
        provider = FakeProvider(error=GroqTimeoutError("slow"))
        orch = ProposalOrchestrator(ProposalService(provider))
        case, retrieval = _case_and_retrieval()
        outcome = orch.resolve(case, retrieval)
        assert outcome.outcome == ProposalOutcomeType.TIMEOUT
        assert outcome.proposal is None

    def test_infrastructure_failure_distinct_from_validation(self):
        provider = FakeProvider(error=GroqProviderError("auth"))
        orch = ProposalOrchestrator(ProposalService(provider))
        case, retrieval = _case_and_retrieval()
        outcome = orch.resolve(case, retrieval)
        assert outcome.outcome == ProposalOutcomeType.API_ERROR
        assert outcome.outcome != ProposalOutcomeType.VALIDATION_FAILED


class TestNoSilentRepair:
    def test_invalid_proposal_never_converted_to_valid(self):
        provider = FakeProvider(
            {
                "proposed_match_ids": ["BANK-1", "NOT-A-CANDIDATE"],
                "confidence": 0.99,
                "rationale": "x",
            }
        )
        orch = ProposalOrchestrator(ProposalService(provider))
        case, retrieval = _case_and_retrieval()
        outcome = orch.resolve(case, retrieval)
        # The bad ID is rejected outright; it is NOT dropped to make a guess.
        assert outcome.outcome == ProposalOutcomeType.VALIDATION_FAILED
        assert outcome.invalid_ids == ("NOT-A-CANDIDATE",)

    def test_outcome_carries_proposal_when_available(self):
        proposal = MatchProposal(
            proposed_match_ids=["BANK-1", "GHOST"], confidence=0.9, rationale="x"
        )
        outcome = validate_proposal(proposal, ["BANK-1", "BANK-2"])
        assert outcome.outcome == ProposalOutcomeType.VALIDATION_FAILED
        assert outcome.proposal is proposal
        assert outcome.proposal.proposed_match_ids == ["BANK-1", "GHOST"]
