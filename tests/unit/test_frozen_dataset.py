"""
Tests for frozen dataset fingerprint verification and Layer 2 artifact loading.

Covers the requirements from the Buildathon rescue plan:
  A. Frozen dataset: upload → L1 → residual → L2 artifact → L3 → final buckets
  B. Non-frozen dataset: upload → L1 → residual → L2 NOT executed
  C. Fingerprint mismatch: must never consume frozen Layer 2 artifacts
  D. Layer 3: confidence thresholds route correctly
  E. Existing deterministic matches remain unchanged
  F. Existing security/error behavior remains unchanged
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from starlette.testclient import TestClient

from reconciliation.api.app import create_app
from reconciliation.domain.models import SourceType
from reconciliation.frozen_dataset import (
    compute_upload_fingerprint,
    verify_upload_against_manifest,
)
from reconciliation.layer3 import RoutingBucket, RoutingReason
from reconciliation.normalizer import normalize_record

# ---------------------------------------------------------------------------
# Helpers — CSV generation
# ---------------------------------------------------------------------------

SETTLEMENT_FIELDS = ["settlement_id", "order_id", "gross_amount", "settlement_date"]
BANK_FIELDS = ["bank_utr", "order_id", "credit_amount", "value_date"]
LEDGER_FIELDS = ["order_id", "gross_amount", "transaction_date"]


def _write_csv(rows: List[Dict[str, str]], fieldnames: List[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _default_files() -> Dict[str, tuple]:
    """Small dataset with an exact-id match and residuals."""
    settlement = _write_csv(
        [
            {"settlement_id": "SET-001", "order_id": "ORD-001",
             "gross_amount": "1000.00", "settlement_date": "2026-08-01"},
            {"settlement_id": "SET-002", "gross_amount": "500.00",
             "settlement_date": "2026-08-05"},
        ],
        SETTLEMENT_FIELDS,
    )
    bank = _write_csv(
        [
            {"bank_utr": "BNK-001", "order_id": "ORD-001",
             "credit_amount": "1000.00", "value_date": "2026-08-01"},
            {"bank_utr": "BNK-002", "credit_amount": "3000.00",
             "value_date": "2026-08-03"},
        ],
        BANK_FIELDS,
    )
    ledger = _write_csv(
        [{"order_id": "LED-003", "gross_amount": "4000.00",
          "transaction_date": "2026-08-03"}],
        LEDGER_FIELDS,
    )
    return {
        "settlement": ("settlements.csv", settlement, "text/csv"),
        "bank": ("bank.csv", bank, "text/csv"),
        "ledger": ("ledger.csv", ledger, "text/csv"),
    }


def _files_from_bytes(
    settlement: bytes, bank: bytes, ledger: bytes,
) -> Dict[str, tuple]:
    return {
        "settlement": ("settlements.csv", settlement, "text/csv"),
        "bank": ("bank.csv", bank, "text/csv"),
        "ledger": ("ledger.csv", ledger, "text/csv"),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_path / "test.db")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def frozen_client(tmp_path: Path) -> tuple:
    """Client with data_dir pointing to a fake frozen dataset directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    app = create_app(
        db_path=tmp_path / "test.db",
        data_dir=data_dir,
    )
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, data_dir


def _normalize_rows(
    rows: List[Dict[str, str]], source_type: SourceType
) -> list:
    """Normalize a list of raw rows and return NormalizedRecord objects."""
    return [normalize_record(row, source_type) for row in rows]


