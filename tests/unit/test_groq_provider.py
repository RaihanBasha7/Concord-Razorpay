"""
Day 4.3 tests: Groq client boundary + structured proposal contract.

All tests run without a real API key or network. The provider boundary is
mocked or patched so no Groq SDK network calls occur.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Mapping
from unittest.mock import MagicMock, patch

import pytest

from reconciliation.domain.models import SourceType
from reconciliation.groq_provider import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    DEFAULT_MODEL,
    GroqClientConfig,
    GroqProviderError,
    GroqStructuredProvider,
    GroqTimeoutError,
    StructuredCompletionProvider,
)
from reconciliation.layer2 import Layer2Case
from reconciliation.proposal import PROPOSAL_JSON_SCHEMA, MatchProposal
from reconciliation.proposal_service import PromptBuilder, ProposalService
from reconciliation.retrieval import CandidateRecord, RetrievalResult, RetrievalConfig
from tests.conftest import make_record


class FakeProvider(StructuredCompletionProvider):
    """Mock provider returning a fixed parsed JSON object."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload
        self.last_request: dict | None = None

    def complete_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.last_request = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "json_schema": json_schema,
        }
        return self._payload


def _make_case_and_retrieval() -> tuple[Layer2Case, RetrievalResult]:
    member = make_record(
        record_id="M1",
        source_type=SourceType.SETTLEMENT,
        source_native_id="SET-1",
        order_id_hint="ORD-1",
        amount_paise=100000,
        date=date(2026, 8, 25),
    )
    case = Layer2Case(
        scenario_id="FEE-001",
        member_records=(member,),
        record_count=1,
    )
    candidate = make_record(
        record_id="BANK-1",
        source_type=SourceType.BANK,
        source_native_id="BNK-1",
        order_id_hint="ORD-1",
        amount_paise=99500,
        date=date(2026, 8, 25),
    )
    retrieval = RetrievalResult(
        scenario_id="DUP-001",
        candidates=(
            CandidateRecord(
                record=candidate,
                score=-3.0,
                rank=1,
                match_signals=("order_id_exact_match", "cross_source"),
                source_type=SourceType.BANK,
            ),
        ),
        candidate_count=1,
        max_candidates=10,
        retrieval_signals_used=("order_id_exact_match",),
    )
    return case, retrieval


class TestValidStructuredResponse:
    def test_valid_response_becomes_typed_proposal(self):
        provider = FakeProvider(
            {
                "proposed_match_ids": ["BANK-1"],
                "confidence": 0.82,
                "rationale": "Order ID and amount align across sources.",
            }
        )
        service = ProposalService(provider)
        case, retrieval = _make_case_and_retrieval()
        proposal = service.propose(case, retrieval)

        assert isinstance(proposal, MatchProposal)
        assert proposal.proposed_match_ids == ["BANK-1"]
        assert proposal.confidence == 0.82
        assert "Order ID" in proposal.rationale

    def test_proposal_service_passes_schema_to_provider(self):
        provider = FakeProvider(
            {"proposed_match_ids": [], "confidence": 0.0, "rationale": "x"}
        )
        service = ProposalService(provider)
        case, retrieval = _make_case_and_retrieval()
        service.propose(case, retrieval)
        assert provider.last_request["json_schema"] is PROPOSAL_JSON_SCHEMA


class TestEmptyProposal:
    def test_empty_proposed_match_ids_represented_correctly(self):
        provider = FakeProvider(
            {
                "proposed_match_ids": [],
                "confidence": 0.1,
                "rationale": "Insufficient evidence to propose a match.",
            }
        )
        service = ProposalService(provider)
        case, retrieval = _make_case_and_retrieval()
        proposal = service.propose(case, retrieval)

        assert proposal.proposed_match_ids == []
        assert proposal.confidence == 0.1
        assert proposal.rationale


