"""
Integration tests for Part 3G.5 — Unify Resumable Evaluation Artifact.

Validates the migration from the legacy `layer2_full_audit.jsonl` artifact
to the active resumable `layer2_clean_audit.jsonl` artifact.

All tests are local (zero network calls).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reconciliation.evaluation.resume import (
    CompletionStatus,
    build_resume_summary,
    filter_residuals_for_resume,
    load_resume_state,
    validate_resume_artifact,
)
from reconciliation.loader import load_residuals


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
CLEAN_ARTIFACT = DATA_DIR / "layer2_clean_audit.jsonl"
LEGACY_ARTIFACT = DATA_DIR / "layer2_full_audit.legacy.jsonl"
RESIDUALS = load_residuals(DATA_DIR)
RESIDUAL_MAP = {r.scenario_id: r for r in RESIDUALS}
EXPECTED_FINGERPRINT = "b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93"


def _extract_scenario_id(correlation_id: str) -> str:
    parts = correlation_id.split("-")
    for i, part in enumerate(parts):
        if len(part) == 8 and all(c in "0123456789abcdef" for c in part.lower()):
            return "-".join(parts[:i])
    return correlation_id


# -------------------------------------------------------------------
# Test A: Real clean artifact produces correct resume plan
# -------------------------------------------------------------------


class TestCleanArtifactResumePlan:
    def test_clean_artifact_has_45_records(self):
        assert CLEAN_ARTIFACT.exists()
        records = []
        with CLEAN_ARTIFACT.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        assert len(records) == 45

    def test_clean_artifact_resume_plan(self):
        state = load_resume_state(CLEAN_ARTIFACT)
        summary = build_resume_summary(state)
        skip, retry, run = filter_residuals_for_resume(RESIDUALS, state)

        assert summary.completed == 36
        assert summary.failed == 9
        assert len(skip) == 36
        assert len(retry) == 9
        assert len(run) == 32
        assert len(skip) + len(retry) + len(run) == 77

    def test_clean_artifact_validation_passes(self):
        errors = validate_resume_artifact(
            CLEAN_ARTIFACT,
            RESIDUALS,
            EXPECTED_FINGERPRINT,
        )
        assert errors == []

    def test_clean_artifact_all_scenario_ids_in_residuals(self):
        state = load_resume_state(CLEAN_ARTIFACT)
        for sid in state:
            assert sid in RESIDUAL_MAP, f"Unknown scenario_id in artifact: {sid}"


# -------------------------------------------------------------------
# Test B: Old fingerprint artifact is rejected
# -------------------------------------------------------------------


class TestOldFingerprintRejected:
    def test_legacy_artifact_fingerprint_mismatch(self):
        if not LEGACY_ARTIFACT.exists():
            pytest.skip("Legacy artifact not present")

        errors = validate_resume_artifact(
            LEGACY_ARTIFACT,
            RESIDUALS,
            EXPECTED_FINGERPRINT,
        )
        assert any("fingerprint mismatch" in e for e in errors)

    def test_legacy_artifact_not_in_active_state(self):
        if not LEGACY_ARTIFACT.exists():
            pytest.skip("Legacy artifact not present")

        state = load_resume_state(LEGACY_ARTIFACT)
        for sid, cp in state.items():
            if sid in RESIDUAL_MAP:
                pytest.fail(
                    f"Legacy artifact scenario_id {sid} should not match "
                    f"current residuals"
                )


# -------------------------------------------------------------------
# Test C: Legacy API_ERROR without scenario_id cannot contaminate state
# -------------------------------------------------------------------


class TestLegacyApiErrorIsolation:
    def test_legacy_record_has_no_valid_scenario_id(self):
        if not LEGACY_ARTIFACT.exists():
            pytest.skip("Legacy artifact not present")

        with LEGACY_ARTIFACT.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                cid = record.get("correlation_id", "")
                sid = _extract_scenario_id(cid)
                # The legacy record's correlation_id is a UUID-like hex string
                # with no hyphen-delimited scenario prefix.
                assert sid not in RESIDUAL_MAP, (
                    f"Legacy record scenario_id {sid} should not match residuals"
                )

    def test_legacy_record_does_not_affect_resume_counts(self):
        if not LEGACY_ARTIFACT.exists():
            pytest.skip("Legacy artifact not present")

        state = load_resume_state(LEGACY_ARTIFACT)
        summary = build_resume_summary(state)
        # The legacy record should either be untracked (unknown scenario_id)
        # or, if somehow tracked, should not count toward the 77 expected.
        for sid in state:
            assert sid not in RESIDUAL_MAP, (
                f"Legacy scenario_id {sid} leaked into active state"
            )


# -------------------------------------------------------------------
# Test D: Wrong fingerprint cannot enter active state
# -------------------------------------------------------------------


class TestWrongFingerprintBlocked:
    def test_wrong_fingerprint_in_artifact_rejected(self, tmp_path: Path):
        bad_artifact = tmp_path / "bad_fingerprint.jsonl"
        bad_artifact.write_text(
            json.dumps({
                "correlation_id": "SCN-A-badfp-test",
                "timestamp": "2026-09-01T10:00:00+00:00",
                "presented_record_ids": ["R1"],
                "outcome": "PROPOSAL_VALID",
                "proposal": {
                    "proposed_match_ids": ["R1"],
                    "confidence": 0.9,
                    "rationale": "test",
                },
                "confidence": 0.9,
                "reason": "test",
                "dataset_fingerprint": "a" * 64,
                "diagnostic": "",
            }) + "\n",
            encoding="utf-8",
        )

        errors = validate_resume_artifact(
            bad_artifact,
            RESIDUALS,
            EXPECTED_FINGERPRINT,
        )
        assert any("fingerprint mismatch" in e for e in errors)

    def test_wrong_fingerprint_skipped_in_resume(self, tmp_path: Path):
        bad_artifact = tmp_path / "bad_fingerprint.jsonl"
        bad_artifact.write_text(
            json.dumps({
                "correlation_id": "SCN-A-badfp-test",
                "timestamp": "2026-09-01T10:00:00+00:00",
                "presented_record_ids": ["R1"],
                "outcome": "PROPOSAL_VALID",
                "proposal": {
                    "proposed_match_ids": ["R1"],
                    "confidence": 0.9,
                    "rationale": "test",
                },
                "confidence": 0.9,
                "reason": "test",
                "dataset_fingerprint": "a" * 64,
                "diagnostic": "",
            }) + "\n",
            encoding="utf-8",
        )

        state = load_resume_state(bad_artifact)
        # SCN-A is not a real residual, so it shouldn't appear in skip/retry/run
        skip, retry, run = filter_residuals_for_resume(RESIDUALS, state)
        assert all(r.scenario_id != "SCN-A" for r in skip)
        assert all(r.scenario_id != "SCN-A" for r in retry)
        assert all(r.scenario_id != "SCN-A" for r in run)


# -------------------------------------------------------------------
# Test E: Duplicate scenario IDs rejected
# -------------------------------------------------------------------


class TestDuplicateScenarioIdsRejected:
    def test_duplicate_scenario_ids_in_artifact_rejected(self, tmp_path: Path):
        dup_artifact = tmp_path / "duplicates.jsonl"
        base_record = {
            "timestamp": "2026-09-01T10:00:00+00:00",
            "presented_record_ids": ["R1"],
            "outcome": "PROPOSAL_VALID",
            "proposal": {
                "proposed_match_ids": ["R1"],
                "confidence": 0.9,
                "rationale": "test",
            },
            "confidence": 0.9,
            "reason": "test",
            "dataset_fingerprint": EXPECTED_FINGERPRINT,
            "diagnostic": "",
        }
        lines = []
        for ts in ("2026-09-01T10:00:01+00:00", "2026-09-01T10:00:02+00:00"):
            rec = dict(base_record)
            rec["correlation_id"] = f"DUP-999-{EXPECTED_FINGERPRINT[:8]}-{ts}"
            rec["timestamp"] = ts
            lines.append(json.dumps(rec))

        dup_artifact.write_text("\n".join(lines) + "\n", encoding="utf-8")

        errors = validate_resume_artifact(
            dup_artifact,
            RESIDUALS,
            EXPECTED_FINGERPRINT,
        )
        assert any("duplicate scenario_id" in e for e in errors)
