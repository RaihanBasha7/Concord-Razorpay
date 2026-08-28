"""
Focused tests for secret-safe local diagnostic observability (Day 4 live
API_ERROR diagnosis). Proves diagnostic info is available locally for CLI
debugging while secrets and the persistent audit trail remain protected.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from reconciliation.audit import Auditor, make_audit_record
from reconciliation.domain.models import SourceType
from reconciliation.groq_provider import (
    GroqProviderError,
    GroqTimeoutError,
    safe_diagnostic,
)
from reconciliation.layer2 import Layer2Case
from reconciliation.loader import load_normalized_records
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_orchestration import ProposalOrchestrator
from reconciliation.proposal_service import ProposalService
from reconciliation.proposal_validation import (
    ProposalOutcomeType,
)
from reconciliation.runner import Day4Runner
from tests.conftest import make_record


FAKE_KEY = "gsk-SUPERSECRETKEY12345"
SECRET_MESSAGE = f"Authentication failed key={FAKE_KEY} Authorization: Bearer {FAKE_KEY}"


class TestSafeDiagnosticRedaction:
    def test_diagnostic_redacts_api_key(self):
        err = GroqProviderError(
            "auth failed",
            error_type="AuthenticationError",
            safe_detail=SECRET_MESSAGE,
        )
        diag = safe_diagnostic(err)
        assert FAKE_KEY not in diag
        assert "[REDACTED]" in diag
        assert "AuthenticationError" in diag

    def test_diagnostic_exposes_error_type_only(self):
        err = GroqProviderError(
            "boom", error_type="RateLimitError", safe_detail="rate limited"
        )
        diag = safe_diagnostic(err)
        assert "RateLimitError" in diag
        assert "rate limited" in diag

    def test_diagnostic_handles_plain_exception(self):
        diag = safe_diagnostic(ValueError("something broke"))
        assert "ValueError" in diag
        assert "something broke" in diag


class TestOrchestratorAttachesDiagnostic:
    def test_api_error_outcome_carries_safe_diagnostic(self):
        from reconciliation.groq_provider import StructuredCompletionProvider

        class FailingProvider(StructuredCompletionProvider):
            def complete_structured(self, *, system_prompt, user_prompt, json_schema):
                raise GroqProviderError(
                    "Groq API error: ...",
                    error_type="AuthenticationError",
                    safe_detail=SECRET_MESSAGE,
                )

        orch = ProposalOrchestrator(ProposalService(FailingProvider()))
        case = Layer2Case(
            scenario_id="X",
            member_records=(
                make_record(
                    record_id="M1",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="S1",
                    amount_paise=100000,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        retrieval = MagicMock()
        retrieval.candidates = ()
        outcome = orch.resolve(case, retrieval)
        assert outcome.outcome == ProposalOutcomeType.API_ERROR
        # Outcome reason stays safe (no secret).
        assert FAKE_KEY not in outcome.reason
        # Diagnostic is available locally and redacted.
        assert FAKE_KEY not in outcome.diagnostic
        assert "AuthenticationError" in outcome.diagnostic

    def test_timeout_outcome_carries_safe_diagnostic(self):
        from reconciliation.groq_provider import StructuredCompletionProvider

        class TimeoutProvider(StructuredCompletionProvider):
            def complete_structured(self, *, system_prompt, user_prompt, json_schema):
                raise GroqTimeoutError(
                    "timed out",
                    error_type="APITimeoutError",
                    safe_detail=f"timeout after 30s key={FAKE_KEY}",
                )

        orch = ProposalOrchestrator(ProposalService(TimeoutProvider()))
        case = Layer2Case(
            scenario_id="X",
            member_records=(
                make_record(
                    record_id="M1",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="S1",
                    amount_paise=100000,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        outcome = orch.resolve(case, MagicMock(candidates=()))
        assert outcome.outcome == ProposalOutcomeType.TIMEOUT
        assert "APITimeoutError" in outcome.diagnostic
        assert FAKE_KEY not in outcome.diagnostic


class TestAuditExcludesDiagnosticAndSecrets:
    def test_audit_record_omits_diagnostic_and_secret(self):
        proposal = MatchProposal(
            proposed_match_ids=["A"], confidence=0.5, rationale="x"
        )
        record = make_audit_record(
            correlation_id="cid",
            presented_record_ids=["A"],
            outcome=ProposalOutcomeType.API_ERROR.value,
            proposal=None,
            reason="LLM provider returned an API error.",
        )
        blob = record.to_json()
        assert FAKE_KEY not in blob
        assert "[REDACTED]" not in blob
        assert "AuthenticationError" not in blob
        # ensure no diagnostic field leaked into audit JSON.
        parsed = json.loads(blob)
        assert "diagnostic" not in parsed

    def test_diagnostic_in_outcome_not_written_to_audit(self):
        from reconciliation.groq_provider import StructuredCompletionProvider

        class FailingProvider(StructuredCompletionProvider):
            def complete_structured(self, *, system_prompt, user_prompt, json_schema):
                raise GroqProviderError(
                    "err", error_type="AuthenticationError", safe_detail=SECRET_MESSAGE
                )

        auditor = Auditor(path=None)

        class CapturingAuditor(Auditor):
            def __init__(self):
                super().__init__(path=None)
                self.records = []

            def write(self, record):
                self.records.append(record)
                return True

        cap = CapturingAuditor()
        runner = Day4Runner(
            data_dir=Path(__file__).parent.parent.parent / "data",
            orchestrator=ProposalOrchestrator(ProposalService(FailingProvider())),
            limit=1,
            auditor=cap,
        )
        summary = runner.run()
        assert summary.attempted == 1
        # Diagnostic available in summary for CLI.
        assert any(FAKE_KEY not in d and "AuthenticationError" in d for d in summary.diagnostics)
        # But audit record JSON contains neither secret nor diagnostic.
        for rec in cap.records:
            blob = rec.to_json()
            assert FAKE_KEY not in blob
            assert "AuthenticationError" not in blob


def test_provider_error_type_captured_from_groq_exception():
    """End-to-end through the real provider with a mocked Groq client raising."""
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = "irrelevant"

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = Exception(
        f"transport failure containing {FAKE_KEY}"
    )

    with patch.dict("os.environ", {"GROQ_API_KEY": FAKE_KEY}):
        with patch("reconciliation.groq_provider.Groq", return_value=fake_client):
            from reconciliation.groq_provider import GroqStructuredProvider

            provider = GroqStructuredProvider()
            with pytest.raises(GroqProviderError) as exc_info:
                provider.complete_structured(
                    system_prompt="s",
                    user_prompt="u",
                    json_schema={"name": "x", "schema": {}},
                )
            diag = safe_diagnostic(exc_info.value)
            assert "Exception" in diag
            assert FAKE_KEY not in diag  # redacted even from raw exception text