class TestInvalidStructuredResponse:
    def test_malformed_payload_raises_provider_error(self):
        provider = FakeProvider(
            {
                "proposed_match_ids": "not-a-list",
                "confidence": 2.0,
                "rationale": "bad",
            }
        )
        service = ProposalService(provider)
        case, retrieval = _make_case_and_retrieval()
        with pytest.raises(GroqProviderError, match="Invalid structured proposal"):
            service.propose(case, retrieval)

    def test_missing_required_field_raises_provider_error(self):
        provider = FakeProvider({"proposed_match_ids": [], "confidence": 0.5})
        service = ProposalService(provider)
        case, retrieval = _make_case_and_retrieval()
        with pytest.raises(GroqProviderError, match="Invalid structured proposal"):
            service.propose(case, retrieval)

    def test_out_of_range_confidence_raises_provider_error(self):
        provider = FakeProvider(
            {
                "proposed_match_ids": [],
                "confidence": 1.7,
                "rationale": "overconfident but invalid",
            }
        )
        service = ProposalService(provider)
        case, retrieval = _make_case_and_retrieval()
        with pytest.raises(GroqProviderError, match="Invalid structured proposal"):
            service.propose(case, retrieval)

    def test_real_provider_empty_content_raises(self):
        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = ""
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
            with patch("reconciliation.groq_provider.Groq", return_value=fake_client):
                provider = GroqStructuredProvider()
                with pytest.raises(GroqProviderError, match="empty completion"):
                    provider.complete_structured(
                        system_prompt="s",
                        user_prompt="u",
                        json_schema=PROPOSAL_JSON_SCHEMA,
                    )

    def test_real_provider_unparseable_json_raises(self):
        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = "{not valid json"
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
            with patch("reconciliation.groq_provider.Groq", return_value=fake_client):
                provider = GroqStructuredProvider()
                with pytest.raises(
                    GroqProviderError, match="Failed to parse"
                ):
                    provider.complete_structured(
                        system_prompt="s",
                        user_prompt="u",
                        json_schema=PROPOSAL_JSON_SCHEMA,
                    )


class TestApiKeyConfiguration:
    def test_api_key_read_from_environment_not_hardcoded(self):
        captured: dict = {}
        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = json.dumps(
            {"proposed_match_ids": [], "confidence": 0.0, "rationale": "x"}
        )
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        def fake_groq_factory(**kwargs):
            captured.update(kwargs)
            return fake_client

        with patch.dict("os.environ", {"GROQ_API_KEY": "env-secret-value"}):
            with patch("reconciliation.groq_provider.Groq", side_effect=fake_groq_factory):
                provider = GroqStructuredProvider(
                    GroqClientConfig(model="custom-model")
                )
                provider.complete_structured(
                    system_prompt="s",
                    user_prompt="u",
                    json_schema=PROPOSAL_JSON_SCHEMA,
                )

        assert captured.get("api_key") == "env-secret-value"
        assert captured.get("api_key") != "sk-..."
        create_kwargs = fake_client.chat.completions.create.call_args.kwargs
        assert create_kwargs["model"] == "custom-model"
        assert create_kwargs["model"] != DEFAULT_MODEL or "custom-model" == DEFAULT_MODEL

    def test_missing_api_key_raises_provider_error(self):
        with patch.dict("os.environ", {}, clear=True):
            provider = GroqStructuredProvider()
            with pytest.raises(GroqProviderError, match="API key not found"):
                provider.complete_structured(
                    system_prompt="s",
                    user_prompt="u",
                    json_schema=PROPOSAL_JSON_SCHEMA,
                )

    def test_config_default_model_is_not_hardcoded_in_call(self):
        assert GroqClientConfig().model == DEFAULT_MODEL


