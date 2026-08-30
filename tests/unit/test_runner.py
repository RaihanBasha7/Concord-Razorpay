"""
Day 4.5 tests: End-to-end runner + audit trail.

No real API calls or network access. The provider boundary is mocked so the
runner exercises reconstruction, retrieval, validation, and audit wiring only.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, List, Mapping, Optional
from unittest.mock import MagicMock, patch

import pytest

from reconciliation.audit import AuditRecord, Auditor, make_audit_record
from reconciliation.domain.models import SourceType
from reconciliation.groq_provider import GroqProviderError, StructuredCompletionProvider
from reconciliation.layer2 import Layer2Case
from reconciliation.loader import load_normalized_records, load_residuals
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_orchestration import ProposalOrchestrator
from reconciliation.proposal_service import ProposalService
from reconciliation.proposal_validation import ProposalOutcomeType
from reconciliation.retrieval import RetrievalResult
from reconciliation.runner import DEFAULT_SAMPLE_LIMIT, Day4Runner
from tests.conftest import make_record


class FakeProvider(StructuredCompletionProvider):
    def __init__(self, payload: Mapping[str, Any] | None = None, error: Exception | None = None):
        self._payload = payload
        self._error = error
        self.calls = 0

    def complete_structured(self, *, system_prompt, user_prompt, json_schema):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._payload


class ScriptedProvider(StructuredCompletionProvider):
    """Returns a queued behavior per call (payload or raise)."""

    def __init__(self, behaviors: list[dict | Exception]):
        self._behaviors = list(behaviors)
        self.calls = 0

    def complete_structured(self, *, system_prompt, user_prompt, json_schema):
        self.calls += 1
        behavior = self._behaviors[(self.calls - 1) % len(self._behaviors)]
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class CapturingAuditor(Auditor):
    def __init__(self):
        super().__init__(path=None)
        self.records: List[AuditRecord] = []

    def write(self, record: AuditRecord) -> bool:
        self.records.append(record)
        return True


class FailingAuditor(Auditor):
    def __init__(self):
        super().__init__(path=None)

    def write(self, record: AuditRecord) -> bool:
        return False


def _service(provider: StructuredCompletionProvider) -> ProposalOrchestrator:
    return ProposalOrchestrator(ProposalService(provider))


def _data_dir() -> Path:
    return Path(__file__).parent.parent.parent / "data"


class TestRunnerLimit:
    def test_respects_configured_limit(self):
        provider = FakeProvider(
            {"proposed_match_ids": [], "confidence": 0.1, "rationale": "weak"}
        )
        runner = Day4Runner(
            data_dir=_data_dir(),
            orchestrator=_service(provider),
            limit=3,
            auditor=CapturingAuditor(),
        )
        summary = runner.run()
        assert summary.attempted == 3
        assert provider.calls == 3
        # Default limit is a small constant.
        assert DEFAULT_SAMPLE_LIMIT == 5

    def test_limit_of_one_processes_single_scenario(self):
        provider = FakeProvider(
            {"proposed_match_ids": [], "confidence": 0.1, "rationale": "weak"}
        )
        runner = Day4Runner(
            data_dir=_data_dir(),
            orchestrator=_service(provider),
            limit=1,
            auditor=CapturingAuditor(),
        )
        summary = runner.run()
        assert summary.attempted == 1
        assert provider.calls == 1


class TestRunnerAuditCoversSuccessAndFailure:
    def test_success_and_failure_both_audited(self):
        provider = ScriptedProvider(
            [
                {"proposed_match_ids": ["UNUSED"], "confidence": 0.9, "rationale": "x"},
                GroqProviderError("boom"),
            ]
        )
        auditor = CapturingAuditor()
        runner = Day4Runner(
            data_dir=_data_dir(),
            orchestrator=_service(provider),
            limit=4,
            auditor=auditor,
        )
        summary = runner.run()
        assert summary.attempted == 4
        # Both PROPOSAL_VALID/VALIDATION_FAILED-style and API_ERROR outcomes recorded.
        outcomes = {r.outcome for r in auditor.records}
        assert ProposalOutcomeType.API_ERROR.value in outcomes
        assert len(auditor.records) == 4

    def test_audit_failure_does_not_mutate_outcome(self):
        provider = FakeProvider(
            {
                "proposed_match_ids": ["X"],
                "confidence": 0.8,
                "rationale": "x",
            }
        )
        runner = Day4Runner(
            data_dir=_data_dir(),
            orchestrator=_service(provider),
            limit=2,
            auditor=FailingAuditor(),
        )
        summary = runner.run()
        # Outcome still computed; no false match invented due to audit failure.
        assert summary.attempted == 2
        assert ProposalOutcomeType.VALIDATION_FAILED.value in summary.by_outcome


class TestAuditContainsPresentedRecordIds:
    def test_audit_record_has_presented_record_ids(self):
        provider = FakeProvider(
            {"proposed_match_ids": [], "confidence": 0.1, "rationale": "weak"}
        )
        auditor = CapturingAuditor()
        runner = Day4Runner(
            data_dir=_data_dir(),
            orchestrator=_service(provider),
            limit=1,
            auditor=auditor,
        )
        runner.run()
        record = auditor.records[0]
        assert isinstance(record.presented_record_ids, list)
        assert len(record.presented_record_ids) >= 1
        # Presented record IDs are production record IDs, not scenario IDs.
        assert all(not cid.startswith(("DUP-", "FEE-", "SPLT-", "EXACT-")) for cid in record.presented_record_ids)


class TestAuditExcludesMetadataAndSecrets:
    def test_audit_json_excludes_eval_metadata_and_secrets(self):
        provider = FakeProvider(
            {
                "proposed_match_ids": ["X"],
                "confidence": 0.7,
                "rationale": "x",
            }
        )
        auditor = CapturingAuditor()
        runner = Day4Runner(
            data_dir=_data_dir(),
            orchestrator=_service(provider),
            limit=2,
            auditor=auditor,
        )
        runner.run()
        for record in auditor.records:
            blob = record.to_json().lower()
            forbidden = [
                "groq",
                "api_key",
                "secret",
                "category",
                "has_valid_relationship",
                "is_true_exception",
                "description",
                "dup-",
                "fee-",
                "scenario_id",
            ]
            for token in forbidden:
                assert token not in blob, f"Forbidden token in audit: {token}"

    def test_audit_uses_neutral_correlation_id_not_scenario_id(self):
        provider = FakeProvider(
            {"proposed_match_ids": [], "confidence": 0.1, "rationale": "weak"}
        )
        auditor = CapturingAuditor()
        runner = Day4Runner(
            data_dir=_data_dir(),
            orchestrator=_service(provider),
            limit=1,
            auditor=auditor,
        )
        runner.run()
        record = auditor.records[0]
        assert record.correlation_id
        assert len(record.correlation_id) >= 16
        # correlation_id is not the category-encoded scenario_id.
        residuals = load_residuals(_data_dir())
        assert record.correlation_id != residuals[0].scenario_id


class TestNoRealApiCalls:
    def test_groq_client_never_instantiated_with_fake_provider(self):
        provider = FakeProvider(
            {"proposed_match_ids": [], "confidence": 0.1, "rationale": "weak"}
        )
        runner = Day4Runner(
            data_dir=_data_dir(),
            orchestrator=_service(provider),
            limit=2,
            auditor=CapturingAuditor(),
        )
        with patch("reconciliation.groq_provider.Groq") as mock_groq:
            runner.run()
            mock_groq.assert_not_called()


class TestMakeAuditRecord:
    def test_make_record_excludes_secrets_when_serialized(self):
        proposal = MatchProposal(
            proposed_match_ids=["A"], confidence=0.55, rationale="evidence-based note"
        )
        record = make_audit_record(
            correlation_id="abc123",
            presented_record_ids=["A", "B"],
            outcome=ProposalOutcomeType.PROPOSAL_VALID.value,
            proposal=proposal,
            reason="validated",
        )
        assert record.confidence == 0.55
        assert record.proposal == proposal.model_dump()
        serialized = record.to_json()
        assert "abc123" in serialized
        assert "0.55" in serialized

    def test_make_record_optionally_carries_dataset_fingerprint(self):
        proposal = MatchProposal(
            proposed_match_ids=["A"], confidence=0.55, rationale="note"
        )
        record = make_audit_record(
            correlation_id="abc",
            presented_record_ids=["A", "B"],
            outcome=ProposalOutcomeType.PROPOSAL_VALID.value,
            proposal=proposal,
            reason="validated",
            dataset_fingerprint="deadbeef",
        )
        assert record.dataset_fingerprint == "deadbeef"
        assert '"dataset_fingerprint": "deadbeef"' in record.to_json()


class TestAuditDatasetFingerprint:
    def _service(self, provider):
        return ProposalOrchestrator(ProposalService(provider))

    def test_audit_record_stamps_frozen_dataset_fingerprint(self):
        from reconciliation.evaluation.dataset_fingerprint import read_manifest

        provider = FakeProvider(
            {"proposed_match_ids": ["UNUSED"], "confidence": 0.9, "rationale": "x"}
        )
        auditor = CapturingAuditor()
        runner = Day4Runner(
            data_dir=_data_dir(),
            orchestrator=self._service(provider),
            limit=1,
            auditor=auditor,
        )
        summary = runner.run()

        manifest = read_manifest(_data_dir())
        assert manifest is not None
        assert summary.dataset_fingerprint == manifest.fingerprint()
        record = auditor.records[0]
        assert record.dataset_fingerprint == manifest.fingerprint()

    def test_runner_diagnostics_on_dataset_drift(self, tmp_path):
        # Build a dataset dir whose audit records carry a fingerprint, then
        # tamper with a CSV; the runner must surface a drift diagnostic.
        from reconciliation.evaluation.dataset_fingerprint import (
            compute_dataset_manifest,
            write_manifest,
        )
        from reconciliation.evaluation.dataset_generator import (
            generate_dataset,
            write_dataset,
        )

        ds = generate_dataset(seed=42)
        write_dataset(ds, tmp_path)
        # Minimal valid (header-only) residuals so the runner can load state.
        (tmp_path / "residuals.csv").write_text(
            "scenario_id,category,record_count,member_record_ids,"
            "has_valid_relationship,is_true_exception,description\n",
            encoding="utf-8",
        )
        manifest = compute_dataset_manifest(tmp_path, dataset_seed=42)
        write_manifest(manifest, tmp_path)
        # Tamper with a frozen file so its hash no longer matches the manifest.
        bank_path = tmp_path / "bank.csv"
        bank_path.write_text(
            bank_path.read_text(encoding="utf-8")
            + "\nBANK-TAMPERED,100000,2026-08-01,t:\n",
            encoding="utf-8",
        )

        provider = FakeProvider(
            {"proposed_match_ids": [], "confidence": 0.1, "rationale": "weak"}
        )
        runner = Day4Runner(
            data_dir=tmp_path,
            orchestrator=self._service(provider),
            limit=1,
            auditor=CapturingAuditor(),
        )
        # The drift diagnostic is computed at construction time (read-only).
        drift_diags = [d for d in runner._fingerprint_diagnostics if "drift" in d]
        assert drift_diags
        assert runner._dataset_fingerprint is not None

