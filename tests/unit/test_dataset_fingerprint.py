"""
Tests for dataset fingerprint / manifest provenance.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reconciliation.evaluation.dataset_fingerprint import (
    DATASET_SCHEMA_VERSION,
    DatasetManifest,
    compute_dataset_manifest,
    freeze_or_verify_dataset,
    manifest_path,
    read_manifest,
    verify_dataset,
)
from reconciliation.evaluation.dataset_generator import generate_dataset, write_dataset
from reconciliation.evaluation.evaluation_harness import run_evaluation
from reconciliation.matcher_config import MatcherConfig


DATA_DIR = Path(__file__).parent.parent.parent / "data"

_FROZEN_FILES = (
    "ground_truth.json",
    "settlements.csv",
    "bank.csv",
    "ledger.csv",
    "residuals.csv",
)


def _seed_dataset(tmp_path: Path) -> None:
    """Materialize a full seed-42 dataset (incl. residuals) into tmp_path."""
    ds = generate_dataset(seed=42)
    write_dataset(ds, tmp_path)
    config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
    run_evaluation(ds, config, tmp_path)


class TestDatasetManifest:
    def test_manifest_includes_all_frozen_files(self, tmp_path: Path):
        _seed_dataset(tmp_path)
        manifest = compute_dataset_manifest(tmp_path, dataset_seed=42)
        assert manifest.schema_version == DATASET_SCHEMA_VERSION
        assert manifest.dataset_seed == 42
        for name in _FROZEN_FILES:
            assert name in manifest.files
            assert len(manifest.files[name]) == 64  # sha256 hex length

    def test_fingerprint_is_deterministic_and_stable(self, tmp_path: Path):
        _seed_dataset(tmp_path)
        m1 = compute_dataset_manifest(tmp_path, dataset_seed=42)
        m2 = compute_dataset_manifest(tmp_path, dataset_seed=42)
        assert m1.fingerprint() == m2.fingerprint()
        assert len(m1.fingerprint()) == 64

    def test_fingerprint_does_not_carry_ground_truth_content(self, tmp_path: Path):
        _seed_dataset(tmp_path)
        manifest = compute_dataset_manifest(tmp_path, dataset_seed=42)
        payload = json.dumps(manifest.to_dict())
        # The fingerprint is a content hash of file hashes, not raw values.
        assert "EXACT_MATCH" not in payload
        assert "FEE_DEDUCTED" not in payload

    def test_manifest_differ_between_seeds(self, tmp_path: Path):
        ds42 = generate_dataset(seed=42)
        write_dataset(ds42, tmp_path)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
        run_evaluation(ds42, config, tmp_path)
        m42 = compute_dataset_manifest(tmp_path, dataset_seed=42)

        other = tmp_path / "other"
        other.mkdir()
        ds99 = generate_dataset(seed=99)
        write_dataset(ds99, other)
        run_evaluation(ds99, config, other)
        m99 = compute_dataset_manifest(other, dataset_seed=99)
        assert m42.fingerprint() != m99.fingerprint()


class TestManifestRoundTrip:
    def test_write_then_read_manifest(self, tmp_path: Path):
        _seed_dataset(tmp_path)
        manifest, status = freeze_or_verify_dataset(tmp_path, dataset_seed=42)
        assert status == "frozen"
        assert manifest_path(tmp_path).exists()
        read_back = read_manifest(tmp_path)
        assert read_back is not None
        assert read_back.fingerprint() == manifest.fingerprint()

    def test_read_manifest_returns_none_when_absent(self, tmp_path: Path):
        assert read_manifest(tmp_path) is None


class TestVerifyDataset:
    def test_verify_passes_when_files_match_manifest(self, tmp_path: Path):
        _seed_dataset(tmp_path)
        manifest, _ = freeze_or_verify_dataset(tmp_path, dataset_seed=42)
        result = verify_dataset(tmp_path, manifest)
        assert result.ok is True
        assert result.mismatches == ()
        assert result.missing == ()
        assert result.dataset_fingerprint == manifest.fingerprint()

    def test_verify_detects_drift(self, tmp_path: Path):
        _seed_dataset(tmp_path)
        manifest, _ = freeze_or_verify_dataset(tmp_path, dataset_seed=42)
        csv_path = tmp_path / "bank.csv"
        original = csv_path.read_text(encoding="utf-8")
        csv_path.write_text(
            original + "\nBANK-TAMPERED,100000,2026-08-01,t:\n", encoding="utf-8"
        )
        result = verify_dataset(tmp_path, manifest)
        assert result.ok is False
        assert "bank.csv" in result.mismatches

    def test_verify_detects_missing_file(self, tmp_path: Path):
        _seed_dataset(tmp_path)
        manifest, _ = freeze_or_verify_dataset(tmp_path, dataset_seed=42)
        (tmp_path / "ledger.csv").unlink()
        result = verify_dataset(tmp_path, manifest)
        assert result.ok is False
        assert "ledger.csv" in result.missing

    def test_verify_missing_manifest_computes_fingerprint_only(self, tmp_path: Path):
        _seed_dataset(tmp_path)
        result = verify_dataset(tmp_path, expected=None)
        assert result.ok is True
        assert result.dataset_fingerprint
        assert "No manifest present" in result.details[0]


class TestFreezeOrVerify:
    def test_frozen_then_verified_idempotent(self, tmp_path: Path):
        _seed_dataset(tmp_path)
        m1, status1 = freeze_or_verify_dataset(tmp_path, dataset_seed=42)
        assert status1 == "frozen"
        m2, status2 = freeze_or_verify_dataset(tmp_path, dataset_seed=42)
        assert status2 == "verified"
        assert m1.fingerprint() == m2.fingerprint()

    def test_drifted_dataset_raises(self, tmp_path: Path):
        _seed_dataset(tmp_path)
        freeze_or_verify_dataset(tmp_path, dataset_seed=42)
        (tmp_path / "settlements.csv").write_text("garbage\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Dataset drift detected"):
            freeze_or_verify_dataset(tmp_path, dataset_seed=42)


class TestRealDataProvenance:
    """The committed data/ directory is the frozen canonical dataset."""

    def test_real_data_dir_is_frozen_and_verified(self):
        manifest = read_manifest(DATA_DIR)
        assert manifest is not None, "data/ must be frozen with a manifest"
        result = verify_dataset(DATA_DIR, manifest)
        assert result.ok is True
        assert result.mismatches == ()

    def test_real_manifest_matches_seed42_generation(self):
        manifest = read_manifest(DATA_DIR)
        assert manifest is not None
        assert manifest.dataset_seed == 42
        ds = generate_dataset(seed=42)
        assert set(manifest.files.keys()) == set(_FROZEN_FILES)
        assert len(ds.scenarios) == 120
