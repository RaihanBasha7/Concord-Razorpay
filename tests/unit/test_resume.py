"""
Resume / checkpoint tests for Layer 2 evaluation.

Validates:
  A. Resume skips successful scenarios, retries failures, runs missing.
  B. Multiple attempts: deterministic authoritative selection (earliest timestamp).
  C. API_ERROR followed by successful retry: success becomes authoritative.
  D. Finalization rejects when any scenario is not completed.
  E. Finalization succeeds when all scenarios are completed.
  F. Finalization rejects wrong fingerprint.
  G. Finalization rejects duplicate scenario IDs.
  H. Crash/partial write preserves previous canonical artifact.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from reconciliation.audit import AuditRecord, Auditor
from reconciliation.domain.models import NormalizedRecord, SourceType
from reconciliation.evaluation.resume import (
    CompletionStatus,
    ScenarioCheckpoint,
    build_resume_summary,
    filter_residuals_for_resume,
    load_resume_state,
    validate_finalization,
)
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_validation import ProposalOutcomeType


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _make_record(
    scenario_id: str,
    outcome: str,
    timestamp: str = "2026-09-01T10:00:00+00:00",
    error_classification: str | None = None,
    proposed_match_ids: list[str] | None = None,
    confidence: float | None = None,
    fingerprint: str = "b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93",
) -> str:
    proposal = None
    if proposed_match_ids is not None:
        proposal = MatchProposal(
            proposed_match_ids=proposed_match_ids,
            confidence=confidence or 0.9,
            rationale="test",
        ).model_dump()
    record = AuditRecord(
        correlation_id=f"{scenario_id}-{fingerprint[:8]}-test",
        timestamp=timestamp,
        presented_record_ids=["R1"],
        outcome=outcome,
        proposal=proposal,
        confidence=confidence,
        reason="test",
        dataset_fingerprint=fingerprint,
        diagnostic=f"error_classification={error_classification}" if error_classification else None,
    )
    return record.to_json()


def _write_artifact(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_artifact(path: Path) -> list[dict]:
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


class FakeResidual:
    def __init__(self, scenario_id: str):
        self.scenario_id = scenario_id


# ===================================================================
# Test A: Skip completed, retry failed, run missing
# ===================================================================


class TestResumeFiltering:
    def test_skip_completed_retry_failed_run_missing(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "PROPOSAL_VALID"),
            _make_record("SCN-B", "API_ERROR", error_classification="transient"),
        ])

        state = load_resume_state(artifact)
        residuals = [FakeResidual(s) for s in ["SCN-A", "SCN-B", "SCN-C"]]

        skip, retry, run = filter_residuals_for_resume(residuals, state)

        assert [r.scenario_id for r in skip] == ["SCN-A"]
        assert [r.scenario_id for r in retry] == ["SCN-B"]
        assert [r.scenario_id for r in run] == ["SCN-C"]

    def test_no_prior_state_runs_all(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        residuals = [FakeResidual(s) for s in ["SCN-A", "SCN-B", "SCN-C"]]

        state = load_resume_state(artifact)
        skip, retry, run = filter_residuals_for_resume(residuals, state)

        assert skip == []
        assert retry == []
        assert [r.scenario_id for r in run] == ["SCN-A", "SCN-B", "SCN-C"]

    def test_all_completed_skips_all(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "PROPOSAL_VALID"),
            _make_record("SCN-B", "NO_PROPOSAL"),
        ])

        state = load_resume_state(artifact)
        residuals = [FakeResidual(s) for s in ["SCN-A", "SCN-B"]]

        skip, retry, run = filter_residuals_for_resume(residuals, state)

        assert [r.scenario_id for r in skip] == ["SCN-A", "SCN-B"]
        assert retry == []
        assert run == []

    def test_timeout_is_retried(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "TIMEOUT", error_classification="timeout"),
        ])

        state = load_resume_state(artifact)
        residuals = [FakeResidual(s) for s in ["SCN-A", "SCN-B"]]

        skip, retry, run = filter_residuals_for_resume(residuals, state)

        assert [r.scenario_id for r in retry] == ["SCN-A"]
        assert [r.scenario_id for r in run] == ["SCN-B"]

    def test_validation_failed_is_completed(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "VALIDATION_FAILED"),
        ])

        state = load_resume_state(artifact)
        residuals = [FakeResidual(s) for s in ["SCN-A", "SCN-B"]]

        skip, retry, run = filter_residuals_for_resume(residuals, state)

        assert [r.scenario_id for r in skip] == ["SCN-A"]
        assert [r.scenario_id for r in run] == ["SCN-B"]


# ===================================================================
# Test B: Multiple attempts — deterministic authoritative selection
# ===================================================================


class TestMultipleAttempts:
    def test_latest_timestamp_is_authoritative(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "PROPOSAL_VALID", timestamp="2026-09-01T10:00:01+00:00", confidence=0.5),
            _make_record("SCN-A", "PROPOSAL_VALID", timestamp="2026-09-01T10:00:02+00:00", confidence=0.9),
            _make_record("SCN-A", "PROPOSAL_VALID", timestamp="2026-09-01T10:00:03+00:00", confidence=0.7),
        ])

        state = load_resume_state(artifact)
        cp = state["SCN-A"]

        assert cp.status == CompletionStatus.COMPLETED
        assert cp.timestamp == "2026-09-01T10:00:03+00:00"
        assert cp.attempt_count == 3

    def test_later_failed_attempt_does_not_overwrite_success(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "PROPOSAL_VALID", timestamp="2026-09-01T10:00:01+00:00"),
            _make_record("SCN-A", "API_ERROR", timestamp="2026-09-01T10:00:02+00:00", error_classification="transient"),
        ])

        state = load_resume_state(artifact)
        cp = state["SCN-A"]

        assert cp.status == CompletionStatus.COMPLETED
        assert cp.outcome == "PROPOSAL_VALID"

    def test_later_success_overwrites_earlier_failure(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "API_ERROR", timestamp="2026-09-01T10:00:01+00:00", error_classification="transient"),
            _make_record("SCN-A", "PROPOSAL_VALID", timestamp="2026-09-01T10:00:02+00:00"),
        ])

        state = load_resume_state(artifact)
        cp = state["SCN-A"]

        assert cp.status == CompletionStatus.COMPLETED
        assert cp.outcome == "PROPOSAL_VALID"


# ===================================================================
# Test C: API_ERROR followed by successful retry
# ===================================================================


class TestRetrySuccessBecomesAuthoritative:
    def test_successful_retry_overwrites_api_error(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "API_ERROR", timestamp="2026-09-01T10:00:01+00:00", error_classification="transient"),
            _make_record("SCN-A", "PROPOSAL_VALID", timestamp="2026-09-01T10:00:02+00:00"),
        ])

        state = load_resume_state(artifact)
        cp = state["SCN-A"]

        assert cp.status == CompletionStatus.COMPLETED
        assert cp.outcome == "PROPOSAL_VALID"
        assert cp.attempt_count == 2


# ===================================================================
# Test D: Finalization rejects incomplete
# ===================================================================


class TestFinalizationRejectsIncomplete:
    def test_five_api_errors_rejects_finalization(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record(f"SCN-{i:02d}", "PROPOSAL_VALID")
            for i in range(72)
        ] + [
            _make_record("SCN-72", "API_ERROR", error_classification="quota_exhausted"),
            _make_record("SCN-73", "API_ERROR", error_classification="quota_exhausted"),
            _make_record("SCN-74", "API_ERROR", error_classification="quota_exhausted"),
            _make_record("SCN-75", "API_ERROR", error_classification="quota_exhausted"),
            _make_record("SCN-76", "API_ERROR", error_classification="quota_exhausted"),
        ])

        state = load_resume_state(artifact)
        expected_ids = [f"SCN-{i:02d}" for i in range(77)]

        errors = validate_finalization(state, expected_ids, "b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93", artifact)
        assert len(errors) > 0
        assert any("Failed scenarios" in e for e in errors)

    def test_missing_scenario_rejects_finalization(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "PROPOSAL_VALID"),
            _make_record("SCN-C", "PROPOSAL_VALID"),
        ])

        state = load_resume_state(artifact)
        expected_ids = ["SCN-A", "SCN-B", "SCN-C"]

        errors = validate_finalization(state, expected_ids, "b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93", artifact)
        assert any("Missing scenarios" in e for e in errors)

    def test_wrong_fingerprint_rejects_finalization(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "PROPOSAL_VALID", fingerprint="deadbeef" * 4),
        ])

        state = load_resume_state(artifact)
        expected_ids = ["SCN-A"]

        errors = validate_finalization(state, expected_ids, "b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93", artifact)
        assert any("Fingerprint mismatch" in e for e in errors)

    def test_duplicate_scenario_ids_reject_finalization(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "PROPOSAL_VALID", timestamp="2026-09-01T10:00:01+00:00"),
            _make_record("SCN-A", "PROPOSAL_VALID", timestamp="2026-09-01T10:00:02+00:00"),
        ])

        state = load_resume_state(artifact)
        expected_ids = ["SCN-A"]

        errors = validate_finalization(state, expected_ids, "b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93", artifact)
        assert any("Duplicate scenario_id" in e for e in errors)


# ===================================================================
# Test E: Finalization succeeds for complete set
# ===================================================================


class TestFinalizationSucceedsWhenComplete:
    def test_all_seventy_seven_completed(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        records = []
        for i in range(77):
            outcome = "PROPOSAL_VALID" if i % 3 != 0 else "NO_PROPOSAL"
            records.append(_make_record(f"SCN-{i:02d}", outcome))
        _write_artifact(artifact, records)

        state = load_resume_state(artifact)
        expected_ids = [f"SCN-{i:02d}" for i in range(77)]

        errors = validate_finalization(state, expected_ids, "b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93", artifact)
        assert errors == []

    def test_validation_failed_counts_as_completed(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "VALIDATION_FAILED"),
        ])

        state = load_resume_state(artifact)
        expected_ids = ["SCN-A"]

        errors = validate_finalization(state, expected_ids, "b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93", artifact)
        assert errors == []


# ===================================================================
# Test F: Wrong fingerprint
# ===================================================================


class TestWrongFingerprintRejected:
    def test_finalization_rejects_wrong_fingerprint(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "PROPOSAL_VALID", fingerprint="a" * 64),
        ])

        state = load_resume_state(artifact)
        errors = validate_finalization(
            state, ["SCN-A"], "b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93", artifact
        )
        assert any("Fingerprint mismatch" in e for e in errors)


# ===================================================================
# Test G: Duplicate scenario IDs
# ===================================================================


class TestDuplicateScenarioIdsRejected:
    def test_finalization_rejects_duplicate_scenario_ids(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "PROPOSAL_VALID", timestamp="2026-09-01T10:00:01+00:00"),
            _make_record("SCN-A", "PROPOSAL_VALID", timestamp="2026-09-01T10:00:02+00:00"),
        ])

        state = load_resume_state(artifact)
        errors = validate_finalization(
            state, ["SCN-A"], "b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93", artifact
        )
        assert any("Duplicate scenario_id" in e for e in errors)


# ===================================================================
# Test H: Crash / partial write preserves previous artifact
# ===================================================================


class TestCrashPreservesPreviousArtifact:
    def test_interrupted_run_preserves_previous_canonical(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"

        # First, create a valid completed artifact via Auditor.
        auditor1 = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor1.write(AuditRecord(
            correlation_id="SCN-A-abc-test",
            timestamp="2026-09-01T09:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            confidence=0.9,
            reason="test",
            dataset_fingerprint="b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93",
        ))
        auditor1.finalize()
        assert artifact.exists()

        # New run starts but is interrupted before finalize.
        auditor2 = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor2.write(AuditRecord(
            correlation_id="SCN-B-def-test",
            timestamp="2026-09-01T10:00:00+00:00",
            presented_record_ids=["R2"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            confidence=0.8,
            reason="test",
            dataset_fingerprint="b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93",
        ))
        # Simulate crash: do NOT call finalize.

        # Canonical path still has the PREVIOUS completed data.
        assert artifact.exists()
        records = _read_artifact(artifact)
        assert len(records) == 1
        assert records[0]["correlation_id"] == "SCN-A-abc-test"

        # Sidecar has partial new data.
        sidecar = Path(str(artifact) + ".tmp")
        assert sidecar.exists()

        # No archive was created (finalize never ran).
        archives = list(tmp_path.glob("audit.*.jsonl"))
        assert len(archives) == 0

    def test_sidecar_recovered_on_resume(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        sidecar = Path(str(artifact) + ".tmp")

        # Write partial sidecar.
        _write_artifact(sidecar, [
            _make_record("SCN-A", "PROPOSAL_VALID"),
        ])

        # Resume state should recover from sidecar.
        state = load_resume_state(artifact)
        assert "SCN-A" in state
        assert state["SCN-A"].status == CompletionStatus.COMPLETED


# ===================================================================
# Test ResumeSummary
# ===================================================================


class TestResumeSummary:
    def test_summary_counts(self, tmp_path):
        artifact = tmp_path / "audit.jsonl"
        _write_artifact(artifact, [
            _make_record("SCN-A", "PROPOSAL_VALID"),
            _make_record("SCN-B", "NO_PROPOSAL"),
            _make_record("SCN-C", "API_ERROR", error_classification="quota_exhausted"),
            _make_record("SCN-D", "TIMEOUT", error_classification="timeout"),
        ])

        state = load_resume_state(artifact)
        summary = build_resume_summary(state)

        assert summary.total_scenarios == 4
        assert summary.completed == 2
        assert summary.failed == 2
        assert summary.missing == 0
        assert "SCN-A" in summary.completed_ids
        assert "SCN-B" in summary.completed_ids
        assert "SCN-C" in summary.failed_ids
        assert "SCN-D" in summary.failed_ids


# ===================================================================
# Test Runner integration
# ===================================================================


class TestDay4RunnerResumeIntegration:
    def _create_synthetic_data_dir(self, tmp_path: Path, scenario_ids: list[str]) -> Path:
        """Create a minimal data directory with required files for Day4Runner."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Build normalized records with deterministic IDs.
        from reconciliation.domain.models import NormalizedRecord, SourceType
        from reconciliation.normalizer import normalize_record

        sett = normalize_record({
            "settlement_id": "SET1",
            "gross_amount": "1000.00",
            "settlement_date": "2026-08-01",
            "order_id": "ORD1",
            "narration": "test",
        }, SourceType.SETTLEMENT)
        bank = normalize_record({
            "bank_utr": "BANK1",
            "credit_amount": "1000.00",
            "value_date": "2026-08-01",
            "order_id": "ORD1",
            "narration": "test",
        }, SourceType.BANK)

        # Write normalized records JSONL (bypasses CSV normalizer).
        with (data_dir / "normalized_records.jsonl").open("w", encoding="utf-8") as f:
            for rec in (sett, bank):
                f.write(json.dumps({
                    "record_id": rec.record_id,
                    "source_type": rec.source_type.value,
                    "source_native_id": rec.source_native_id,
                    "order_id_hint": rec.order_id_hint,
                    "amount_paise": rec.amount_paise,
                    "date": rec.date.isoformat(),
                    "narration": rec.narration,
                }) + "\n")

        # Write residuals CSV
        import csv as csv_mod
        with (data_dir / "residuals.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=[
                "scenario_id", "category", "record_count", "member_record_ids",
                "has_valid_relationship", "is_true_exception", "description"
            ])
            writer.writeheader()
            for sid in scenario_ids:
                writer.writerow({
                    "scenario_id": sid,
                    "category": "DUPLICATE",
                    "record_count": 2,
                    "member_record_ids": json.dumps([sett.record_id, bank.record_id]),
                    "has_valid_relationship": "true",
                    "is_true_exception": "false",
                    "description": "test",
                })

        # Write minimal manifest
        import hashlib
        manifest = {
            "schema_version": "1.0",
            "dataset_seed": 42,
            "fingerprint": "b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93",
            "files": {
                "settlements.csv": hashlib.sha256(b"test").hexdigest(),
                "bank.csv": hashlib.sha256(b"test").hexdigest(),
                "ledger.csv": hashlib.sha256(b"test").hexdigest(),
                "residuals.csv": hashlib.sha256(b"test").hexdigest(),
            },
        }
        (data_dir / "dataset_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

        return data_dir

    def test_runner_skips_completed_and_runs_missing(self, tmp_path, monkeypatch):
        """Day4Runner with resume_state_path skips completed and runs missing."""
        from reconciliation.proposal_validation import ProposalOutcome, ProposalOutcomeType
        from reconciliation.runner import Day4Runner

        scenario_ids = [f"SCN-{i:03d}" for i in range(5)]
        data_dir = self._create_synthetic_data_dir(tmp_path, scenario_ids)
        artifact = data_dir / "layer2_full_audit.jsonl"
        sidecar = Path(str(artifact) + ".tmp")

        # Monkeypatch loader to use our JSONL records.
        from reconciliation import runner as runner_mod
        def _load_normalized(d):
            records = []
            with (data_dir / "normalized_records.jsonl").open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        r = json.loads(line)
                        records.append(NormalizedRecord(
                            record_id=r["record_id"],
                            source_type=SourceType(r["source_type"]),
                            source_native_id=r["source_native_id"],
                            order_id_hint=r.get("order_id_hint"),
                            amount_paise=r["amount_paise"],
                            date=date.fromisoformat(r["date"]),
                            narration=r.get("narration"),
                            raw_payload="",
                        ))
            return tuple(records)

        monkeypatch.setattr(
            runner_mod,
            "load_normalized_records",
            _load_normalized,
        )

        # Create a fake sidecar with one completed scenario.
        _write_artifact(sidecar, [
            _make_record("SCN-000", "PROPOSAL_VALID"),
        ])

        mock_outcome = ProposalOutcome(
            outcome=ProposalOutcomeType.NO_PROPOSAL,
            proposal=None,
            presented_record_ids=("R1",),
            reason="no match",
            error_classification=None,
        )
        mock_orchestrator = MagicMock()
        mock_orchestrator.resolve.return_value = mock_outcome

        runner = Day4Runner(
            data_dir=data_dir,
            orchestrator=mock_orchestrator,
            limit=5,
            resume_state_path=str(artifact),
        )
        summary = runner.run()

        # Should have attempted only the missing 4 scenarios.
        assert summary.attempted == 4
        assert mock_orchestrator.resolve.call_count == 4

        # Cleanup sidecar.
        if sidecar.exists():
            sidecar.unlink()

    def test_runner_retries_failed_scenarios(self, tmp_path, monkeypatch):
        """Day4Runner with resume_state_path retries failed scenarios."""
        from datetime import date
        from reconciliation.domain.models import SourceType
        from reconciliation.proposal_validation import ProposalOutcome, ProposalOutcomeType
        from reconciliation.runner import Day4Runner

        scenario_ids = [f"SCN-{i:03d}" for i in range(3)]
        data_dir = self._create_synthetic_data_dir(tmp_path, scenario_ids)
        artifact = data_dir / "layer2_full_audit.jsonl"
        sidecar = Path(str(artifact) + ".tmp")

        # Monkeypatch loader.
        from reconciliation import runner as runner_mod
        def _load_normalized(d):
            records = []
            with (data_dir / "normalized_records.jsonl").open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        r = json.loads(line)
                        records.append(NormalizedRecord(
                            record_id=r["record_id"],
                            source_type=SourceType(r["source_type"]),
                            source_native_id=r["source_native_id"],
                            order_id_hint=r.get("order_id_hint"),
                            amount_paise=r["amount_paise"],
                            date=date.fromisoformat(r["date"]),
                            narration=r.get("narration"),
                            raw_payload="",
                        ))
            return tuple(records)

        monkeypatch.setattr(
            runner_mod,
            "load_normalized_records",
            _load_normalized,
        )

        # Create a fake sidecar with one failed scenario.
        _write_artifact(sidecar, [
            _make_record("SCN-000", "API_ERROR", error_classification="transient"),
        ])

        mock_outcome = ProposalOutcome(
            outcome=ProposalOutcomeType.PROPOSAL_VALID,
            proposal=MagicMock(proposed_match_ids=["R1"], confidence=0.9, rationale="x"),
            presented_record_ids=("R1",),
            reason="matched",
            error_classification=None,
        )
        mock_orchestrator = MagicMock()
        mock_orchestrator.resolve.return_value = mock_outcome

        runner = Day4Runner(
            data_dir=data_dir,
            orchestrator=mock_orchestrator,
            limit=3,
            resume_state_path=str(artifact),
        )
        summary = runner.run()

        # Should retry SCN-000 and run the other 2.
        assert summary.attempted == 3
        assert mock_orchestrator.resolve.call_count == 3

        # Cleanup sidecar.
        if sidecar.exists():
            sidecar.unlink()
