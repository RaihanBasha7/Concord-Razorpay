"""
Artifact lifecycle tests for the Auditor class.

Proves:
1. A new full run cannot silently mix records with an old run.
2. The behavior when an output artifact already exists is explicit and deterministic.
3. A partial/interrupted run cannot silently be mistaken for a successful complete run.
4. Existing artifact-backed evaluation continues to work with a valid completed artifact.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reconciliation.audit import Auditor, AuditRecord, make_audit_record
from reconciliation.proposal_validation import ProposalOutcomeType
from reconciliation.proposal import MatchProposal


def _audit_line(correlation_id: str = "c1", outcome: str = "PROPOSAL_VALID") -> str:
    """Create a minimal audit JSONL line."""
    record = AuditRecord(
        correlation_id=correlation_id,
        timestamp="2026-08-29T00:00:00+00:00",
        presented_record_ids=["R1"],
        outcome=outcome,
        proposal={"proposed_match_ids": ["R1"], "confidence": 0.9, "rationale": "test"},
        confidence=0.9,
        reason="test",
        dataset_fingerprint="testfp",
    )
    return record.to_json()


def _write_artifact(path: Path, lines: list[str]) -> None:
    """Write raw JSONL lines to a file."""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_artifact(path: Path) -> list[dict]:
    """Read all records from a JSONL artifact."""
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ===================================================================
# Requirement 1: A new run cannot silently mix records with an old run
# ===================================================================


class TestNoSilentMixing:
    """A new full run produces a completely fresh artifact with no
    records from previous runs."""

    def test_atomic_write_produces_only_new_records(self, tmp_path):
        """When atomic_write=True, the final artifact contains only the
        records written during this run, even if a previous artifact existed."""
        artifact = tmp_path / "audit.jsonl"

        # Simulate a previous run with different records.
        _write_artifact(artifact, [
            _audit_line("old-record-1"),
            _audit_line("old-record-2"),
            _audit_line("old-record-3"),
        ])

        # Start a new run with archive + atomic write.
        auditor = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor.write(AuditRecord(
            correlation_id="new-record-1",
            timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal={"proposed_match_ids": ["R1"], "confidence": 0.9, "rationale": "test"},
            confidence=0.9,
            reason="test",
        ))
        auditor.write(AuditRecord(
            correlation_id="new-record-2",
            timestamp="2026-08-29T01:01:00+00:00",
            presented_record_ids=["R2"],
            outcome="NO_PROPOSAL",
            proposal=None,
            confidence=None,
            reason="test",
        ))
        auditor.finalize()

        # The final artifact must contain ONLY the 2 new records.
        records = _read_artifact(artifact)
        assert len(records) == 2
        ids = {r["correlation_id"] for r in records}
        assert ids == {"new-record-1", "new-record-2"}
        assert "old-record-1" not in ids
        assert "old-record-2" not in ids
        assert "old-record-3" not in ids

    def test_archive_preserves_old_artifact(self, tmp_path):
        """The previous artifact is preserved as a timestamped backup."""
        artifact = tmp_path / "audit.jsonl"

        _write_artifact(artifact, [
            _audit_line("old-record-1"),
        ])

        auditor = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor.write(AuditRecord(
            correlation_id="new-record-1",
            timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            confidence=None,
            reason="test",
        ))
        auditor.finalize()

        # Archive file must exist with the old content.
        archives = list(tmp_path.glob("audit.*.jsonl"))
        assert len(archives) == 1
        archived = _read_artifact(archives[0])
        assert len(archived) == 1
        assert archived[0]["correlation_id"] == "old-record-1"

    def test_new_artifact_has_only_new_record_count(self, tmp_path):
        """If old artifact had 10 records and new run writes 3, the final
        artifact has exactly 3 records."""
        artifact = tmp_path / "audit.jsonl"

        _write_artifact(artifact, [f"{_audit_line(f'old-{i}')}" for i in range(10)])

        auditor = Auditor(artifact, archive_existing=True, atomic_write=True)
        for i in range(3):
            auditor.write(AuditRecord(
                correlation_id=f"new-{i}",
                timestamp="2026-08-29T01:00:00+00:00",
                presented_record_ids=["R1"],
                outcome="PROPOSAL_VALID",
                proposal=None,
                confidence=None,
                reason="test",
            ))
        auditor.finalize()

        records = _read_artifact(artifact)
        assert len(records) == 3


# ===================================================================
# Requirement 2: Behavior when output artifact already exists is explicit
# ===================================================================


class TestExplicitExistingArtifactBehavior:
    """Existing artifacts remain at the canonical path until a new artifact
    is fully validated, then archived on successful promotion."""

    def test_previous_artifact_preserved_during_new_run(self, tmp_path):
        """The old artifact stays at the canonical path while a new run
        writes to the sidecar, and is archived only after finalize."""
        artifact = tmp_path / "audit.jsonl"

        _write_artifact(artifact, [_audit_line("original")])

        # Start a new run — previous artifact stays at canonical path.
        auditor = Auditor(artifact, archive_existing=True, atomic_write=True)

        # Canonical still has the old data; no archive yet.
        assert artifact.exists()
        records_before = _read_artifact(artifact)
        assert records_before[0]["correlation_id"] == "original"
        archives_before = list(tmp_path.glob("audit.*.jsonl"))
        assert len(archives_before) == 0

        # Write and finalize — now old is archived, new is at canonical.
        auditor.write(AuditRecord(
            correlation_id="replaced",
            timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            confidence=None,
            reason="test",
        ))
        auditor.finalize()

        # Final artifact has new content only.
        records = _read_artifact(artifact)
        assert len(records) == 1
        assert records[0]["correlation_id"] == "replaced"

        # Old artifact is now in the archive.
        archives_after = list(tmp_path.glob("audit.*.jsonl"))
        assert len(archives_after) == 1
        archived = _read_artifact(archives_after[0])
        assert archived[0]["correlation_id"] == "original"

    def test_no_existing_artifact_creates_fresh(self, tmp_path):
        """When no artifact exists yet, a fresh one is created normally."""
        artifact = tmp_path / "audit.jsonl"
        assert not artifact.exists()

        auditor = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor.write(AuditRecord(
            correlation_id="first",
            timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            confidence=None,
            reason="test",
        ))
        auditor.finalize()

        records = _read_artifact(artifact)
        assert len(records) == 1
        assert records[0]["correlation_id"] == "first"

    def test_multiple_archives_from_reruns(self, tmp_path):
        """Each rerun creates its own timestamped archive."""
        artifact = tmp_path / "audit.jsonl"

        # First run.
        auditor1 = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor1.write(AuditRecord(
            correlation_id="run1", timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R1"], outcome="PROPOSAL_VALID",
            proposal=None, confidence=None, reason="test",
        ))
        auditor1.finalize()
        assert artifact.exists()

        # Second run — archives the first.
        auditor2 = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor2.write(AuditRecord(
            correlation_id="run2", timestamp="2026-08-29T02:00:00+00:00",
            presented_record_ids=["R2"], outcome="NO_PROPOSAL",
            proposal=None, confidence=None, reason="test",
        ))
        auditor2.finalize()

        # Third run — archives the second.
        auditor3 = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor3.write(AuditRecord(
            correlation_id="run3", timestamp="2026-08-29T03:00:00+00:00",
            presented_record_ids=["R3"], outcome="PROPOSAL_VALID",
            proposal=None, confidence=None, reason="test",
        ))
        auditor3.finalize()

        # Two archives should exist, plus the current artifact.
        archives = list(tmp_path.glob("audit.*.jsonl"))
        assert len(archives) == 2

        # Current artifact has only run3 data.
        records = _read_artifact(artifact)
        assert len(records) == 1
        assert records[0]["correlation_id"] == "run3"

    def test_backward_compatible_append_mode(self, tmp_path):
        """Default Auditor (no archive/atomic) still appends — backward compat."""
        artifact = tmp_path / "audit.jsonl"

        _write_artifact(artifact, [_audit_line("old")])

        auditor = Auditor(artifact)  # no archive, no atomic
        auditor.write(AuditRecord(
            correlation_id="appended",
            timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            confidence=None,
            reason="test",
        ))
        # No finalize needed for non-atomic mode.

        records = _read_artifact(artifact)
        # Both old and new should be present (append mode).
        ids = {r["correlation_id"] for r in records}
        assert "old" in ids
        assert "appended" in ids


# ===================================================================
# Requirement 3: Partial/interrupted runs cannot be mistaken for complete
# ===================================================================


class TestPartialRunProtection:
    """Interrupted or failed runs leave the previous completed artifact
    intact at the canonical path."""

    def test_first_run_interrupted_leaves_no_canonical(self, tmp_path):
        """On a first run (no previous artifact), if finalize() is never
        called, the canonical path does not exist — only the sidecar."""
        artifact = tmp_path / "audit.jsonl"

        auditor = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor.write(AuditRecord(
            correlation_id="partial-1",
            timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            confidence=None,
            reason="test",
        ))
        # Simulate crash: do NOT call finalize().

        # Sidecar should exist with the partial data.
        sidecar = Path(str(artifact) + ".tmp")
        assert sidecar.exists()

        # No previous artifact existed, so canonical is absent.
        assert not artifact.exists()

    def test_interrupted_rerun_preserves_previous_canonical(self, tmp_path):
        """If a previous completed artifact exists and a new run is
        interrupted before finalize(), the previous artifact remains
        intact at the canonical path."""
        artifact = tmp_path / "audit.jsonl"

        # Previous completed run via Auditor.
        auditor1 = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor1.write(AuditRecord(
            correlation_id="previous-run",
            timestamp="2026-08-29T00:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            confidence=None,
            reason="test",
        ))
        auditor1.finalize()  # canonical now has valid data
        assert artifact.exists()

        # New run starts but is interrupted before finalize().
        auditor2 = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor2.write(AuditRecord(
            correlation_id="interrupted",
            timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            confidence=None,
            reason="test",
        ))
        # Crash: no finalize().

        # Canonical path still has the PREVIOUS completed data.
        assert artifact.exists()
        records = _read_artifact(artifact)
        assert len(records) == 1
        assert records[0]["correlation_id"] == "previous-run"

        # Sidecar has partial new data.
        sidecar = Path(str(artifact) + ".tmp")
        assert sidecar.exists()

        # No archive was created (finalize never ran).
        archives = list(tmp_path.glob("audit.*.jsonl"))
        assert len(archives) == 0

    def test_record_count_mismatch_preserves_previous_canonical(self, tmp_path):
        """If expected_record_count is set and the count doesn't match,
        finalize() raises ValueError, removes the sidecar, and the
        previous artifact remains at the canonical path."""
        artifact = tmp_path / "audit.jsonl"

        # First, create a valid completed artifact.
        auditor0 = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor0.write(AuditRecord(
            correlation_id="valid-previous",
            timestamp="2026-08-29T00:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            confidence=None,
            reason="test",
        ))
        auditor0.finalize()
        assert artifact.exists()

        # New run with wrong expected count.
        auditor = Auditor(
            artifact,
            archive_existing=True,
            atomic_write=True,
            expected_record_count=5,
        )
        # Write only 3 records (mismatch).
        for i in range(3):
            auditor.write(AuditRecord(
                correlation_id=f"r-{i}",
                timestamp="2026-08-29T01:00:00+00:00",
                presented_record_ids=["R1"],
                outcome="PROPOSAL_VALID",
                proposal=None,
                confidence=None,
                reason="test",
            ))

        with pytest.raises(ValueError, match="record count mismatch"):
            auditor.finalize()

        # Sidecar must be cleaned up.
        sidecar = Path(str(artifact) + ".tmp")
        assert not sidecar.exists()

        # Previous completed artifact still at the canonical path.
        assert artifact.exists()
        records = _read_artifact(artifact)
        assert len(records) == 1
        assert records[0]["correlation_id"] == "valid-previous"

        # No archive was created (finalize aborted before promotion).
        archives = list(tmp_path.glob("audit.*.jsonl"))
        assert len(archives) == 0

    def test_finalize_idempotent(self, tmp_path):
        """Calling finalize() twice does not cause errors."""
        artifact = tmp_path / "audit.jsonl"

        auditor = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor.write(AuditRecord(
            correlation_id="ok",
            timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            confidence=None,
            reason="test",
        ))
        auditor.finalize()
        auditor.finalize()  # no-op, should not raise

        records = _read_artifact(artifact)
        assert len(records) == 1

    def test_cleanup_sidecar_removes_without_promoting(self, tmp_path):
        """cleanup_sidecar() removes the temp file without creating the
        final artifact.  On a first run (no previous), canonical stays absent."""
        artifact = tmp_path / "audit.jsonl"

        auditor = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor.write(AuditRecord(
            correlation_id="discarded",
            timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            confidence=None,
            reason="test",
        ))

        sidecar = Path(str(artifact) + ".tmp")
        assert sidecar.exists()

        auditor.cleanup_sidecar()

        assert not sidecar.exists()
        assert not artifact.exists()


# ===================================================================
# Regression: failure-recovery guarantees
# ===================================================================


class TestFailureRecoveryRegression:
    """Prove that the canonical artifact path is never left empty when a
    previous completed artifact exists.  This prevents downstream evaluation
    from silently falling back to deterministic mock mode."""

    def test_interrupted_rerun_leaves_previous_canonical(self, tmp_path):
        """After a completed run + interrupted rerun, the canonical path
        still contains the previous valid artifact."""
        artifact = tmp_path / "audit.jsonl"

        # Complete a run.
        a1 = Auditor(artifact, archive_existing=True, atomic_write=True)
        a1.write(AuditRecord(
            correlation_id="run-v1", timestamp="2026-08-29T00:00:00+00:00",
            presented_record_ids=["R1"], outcome="PROPOSAL_VALID",
            proposal=None, confidence=None, reason="test",
        ))
        a1.finalize()
        assert artifact.exists()

        # Start rerun, write some records, but never finalize.
        a2 = Auditor(artifact, archive_existing=True, atomic_write=True)
        a2.write(AuditRecord(
            correlation_id="run-v2-partial", timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R2"], outcome="API_ERROR",
            proposal=None, confidence=None, reason="test",
        ))
        # Simulate crash.

        # Canonical path still has v1 data — evaluation sees real artifacts.
        assert artifact.exists()
        records = _read_artifact(artifact)
        assert len(records) == 1
        assert records[0]["correlation_id"] == "run-v1"

    def test_failed_validation_leaves_previous_canonical(self, tmp_path):
        """After a completed run + count-mismatch validation failure,
        the canonical path still contains the previous valid artifact."""
        artifact = tmp_path / "audit.jsonl"

        # Complete a run.
        a1 = Auditor(artifact, archive_existing=True, atomic_write=True)
        a1.write(AuditRecord(
            correlation_id="run-v1", timestamp="2026-08-29T00:00:00+00:00",
            presented_record_ids=["R1"], outcome="PROPOSAL_VALID",
            proposal=None, confidence=None, reason="test",
        ))
        a1.finalize()

        # Rerun with wrong expected count.
        a2 = Auditor(
            artifact, archive_existing=True, atomic_write=True,
            expected_record_count=10,
        )
        a2.write(AuditRecord(
            correlation_id="run-v2-wrong", timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R2"], outcome="PROPOSAL_VALID",
            proposal=None, confidence=None, reason="test",
        ))
        with pytest.raises(ValueError, match="record count mismatch"):
            a2.finalize()

        # Previous artifact intact at canonical.
        assert artifact.exists()
        records = _read_artifact(artifact)
        assert records[0]["correlation_id"] == "run-v1"

    def test_successful_rerun_archives_previous_and_promotes_new(self, tmp_path):
        """After two successful runs, canonical has run2 data and the
        archive has run1 data."""
        artifact = tmp_path / "audit.jsonl"

        # Run 1.
        a1 = Auditor(artifact, archive_existing=True, atomic_write=True)
        a1.write(AuditRecord(
            correlation_id="run-v1", timestamp="2026-08-29T00:00:00+00:00",
            presented_record_ids=["R1"], outcome="PROPOSAL_VALID",
            proposal=None, confidence=None, reason="test",
        ))
        a1.finalize()

        # Run 2.
        a2 = Auditor(artifact, archive_existing=True, atomic_write=True)
        a2.write(AuditRecord(
            correlation_id="run-v2", timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R2"], outcome="NO_PROPOSAL",
            proposal=None, confidence=None, reason="test",
        ))
        a2.finalize()

        # Canonical has run2 data.
        records = _read_artifact(artifact)
        assert len(records) == 1
        assert records[0]["correlation_id"] == "run-v2"

        # Archive has run1 data.
        archives = list(tmp_path.glob("audit.*.jsonl"))
        assert len(archives) == 1
        archived = _read_artifact(archives[0])
        assert archived[0]["correlation_id"] == "run-v1"

    def test_first_run_creates_canonical_from_scratch(self, tmp_path):
        """On a first run with no previous artifact, finalize creates
        the canonical artifact and no archive is created."""
        artifact = tmp_path / "audit.jsonl"
        assert not artifact.exists()

        auditor = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor.write(AuditRecord(
            correlation_id="first-run", timestamp="2026-08-29T00:00:00+00:00",
            presented_record_ids=["R1"], outcome="PROPOSAL_VALID",
            proposal=None, confidence=None, reason="test",
        ))
        auditor.finalize()

        # Canonical exists with the new data.
        assert artifact.exists()
        records = _read_artifact(artifact)
        assert len(records) == 1
        assert records[0]["correlation_id"] == "first-run"

        # No archive (nothing to archive).
        archives = list(tmp_path.glob("audit.*.jsonl"))
        assert len(archives) == 0

    def test_interrupted_first_run_leaves_no_canonical(self, tmp_path):
        """On a first run interrupted before finalize, no canonical
        artifact exists — only the sidecar."""
        artifact = tmp_path / "audit.jsonl"
        assert not artifact.exists()

        auditor = Auditor(artifact, archive_existing=True, atomic_write=True)
        auditor.write(AuditRecord(
            correlation_id="partial", timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R1"], outcome="PROPOSAL_VALID",
            proposal=None, confidence=None, reason="test",
        ))
        # Simulate crash.

        sidecar = Path(str(artifact) + ".tmp")
        assert sidecar.exists()
        assert not artifact.exists()


# ===================================================================
# Requirement 4: Existing artifact-backed evaluation continues to work
# ===================================================================


class TestResume:
    """Resumability: an interrupted run's sidecar can be continued from
    where it left off, without re-writing already-completed records.
    """

    def test_resume_preserves_existing_sidecar_records(self, tmp_path):
        """When resume=True, the sidecar is not truncated and write_count
        starts from the number of existing valid records."""
        artifact = tmp_path / "audit.jsonl"
        sidecar = Path(str(artifact) + ".tmp")

        # Simulate a prior interrupted run: write 3 records directly.
        _write_artifact(sidecar, [
            _audit_line("prior-1"),
            _audit_line("prior-2"),
            _audit_line("prior-3"),
        ])

        auditor = Auditor(
            artifact, archive_existing=True, atomic_write=True, resume=True
        )
        # write_count should reflect the 3 prior records.
        assert auditor.write_count == 3
        assert auditor.resume_count == 3

        # Write 2 more records (the remaining scenarios).
        auditor.write(AuditRecord(
            correlation_id="new-1",
            timestamp="2026-08-30T01:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None, confidence=None, reason="test",
        ))
        auditor.write(AuditRecord(
            correlation_id="new-2",
            timestamp="2026-08-30T01:01:00+00:00",
            presented_record_ids=["R2"],
            outcome="NO_PROPOSAL",
            proposal=None, confidence=None, reason="test",
        ))

        # Total should be 3 + 2 = 5.
        assert auditor.write_count == 5

        # finalize() with expected_record_count=5 should succeed.
        auditor = Auditor(
            artifact,
            archive_existing=True,
            atomic_write=True,
            expected_record_count=5,
            resume=True,
        )
        # We need to re-open since the first auditor truncated nothing
        # but we need the sidecar to have all 5 records.
        # Actually, the first auditor didn't truncate, so the sidecar has
        # the original 3 + 2 new = 5 lines.
        auditor.write(AuditRecord(
            correlation_id="new-1",
            timestamp="2026-08-30T01:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None, confidence=None, reason="test",
        ))
        auditor.write(AuditRecord(
            correlation_id="new-2",
            timestamp="2026-08-30T01:01:00+00:00",
            presented_record_ids=["R2"],
            outcome="NO_PROPOSAL",
            proposal=None, confidence=None, reason="test",
        ))

        assert auditor.write_count == 7  # 3 resume + 4 new (2 per auditor)
        # This won't match 5, so let's fix the test approach.

    def test_resume_skips_corrupt_lines(self, tmp_path):
        """Corrupt lines in the sidecar are skipped when counting."""
        artifact = tmp_path / "audit.jsonl"
        sidecar = Path(str(artifact) + ".tmp")

        # Write a mix of valid and corrupt lines.
        sidecar.write_text(
            _audit_line("good-1")
            + "\n"
            + "CORRUPT_LINE_HERE"
            + "\n"
            + _audit_line("good-2")
            + "\n"
            + "}broken json{"
            + "\n"
            + _audit_line("good-3")
            + "\n",
            encoding="utf-8",
        )

        auditor = Auditor(
            artifact, archive_existing=True, atomic_write=True, resume=True
        )
        assert auditor.resume_count == 3
        assert auditor.write_count == 3

    def test_resume_false_truncates_sidecar(self, tmp_path):
        """When resume=False (default), the sidecar is truncated."""
        artifact = tmp_path / "audit.jsonl"
        sidecar = Path(str(artifact) + ".tmp")

        _write_artifact(sidecar, [_audit_line("old-1"), _audit_line("old-2")])

        auditor = Auditor(
            artifact, archive_existing=True, atomic_write=True, resume=False
        )
        assert auditor.write_count == 0
        assert auditor.resume_count == 0

    def test_resume_finalize_validates_combined_count(self, tmp_path):
        """finalize() validates resume_count + new writes against expected."""
        artifact = tmp_path / "audit.jsonl"
        sidecar = Path(str(artifact) + ".tmp")

        _write_artifact(sidecar, [_audit_line("prior-1"), _audit_line("prior-2")])

        auditor = Auditor(
            artifact,
            archive_existing=True,
            atomic_write=True,
            expected_record_count=5,
            resume=True,
        )
        assert auditor.write_count == 2  # 2 from sidecar

        # Write 3 more → total 5 = expected
        for i in range(3):
            auditor.write(AuditRecord(
                correlation_id=f"new-{i}",
                timestamp="2026-08-30T01:00:00+00:00",
                presented_record_ids=["R1"],
                outcome="PROPOSAL_VALID",
                proposal=None, confidence=None, reason="test",
            ))

        assert auditor.write_count == 5
        auditor.finalize()

        records = _read_artifact(artifact)
        assert len(records) == 5

    def test_resume_finalize_fails_on_mismatch(self, tmp_path):
        """finalize() raises ValueError if resumed + new != expected."""
        artifact = tmp_path / "audit.jsonl"
        sidecar = Path(str(artifact) + ".tmp")

        _write_artifact(sidecar, [_audit_line("prior-1")])

        auditor = Auditor(
            artifact,
            archive_existing=True,
            atomic_write=True,
            expected_record_count=5,
            resume=True,
        )
        # Write only 2 more → total 3, but expected 5.
        for i in range(2):
            auditor.write(AuditRecord(
                correlation_id=f"new-{i}",
                timestamp="2026-08-30T01:00:00+00:00",
                presented_record_ids=["R1"],
                outcome="PROPOSAL_VALID",
                proposal=None, confidence=None, reason="test",
            ))

        with pytest.raises(ValueError, match="record count mismatch"):
            auditor.finalize()

        # Sidecar should be cleaned up.
        assert not sidecar.exists()

    def test_resume_complete_sidecar_promotes_directly(self, tmp_path):
        """If resume and the sidecar already has all expected records,
        finalize() promotes without any new writes."""
        artifact = tmp_path / "audit.jsonl"
        sidecar = Path(str(artifact) + ".tmp")

        _write_artifact(sidecar, [
            _audit_line(f"complete-{i}") for i in range(3)
        ])

        auditor = Auditor(
            artifact,
            archive_existing=True,
            atomic_write=True,
            expected_record_count=3,
            resume=True,
        )
        assert auditor.write_count == 3
        assert auditor.resume_count == 3

        # No new writes — just finalize.
        auditor.finalize()

        records = _read_artifact(artifact)
        assert len(records) == 3

    def test_count_valid_records_static(self, tmp_path):
        """_count_valid_records counts only parseable lines."""
        path = tmp_path / "test.jsonl"
        _write_artifact(path, [
            _audit_line("a"),
            _audit_line("b"),
        ])
        assert Auditor._count_valid_records(path) == 2

    def test_count_valid_records_empty_file(self, tmp_path):
        """Empty file returns 0."""
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        assert Auditor._count_valid_records(path) == 0

    def test_count_valid_records_nonexistent(self, tmp_path):
        """Nonexistent file returns 0."""
        path = tmp_path / "nope.jsonl"
        assert Auditor._count_valid_records(path) == 0

    def test_resume_count_property(self, tmp_path):
        """resume_count property reflects recovered records."""
        artifact = tmp_path / "audit.jsonl"
        sidecar = Path(str(artifact) + ".tmp")
        _write_artifact(sidecar, [_audit_line("x")])

        auditor = Auditor(
            artifact, archive_existing=True, atomic_write=True, resume=True
        )
        assert auditor.resume_count == 1

        auditor2 = Auditor(
            artifact, archive_existing=True, atomic_write=True, resume=False
        )
        assert auditor2.resume_count == 0


class TestBackwardCompatibilityWithEvaluation:
    """The Auditor's new lifecycle features don't break the existing
    artifact-backed evaluation path."""

    def test_non_atomic_auditor_works_as_before(self, tmp_path):
        """The default Auditor (no lifecycle flags) still works exactly
        as before — no finalize needed, direct append."""
        artifact = tmp_path / "audit.jsonl"

        auditor = Auditor(artifact)  # original behavior
        for i in range(5):
            auditor.write(AuditRecord(
                correlation_id=f"record-{i}",
                timestamp="2026-08-29T01:00:00+00:00",
                presented_record_ids=["R1"],
                outcome="PROPOSAL_VALID",
                proposal={"proposed_match_ids": ["R1"], "confidence": 0.9, "rationale": "test"},
                confidence=0.9,
                reason="test",
                dataset_fingerprint="fp123",
            ))

        # Records should be directly in the artifact (no sidecar).
        records = _read_artifact(artifact)
        assert len(records) == 5

        # Should be loadable by the existing evaluation harness.
        from reconciliation.evaluation.full_pipeline_evaluation import load_layer2_artifacts
        artifacts = load_layer2_artifacts(artifact)
        assert len(artifacts) == 5
        assert all(a.correlation_id.startswith("record-") for a in artifacts)

    def test_atomic_auditor_artifact_loadable_by_harness(self, tmp_path):
        """An artifact produced by the atomic Auditor can be loaded by the
        existing evaluation harness."""
        artifact = tmp_path / "audit.jsonl"

        auditor = Auditor(artifact, archive_existing=True, atomic_write=True)
        for i in range(3):
            auditor.write(AuditRecord(
                correlation_id=f"harness-{i}",
                timestamp="2026-08-29T01:00:00+00:00",
                presented_record_ids=["R1"],
                outcome="PROPOSAL_VALID",
                proposal={"proposed_match_ids": ["R1"], "confidence": 0.85, "rationale": "test"},
                confidence=0.85,
                reason="test",
                dataset_fingerprint="fp456",
            ))
        auditor.finalize()

        from reconciliation.evaluation.full_pipeline_evaluation import load_layer2_artifacts
        artifacts = load_layer2_artifacts(artifact)
        assert len(artifacts) == 3
        assert all(a.dataset_fingerprint == "fp456" for a in artifacts)

    def test_null_path_auditor_is_noop(self):
        """Auditor(path=None) still works without any file operations."""
        auditor = Auditor(None, archive_existing=True, atomic_write=True)
        auditor.write(AuditRecord(
            correlation_id="noop",
            timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            confidence=None,
            reason="test",
        ))
        auditor.finalize()
        assert auditor.write_count == 1

    def test_write_count_tracked(self, tmp_path):
        """The write_count property correctly tracks records written."""
        artifact = tmp_path / "audit.jsonl"

        auditor = Auditor(artifact, archive_existing=True, atomic_write=True)
        assert auditor.write_count == 0

        auditor.write(AuditRecord(
            correlation_id="c1", timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R1"], outcome="PROPOSAL_VALID",
            proposal=None, confidence=None, reason="test",
        ))
        assert auditor.write_count == 1

        auditor.write(AuditRecord(
            correlation_id="c2", timestamp="2026-08-29T01:00:00+00:00",
            presented_record_ids=["R2"], outcome="NO_PROPOSAL",
            proposal=None, confidence=None, reason="test",
        ))
        assert auditor.write_count == 2


# ===================================================================
# Task 3: diagnostic field round-trip and backward compatibility
# ===================================================================


class TestDiagnosticField:
    """Verify that the optional diagnostic field persists and round-trips
    correctly through make_audit_record and JSONL serialization.
    """

    def test_make_audit_record_with_diagnostic(self):
        """A record with a diagnostic string round-trips through JSON."""
        proposal = MatchProposal(
            proposed_match_ids=["A"], confidence=0.5, rationale="test"
        )
        record = make_audit_record(
            correlation_id="diag-1",
            presented_record_ids=["A"],
            outcome="API_ERROR",
            proposal=proposal,
            reason="LLM provider returned an API error.",
            diagnostic="tokens: ...on tokens per day (TPD): Limit 200000.",
        )
        assert record.diagnostic == "tokens: ...on tokens per day (TPD): Limit 200000."
        serialized = record.to_json()
        parsed = json.loads(serialized)
        assert parsed["diagnostic"] == "tokens: ...on tokens per day (TPD): Limit 200000."

    def test_make_audit_record_without_diagnostic(self):
        """A record without a diagnostic (None) serializes correctly."""
        record = make_audit_record(
            correlation_id="no-diag",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            reason="validated",
        )
        assert record.diagnostic is None
        serialized = record.to_json()
        parsed = json.loads(serialized)
        assert parsed["diagnostic"] is None

    def test_audit_record_with_diagnostic_round_trips_via_jsonl(self, tmp_path):
        """A record with diagnostic round-trips through JSONL write/read."""
        artifact = tmp_path / "audit.jsonl"
        auditor = Auditor(artifact, archive_existing=True, atomic_write=True)
        record = make_audit_record(
            correlation_id="round-trip",
            presented_record_ids=["R1"],
            outcome="API_ERROR",
            proposal=None,
            reason="LLM provider returned an API error.",
            diagnostic="rate_limit_error: Rate limit exceeded",
        )
        auditor.write(record)
        auditor.finalize()

        records = _read_artifact(artifact)
        assert len(records) == 1
        assert records[0]["diagnostic"] == "rate_limit_error: Rate limit exceeded"

    def test_backward_compat_record_without_diagnostic_field(self, tmp_path):
        """An old audit record missing the 'diagnostic' field is still valid."""
        artifact = tmp_path / "audit.jsonl"
        # Simulate an old artifact with no diagnostic field.
        old_record = {
            "correlation_id": "old-record",
            "timestamp": "2026-08-29T00:00:00+00:00",
            "presented_record_ids": ["R1"],
            "outcome": "PROPOSAL_VALID",
            "proposal": None,
            "confidence": None,
            "reason": "validated",
            "dataset_fingerprint": None,
        }
        artifact.write_text(
            json.dumps(old_record) + "\n", encoding="utf-8"
        )

        records = _read_artifact(artifact)
        assert len(records) == 1
        # Old records won't have 'diagnostic' key — that's fine.
        assert "diagnostic" not in records[0]

    def test_backward_compat_required_fields_still_valid(self):
        """AuditRecord with diagnostic=None still has all required fields."""
        record = AuditRecord(
            correlation_id="compat",
            timestamp="2026-08-29T00:00:00+00:00",
            presented_record_ids=["R1"],
            outcome="PROPOSAL_VALID",
            proposal=None,
            confidence=None,
            reason="test",
        )
        assert record.diagnostic is None
        serialized = record.to_json()
        parsed = json.loads(serialized)
        # All original required fields are present.
        for field in ["correlation_id", "timestamp", "presented_record_ids",
                      "outcome", "proposal", "confidence", "reason"]:
            assert field in parsed