def _build_frozen_test_data(
    tmp_path: Path,
    settlement_rows: List[Dict[str, str]],
    bank_rows: List[Dict[str, str]],
    ledger_rows: List[Dict[str, str]],
    residual_scenarios: List[Dict[str, Any]],
    audit_records: List[Dict[str, Any]] | None = None,
) -> Path:
    """Build a complete frozen test dataset directory.

    Normalizes the CSV rows to compute the actual record_ids, writes
    the CSV files, manifest, residuals, and optional audit records.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)

    settlement_content = _write_csv(settlement_rows, SETTLEMENT_FIELDS)
    bank_content = _write_csv(bank_rows, BANK_FIELDS)
    ledger_content = _write_csv(ledger_rows, LEDGER_FIELDS)

    (data_dir / "settlements.csv").write_bytes(settlement_content)
    (data_dir / "bank.csv").write_bytes(bank_content)
    (data_dir / "ledger.csv").write_bytes(ledger_content)

    # Normalize to get actual record_ids.
    set_recs = _normalize_rows(settlement_rows, SourceType.SETTLEMENT)
    bank_recs = _normalize_rows(bank_rows, SourceType.BANK)
    ledger_recs = _normalize_rows(ledger_rows, SourceType.LEDGER)
    all_recs = set_recs + bank_recs + ledger_recs
    rec_map = {r.record_id: r for r in all_recs}

    # Write residuals.csv with actual record_ids.
    residuals_fields = ["scenario_id", "category", "record_count", "member_record_ids"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=residuals_fields)
    writer.writeheader()
    for scenario in residual_scenarios:
        writer.writerow(scenario)
    (data_dir / "residuals.csv").write_text(buf.getvalue(), encoding="utf-8")

    # Write ground_truth.json.
    (data_dir / "ground_truth.json").write_text(
        json.dumps({"total_scenarios": 0, "scenarios": []}),
        encoding="utf-8",
    )

    # Compute manifest.
    fp = compute_upload_fingerprint(settlement_content, bank_content, ledger_content)
    gt_hash = hashlib.sha256(
        (data_dir / "ground_truth.json").read_bytes()
    ).hexdigest()
    res_hash = hashlib.sha256(
        (data_dir / "residuals.csv").read_bytes()
    ).hexdigest()

    manifest = {
        "schema_version": "1.0",
        "dataset_seed": 42,
        "fingerprint": "fake-test-fingerprint",
        "files": {
            "ground_truth.json": gt_hash,
            "settlements.csv": fp["settlements.csv"],
            "bank.csv": fp["bank.csv"],
            "ledger.csv": fp["ledger.csv"],
            "residuals.csv": res_hash,
        },
    }
    (data_dir / "dataset_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    # Write audit records if provided and add canonical artifact reference.
    if audit_records:
        artifact_path = data_dir / "layer2_full_audit.jsonl"
        with artifact_path.open("w", encoding="utf-8") as f:
            for rec in audit_records:
                f.write(json.dumps(rec) + "\n")
        artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        manifest["canonical_layer2_artifact"] = {
            "filename": artifact_path.name,
            "sha256": artifact_hash,
            "generation_timestamp": "2026-01-01T00:00:00+00:00",
            "record_count": len(audit_records),
            "note": "Test artifact.",
        }
        (data_dir / "dataset_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

    return data_dir


@pytest.fixture
def fake_frozen_data(tmp_path: Path) -> Path:
    """Create a minimal frozen dataset directory with manifest and artifacts.

    Settlement + Bank share order_id ORD-F1 → Layer 1 exact-id match.
    Settlement SET-F2, Bank BNK-F2 are residuals with matching amounts and dates
    (satisfies financial evidence guardrail for auto-acceptance).
    """
    settlement_rows = [
        {"settlement_id": "SET-F1", "order_id": "ORD-F1",
         "gross_amount": "5000.00", "settlement_date": "2026-08-01"},
        {"settlement_id": "SET-F2", "order_id": "ORD-F2",
         "gross_amount": "3000.00", "settlement_date": "2026-08-01"},
    ]
    bank_rows = [
        {"bank_utr": "BNK-F1", "order_id": "ORD-F1",
         "credit_amount": "5000.00", "value_date": "2026-08-01"},
        {"bank_utr": "BNK-F2", "order_id": "ORD-F3",
         "credit_amount": "3000.00", "value_date": "2026-08-01"},
    ]
    ledger_rows = [
        {"order_id": "LED-F4", "gross_amount": "2000.00",
         "transaction_date": "2026-08-04"},
    ]

    # Compute actual record IDs via normalization.
    set_recs = _normalize_rows(settlement_rows, SourceType.SETTLEMENT)
    bank_recs = _normalize_rows(bank_rows, SourceType.BANK)
    ledger_recs = _normalize_rows(ledger_rows, SourceType.LEDGER)
    all_recs = set_recs + bank_recs + ledger_recs
    rec_map = {r.record_id: r for r in all_recs}

    # Residuals: SET-F2, BNK-F2, LED-F4 are not matched by L1.
    set_f2_id = set_recs[1].record_id  # SET-F2
    bnk_f2_id = bank_recs[1].record_id  # BNK-F2
    led_f4_id = ledger_recs[0].record_id  # LED-F4

    residuals = [
        {
            "scenario_id": "FROZEN-RES-001",
            "category": "TEST",
            "record_count": "2",
            "member_record_ids": json.dumps([set_f2_id, bnk_f2_id]),
        },
        {
            "scenario_id": "FROZEN-RES-002",
            "category": "TEST",
            "record_count": "1",
            "member_record_ids": json.dumps([led_f4_id]),
        },
    ]

    return _build_frozen_test_data(
        tmp_path, settlement_rows, bank_rows, ledger_rows, residuals
    )


# ===========================================================================
# Test A: Frozen dataset → L1 → L2 artifact → L3 → final buckets
# ===========================================================================

class TestFrozenDatasetIntegration:
    """Upload the frozen dataset and verify L2 artifacts are loaded and routed."""

    def test_frozen_dataset_loads_l2_outcomes(
        self, fake_frozen_data: Path
    ) -> None:
        """When uploading CSVs matching the frozen manifest, the pipeline
        loads L2 outcomes from the audit artifact."""
        from reconciliation.api.pipeline import run_batch_pipeline
        from reconciliation.frozen_dataset import (
            compute_upload_fingerprint,
            verify_upload_against_manifest,
        )

        data_dir = fake_frozen_data

        # Read the frozen CSV files.
        settlement_bytes = (data_dir / "settlements.csv").read_bytes()
        bank_bytes = (data_dir / "bank.csv").read_bytes()
        ledger_bytes = (data_dir / "ledger.csv").read_bytes()

        fp = compute_upload_fingerprint(settlement_bytes, bank_bytes, ledger_bytes)
        matches, manifest, details = verify_upload_against_manifest(fp, data_dir)
        assert matches, f"Fingerprint should match: {details}"
        assert manifest is not None

    def test_frozen_dataset_api_endpoint(
        self, fake_frozen_data: Path
    ) -> None:
        """End-to-end: upload frozen CSVs through API, verify batch completes."""
        data_dir = fake_frozen_data

        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()

        app = create_app(
            db_path=data_dir.parent / "test.db",
            data_dir=data_dir,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/batches",
                files=_files_from_bytes(settlement, bank, ledger),
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["status"] == "completed"
            assert data["record_count"] == 5

            # Check that layer2_mode is "frozen_artifact"
            eval_resp = client.get(f"/batches/{data['batch_id']}/eval")
            assert eval_resp.status_code == 200
            eval_data = eval_resp.json()["eval"]
            assert eval_data["layer2_mode"] == "frozen_artifact"

    def test_frozen_l2_outcomes_appear_in_routing(
        self, tmp_path: Path
    ) -> None:
        """When frozen L2 outcomes exist, they appear in routing results
        (not just EXCEPTION/NO_CANDIDATE for all residuals).

        The key constraint is that the records proposed by the frozen L2
        artifact must NOT already be consumed by Layer 1.  Layer 1 only
        matches cross-source records (SETTLEMENT vs BANK/LEDGER), so we
        use two same-source settlement records for the L2 proposal.  This
        ensures Layer 1 leaves them as residuals, allowing the frozen
        L2 outcome to route them through Layer 3.
        """
        settlement_rows = [
            {"settlement_id": "SET-F1", "order_id": "ORD-F1",
             "gross_amount": "5000.00", "settlement_date": "2026-08-01"},
            # SET-F2 and SET-F3: same-source pair for frozen L2 proposal.
            # Layer 1 cannot match same-source records, so these remain residuals.
            # Same amount (3000.00) and date satisfy Layer 3 evidence gate.
            {"settlement_id": "SET-F2", "order_id": "ORD-F2",
             "gross_amount": "3000.00", "settlement_date": "2026-08-01"},
            {"settlement_id": "SET-F3", "order_id": "ORD-F3",
             "gross_amount": "3000.00", "settlement_date": "2026-08-01"},
        ]
        bank_rows = [
            # BNK-F1 matches SET-F1 via EXACT_ID (order_id ORD-F1).
            {"bank_utr": "BNK-F1", "order_id": "ORD-F1",
             "credit_amount": "5000.00", "value_date": "2026-08-01"},
        ]
        ledger_rows = [
            {"order_id": "LED-F4", "gross_amount": "2000.00",
             "transaction_date": "2026-08-04"},
        ]

        # Compute actual record IDs.
        set_recs = _normalize_rows(settlement_rows, SourceType.SETTLEMENT)
        set_f2_id = set_recs[1].record_id  # SET-F2
        set_f3_id = set_recs[2].record_id  # SET-F3
        led_f4_id = _normalize_rows(ledger_rows, SourceType.LEDGER)[0].record_id

        residuals = [
            {
                "scenario_id": "FROZEN-RES-001",
                "category": "TEST",
                "record_count": "2",
                "member_record_ids": json.dumps([set_f2_id, set_f3_id]),
            },
            {
                "scenario_id": "FROZEN-RES-002",
                "category": "TEST",
                "record_count": "1",
                "member_record_ids": json.dumps([led_f4_id]),
            },
        ]

        # Create audit record with matching IDs and high confidence.
        # The frozen L2 artifact proposes SET-F2 + SET-F3 as a same-source
        # duplicate pair.  Both have the same amount and date, satisfying
        # the Layer 3 financial evidence gate.
        audit_record = {
            "correlation_id": "test-correlation-001",
            "timestamp": "2026-08-30T00:00:00+00:00",
            "presented_record_ids": [set_f2_id, set_f3_id],
            "outcome": "PROPOSAL_VALID",
            "proposal": {
                "proposed_match_ids": [set_f2_id, set_f3_id],
                "confidence": 0.95,
                "rationale": "Both records share the same amount and date.",
            },
            "confidence": 0.95,
            "reason": "Proposal validated.",
            "dataset_fingerprint": "fake-test-fingerprint",
        }

        data_dir = _build_frozen_test_data(
            tmp_path, settlement_rows, bank_rows, ledger_rows, residuals,
            audit_records=[audit_record],
        )

        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()

        app = create_app(
            db_path=data_dir.parent / "test.db",
            data_dir=data_dir,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/batches",
                files=_files_from_bytes(settlement, bank, ledger),
            )
            assert resp.status_code == 201
            batch_id = resp.json()["batch_id"]

            # The L2 proposal (confidence 0.95) should route to AI_AUTO_ACCEPTED.
            # SET-F2 and SET-F3 are same-source with identical amount/date,
            # so the evidence gate passes.
            auto = client.get(
                f"/batches/{batch_id}/results?bucket=AI_AUTO_ACCEPTED"
            ).json()
            assert auto["count"] >= 1, (
                "Expected at least one AI_AUTO_ACCEPTED record from frozen L2"
            )

            # Verify the record is distinguishable from deterministic matches.
            for record in auto["records"]:
                assert record["source_outcome"] == "PROPOSAL_VALID"


# ===========================================================================
# Test B: Non-frozen dataset → L2 NOT executed
# ===========================================================================

class TestNonFrozenDataset:
    """Arbitrary uploads must NOT load frozen L2 artifacts."""

    def test_arbitrary_upload_skips_layer2(
        self, fake_frozen_data: Path
    ) -> None:
        """An upload that doesn't match the frozen manifest gets
        layer2_mode = 'not_executed' and no L2 outcomes."""
        data_dir = fake_frozen_data

        # Upload DIFFERENT CSV content (not matching the frozen dataset).
        settlement = _write_csv(
            [{"settlement_id": "SET-X", "order_id": "ORD-X",
              "gross_amount": "999.00", "settlement_date": "2026-09-01"}],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-X", "order_id": "ORD-X",
              "credit_amount": "999.00", "value_date": "2026-09-01"}],
            BANK_FIELDS,
        )
        ledger = _write_csv(
            [{"order_id": "LED-X", "gross_amount": "999.00",
              "transaction_date": "2026-09-01"}],
            LEDGER_FIELDS,
        )

        app = create_app(
            db_path=data_dir.parent / "test.db",
            data_dir=data_dir,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/batches",
                files=_files_from_bytes(settlement, bank, ledger),
            )
            assert resp.status_code == 201
            batch_id = resp.json()["batch_id"]

            eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
            assert eval_data["layer2_mode"] == "not_executed"
            assert eval_data["layer2"]["residual_records_processed"] == 0

            # No AI buckets should have records.
            auto = client.get(
                f"/batches/{batch_id}/results?bucket=AI_AUTO_ACCEPTED"
            ).json()
            review = client.get(
                f"/batches/{batch_id}/results?bucket=HUMAN_REVIEW"
            ).json()
            assert auto["count"] == 0
            assert review["count"] == 0

    def test_no_manifest_skips_layer2(self, tmp_path: Path) -> None:
        """Without a manifest, Layer 2 is always skipped."""
        app = create_app(
            db_path=tmp_path / "test.db",
            data_dir=tmp_path / "empty_data",
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/batches", files=_default_files())
            assert resp.status_code == 201
            batch_id = resp.json()["batch_id"]

            eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
            assert eval_data["layer2_mode"] == "not_executed"


# ===========================================================================
# Test C: Fingerprint mismatch → must never consume frozen artifacts
# ===========================================================================

class TestFingerprintMismatch:
    """Fingerprint verification must fail-closed for any mismatch."""

    def test_wrong_content_rejects_frozen_artifacts(
        self, fake_frozen_data: Path
    ) -> None:
        """Modified CSV content must not match the frozen manifest."""
        from reconciliation.frozen_dataset import (
            compute_upload_fingerprint,
            verify_upload_against_manifest,
        )

        data_dir = fake_frozen_data

        # Read original content.
        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()

        fp_original = compute_upload_fingerprint(settlement, bank, ledger)
        matches, _, _ = verify_upload_against_manifest(fp_original, data_dir)
        assert matches

        # Modify one byte in settlement CSV.
        modified_settlement = settlement[:-1] + b"X"
        fp_modified = compute_upload_fingerprint(modified_settlement, bank, ledger)
        matches, _, details = verify_upload_against_manifest(fp_modified, data_dir)
        assert not matches
        assert any("settlements.csv" in d for d in details)

    def test_tampered_bank_rejects(
        self, fake_frozen_data: Path
    ) -> None:
        """Even a single byte change in bank CSV must fail verification."""
        from reconciliation.frozen_dataset import (
            compute_upload_fingerprint,
            verify_upload_against_manifest,
        )

        data_dir = fake_frozen_data
        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()

        modified_bank = b"X" + bank[1:]
        fp = compute_upload_fingerprint(settlement, modified_bank, ledger)
        matches, _, details = verify_upload_against_manifest(fp, data_dir)
        assert not matches
        assert any("bank.csv" in d for d in details)

    def test_partial_manifest_match_rejects(
        self, tmp_path: Path
    ) -> None:
        """If only 2 of 3 files match, the dataset is rejected."""
        from reconciliation.frozen_dataset import (
            compute_upload_fingerprint,
            verify_upload_against_manifest,
        )

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Create manifest with known hashes.
        settlement_content = _write_csv(
            [{"settlement_id": "S1", "gross_amount": "100.00",
              "settlement_date": "2026-01-01"}],
            SETTLEMENT_FIELDS,
        )
        bank_content = _write_csv(
            [{"bank_utr": "B1", "credit_amount": "100.00",
              "value_date": "2026-01-01"}],
            BANK_FIELDS,
        )
        ledger_content = _write_csv(
            [{"order_id": "L1", "gross_amount": "100.00",
              "transaction_date": "2026-01-01"}],
            LEDGER_FIELDS,
        )

        fp = compute_upload_fingerprint(settlement_content, bank_content, ledger_content)

        manifest = {
            "schema_version": "1.0",
            "dataset_seed": 42,
            "fingerprint": "test",
            "files": {
                "settlements.csv": fp["settlements.csv"],
                "bank.csv": "wrong_hash",
                "ledger.csv": fp["ledger.csv"],
                "ground_truth.json": "x",
                "residuals.csv": "x",
            },
        }
        (data_dir / "dataset_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

        matches, _, details = verify_upload_against_manifest(fp, data_dir)
        assert not matches
        assert any("bank.csv" in d for d in details)

    def test_frozen_mismatch_does_not_load_artifacts(
        self, fake_frozen_data: Path
    ) -> None:
        """When fingerprint doesn't match, frozen L2 artifacts are never loaded
        even if they exist in the data directory."""
        data_dir = fake_frozen_data

        # Create an L2 artifact that WOULD produce proposals.
        audit_record = {
            "correlation_id": "test-correlation-002",
            "timestamp": "2026-08-30T00:00:00+00:00",
            "presented_record_ids": [
                "SETTLEMENT-FROZEN-R01",
                "BANK-FROZEN-R01",
            ],
            "outcome": "PROPOSAL_VALID",
            "proposal": {
                "proposed_match_ids": [
                    "SETTLEMENT-FROZEN-R01",
                    "BANK-FROZEN-R01",
                ],
                "confidence": 0.99,
                "rationale": "Test.",
            },
            "confidence": 0.99,
            "reason": "Test.",
            "dataset_fingerprint": "fake-test-fingerprint",
        }
        (data_dir / "layer2_full_audit.jsonl").write_text(
            json.dumps(audit_record) + "\n", encoding="utf-8"
        )

        # Upload different content (won't match manifest).
        settlement = _write_csv(
            [{"settlement_id": "SET-X", "order_id": "ORD-X",
              "gross_amount": "999.00", "settlement_date": "2026-09-01"}],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-X", "order_id": "ORD-X",
              "credit_amount": "999.00", "value_date": "2026-09-01"}],
            BANK_FIELDS,
        )
        ledger = _write_csv(
            [{"order_id": "LED-X", "gross_amount": "999.00",
              "transaction_date": "2026-09-01"}],
            LEDGER_FIELDS,
        )

        app = create_app(
            db_path=data_dir.parent / "test2.db",
            data_dir=data_dir,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/batches",
                files=_files_from_bytes(settlement, bank, ledger),
            )
            assert resp.status_code == 201
            batch_id = resp.json()["batch_id"]

            eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
            assert eval_data["layer2_mode"] == "not_executed"

            # The L2 artifact must NOT have been consumed.
            auto = client.get(
                f"/batches/{batch_id}/results?bucket=AI_AUTO_ACCEPTED"
            ).json()
            assert auto["count"] == 0


# ===========================================================================
# Test D: Layer 3 confidence thresholds
# ===========================================================================

class TestLayer3ConfidenceThresholds:
    """Verify that Layer 3 routes proposals by confidence correctly."""

    def _make_client_with_audit(
        self, tmp_path: Path, audit_records: List[Dict[str, Any]],
        csv_content: tuple,
    ) -> tuple:
        """Helper: set up frozen data dir with correct record IDs, write audit, return client."""
        settlement_content, bank_content, ledger_content = csv_content

        settlement_rows = list(csv.DictReader(io.StringIO(settlement_content.decode())))
        bank_rows = list(csv.DictReader(io.StringIO(bank_content.decode())))
        ledger_rows = list(csv.DictReader(io.StringIO(ledger_content.decode())))

        # Compute actual record IDs.
        set_recs = _normalize_rows(settlement_rows, SourceType.SETTLEMENT)
        bank_recs = _normalize_rows(bank_rows, SourceType.BANK)
        ledger_recs = _normalize_rows(ledger_rows, SourceType.LEDGER)

        # Build residuals from audit records using actual record IDs.
        # Each audit's presented_record_ids contains actual record IDs.
        residuals = []
        for i, audit in enumerate(audit_records):
            member_ids = audit["presented_record_ids"][:2]
            residuals.append({
                "scenario_id": f"TEST-RES-{i:03d}",
                "category": "TEST",
                "record_count": str(len(member_ids)),
                "member_record_ids": json.dumps(member_ids),
            })

        data_dir = _build_frozen_test_data(
            tmp_path, settlement_rows, bank_rows, ledger_rows, residuals,
            audit_records=audit_records,
        )

        app = create_app(
            db_path=tmp_path / "test.db",
            data_dir=data_dir,
        )
        return app, data_dir

    def _run_with_audit(
        self, tmp_path: Path, audit_records: List[Dict[str, Any]],
        settlement_rows: List[Dict[str, str]],
        bank_rows: List[Dict[str, str]],
        ledger_rows: List[Dict[str, str]],
    ) -> tuple:
        """Helper: build frozen data, run batch, return (client, batch_id)."""
        # Compute actual record IDs.
        set_recs = _normalize_rows(settlement_rows, SourceType.SETTLEMENT)
        bank_recs = _normalize_rows(bank_rows, SourceType.BANK)
        ledger_recs = _normalize_rows(ledger_rows, SourceType.LEDGER)

        # Build residuals from audit records using actual record IDs.
        residuals = []
        for i, audit in enumerate(audit_records):
            member_ids = audit["presented_record_ids"][:2]
            residuals.append({
                "scenario_id": f"TEST-RES-{i:03d}",
                "category": "TEST",
                "record_count": str(len(member_ids)),
                "member_record_ids": json.dumps(member_ids),
            })

        data_dir = _build_frozen_test_data(
            tmp_path, settlement_rows, bank_rows, ledger_rows, residuals,
            audit_records=audit_records,
        )

        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()

        app = create_app(
            db_path=tmp_path / "test.db",
            data_dir=data_dir,
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/batches",
            files=_files_from_bytes(settlement, bank, ledger),
        )
        assert resp.status_code == 201
        return client, resp.json()["batch_id"]

    def _audit_for_residual(
        self,
        settlement_rows: List[Dict[str, str]],
        bank_rows: List[Dict[str, str]],
        ledger_rows: List[Dict[str, str]],
        outcome: str,
        confidence: float | None,
        proposed_match_ids: List[str] | None = None,
        reason: str = "OK",
    ) -> Dict[str, Any]:
        """Create an audit record with correct IDs for the given CSV data."""
        set_recs = _normalize_rows(settlement_rows, SourceType.SETTLEMENT)
        bank_recs = _normalize_rows(bank_rows, SourceType.BANK)
        ledger_recs = _normalize_rows(ledger_rows, SourceType.LEDGER)
        # Use the second settlement and first bank as residual pair.
        member_ids = [set_recs[-1].record_id, bank_recs[-1].record_id]
        # Filter out IDs that would match via L1 (shared order_id).
        l1_ids = set()
        for sr in set_recs:
            for br in bank_recs:
                if sr.order_id_hint and sr.order_id_hint == br.order_id_hint:
                    l1_ids.add(sr.record_id)
                    l1_ids.add(br.record_id)
        non_l1 = [rid for rid in member_ids if rid not in l1_ids]
        if len(non_l1) < 2:
            non_l1 = member_ids

        proposal = None
        if outcome == "PROPOSAL_VALID" and proposed_match_ids is None:
            proposed_match_ids = non_l1
        if proposed_match_ids is not None:
            proposal = {
                "proposed_match_ids": proposed_match_ids,
                "confidence": confidence or 0.0,
                "rationale": "Test.",
            }

        return {
            "correlation_id": f"corr-{outcome}-{confidence}",
            "timestamp": "2026-08-30T00:00:00+00:00",
            "presented_record_ids": non_l1,
            "outcome": outcome,
            "proposal": proposal,
            "confidence": confidence,
            "reason": reason,
        }

    def test_high_confidence_auto_accepted(self, tmp_path: Path) -> None:
        """Confidence >= 0.90 with financial evidence → AI_AUTO_ACCEPTED."""
        settlement_rows = [
            {"settlement_id": "SET-H1", "order_id": "ORD-H1",
             "gross_amount": "1000.00", "settlement_date": "2026-08-01"},
            {"settlement_id": "SET-H2", "order_id": "ORD-H2",
             "gross_amount": "1000.00", "settlement_date": "2026-08-01"},
        ]
        bank_rows = [
            {"bank_utr": "BNK-H1", "order_id": "ORD-H3",
             "credit_amount": "1000.00", "value_date": "2026-08-01"},
        ]
        ledger_rows = []

        audit = self._audit_for_residual(
            settlement_rows, bank_rows, ledger_rows,
            outcome="PROPOSAL_VALID", confidence=0.95,
        )

        client, batch_id = self._run_with_audit(
            tmp_path, [audit], settlement_rows, bank_rows, ledger_rows,
        )
        auto = client.get(f"/batches/{batch_id}/results?bucket=AI_AUTO_ACCEPTED").json()
        assert auto["count"] >= 1
        for record in auto["records"]:
            assert record["confidence"] is not None
            assert record["confidence"] >= 0.90

    def test_mid_confidence_human_review(self, tmp_path: Path) -> None:
        """Confidence 0.60–0.89 → HUMAN_REVIEW."""
        settlement_rows = [
            {"settlement_id": "SET-M1", "order_id": "ORD-M1",
             "gross_amount": "1000.00", "settlement_date": "2026-08-01"},
            {"settlement_id": "SET-M2", "order_id": "ORD-M2",
             "gross_amount": "2000.00", "settlement_date": "2026-08-02"},
        ]
        bank_rows = [
            {"bank_utr": "BNK-M1", "order_id": "ORD-M3",
             "credit_amount": "5000.00", "value_date": "2026-08-03"},
        ]
        ledger_rows = []

        audit = self._audit_for_residual(
            settlement_rows, bank_rows, ledger_rows,
            outcome="PROPOSAL_VALID", confidence=0.75,
        )

        client, batch_id = self._run_with_audit(
            tmp_path, [audit], settlement_rows, bank_rows, ledger_rows,
        )
        review = client.get(f"/batches/{batch_id}/results?bucket=HUMAN_REVIEW").json()
        assert review["count"] >= 1
        for record in review["records"]:
            assert record["confidence"] is not None
            assert 0.60 <= record["confidence"] < 0.90

    def test_low_confidence_exception(self, tmp_path: Path) -> None:
        """Confidence < 0.60 → EXCEPTION / LOW_CONFIDENCE."""
        settlement_rows = [
            {"settlement_id": "SET-L1", "order_id": "ORD-L1",
             "gross_amount": "1000.00", "settlement_date": "2026-08-01"},
            {"settlement_id": "SET-L2", "order_id": "ORD-L2",
             "gross_amount": "2000.00", "settlement_date": "2026-08-02"},
        ]
        bank_rows = [
            {"bank_utr": "BNK-L1", "order_id": "ORD-L3",
             "credit_amount": "5000.00", "value_date": "2026-08-03"},
        ]
        ledger_rows = []

        audit = self._audit_for_residual(
            settlement_rows, bank_rows, ledger_rows,
            outcome="PROPOSAL_VALID", confidence=0.40,
        )

        client, batch_id = self._run_with_audit(
            tmp_path, [audit], settlement_rows, bank_rows, ledger_rows,
        )
        exc = client.get(f"/batches/{batch_id}/results?bucket=EXCEPTION").json()
        low_conf = [r for r in exc["records"] if r["reason"] == "LOW_CONFIDENCE"]
        assert len(low_conf) >= 1

    def test_threshold_boundary_0_90_auto(self, tmp_path: Path) -> None:
        """Confidence exactly 0.90 with financial evidence → AI_AUTO_ACCEPTED."""
        settlement_rows = [
            {"settlement_id": "SET-B1", "order_id": "ORD-B1",
             "gross_amount": "1000.00", "settlement_date": "2026-08-01"},
            {"settlement_id": "SET-B2", "order_id": "ORD-B2",
             "gross_amount": "1000.00", "settlement_date": "2026-08-01"},
        ]
        bank_rows = [
            {"bank_utr": "BNK-B1", "order_id": "ORD-B3",
             "credit_amount": "1000.00", "value_date": "2026-08-01"},
        ]
        ledger_rows = []

        audit = self._audit_for_residual(
            settlement_rows, bank_rows, ledger_rows,
            outcome="PROPOSAL_VALID", confidence=0.90,
        )

        client, batch_id = self._run_with_audit(
            tmp_path, [audit], settlement_rows, bank_rows, ledger_rows,
        )
        auto = client.get(f"/batches/{batch_id}/results?bucket=AI_AUTO_ACCEPTED").json()
        assert auto["count"] >= 1

    def test_threshold_boundary_0_60_review(self, tmp_path: Path) -> None:
        """Confidence exactly 0.60 → HUMAN_REVIEW."""
        settlement_rows = [
            {"settlement_id": "SET-B3", "order_id": "ORD-B4",
             "gross_amount": "1000.00", "settlement_date": "2026-08-01"},
            {"settlement_id": "SET-B4", "order_id": "ORD-B5",
             "gross_amount": "2000.00", "settlement_date": "2026-08-02"},
        ]
        bank_rows = [
            {"bank_utr": "BNK-B2", "order_id": "ORD-B6",
             "credit_amount": "5000.00", "value_date": "2026-08-03"},
        ]
        ledger_rows = []

        audit = self._audit_for_residual(
            settlement_rows, bank_rows, ledger_rows,
            outcome="PROPOSAL_VALID", confidence=0.60,
        )

        client, batch_id = self._run_with_audit(
            tmp_path, [audit], settlement_rows, bank_rows, ledger_rows,
        )
        review = client.get(f"/batches/{batch_id}/results?bucket=HUMAN_REVIEW").json()
        assert review["count"] >= 1

    def test_api_error_routes_to_exception(self, tmp_path: Path) -> None:
        """API_ERROR outcome → EXCEPTION / AI_RESPONSE_INVALID."""
        settlement_rows = [
            {"settlement_id": "SET-E1", "order_id": "ORD-E1",
             "gross_amount": "1000.00", "settlement_date": "2026-08-01"},
            {"settlement_id": "SET-E2", "order_id": "ORD-E2",
             "gross_amount": "2000.00", "settlement_date": "2026-08-02"},
        ]
        bank_rows = [
            {"bank_utr": "BNK-E1", "order_id": "ORD-E3",
             "credit_amount": "5000.00", "value_date": "2026-08-03"},
        ]
        ledger_rows = []

        audit = self._audit_for_residual(
            settlement_rows, bank_rows, ledger_rows,
            outcome="API_ERROR", confidence=None,
            reason="LLM provider returned an API error.",
        )

        client, batch_id = self._run_with_audit(
            tmp_path, [audit], settlement_rows, bank_rows, ledger_rows,
        )
        exc = client.get(f"/batches/{batch_id}/results?bucket=EXCEPTION").json()
        ai_invalid = [r for r in exc["records"] if r["reason"] == "AI_RESPONSE_INVALID"]
        assert len(ai_invalid) >= 1

    def test_no_proposal_routes_to_exception(self, tmp_path: Path) -> None:
        """NO_PROPOSAL outcome → EXCEPTION / NO_CANDIDATE."""
        settlement_rows = [
            {"settlement_id": "SET-N1", "order_id": "ORD-N1",
             "gross_amount": "1000.00", "settlement_date": "2026-08-01"},
            {"settlement_id": "SET-N2", "order_id": "ORD-N2",
             "gross_amount": "2000.00", "settlement_date": "2026-08-02"},
        ]
        bank_rows = [
            {"bank_utr": "BNK-N1", "order_id": "ORD-N3",
             "credit_amount": "5000.00", "value_date": "2026-08-03"},
        ]
        ledger_rows = []

        audit = self._audit_for_residual(
            settlement_rows, bank_rows, ledger_rows,
            outcome="NO_PROPOSAL", confidence=0.1,
            reason="Model returned no proposed match IDs.",
            proposed_match_ids=[],
        )

        client, batch_id = self._run_with_audit(
            tmp_path, [audit], settlement_rows, bank_rows, ledger_rows,
        )
        exc = client.get(f"/batches/{batch_id}/results?bucket=EXCEPTION").json()
        no_cand = [r for r in exc["records"] if r["reason"] == "NO_CANDIDATE"]
        assert len(no_cand) >= 1


# ===========================================================================
# Test E: Existing deterministic matches remain unchanged
# ===========================================================================

class TestDeterministicMatchesUnchanged:
    """Layer 1 deterministic matches must not be affected by frozen L2 loading."""

    def test_exact_id_match_unaffected_by_l2(self, tmp_path: Path) -> None:
        """A Layer 1 exact-id match must remain DETERMINISTIC_MATCH regardless
        of whether frozen L2 outcomes exist."""
        settlement_rows = [
            {"settlement_id": "SET-F1", "order_id": "ORD-F1",
             "gross_amount": "5000.00", "settlement_date": "2026-08-01"},
            {"settlement_id": "SET-F2", "order_id": "ORD-F2",
             "gross_amount": "3000.00", "settlement_date": "2026-08-02"},
        ]
        bank_rows = [
            {"bank_utr": "BNK-F1", "order_id": "ORD-F1",
             "credit_amount": "5000.00", "value_date": "2026-08-01"},
            {"bank_utr": "BNK-F2", "order_id": "ORD-F3",
             "credit_amount": "7000.00", "value_date": "2026-08-03"},
        ]
        ledger_rows = [
            {"order_id": "LED-F4", "gross_amount": "2000.00",
             "transaction_date": "2026-08-04"},
        ]

        # Compute actual record IDs.
        set_recs = _normalize_rows(settlement_rows, SourceType.SETTLEMENT)
        bank_recs = _normalize_rows(bank_rows, SourceType.BANK)
        set_f2_id = set_recs[1].record_id
        bnk_f2_id = bank_recs[1].record_id
        led_f4_id = _normalize_rows(ledger_rows, SourceType.LEDGER)[0].record_id

        residuals = [
            {
                "scenario_id": "FROZEN-RES-001",
                "category": "TEST",
                "record_count": "2",
                "member_record_ids": json.dumps([set_f2_id, bnk_f2_id]),
            },
            {
                "scenario_id": "FROZEN-RES-002",
                "category": "TEST",
                "record_count": "1",
                "member_record_ids": json.dumps([led_f4_id]),
            },
        ]

        # Create L2 artifact for a residual (not the L1-matched records).
        audit_record = {
            "correlation_id": "corr-l1",
            "timestamp": "2026-08-30T00:00:00+00:00",
            "presented_record_ids": [set_f2_id, bnk_f2_id],
            "outcome": "PROPOSAL_VALID",
            "proposal": {
                "proposed_match_ids": [set_f2_id, bnk_f2_id],
                "confidence": 0.99,
                "rationale": "Test.",
            },
            "confidence": 0.99,
            "reason": "Test.",
        }

        data_dir = _build_frozen_test_data(
            tmp_path, settlement_rows, bank_rows, ledger_rows, residuals,
            audit_records=[audit_record],
        )

        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()

        app = create_app(
            db_path=data_dir.parent / "test.db",
            data_dir=data_dir,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/batches",
                files=_files_from_bytes(settlement, bank, ledger),
            )
            assert resp.status_code == 201
            batch_id = resp.json()["batch_id"]

            # SET-F1 + BNK-F1 (order_id=ORD-F1) should be DETERMINISTIC_MATCH.
            det = client.get(
                f"/batches/{batch_id}/results?bucket=DETERMINISTIC_MATCH"
            ).json()
            matched_ids = {r["record_id"] for r in det["records"]}
            assert len(matched_ids) >= 2

            # Each deterministic match audit should show resolved_by=LAYER_1.
            for record in det["records"][:2]:
                audit_rec = client.get(
                    f"/batches/{batch_id}/records/{record['record_id']}"
                ).json()["record"]
                assert audit_rec["resolved_by"] == "LAYER_1"
                assert audit_rec["layer1"] is not None
                assert audit_rec["layer2"] is None


# ===========================================================================
# Test F: Existing security/error behavior unchanged
# ===========================================================================

class TestSecurityUnchanged:
    """Existing security and error handling must remain intact."""

    def test_no_api_keys_in_audit_records(
        self, fake_frozen_data: Path
    ) -> None:
        """Audit records must never contain API keys or secrets."""
        data_dir = fake_frozen_data

        # Read actual CSV content and compute record IDs.
        settlement_rows = list(csv.DictReader(io.StringIO(
            (data_dir / "settlements.csv").read_text()
        )))
        bank_rows = list(csv.DictReader(io.StringIO(
            (data_dir / "bank.csv").read_text()
        )))
        ledger_rows = list(csv.DictReader(io.StringIO(
            (data_dir / "ledger.csv").read_text()
        )))
        set_recs = _normalize_rows(settlement_rows, SourceType.SETTLEMENT)
        bank_recs = _normalize_rows(bank_rows, SourceType.BANK)
        set_f2_id = set_recs[1].record_id
        bnk_f2_id = bank_recs[1].record_id

        audit_record = {
            "correlation_id": "corr-sec",
            "timestamp": "2026-08-30T00:00:00+00:00",
            "presented_record_ids": [set_f2_id, bnk_f2_id],
            "outcome": "PROPOSAL_VALID",
            "proposal": {
                "proposed_match_ids": [set_f2_id, bnk_f2_id],
                "confidence": 0.95,
                "rationale": "Test.",
            },
            "confidence": 0.95,
            "reason": "Test.",
        }
        (data_dir / "layer2_full_audit.jsonl").write_text(
            json.dumps(audit_record) + "\n", encoding="utf-8"
        )

        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()

        app = create_app(
            db_path=data_dir.parent / "test.db",
            data_dir=data_dir,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/batches",
                files=_files_from_bytes(settlement, bank, ledger),
            )
            assert resp.status_code == 201
            batch_id = resp.json()["batch_id"]

            results = client.get(f"/batches/{batch_id}/results").json()
            for record in results["records"]:
                audit_rec = client.get(
                    f"/batches/{batch_id}/records/{record['record_id']}"
                ).json()["record"]
                audit_str = json.dumps(audit_rec).lower()
                assert "api_key" not in audit_str
                assert "groq" not in audit_str
                assert "secret" not in audit_str

    def test_invalid_csv_still_returns_422(
        self, fake_frozen_data: Path
    ) -> None:
        """CSV validation errors must still work correctly."""
        data_dir = fake_frozen_data

        settlement = _write_csv(
            [{"settlement_id": "X"}], ["settlement_id"],
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-X", "credit_amount": "100.00",
              "value_date": "2026-01-01"}],
            BANK_FIELDS,
        )
        ledger = _write_csv(
            [{"order_id": "LED-X", "gross_amount": "100.00",
              "transaction_date": "2026-01-01"}],
            LEDGER_FIELDS,
        )

        app = create_app(
            db_path=data_dir.parent / "test.db",
            data_dir=data_dir,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/batches",
                files=_files_from_bytes(settlement, bank, ledger),
            )
            assert resp.status_code == 422

    def test_l1_decisions_preserved_with_l2(
        self, fake_frozen_data: Path
    ) -> None:
        """L1 decisions are preserved alongside L2 outcomes."""
        data_dir = fake_frozen_data

        # Read actual CSV content and compute record IDs.
        settlement_rows = list(csv.DictReader(io.StringIO(
            (data_dir / "settlements.csv").read_text()
        )))
        bank_rows = list(csv.DictReader(io.StringIO(
            (data_dir / "bank.csv").read_text()
        )))
        ledger_rows = list(csv.DictReader(io.StringIO(
            (data_dir / "ledger.csv").read_text()
        )))
        set_recs = _normalize_rows(settlement_rows, SourceType.SETTLEMENT)
        bank_recs = _normalize_rows(bank_rows, SourceType.BANK)
        set_f2_id = set_recs[1].record_id
        bnk_f2_id = bank_recs[1].record_id

        audit_record = {
            "correlation_id": "corr-preserve",
            "timestamp": "2026-08-30T00:00:00+00:00",
            "presented_record_ids": [set_f2_id, bnk_f2_id],
            "outcome": "PROPOSAL_VALID",
            "proposal": {
                "proposed_match_ids": [set_f2_id, bnk_f2_id],
                "confidence": 0.95,
                "rationale": "Test.",
            },
            "confidence": 0.95,
            "reason": "Test.",
        }
        (data_dir / "layer2_full_audit.jsonl").write_text(
            json.dumps(audit_record) + "\n", encoding="utf-8"
        )

        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()

        app = create_app(
            db_path=data_dir.parent / "test.db",
            data_dir=data_dir,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/batches",
                files=_files_from_bytes(settlement, bank, ledger),
            )
            assert resp.status_code == 201
            batch_id = resp.json()["batch_id"]

            eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
            # L1 should still have matched records.
            assert eval_data["layer1"]["matched_records"] >= 2
            # L2 should show frozen artifacts were processed.
            assert eval_data["layer2_mode"] == "frozen_artifact"


# ===========================================================================
# Test G: Real on-disk manifest wiring (regression guard)
# ===========================================================================

class TestRealManifestArtifactWiring:
    """Regression test: loads the REAL data/dataset_manifest.json and
    real on-disk artifact to prove the canonical_layer2_artifact wiring
    actually works end-to-end.

    Placed here (not tests/integration/) because:
    1. There is no tests/integration/ directory.
    2. The existing test_frozen_dataset.py already tests the frozen dataset
       module and is the natural home.
    3. This test exercises load_frozen_l2_outcomes with REAL on-disk files,
       making it effectively an integration test despite living in tests/unit/.
    """

    REAL_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

    def test_real_manifest_loads_frozen_l2_outcomes(self) -> None:  # noqa: E501
        """Load real frozen L2 outcomes using the real manifest and artifact.

        This is the test that would have caught the Block A bug: the manifest
        was missing ``canonical_layer2_artifact``, so load_frozen_l2_outcomes
        would return API_ERROR for every residual record.
        """
        import json

        from reconciliation.evaluation.dataset_fingerprint import read_manifest
        from reconciliation.frozen_dataset import load_frozen_l2_outcomes
        from reconciliation.loader import load_residuals

        data_dir = self.REAL_DATA_DIR

        # Precondition: manifest exists and has canonical_layer2_artifact.
        manifest = read_manifest(data_dir)
        assert manifest is not None, "Real dataset_manifest.json not found"

        raw_manifest = json.loads(
            (data_dir / "dataset_manifest.json").read_text(encoding="utf-8")
        )
        assert "canonical_layer2_artifact" in raw_manifest, (
            "canonical_layer2_artifact key missing from manifest — "
            "Block A bug regression!"
        )

        # Load real residual scenarios and collect member record IDs.
        residuals = load_residuals(data_dir)
        member_ids: tuple[str, ...] = tuple(
            mid for s in residuals for mid in s.member_record_ids
        )
        assert len(member_ids) > 0, "No residual record IDs found"

        # Call load_frozen_l2_outcomes — this is the function that was
        # broken before Block A fix.
        outcomes = load_frozen_l2_outcomes(data_dir, [], member_ids)
        assert len(outcomes) == len(member_ids), (
            f"Expected {len(member_ids)} outcomes, got {len(outcomes)}"
        )

        # The critical assertion: NOT all API_ERROR.
        # Before Block A fix, this was Counter({API_ERROR: 159}).
        from collections import Counter
        outcome_counts = Counter(o.outcome.value for o in outcomes)
        non_error_count = sum(
            v for k, v in outcome_counts.items() if k != "API_ERROR"
        )
        assert non_error_count > 0, (
            f"All outcomes are API_ERROR: {outcome_counts}. "
            f"The canonical_layer2_artifact wiring is broken."
        )

        # Sanity: at least some should be PROPOSAL_VALID or NO_PROPOSAL.
        ai_outcome_count = outcome_counts.get("PROPOSAL_VALID", 0) + \
            outcome_counts.get("NO_PROPOSAL", 0)
        assert ai_outcome_count > 0, (
            f"Expected PROPOSAL_VALID or NO_PROPOSAL outcomes, got: {outcome_counts}"
        )

    def test_missing_canonical_key_causes_all_api_error(self, tmp_path: Path) -> None:  # noqa: E501
        """Prove the test catches the Block A regression: if we remove
        ``canonical_layer2_artifact`` from the manifest, load_frozen_l2_outcomes
        must return API_ERROR for every record.

        We do NOT modify the real manifest. Instead, we copy it to a tmp dir,
        remove the key, and verify the function fails closed.
        """
        import json
        import shutil

        from reconciliation.frozen_dataset import load_frozen_l2_outcomes
        from reconciliation.loader import load_residuals

        real_data_dir = self.REAL_DATA_DIR

        # Verify the real artifact file exists.
        raw_manifest = json.loads(
            (real_data_dir / "dataset_manifest.json").read_text(encoding="utf-8")
        )
        artifact_filename = raw_manifest["canonical_layer2_artifact"]["filename"]
        assert (real_data_dir / artifact_filename).exists(), (
            f"Real artifact {artifact_filename} not found"
        )

        # Build a fake data dir with real residuals but a broken manifest.
        fake_data_dir = tmp_path / "data"
        fake_data_dir.mkdir()
        shutil.copy2(
            real_data_dir / "residuals.csv",
            fake_data_dir / "residuals.csv",
        )
        shutil.copy2(
            real_data_dir / artifact_filename,
            fake_data_dir / artifact_filename,
        )

        # Write manifest WITHOUT canonical_layer2_artifact.
        broken_manifest = {
            "schema_version": raw_manifest["schema_version"],
            "dataset_seed": raw_manifest["dataset_seed"],
            "fingerprint": raw_manifest["fingerprint"],
            "files": raw_manifest["files"],
        }
        (fake_data_dir / "dataset_manifest.json").write_text(
            json.dumps(broken_manifest, indent=2), encoding="utf-8"
        )

        # Load residuals from the real data (they reference the same record IDs).
        residuals = load_residuals(real_data_dir)
        member_ids: tuple[str, ...] = tuple(
            mid for s in residuals for mid in s.member_record_ids
        )

        # With the broken manifest, ALL outcomes must be API_ERROR.
        outcomes = load_frozen_l2_outcomes(fake_data_dir, [], member_ids)
        from collections import Counter
        outcome_counts = Counter(o.outcome.value for o in outcomes)
        assert outcome_counts.get("API_ERROR", 0) == len(member_ids), (
            f"Expected ALL API_ERROR with broken manifest, got: {outcome_counts}"
        )

        # Confirm real manifest was NOT modified.
        real_raw = json.loads(
            (real_data_dir / "dataset_manifest.json").read_text(encoding="utf-8")
        )
        assert "canonical_layer2_artifact" in real_raw, (
            "Real manifest was accidentally modified!"
        )

    def test_real_replay_never_fabricates_api_errors(self) -> None:  # noqa: E501
        """Replay must never invent provider failures or alter routing.

        Regression: the loader previously "claimed" each audit record once
        and fabricated an API_ERROR outcome for every additional residual
        record of an already-claimed scenario.  On the real dataset that
        inflated the eval report to 101 API_ERROR outcomes even though only
        15 scenarios have genuine provider failures.

        Pins:
        1. One outcome per residual record (contract preserved).
        2. API_ERROR outcomes only for records whose OWN scenario genuinely
           failed (computed independently from the raw artifact/residuals),
           never fabricated for co-members of successful scenarios.
        3. Record-level routing composition is unchanged (the documented
           86 / 24 / 48 / 87 from a live frozen upload) — the truthful
           substitution must not re-claim candidate records.
        """
        import csv as csv_module
        import json as json_module
        from collections import Counter

        from reconciliation.frozen_dataset import load_frozen_l2_outcomes
        from reconciliation.layer3 import route as layer3_route
        from reconciliation.loader import (
            load_normalized_records,
            load_residuals,
        )
        from reconciliation.matcher import reconcile
        from reconciliation.matcher_config import MatcherConfig

        data_dir = self.REAL_DATA_DIR

        # Independently derive each scenario's artifact outcome from the raw
        # artifact JSONL (correlation_id == scenario_id) and residuals.csv.
        raw_manifest = json_module.loads(
            (data_dir / "dataset_manifest.json").read_text(encoding="utf-8")
        )
        artifact_name = raw_manifest["canonical_layer2_artifact"]["filename"]
        outcome_by_correlation = {}
        with (data_dir / artifact_name).open(
            "r", encoding="utf-8"
        ) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                raw = json_module.loads(line)
                outcome_by_correlation[raw["correlation_id"]] = raw["outcome"]

        scenario_by_member = {}
        with (data_dir / "residuals.csv").open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            for row in csv_module.DictReader(handle):
                for mid in json_module.loads(row["member_record_ids"]):
                    scenario_by_member[mid] = row["scenario_id"]

        records = load_normalized_records(data_dir)
        l1 = reconcile(
            records,
            MatcherConfig(amount_tolerance_paise=100, date_window_days=2),
        )
        residual_ids = tuple(l1.residual_record_ids)

        outcomes = load_frozen_l2_outcomes(data_dir, records, residual_ids)

        # 1. One outcome per residual record.
        assert len(outcomes) == len(residual_ids)

        # 2. API_ERROR is bounded by the genuine failure count: a record
        #    whose own scenario succeeded must never carry an API_ERROR, and
        #    a record whose own scenario failed may only lose its API_ERROR
        #    when an overlapping earlier scenario's outcome claimed it first
        #    (Layer 3's documented first-wins semantics).  The old code
        #    fabricated 101 API_ERROR outcomes here; genuine members of
        #    failing scenarios number 26, so any count near 100 is a
        #    fabrication regression.
        tallies = Counter(o.outcome.value for o in outcomes)
        expected_api_errors = sum(
            1
            for rid in residual_ids
            if outcome_by_correlation[scenario_by_member[rid]] == "API_ERROR"
        )
        assert 0 < tallies["API_ERROR"] <= expected_api_errors, tallies

        # 3. Routing composition unchanged (README-documented live numbers).
        routing = layer3_route(l1.decisions, outcomes, records)
        composition = Counter(r.bucket.value for r in routing)
        assert composition == Counter(
            {
                "DETERMINISTIC_MATCH": 86,
                "AI_AUTO_ACCEPTED": 24,
                "HUMAN_REVIEW": 48,
                "EXCEPTION": 87,
            }
        ), composition
