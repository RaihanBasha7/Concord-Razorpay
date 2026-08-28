"""
Day 4.3 tests: Prompt construction and production-boundary enforcement.

Verifies that the Layer 2 prompt explicitly instructs the model to evaluate
unresolved case records first and that evaluation metadata never leaks into
the rendered prompts.
"""
from __future__ import annotations

from datetime import date

import pytest

from reconciliation.domain.models import SourceType
from reconciliation.groq_provider import GroqProviderError, StructuredCompletionProvider
from reconciliation.layer2 import Layer2Case
from reconciliation.proposal_service import ProposalService, PromptBuilder
from reconciliation.retrieval import CandidateRecord, RetrievalResult
from tests.conftest import make_record


def _case_and_retrieval(
    candidate_ids=("BANK-1", "BANK-2"),
) -> tuple[Layer2Case, RetrievalResult]:
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


class TestPromptReasoningOrder:
    def test_system_prompt_explicitly_evaluates_case_records_first(self):
        builder = PromptBuilder()
        case, retrieval = _case_and_retrieval()
        result = builder.build(case, retrieval)
        text = result.system_prompt
        assert "First examine the unresolved case records" in text
        assert "Then examine the retrieved records" in text

    def test_user_prompt_explicitly_evaluates_case_records_first(self):
        builder = PromptBuilder()
        case, retrieval = _case_and_retrieval()
        result = builder.build(case, retrieval)
        text = result.user_prompt
        assert "First examine the unresolved case records" in text
        assert "Then examine the retrieved records" in text

    def test_prompt_allows_ids_from_either_set(self):
        builder = PromptBuilder()
        case, retrieval = _case_and_retrieval()
        result = builder.build(case, retrieval)
        text = result.system_prompt + " " + result.user_prompt
        assert "proposed_match_ids list may contain IDs from either" in text
        assert "unresolved case records or the retrieved candidate records" in text

    def test_empty_proposal_condition_after_both_sets(self):
        builder = PromptBuilder()
        case, retrieval = _case_and_retrieval()
        result = builder.build(case, retrieval)
        text = result.system_prompt + " " + result.user_prompt
        assert "Return an empty proposed_match_ids list only if there is insufficient evidence after considering both sets" in text


class TestPromptProductionBoundary:
    def test_prompt_excludes_scenario_id(self):
        builder = PromptBuilder()
        case, retrieval = _case_and_retrieval()
        result = builder.build(case, retrieval)
        text = result.system_prompt + " " + result.user_prompt
        assert case.scenario_id not in text

    def test_prompt_excludes_evaluation_metadata(self):
        builder = PromptBuilder()
        case, retrieval = _case_and_retrieval()
        result = builder.build(case, retrieval)
        text = result.system_prompt + " " + result.user_prompt
        forbidden = [
            "category",
            "has_valid_relationship",
            "is_true_exception",
            "description",
            "ground truth",
            "DUP-",
            "FEE-",
            "SPLT-",
            "EXACT-",
        ]
        for token in forbidden:
            assert token not in text, f"Forbidden token in prompt: {token}"

    def test_prompt_excludes_raw_payload(self):
        builder = PromptBuilder()
        case, retrieval = _case_and_retrieval()
        result = builder.build(case, retrieval)
        text = result.system_prompt + " " + result.user_prompt
        assert "raw_payload" not in text

    def test_prompt_excludes_routing_thresholds(self):
        builder = PromptBuilder()
        case, retrieval = _case_and_retrieval()
        result = builder.build(case, retrieval)
        text = result.system_prompt + " " + result.user_prompt
        assert "threshold" not in text.lower()
        assert "auto-accept" not in text.lower()
        assert "confidence" not in text.lower() or "confidence between" in text


class _FakeProvider(StructuredCompletionProvider):
    def __init__(self, payload):
        self._payload = payload

    def complete_structured(self, *, system_prompt, user_prompt, json_schema):
        return self._payload


class TestProposalValidation:
    def test_out_of_range_confidence_raises_groq_provider_error(self):
        provider = _FakeProvider(
            {
                "proposed_match_ids": [],
                "confidence": 1.5,
                "rationale": "overconfident but invalid confidence",
            }
        )
        service = ProposalService(provider)
        case, retrieval = _case_and_retrieval()
        with pytest.raises(GroqProviderError, match="Invalid structured proposal"):
            service.propose(case, retrieval)

    def test_missing_required_field_raises_groq_provider_error(self):
        provider = _FakeProvider(
            {
                "proposed_match_ids": [],
                "confidence": 0.8,
            }
        )
        service = ProposalService(provider)
        case, retrieval = _case_and_retrieval()
        with pytest.raises(GroqProviderError, match="Invalid structured proposal"):
            service.propose(case, retrieval)
