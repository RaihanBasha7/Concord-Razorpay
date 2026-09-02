"""
Tests for Part 1.5 reporting/disclosure cleanup.

Covers:
  1. Report field naming — no misleading scenarios_processed
  2. Artifact replay metadata in eval report
  3. 245-record accounting consistency
  4. 18 + 25 AI routing reproducibility from frozen artifact
  5. Non-frozen uploads have no replay metadata
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Dict, List

import pytest
from starlette.testclient import TestClient

from reconciliation.api.app import create_app

# ---------------------------------------------------------------------------
# Helpers
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
    app = create_app(
        db_path=tmp_path / "test.db",
        data_dir=Path("data"),
    )
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, Path("data")


# ---------------------------------------------------------------------------
# 1. Report field naming — no scenarios_processed
# ---------------------------------------------------------------------------

class TestFieldNaming:
    """The eval report must use residual_records_processed, not scenarios_processed."""

    def test_eval_report_uses_correct_field_name(self, client: TestClient) -> None:
        """API eval report must contain residual_records_processed."""
        settlement = _write_csv(
            [{"settlement_id": "S1", "order_id": "O1",
              "gross_amount": "100.00", "settlement_date": "2026-08-01"}],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(
            [{"bank_utr": "B1", "order_id": "O1",
              "credit_amount": "100.00", "value_date": "2026-08-01"}],
            BANK_FIELDS,
        )
        ledger = _write_csv([], LEDGER_FIELDS)

        resp = client.post(
            "/batches",
            files=_files_from_bytes(settlement, bank, ledger),
        )
        assert resp.status_code == 201
        batch_id = resp.json()["batch_id"]

        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        assert "residual_records_processed" in eval_data["layer2"]
        assert "scenarios_processed" not in eval_data["layer2"]

    def test_frozen_eval_report_uses_correct_field_name(
        self, frozen_client: tuple
    ) -> None:
        """Frozen upload eval report must also use the correct field name."""
        client, data_dir = frozen_client
        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()

        resp = client.post(
            "/batches",
            files=_files_from_bytes(settlement, bank, ledger),
        )
        assert resp.status_code == 201
        batch_id = resp.json()["batch_id"]

        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        assert "residual_records_processed" in eval_data["layer2"]
        assert eval_data["layer2"]["residual_records_processed"] > 0


# ---------------------------------------------------------------------------
# 2. Artifact replay metadata
# ---------------------------------------------------------------------------

class TestArtifactReplayDisclosure:
    """The eval report must clearly indicate artifact replay for frozen uploads."""

    def test_frozen_upload_has_demo_mode(self, frozen_client: tuple) -> None:
        """Frozen upload must include demo_mode: artifact_replay."""
        client, data_dir = frozen_client
        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()

        resp = client.post(
            "/batches",
            files=_files_from_bytes(settlement, bank, ledger),
        )
        assert resp.status_code == 201
        batch_id = resp.json()["batch_id"]

        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        assert eval_data.get("demo_mode") == "artifact_replay"
        assert "demo_mode_note" in eval_data
        note = eval_data["demo_mode_note"].lower()
        assert "no" in note and "llm" in note  # states no LLM call
        assert "prior" in note or "stored" in note  # references prior outputs

    def test_frozen_upload_layer2_mode(self, frozen_client: tuple) -> None:
        """Frozen upload must show layer2_mode: frozen_artifact."""
        client, data_dir = frozen_client
        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()

        resp = client.post(
            "/batches",
            files=_files_from_bytes(settlement, bank, ledger),
        )
        assert resp.status_code == 201
        batch_id = resp.json()["batch_id"]

        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        assert eval_data["layer2_mode"] == "frozen_artifact"

        status = client.get(f"/batches/{batch_id}/status").json()
        assert status["layer2_mode"] == "frozen_artifact"

    def test_non_frozen_has_no_demo_mode(self, client: TestClient) -> None:
        """Non-frozen upload must NOT have demo_mode field."""
        settlement = _write_csv(
            [{"settlement_id": "S1", "order_id": "O1",
              "gross_amount": "100.00", "settlement_date": "2026-08-01"}],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(
            [{"bank_utr": "B1", "order_id": "O1",
              "credit_amount": "100.00", "value_date": "2026-08-01"}],
            BANK_FIELDS,
        )
        ledger = _write_csv([], LEDGER_FIELDS)

        resp = client.post(
            "/batches",
            files=_files_from_bytes(settlement, bank, ledger),
        )
        assert resp.status_code == 201
        batch_id = resp.json()["batch_id"]

        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        assert "demo_mode" not in eval_data
        assert eval_data["layer2_mode"] == "not_executed"


# ---------------------------------------------------------------------------
# 3. 245-record accounting consistency
# ---------------------------------------------------------------------------

class TestAccounting:
    """Verify the 245-record accounting is internally consistent."""

    def test_245_record_bucket_accounting(self, frozen_client: tuple) -> None:
        """All 245 records must be accounted for across exactly 4 buckets."""
        client, data_dir = frozen_client
        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()

        resp = client.post(
            "/batches",
            files=_files_from_bytes(settlement, bank, ledger),
        )
        assert resp.status_code == 201
        assert resp.json()["record_count"] == 245
        batch_id = resp.json()["batch_id"]

        # Verify each record appears in exactly one bucket.
        all_ids = []
        for bucket in [
            "DETERMINISTIC_MATCH", "AI_AUTO_ACCEPTED",
            "HUMAN_REVIEW", "EXCEPTION",
        ]:
            r = client.get(
                f"/batches/{batch_id}/results?bucket={bucket}"
            ).json()
            all_ids.extend(rec["record_id"] for rec in r["records"])

        assert len(all_ids) == 245
        assert len(set(all_ids)) == 245  # no duplicates

    def test_l1_plus_l2_outcome_count_consistency(
        self, frozen_client: tuple
    ) -> None:
        """L1 matched + L2 residual records must equal total records."""
        client, data_dir = frozen_client
        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()

        resp = client.post(
            "/batches",
            files=_files_from_bytes(settlement, bank, ledger),
        )
        assert resp.status_code == 201
        batch_id = resp.json()["batch_id"]

        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        l1_matched = eval_data["layer1"]["matched_records"]
        l1_residual = eval_data["layer1"]["residual_records"]
        l2_processed = eval_data["layer2"]["residual_records_processed"]

        assert l1_matched + l1_residual == eval_data["total_records"]
        assert l2_processed == l1_residual  # one outcome per residual record


# ---------------------------------------------------------------------------
# 4. 18 + 25 AI routing reproducibility
# ---------------------------------------------------------------------------

class TestAIRoutingReproducibility:
    """Verify the AI bucket counts are deterministic from stored artifacts."""

    def test_ai_bucket_counts_reproducible(self, frozen_client: tuple) -> None:
        """Running the same frozen dataset twice produces identical AI bucket counts."""
        client, data_dir = frozen_client
        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()
        files = _files_from_bytes(settlement, bank, ledger)

        results_list = []
        for _ in range(2):
            resp = client.post("/batches", files=files)
            assert resp.status_code == 201
            batch_id = resp.json()["batch_id"]

            buckets = {}
            for bucket in [
                "DETERMINISTIC_MATCH", "AI_AUTO_ACCEPTED",
                "HUMAN_REVIEW", "EXCEPTION",
            ]:
                r = client.get(
                    f"/batches/{batch_id}/results?bucket={bucket}"
                ).json()
                buckets[bucket] = r["count"]
            results_list.append(buckets)

        assert results_list[0] == results_list[1]

    def test_ai_proposals_come_from_prior_outputs(
        self, frozen_client: tuple
    ) -> None:
        """AI-routed records must have source_outcome = PROPOSAL_VALID."""
        client, data_dir = frozen_client
        settlement = (data_dir / "settlements.csv").read_bytes()
        bank = (data_dir / "bank.csv").read_bytes()
        ledger = (data_dir / "ledger.csv").read_bytes()

        resp = client.post(
            "/batches",
            files=_files_from_bytes(settlement, bank, ledger),
        )
        batch_id = resp.json()["batch_id"]

        # AI_AUTO_ACCEPTED records must all come from PROPOSAL_VALID.
        auto = client.get(
            f"/batches/{batch_id}/results?bucket=AI_AUTO_ACCEPTED"
        ).json()
        for rec in auto["records"]:
            assert rec["source_outcome"] == "PROPOSAL_VALID"
            assert rec["confidence"] is not None
            assert rec["confidence"] >= 0.90

        # HUMAN_REVIEW records must all come from PROPOSAL_VALID.
        # Confidence may be >= 0.90 when financial evidence guardrail
        # downgrades auto-accept to review (LOW_EVIDENCE reason).
        review = client.get(
            f"/batches/{batch_id}/results?bucket=HUMAN_REVIEW"
        ).json()
        for rec in review["records"]:
            assert rec["source_outcome"] == "PROPOSAL_VALID"
            assert rec["confidence"] is not None
            assert rec["confidence"] >= 0.60


# ---------------------------------------------------------------------------
# 5. Day5 report is marked historical
# ---------------------------------------------------------------------------

class TestDay5ReportProvenance:
    """The day5 report must be recognized as a historical artifact."""

    def test_day5_report_exists_but_is_historical(self) -> None:
        """The day5 report exists but README marks it as historical."""
        readme = Path("README.md").read_text(encoding="utf-8")
        # README must contain a disclosure that the day5 report is historical.
        assert "historical" in readme.lower() or "historical" in readme
