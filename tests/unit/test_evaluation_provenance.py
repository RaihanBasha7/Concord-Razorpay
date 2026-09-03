"""
Tests for evaluation provenance, lifecycle, and current-report generation.

Covers the Buildathon submission requirements:

  A. Manifest fingerprint matches independent recomputation.
  B. Current artifacts (layer2_clean_audit.jsonl, current_evaluation_report.*)
     carry the canonical manifest fingerprint.
  C. Mismatched artifacts fail closed (SHA-256 or fingerprint mismatch).
  D. Partial artifacts cannot be labelled FINAL.
  E. Historical artifacts (b8bf3feb) cannot become the current evaluation
     accidentally — they remain historical, and the canonical artifact is
     explicitly named in the manifest.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "data"

CANONICAL_FINGERPRINT = (
    "d91ead9a86a4d1dc949cf020a118957eeacb6eb4f18efb844c3c1b7afd0c6be0"
)
HISTORICAL_FINGERPRINT = (
    "b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93"
)
FROZEN_FILES = (
    "ground_truth.json",
    "settlements.csv",
    "bank.csv",
    "ledger.csv",
    "residuals.csv",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return json.loads(raw.decode("utf-8"))


def _recompute_fingerprint(data_dir: Path) -> str:
    """Re-derive the manifest fingerprint from on-disk files."""
    hashes = {}
    for name in FROZEN_FILES:
        path = data_dir / name
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = json.dumps(
        {"schema_version": "1.0", "dataset_seed": 42, "files": hashes},
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _build_current_report() -> dict:
    """Re-run scripts/build_current_report.py and return the JSON output."""
    repo = REPO_ROOT
    result = subprocess.run(
        [sys.executable, "scripts/build_current_report.py"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"build_current_report.py failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    return _read_json(DATA_DIR / "current_evaluation_report.json")


# ---------------------------------------------------------------------------
# A. Manifest fingerprint matches recomputation
# ---------------------------------------------------------------------------

class TestManifestFingerprint:
    def test_manifest_fingerprint_matches_recomputation(self):
        manifest = _read_json(DATA_DIR / "dataset_manifest.json")
        assert manifest["fingerprint"] == CANONICAL_FINGERPRINT
        assert _recompute_fingerprint(DATA_DIR) == CANONICAL_FINGERPRINT

    def test_manifest_lists_canonical_layer2_artifact(self):
        manifest = _read_json(DATA_DIR / "dataset_manifest.json")
        canonical = manifest["canonical_layer2_artifact"]
        assert canonical["filename"] == "layer2_clean_audit.jsonl"
        assert canonical["sha256"] == hashlib.sha256(
            (DATA_DIR / "layer2_clean_audit.jsonl").read_bytes()
        ).hexdigest()
        assert canonical["status"] in ("partial", "final")

    def test_manifest_declares_lifecycle_sections(self):
        manifest = _read_json(DATA_DIR / "dataset_manifest.json")
        for key in (
            "active_resumable_partial_artifact",
            "canonical_layer2_artifact",
            "final_canonical_artifact",
            "historical_artifacts",
        ):
            assert key in manifest, (
                f"Manifest is missing lifecycle section '{key}'"
            )
        for h in manifest["historical_artifacts"]:
            assert h["lifecycle"] == "historical"
            assert h["fingerprint_at_generation"] == HISTORICAL_FINGERPRINT


# ---------------------------------------------------------------------------
# B. Current artifacts match manifest fingerprint
# ---------------------------------------------------------------------------

class TestCurrentArtifacts:
    def test_canonical_artifact_records_carry_canonical_fingerprint(self):
        path = DATA_DIR / "layer2_clean_audit.jsonl"
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(records) > 0
        fps = {r.get("dataset_fingerprint") for r in records}
        assert fps == {CANONICAL_FINGERPRINT}, (
            f"Artifact carries mismatched fingerprints: {fps}"
        )

    def test_canonical_artifact_correlation_ids_are_clean(self):
        path = DATA_DIR / "layer2_clean_audit.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            cid = rec["correlation_id"]
            # Old fingerprint suffix must NOT be embedded in correlation IDs
            assert HISTORICAL_FINGERPRINT[:8] not in cid, (
                f"correlation_id still embeds old fingerprint: {cid}"
            )
            assert not cid.endswith("-b8bf3feb"), (
                f"correlation_id still ends with old fingerprint suffix: {cid}"
            )

    def test_current_evaluation_report_carries_canonical_fingerprint(self):
        report = _build_current_report()
        assert report["dataset_fingerprint"] == CANONICAL_FINGERPRINT
        assert report["evaluation_status"] in ("partial", "final")

    def test_current_report_identifies_canonical_artifact(self):
        report = _build_current_report()
        assert report["_canonical_artifact_filename"] == "layer2_clean_audit.jsonl"
        assert report["_canonical_artifact_sha256"] == hashlib.sha256(
            (DATA_DIR / "layer2_clean_audit.jsonl").read_bytes()
        ).hexdigest()


# ---------------------------------------------------------------------------
# C. Mismatched artifacts fail closed
# ---------------------------------------------------------------------------

class TestMismatchedArtifactsFailClosed:
    def test_modified_artifact_rejected_by_manifest(self, tmp_path: Path):
        """If the canonical artifact on disk differs from the manifest's
        recorded SHA-256, the canonical loader must fail closed."""
        from reconciliation.frozen_dataset import _load_canonical_audit_records

        real_manifest = _read_json(DATA_DIR / "dataset_manifest.json")
        canonical = real_manifest["canonical_layer2_artifact"]
        artifact_filename = canonical["filename"]

        # Copy artifact into tmp, then tamper with one byte
        tampered_dir = tmp_path / "data"
        tampered_dir.mkdir()
        artifact_src = DATA_DIR / artifact_filename
        (tampered_dir / artifact_filename).write_bytes(
            artifact_src.read_bytes()[:-1] + b"X"
        )
        # Also copy manifest into tmp
        (tampered_dir / "dataset_manifest.json").write_text(
            (DATA_DIR / "dataset_manifest.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        # And the frozen files (so the manifest's file hashes match too)
        for fname in FROZEN_FILES:
            (tampered_dir / fname).write_bytes(
                (DATA_DIR / fname).read_bytes()
            )

        with pytest.raises(ValueError, match="hash mismatch"):
            _load_canonical_audit_records(tampered_dir)

    def test_modified_artifact_rejected_by_current_report_builder(self, tmp_path: Path):
        """build_current_report.py must refuse to write a current report
        when the canonical artifact's SHA-256 does not match the manifest."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "data").mkdir()
        (repo / "scripts").mkdir()

        # Copy in everything we need
        import shutil
        for f in FROZEN_FILES + ("dataset_manifest.json", "ground_truth.json",
                                 "residuals.csv", "layer2_clean_audit.jsonl"):
            shutil.copy2(DATA_DIR / f, repo / "data")
        shutil.copy2(REPO_ROOT / "scripts" / "build_current_report.py", repo / "scripts")

        # Tamper the artifact on disk
        artifact_path = repo / "data" / "layer2_clean_audit.jsonl"
        artifact_path.write_bytes(artifact_path.read_bytes()[:-1] + b"X")

        result = subprocess.run(
            [sys.executable, "scripts/build_current_report.py"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0, (
            "build_current_report.py must refuse tampered artifacts"
        )
        assert "hash mismatch" in result.stderr or "mismatch" in result.stderr.lower()


# ---------------------------------------------------------------------------
# D. Partial artifacts cannot be labelled FINAL
# ---------------------------------------------------------------------------

class TestPartialCannotBeFinal:
    def test_partial_artifact_produces_partial_report(self):
        """When the canonical artifact has fewer than 77 records, the
        current report must be labelled PARTIAL — never FINAL."""
        report = _build_current_report()
        records_in_artifact = sum(
            1
            for line in (DATA_DIR / "layer2_clean_audit.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        )
        if records_in_artifact < 77:
            assert report["evaluation_status"] == "partial"
            assert report["completeness"] < 1.0
            assert any("PARTIAL" in lim for lim in report["limitations"])

    def test_final_status_requires_full_record_count(self):
        """If we set status='final' in the manifest but the artifact has
        fewer than 77 records, build_current_report.py must still label
        the report PARTIAL (defence in depth)."""
        # Build a temporary data dir where manifest claims "final" but the
        # artifact only has 1 record.
        import shutil

        tmp = REPO_ROOT / ".tmp_test_partial_final"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp_data = tmp / "data"
        tmp_data.mkdir(parents=True)
        tmp_scripts = tmp / "scripts"
        tmp_scripts.mkdir()

        for f in FROZEN_FILES + ("dataset_manifest.json",):
            shutil.copy2(DATA_DIR / f, tmp_data / f)
        shutil.copy2(REPO_ROOT / "scripts" / "build_current_report.py", tmp_scripts)

        # Minimal 1-record artifact
        rec = {
            "correlation_id": "DUP-001",
            "presented_record_ids": ["a", "b"],
            "outcome": "NO_PROPOSAL",
            "proposal": None,
            "confidence": None,
            "reason": "test",
            "dataset_fingerprint": CANONICAL_FINGERPRINT,
        }
        (tmp_data / "layer2_clean_audit.jsonl").write_text(
            json.dumps(rec) + "\n", encoding="utf-8"
        )

        manifest = _read_json(tmp_data / "dataset_manifest.json")
        manifest["canonical_layer2_artifact"]["status"] = "final"
        manifest["canonical_layer2_artifact"]["sha256"] = (
            hashlib.sha256((tmp_data / "layer2_clean_audit.jsonl").read_bytes()).hexdigest()
        )
        (tmp_data / "dataset_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        result = subprocess.run(
            [sys.executable, "scripts/build_current_report.py"],
            cwd=str(tmp),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0
        report = json.loads(
            (tmp_data / "current_evaluation_report.json").read_text(encoding="utf-8")
        )
        # Defence in depth: must NOT be labelled FINAL.
        assert report["evaluation_status"] != "final"

        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# E. Historical artifacts cannot become current
# ---------------------------------------------------------------------------

class TestHistoricalArtifactsAreLockedHistorical:
    HISTORICAL_JSON = (
        "clean_evaluation_report.json",
        "clean_eval_results.json",
        "clean_full_evaluation_report.json",
        "clean_full_eval_results.json",
    )

    HISTORICAL_MD = (
        "clean_evaluation_report.md",
        "clean_full_evaluation_report.md",
    )

    def test_each_historical_json_carries_lifecycle_historical(self):
        for relpath in self.HISTORICAL_JSON:
            data = _read_json(DATA_DIR / relpath)
            assert data.get("_lifecycle") == "historical", (
                f"{relpath} missing _lifecycle=historical"
            )
            assert data.get("_superseded_by") == "data/layer2_clean_audit.jsonl"
            assert data.get("_current_canonical_fingerprint") == CANONICAL_FINGERPRINT
            top_fp = (
                data.get("fingerprint")
                or (data.get("dataset") or {}).get("fingerprint")
            )
            assert top_fp == HISTORICAL_FINGERPRINT, (
                f"{relpath} historical fingerprint drifted from b8bf3feb"
            )

    def test_each_historical_markdown_declares_historical_lifecycle(self):
        for relpath in self.HISTORICAL_MD:
            text = (DATA_DIR / relpath).read_text(encoding="utf-8")
            assert "LIFECYCLE: HISTORICAL" in text, (
                f"{relpath} missing 'LIFECYCLE: HISTORICAL' header"
            )
            assert CANONICAL_FINGERPRINT in text, (
                f"{relpath} missing current canonical fingerprint reference"
            )

    def test_current_evaluation_report_lists_historical_artifacts(self):
        report = _build_current_report()
        assert "data/clean_evaluation_report.json" in (
            report.get("_historical_artifacts_superseded") or []
        )
        assert "data/clean_full_evaluation_report.json" in (
            report.get("_historical_artifacts_superseded") or []
        )

    def test_manifest_lists_all_historical_artifacts(self):
        manifest = _read_json(DATA_DIR / "dataset_manifest.json")
        paths = {h["path"] for h in manifest["historical_artifacts"]}
        assert "data/clean_evaluation_report.json" in paths
        assert "data/clean_full_evaluation_report.json" in paths


# ---------------------------------------------------------------------------
# F. Evaluation scripts do not hardcode the canonical fingerprint
# ---------------------------------------------------------------------------

class TestScriptsNoHardcodedFingerprint:
    def test_run_full_clean_eval_does_not_pin_old_fingerprint(self):
        text = (REPO_ROOT / "scripts" / "run_full_clean_eval.py").read_text(
            encoding="utf-8"
        )
        assert HISTORICAL_FINGERPRINT not in text, (
            "scripts/run_full_clean_eval.py still hardcodes the OLD fingerprint"
        )

    def test_run_merged_eval_does_not_pin_old_fingerprint(self):
        text = (REPO_ROOT / "scripts" / "run_merged_eval.py").read_text(
            encoding="utf-8"
        )
        assert HISTORICAL_FINGERPRINT not in text, (
            "scripts/run_merged_eval.py still hardcodes the OLD fingerprint"
        )