class TestProviderRetry:
    def test_retries_timeout_once_then_succeeds(self):
        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = json.dumps(
            {"proposed_match_ids": [], "confidence": 0.0, "rationale": "x"}
        )
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = [
            APITimeoutError(request=MagicMock()),
            fake_response,
        ]

        with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
            with patch("reconciliation.groq_provider.Groq", return_value=fake_client):
                with patch("reconciliation.groq_provider._sleep") as mock_sleep:
                    provider = GroqStructuredProvider()
                    result = provider.complete_structured(
                        system_prompt="s",
                        user_prompt="u",
                        json_schema={"name": "x", "schema": {}},
                    )
                    assert result == {
                        "proposed_match_ids": [],
                        "confidence": 0.0,
                        "rationale": "x",
                    }
                    assert fake_client.chat.completions.create.call_count == 2
                    mock_sleep.assert_called_once_with(2.0)

    def test_retries_connection_error_once_then_succeeds(self):
        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = json.dumps(
            {"proposed_match_ids": ["BANK-1"], "confidence": 0.8, "rationale": "x"}
        )
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = [
            APIConnectionError(request=MagicMock()),
            fake_response,
        ]

        with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
            with patch("reconciliation.groq_provider.Groq", return_value=fake_client):
                with patch("reconciliation.groq_provider._sleep") as mock_sleep:
                    provider = GroqStructuredProvider()
                    result = provider.complete_structured(
                        system_prompt="s",
                        user_prompt="u",
                        json_schema={"name": "x", "schema": {}},
                    )
                    assert result["proposed_match_ids"] == ["BANK-1"]
                    assert fake_client.chat.completions.create.call_count == 2
                    mock_sleep.assert_called_once_with(2.0)

    def test_timeout_twice_raises_after_retry(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = APITimeoutError(
            request=MagicMock()
        )

        with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
            with patch("reconciliation.groq_provider.Groq", return_value=fake_client):
                with patch("reconciliation.groq_provider._sleep"):
                    provider = GroqStructuredProvider()
                    with pytest.raises(GroqTimeoutError, match="timed out"):
                        provider.complete_structured(
                            system_prompt="s",
                            user_prompt="u",
                            json_schema={"name": "x", "schema": {}},
                        )
                    assert fake_client.chat.completions.create.call_count == 2

    def test_api_error_not_retried(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = APIError(
            "auth", request=MagicMock(), body=None
        )

        with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
            with patch("reconciliation.groq_provider.Groq", return_value=fake_client):
                with patch("reconciliation.groq_provider._sleep") as mock_sleep:
                    provider = GroqStructuredProvider()
                    with pytest.raises(GroqProviderError, match="Groq API error"):
                        provider.complete_structured(
                            system_prompt="s",
                            user_prompt="u",
                            json_schema={"name": "x", "schema": {}},
                        )
                    assert fake_client.chat.completions.create.call_count == 1
                    mock_sleep.assert_not_called()


class TestPromptBoundary:
    def _eval_polluted_case_and_retrieval(self) -> tuple[Layer2Case, RetrievalResult]:
        member = make_record(
            record_id="M1",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-1",
            order_id_hint="ORD-1",
            amount_paise=100000,
            date=date(2026, 8, 25),
            raw_payload={
                "category": "FEE_DEDUCTED",
                "has_valid_relationship": True,
                "is_true_exception": False,
                "description": "synthetic fee scenario",
            },
        )
        case = Layer2Case(
            scenario_id="FEE-001",
            member_records=(member,),
            record_count=1,
        )
        candidate = make_record(
            record_id="BANK-1",
            source_type=SourceType.BANK,
            source_native_id="BNK-1",
            order_id_hint="ORD-1",
            amount_paise=99500,
            date=date(2026, 8, 25),
            raw_payload={
                "category": "PARTIAL_REFUND",
                "has_valid_relationship": True,
            },
        )
        retrieval = RetrievalResult(
            scenario_id="DUP-001",
            candidates=(
                CandidateRecord(
                    record=candidate,
                    score=-3.0,
                    rank=1,
                    match_signals=("order_id_exact_match",),
                    source_type=SourceType.BANK,
                ),
            ),
            candidate_count=1,
            max_candidates=10,
            retrieval_signals_used=("order_id_exact_match",),
        )
        return case, retrieval

    def test_prompt_excludes_evaluation_metadata_and_synthetic_ids(self):
        builder = PromptBuilder()
        case, retrieval = self._eval_polluted_case_and_retrieval()
        result = builder.build(case, retrieval)
        text = (result.system_prompt + "\n" + result.user_prompt).lower()

        forbidden = [
            "fee-001",
            "dup-001",
            "has_valid_relationship",
            "is_true_exception",
            "category",
            "scenario",
            "synthetic",
            "description",
        ]
        for token in forbidden:
            assert token not in text, f"Forbidden token leaked into prompt: {token}"

    def test_prompt_contains_only_production_fields(self):
        builder = PromptBuilder()
        case, retrieval = self._eval_polluted_case_and_retrieval()
        result = builder.build(case, retrieval)

        assert "M1" in result.user_prompt
        assert "BANK-1" in result.user_prompt
        assert "ORD-1" in result.user_prompt
        assert "99500" in result.user_prompt
        assert "100000" in result.user_prompt

    def test_prompt_allows_empty_match_ids(self):
        builder = PromptBuilder()
        case, retrieval = self._eval_polluted_case_and_retrieval()
        result = builder.build(case, retrieval)
        assert "empty" in result.user_prompt.lower()
        assert "proposed_match_ids" in result.user_prompt
