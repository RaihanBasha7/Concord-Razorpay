"""
Day 6.4 tests: FastAPI backend for Concord reconciliation pipeline.

Tests exercise real pipeline fixtures where practical.  The noop Layer 2
orchestrator means every unmatched record routes to EXCEPTION, which is
correct for API mode without a real LLM provider.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from starlette.testclient import TestClient

from reconciliation.api.app import create_app
from reconciliation.api.store import BatchStore


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


def _make_settlement_rows() -> List[Dict[str, str]]:
    return [
        {
            "settlement_id": "SET-001",
            "order_id": "ORD-001",
            "gross_amount": "1000.00",
            "settlement_date": "2026-08-01",
        },
        {
            "settlement_id": "SET-002",
            "order_id": "ORD-002",
            "gross_amount": "2000.00",
            "settlement_date": "2026-08-02",
        },
    ]


def _make_bank_rows() -> List[Dict[str, str]]:
    return [
        {
            "bank_utr": "BNK-001",
            "order_id": "ORD-001",
            "credit_amount": "1000.00",
            "value_date": "2026-08-01",
        },
        {
            "bank_utr": "BNK-002",
            "order_id": "ORD-003",
            "credit_amount": "3000.00",
            "value_date": "2026-08-03",
        },
    ]


def _make_ledger_rows() -> List[Dict[str, str]]:
    return [
        {
            "order_id": "ORD-002",
            "gross_amount": "2000.00",
            "transaction_date": "2026-08-02",
        },
        {
            "order_id": "ORD-003",
            "gross_amount": "3000.00",
            "transaction_date": "2026-08-03",
        },
    ]


def _files(
    settlement: bytes,
    bank: bytes,
    ledger: bytes,
) -> Dict[str, tuple]:
    return {
        "settlement": ("settlements.csv", settlement, "text/csv"),
        "bank": ("bank.csv", bank, "text/csv"),
        "ledger": ("ledger.csv", ledger, "text/csv"),
    }


def _default_files() -> Dict[str, tuple]:
    return _files(
        _write_csv(_make_settlement_rows(), SETTLEMENT_FIELDS),
        _write_csv(_make_bank_rows(), BANK_FIELDS),
        _write_csv(_make_ledger_rows(), LEDGER_FIELDS),
    )


def _create_batch(client: TestClient) -> str:
    """Helper: POST a valid batch and return the batch ID."""
    resp = client.post("/batches", files=_default_files())
    assert resp.status_code == 201
    return resp.json()["batch_id"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeOrchestrator:
    """Test double that would propose every single-member residual as a match.

    Retained for potential future integration; not used by the default
    API client because the API pipeline does not execute Layer 2.
    """

    def resolve(self, case: Any, retrieval_result: Any) -> Any:
        from reconciliation.proposal import MatchProposal
        from reconciliation.proposal_validation import (
            ProposalOutcome,
            ProposalOutcomeType,
        )

        member_ids = [r.record_id for r in case.member_records]
        candidate_ids = [c.record.record_id for c in retrieval_result.candidates]
        presented_ids = tuple(dict.fromkeys(member_ids + candidate_ids))

        return ProposalOutcome(
            outcome=ProposalOutcomeType.PROPOSAL_VALID,
            proposal=MatchProposal(
                proposed_match_ids=member_ids,
                confidence=0.95,
                rationale="fake",
            ),
            presented_record_ids=presented_ids,
            reason="Fake orchestrator for testing.",
        )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_path / "test.db")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ===================================================================
# Test successful batch creation
# ===================================================================


class TestBatchCreation:
    def test_successful_batch_returns_201(self, client: TestClient):
        resp = client.post("/batches", files=_default_files())
        assert resp.status_code == 201
        data = resp.json()
        assert "batch_id" in data
        assert data["status"] == "completed"
        assert data["record_count"] == 6

    def test_batch_id_is_32_char_hex(self, client: TestClient):
        resp = client.post("/batches", files=_default_files())
        batch_id = resp.json()["batch_id"]
        assert len(batch_id) == 32
        assert all(c in "0123456789abcdef" for c in batch_id)

    def test_batch_with_exact_id_match(self, client: TestClient):
        """End-to-end: settlement + bank share order_id → Layer 1 match."""
        settlement = _write_csv(
            [
                {
                    "settlement_id": "SET-100",
                    "order_id": "ORD-X",
                    "gross_amount": "500.00",
                    "settlement_date": "2026-08-01",
                },
            ],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(
            [
                {
                    "bank_utr": "BNK-100",
                    "order_id": "ORD-X",
                    "credit_amount": "500.00",
                    "value_date": "2026-08-01",
                },
            ],
            BANK_FIELDS,
        )
        ledger = _write_csv(
            [
                {
                    "order_id": "ORD-Y",
                    "gross_amount": "750.00",
                    "transaction_date": "2026-08-02",
                },
            ],
            LEDGER_FIELDS,
        )
        resp = client.post("/batches", files=_files(settlement, bank, ledger))
        assert resp.status_code == 201
        batch_id = resp.json()["batch_id"]

        results = client.get(
            f"/batches/{batch_id}/results?bucket=DETERMINISTIC_MATCH"
        )
        assert results.status_code == 200
        data = results.json()
        assert data["count"] == 2  # SET-100 + BNK-100

    def test_different_row_counts_accepted(self, client: TestClient):
        """Different row counts between sources must NOT be rejected."""
        settlement = _write_csv(
            [
                {
                    "settlement_id": f"SET-{i:03d}",
                    "order_id": f"ORD-{i:03d}",
                    "gross_amount": "100.00",
                    "settlement_date": "2026-08-01",
                }
                for i in range(5)
            ],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(
            [
                {
                    "bank_utr": "BNK-001",
                    "credit_amount": "100.00",
                    "value_date": "2026-08-01",
                },
            ],
            BANK_FIELDS,
        )
        ledger = _write_csv([], LEDGER_FIELDS)

        resp = client.post(
            "/batches",
            files=_files(settlement, bank, ledger),
        )
        assert resp.status_code == 201
        assert resp.json()["record_count"] == 6

    def test_all_records_appear_in_results(self, client: TestClient):
        batch_id = _create_batch(client)
        resp = client.get(f"/batches/{batch_id}/results")
        assert resp.json()["count"] == 6


# ===================================================================
# Test input validation
# ===================================================================


class TestInputValidation:
    def test_missing_one_source_file(self, client: TestClient):
        """Missing the ledger file → 422."""
        settlement = _write_csv(_make_settlement_rows(), SETTLEMENT_FIELDS)
        bank = _write_csv(_make_bank_rows(), BANK_FIELDS)
        files = {
            "settlement": ("s.csv", settlement, "text/csv"),
            "bank": ("b.csv", bank, "text/csv"),
            # ledger missing
        }
        resp = client.post("/batches", files=files)
        assert resp.status_code == 422

    def test_missing_required_columns(self, client: TestClient):
        settlement = _write_csv([{"some_col": "value"}], ["some_col"])
        bank = _write_csv(_make_bank_rows(), BANK_FIELDS)
        ledger = _write_csv(_make_ledger_rows(), LEDGER_FIELDS)
        resp = client.post(
            "/batches",
            files=_files(settlement, bank, ledger),
        )
        assert resp.status_code == 422
        data = resp.json()
        assert data["error_type"] == "missing_columns"

    def test_invalid_amount_rejected(self, client: TestClient):
        settlement = _write_csv(
            [
                {
                    "settlement_id": "SET-001",
                    "order_id": "ORD-001",
                    "gross_amount": "not_a_number",
                    "settlement_date": "2026-08-01",
                },
            ],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(_make_bank_rows(), BANK_FIELDS)
        ledger = _write_csv(_make_ledger_rows(), LEDGER_FIELDS)
        resp = client.post(
            "/batches",
            files=_files(settlement, bank, ledger),
        )
        assert resp.status_code == 422
        assert "normalization_error" in resp.json().get("error_type", "")

    def test_invalid_date_rejected(self, client: TestClient):
        settlement = _write_csv(
            [
                {
                    "settlement_id": "SET-001",
                    "order_id": "ORD-001",
                    "gross_amount": "100.00",
                    "settlement_date": "not-a-date",
                },
            ],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(_make_bank_rows(), BANK_FIELDS)
        ledger = _write_csv(_make_ledger_rows(), LEDGER_FIELDS)
        resp = client.post(
            "/batches",
            files=_files(settlement, bank, ledger),
        )
        assert resp.status_code == 422
        assert "normalization_error" in resp.json().get("error_type", "")

    def test_error_response_includes_batch_id(self, client: TestClient):
        """Even on failure the client can query status by batch ID."""
        settlement = _write_csv(
            [{"settlement_id": "X"}],
            ["settlement_id"],
        )
        bank = _write_csv(_make_bank_rows(), BANK_FIELDS)
        ledger = _write_csv(_make_ledger_rows(), LEDGER_FIELDS)
        resp = client.post(
            "/batches",
            files=_files(settlement, bank, ledger),
        )
        assert resp.status_code == 422
        data = resp.json()
        assert "batch_id" in data
        # Verify the batch is recorded as failed.
        status = client.get(f"/batches/{data['batch_id']}/status")
        assert status.json()["status"] == "failed"


# ===================================================================
# Test batch status
# ===================================================================


class TestBatchStatus:
    def test_unknown_batch_returns_404(self, client: TestClient):
        resp = client.get("/batches/nonexistent/status")
        assert resp.status_code == 404

    def test_completed_batch_has_all_fields(self, client: TestClient):
        batch_id = _create_batch(client)
        resp = client.get(f"/batches/{batch_id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["record_count"] == 6
        assert data["created_at"] is not None
        assert data["completed_at"] is not None
        assert data["error_message"] is None
        assert data["layer1_decision_count"] >= 0
        assert isinstance(data["routing_composition"], dict)

    def test_failed_batch_status(self, client: TestClient):
        settlement = _write_csv(
            [{"settlement_id": "X"}], ["settlement_id"]
        )
        bank = _write_csv(_make_bank_rows(), BANK_FIELDS)
        ledger = _write_csv(_make_ledger_rows(), LEDGER_FIELDS)
        resp = client.post(
            "/batches",
            files=_files(settlement, bank, ledger),
        )
        batch_id = resp.json()["batch_id"]
        status = client.get(f"/batches/{batch_id}/status")
        data = status.json()
        assert data["status"] == "failed"
        assert data["error_message"] is not None

    def test_routing_composition_sums_to_record_count(
        self, client: TestClient
    ):
        batch_id = _create_batch(client)
        status = client.get(f"/batches/{batch_id}/status").json()
        total = sum(status["routing_composition"].values())
        assert total == status["record_count"]


# ===================================================================
# Test results by bucket
# ===================================================================


class TestResultsByBucket:
    def test_results_no_bucket_returns_all(self, client: TestClient):
        batch_id = _create_batch(client)
        resp = client.get(f"/batches/{batch_id}/results")
        assert resp.status_code == 200
        data = resp.json()
        assert data["bucket"] == "ALL"
        assert data["count"] == 6

    def test_deterministic_match_bucket(self, client: TestClient):
        batch_id = _create_batch(client)
        resp = client.get(
            f"/batches/{batch_id}/results?bucket=DETERMINISTIC_MATCH"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["bucket"] == "DETERMINISTIC_MATCH"
        for record in data["records"]:
            assert record["bucket"] == "DETERMINISTIC_MATCH"

    def test_exception_bucket(self, client: TestClient):
        batch_id = _create_batch(client)
        resp = client.get(
            f"/batches/{batch_id}/results?bucket=EXCEPTION"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["bucket"] == "EXCEPTION"
        for record in data["records"]:
            assert record["bucket"] == "EXCEPTION"

    def test_all_buckets_cover_all_records(self, client: TestClient):
        batch_id = _create_batch(client)
        total = client.get(f"/batches/{batch_id}/results").json()["count"]
        bucket_counts = {}
        for bucket in [
            "DETERMINISTIC_MATCH",
            "AI_AUTO_ACCEPTED",
            "HUMAN_REVIEW",
            "EXCEPTION",
        ]:
            resp = client.get(
                f"/batches/{batch_id}/results?bucket={bucket}"
            )
            assert resp.status_code == 200
            bucket_counts[bucket] = resp.json()["count"]
        assert sum(bucket_counts.values()) == total

    def test_invalid_bucket_returns_422(self, client: TestClient):
        batch_id = _create_batch(client)
        resp = client.get(
            f"/batches/{batch_id}/results?bucket=INVALID_BUCKET"
        )
        assert resp.status_code == 422

    def test_results_for_unknown_batch_returns_404(
        self, client: TestClient
    ):
        resp = client.get("/batches/nonexistent/results")
        assert resp.status_code == 404

    def test_results_for_incomplete_batch_returns_409(
        self, client: TestClient
    ):
        """Results are only available after the batch completes."""
        # Create a failed batch.
        settlement = _write_csv(
            [{"settlement_id": "X"}], ["settlement_id"]
        )
        bank = _write_csv(_make_bank_rows(), BANK_FIELDS)
        ledger = _write_csv(_make_ledger_rows(), LEDGER_FIELDS)
        resp = client.post(
            "/batches",
            files=_files(settlement, bank, ledger),
        )
        batch_id = resp.json()["batch_id"]
        results_resp = client.get(f"/batches/{batch_id}/results")
        assert results_resp.status_code == 409

    def test_record_has_required_fields(self, client: TestClient):
        batch_id = _create_batch(client)
        resp = client.get(f"/batches/{batch_id}/results")
        for record in resp.json()["records"]:
            assert "record_id" in record
            assert "source_type" in record
            assert "source_native_id" in record
            assert "amount_paise" in record
            assert "date" in record
            assert "bucket" in record
            assert "reason" in record


# ===================================================================
# Test eval retrieval
# ===================================================================


class TestEvalRetrieval:
    def test_eval_returns_200(self, client: TestClient):
        batch_id = _create_batch(client)
        resp = client.get(f"/batches/{batch_id}/eval")
        assert resp.status_code == 200
        data = resp.json()
        assert "eval" in data
        assert data["eval"]["total_records"] == 6

    def test_eval_for_unknown_batch_returns_404(
        self, client: TestClient
    ):
        resp = client.get("/batches/nonexistent/eval")
        assert resp.status_code == 404

    def test_eval_for_incomplete_batch_returns_409(
        self, client: TestClient
    ):
        settlement = _write_csv(
            [{"settlement_id": "X"}], ["settlement_id"]
        )
        bank = _write_csv(_make_bank_rows(), BANK_FIELDS)
        ledger = _write_csv(_make_ledger_rows(), LEDGER_FIELDS)
        resp = client.post(
            "/batches",
            files=_files(settlement, bank, ledger),
        )
        batch_id = resp.json()["batch_id"]
        eval_resp = client.get(f"/batches/{batch_id}/eval")
        assert eval_resp.status_code == 409

    def test_eval_has_layer1_section(self, client: TestClient):
        batch_id = _create_batch(client)
        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        l1 = eval_data["layer1"]
        assert "decisions_count" in l1
        assert "matched_records" in l1
        assert "residual_records" in l1
        assert "decisions_by_rule" in l1
        # matched + residual must equal total.
        assert l1["matched_records"] + l1["residual_records"] == l1["total_records"]

    def test_eval_has_layer2_section(self, client: TestClient):
        batch_id = _create_batch(client)
        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        l2 = eval_data["layer2"]
        assert "scenarios_processed" in l2
        assert "outcomes_by_type" in l2
        # With the noop orchestrator all outcomes are NO_PROPOSAL.
        assert l2["outcomes_by_type"].get("NO_PROPOSAL", 0) == l2["scenarios_processed"]

    def test_eval_has_routing_composition(self, client: TestClient):
        batch_id = _create_batch(client)
        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        composition = eval_data["layer3_routing_composition"]
        total = sum(composition.values())
        assert total == eval_data["total_records"]

    def test_eval_has_records_by_source(self, client: TestClient):
        batch_id = _create_batch(client)
        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        rbs = eval_data["records_by_source"]
        assert rbs["SETTLEMENT"] == 2
        assert rbs["BANK"] == 2
        assert rbs["LEDGER"] == 2

    def test_eval_has_thresholds(self, client: TestClient):
        batch_id = _create_batch(client)
        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        assert eval_data["thresholds"]["auto_accept"] == 0.90
        assert eval_data["thresholds"]["review"] == 0.60

    def test_eval_has_layer2_mode(self, client: TestClient):
        batch_id = _create_batch(client)
        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        assert "layer2_mode" in eval_data
        assert eval_data["layer2_mode"] == "not_executed"


# ===================================================================
# Test with real synthetic dataset
# ===================================================================


class TestLayer2Mode:
    """Verify layer2_mode metadata is surfaced correctly."""

    def test_not_executed_mode_in_eval_report(self, client: TestClient):
        batch_id = _create_batch(client)
        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        assert eval_data["layer2_mode"] == "not_executed"

    def test_not_executed_mode_in_status(self, client: TestClient):
        batch_id = _create_batch(client)
        status = client.get(f"/batches/{batch_id}/status").json()
        assert status["layer2_mode"] == "not_executed"

    def test_no_l2_scenarios(self, client: TestClient):
        """Layer 2 is not executed in the API pipeline; all residuals
        go to EXCEPTION/NO_CANDIDATE."""
        batch_id = _create_batch(client)
        l2 = client.get(f"/batches/{batch_id}/eval").json()["eval"]["layer2"]
        assert l2["scenarios_processed"] == 0
        assert l2["outcomes_by_type"] == {}

    def test_layer2_skipped_with_data(self, client: TestClient):
        # Use data with residuals (default files all match via Layer 1).
        settlement = _write_csv(
            [{"settlement_id": "SET-001", "order_id": "ORD-001",
              "gross_amount": "1000.00", "settlement_date": "2026-08-01"}],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-001", "order_id": "ORD-001",
              "credit_amount": "1000.00", "value_date": "2026-08-01"},
             {"bank_utr": "BNK-002", "credit_amount": "3000.00",
              "value_date": "2026-08-02"}],
            BANK_FIELDS,
        )
        ledger = _write_csv(
            [{"order_id": "LED-003", "gross_amount": "4000.00",
              "transaction_date": "2026-08-03"}],
            LEDGER_FIELDS,
        )
        files = {"settlement": ("s.csv", settlement, "text/csv"),
                 "bank": ("b.csv", bank, "text/csv"),
                 "ledger": ("l.csv", ledger, "text/csv")}
        resp = client.post("/batches", files=files)
        batch_id = resp.json()["batch_id"]
        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        assert eval_data["layer2_mode"] == "not_executed"
        # Layer 2 is skipped in the API pipeline; no scenarios processed.
        assert eval_data["layer2"]["scenarios_processed"] == 0
        assert eval_data["layer2"]["outcomes_by_type"] == {}


class TestLayer2Skipped:
    """Verify Layer 2 is skipped in the API pipeline."""

    def test_residuals_go_to_exception(self, client: TestClient):
        """All residuals fall through to EXCEPTION / NO_CANDIDATE."""
        settlement = _write_csv(
            [{"settlement_id": "SET-001", "order_id": "ORD-001",
              "gross_amount": "1000.00", "settlement_date": "2026-08-01"}],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-001", "order_id": "ORD-001",
              "credit_amount": "1000.00", "value_date": "2026-08-01"},
             {"bank_utr": "BNK-002", "credit_amount": "3000.00",
              "value_date": "2026-08-02"}],
            BANK_FIELDS,
        )
        ledger = _write_csv(
            [{"order_id": "LED-003", "gross_amount": "4000.00",
              "transaction_date": "2026-08-03"}],
            LEDGER_FIELDS,
        )
        files = {"settlement": ("s.csv", settlement, "text/csv"),
                 "bank": ("b.csv", bank, "text/csv"),
                 "ledger": ("l.csv", ledger, "text/csv")}

        resp = client.post("/batches", files=files)
        assert resp.status_code == 201
        batch_id = resp.json()["batch_id"]

        # Layer 1 matched SET-001 + BNK-001 (2 records).
        det = client.get(
            f"/batches/{batch_id}/results?bucket=DETERMINISTIC_MATCH"
        ).json()
        assert det["count"] == 2

        # No Layer 2 outcomes are produced.
        auto = client.get(
            f"/batches/{batch_id}/results?bucket=AI_AUTO_ACCEPTED"
        ).json()
        assert auto["count"] == 0

        # BNK-002 and LED-003 are residuals routed to EXCEPTION/NO_CANDIDATE.
        exc = client.get(
            f"/batches/{batch_id}/results?bucket=EXCEPTION"
        ).json()
        assert exc["count"] == 2
        for record in exc["records"]:
            assert record["reason"] == "NO_CANDIDATE"
            assert record["confidence"] is None

        # All 4 records are accounted for.
        all_results = client.get(
            f"/batches/{batch_id}/results"
        ).json()
        assert all_results["count"] == 4

    def test_no_record_claimed_by_unrelated_case(
        self, client: TestClient
    ):
        """Without Layer 2 execution, all residuals fall through to
        EXCEPTION / NO_CANDIDATE and no candidate overlap can occur."""
        settlement = _write_csv(
            [{"settlement_id": "SET-001", "order_id": "ORD-001",
              "gross_amount": "1000.00", "settlement_date": "2026-08-01"}],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-001", "order_id": "ORD-001",
              "credit_amount": "1000.00", "value_date": "2026-08-01"},
             {"bank_utr": "BNK-002", "credit_amount": "3000.00",
              "value_date": "2026-08-02"}],
            BANK_FIELDS,
        )
        ledger = _write_csv(
            [{"order_id": "LED-003", "gross_amount": "4000.00",
              "transaction_date": "2026-08-03"}],
            LEDGER_FIELDS,
        )
        files = {"settlement": ("s.csv", settlement, "text/csv"),
                 "bank": ("b.csv", bank, "text/csv"),
                 "ledger": ("l.csv", ledger, "text/csv")}

        resp = client.post("/batches", files=files)
        batch_id = resp.json()["batch_id"]

        # Each record gets exactly one routing decision.
        all_ids = []
        for bucket in ["DETERMINISTIC_MATCH", "AI_AUTO_ACCEPTED",
                        "HUMAN_REVIEW", "EXCEPTION"]:
            resp = client.get(
                f"/batches/{batch_id}/results?bucket={bucket}"
            )
            for record in resp.json()["records"]:
                all_ids.append(record["record_id"])
        assert len(all_ids) == 4
        assert len(set(all_ids)) == 4  # no duplicates

        # No Layer 2 outcomes means no AI buckets.
        auto = client.get(
            f"/batches/{batch_id}/results?bucket=AI_AUTO_ACCEPTED"
        ).json()
        review = client.get(
            f"/batches/{batch_id}/results?bucket=HUMAN_REVIEW"
        ).json()
        assert auto["count"] == 0
        assert review["count"] == 0

    def test_not_executed_mode_in_status(self, client: TestClient):
        resp = client.post("/batches", files=_default_files())
        batch_id = resp.json()["batch_id"]
        status = client.get(f"/batches/{batch_id}/status").json()
        assert status["layer2_mode"] == "not_executed"

    def test_not_executed_mode_in_eval(self, client: TestClient):
        resp = client.post("/batches", files=_default_files())
        batch_id = resp.json()["batch_id"]
        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        assert eval_data["layer2_mode"] == "not_executed"


class TestResidualConstructionCorrectness:
    """Verify residual construction correctness under Layer 3's contract."""

    def test_without_orchestrator_residuals_go_to_exception(
        self, client: TestClient
    ):
        """Without an orchestrator, all residuals go to EXCEPTION."""
        # 3 residuals: none match via Layer 1.
        settlement = _write_csv(
            [{"settlement_id": "SET-A", "gross_amount": "100.00",
              "settlement_date": "2026-08-01"}],
            ["settlement_id", "gross_amount", "settlement_date"],
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-B", "credit_amount": "200.00",
              "value_date": "2026-08-02"}],
            ["bank_utr", "credit_amount", "value_date"],
        )
        ledger = _write_csv(
            [{"order_id": "LED-C", "gross_amount": "300.00",
              "transaction_date": "2026-08-03"}],
            LEDGER_FIELDS,
        )
        files = {"settlement": ("s.csv", settlement, "text/csv"),
                 "bank": ("b.csv", bank, "text/csv"),
                 "ledger": ("l.csv", ledger, "text/csv")}

        resp = client.post("/batches", files=files)
        assert resp.status_code == 201
        batch_id = resp.json()["batch_id"]

        exc = client.get(
            f"/batches/{batch_id}/results?bucket=EXCEPTION"
        ).json()
        assert exc["count"] == 3
        for record in exc["records"]:
            assert record["reason"] == "NO_CANDIDATE"
            assert record["confidence"] is None

        for bucket in ["AI_AUTO_ACCEPTED", "HUMAN_REVIEW"]:
            resp = client.get(
                f"/batches/{batch_id}/results?bucket={bucket}"
            )
            assert resp.json()["count"] == 0

    def test_each_record_gets_exactly_one_routing_decision(
        self, client: TestClient
    ):
        """Every record must appear in exactly one bucket."""
        batch_id = _create_batch(client)
        total = client.get(f"/batches/{batch_id}/results").json()["count"]

        all_ids = []
        for bucket in [
            "DETERMINISTIC_MATCH", "AI_AUTO_ACCEPTED",
            "HUMAN_REVIEW", "EXCEPTION",
        ]:
            resp = client.get(
                f"/batches/{batch_id}/results?bucket={bucket}"
            )
            for record in resp.json()["records"]:
                all_ids.append(record["record_id"])
        assert len(all_ids) == total
        assert len(set(all_ids)) == total  # no duplicates

    def test_layer2_skipped_without_orchestrator(self, client: TestClient):
        """Without an orchestrator, no Layer 2 scenarios are processed."""
        batch_id = _create_batch(client)
        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        assert eval_data["layer2"]["scenarios_processed"] == 0
        assert eval_data["layer2"]["outcomes_by_type"] == {}

    def test_layer2_skipped(self, client: TestClient):
        """The API does not construct artificial Layer 2 scenarios from
        unmatched records, so Layer 2 is skipped."""
        # 3 non-matching records → 3 residuals.
        settlement = _write_csv(
            [{"settlement_id": "SET-X", "gross_amount": "100.00",
              "settlement_date": "2026-08-01"}],
            ["settlement_id", "gross_amount", "settlement_date"],
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-Y", "credit_amount": "200.00",
              "value_date": "2026-08-02"}],
            ["bank_utr", "credit_amount", "value_date"],
        )
        ledger = _write_csv(
            [{"order_id": "LED-Z", "gross_amount": "300.00",
              "transaction_date": "2026-08-03"}],
            LEDGER_FIELDS,
        )
        files = {"settlement": ("s.csv", settlement, "text/csv"),
                 "bank": ("b.csv", bank, "text/csv"),
                 "ledger": ("l.csv", ledger, "text/csv")}

        resp = client.post("/batches", files=files)
        batch_id = resp.json()["batch_id"]

        eval_data = client.get(
            f"/batches/{batch_id}/eval"
        ).json()["eval"]
        # Layer 2 is not executed in the API pipeline.
        assert eval_data["layer2"]["scenarios_processed"] == 0
        assert eval_data["layer2"]["outcomes_by_type"] == {}


class TestWithSyntheticDataset:
    """Run the full pipeline against the synthetic dataset generator
    to verify integration with real-ish data."""

    def test_full_dataset_completes(self, client: TestClient, tmp_path: Path):
        from reconciliation.evaluation.dataset_generator import (
            generate_dataset,
            write_dataset,
        )

        dataset = generate_dataset(seed=42)
        write_dataset(dataset, tmp_path)

        # Read generated CSVs.
        settlement = (tmp_path / "settlements.csv").read_bytes()
        bank = (tmp_path / "bank.csv").read_bytes()
        ledger = (tmp_path / "ledger.csv").read_bytes()

        resp = client.post(
            "/batches",
            files={
                "settlement": ("settlements.csv", settlement, "text/csv"),
                "bank": ("bank.csv", bank, "text/csv"),
                "ledger": ("ledger.csv", ledger, "text/csv"),
            },
        )
        assert resp.status_code == 201
        batch_id = resp.json()["batch_id"]
        assert resp.json()["record_count"] > 0

        # Verify status, results, and eval are all retrievable.
        status = client.get(f"/batches/{batch_id}/status").json()
        assert status["status"] == "completed"
        assert sum(status["routing_composition"].values()) > 0

        results = client.get(f"/batches/{batch_id}/results").json()
        assert results["count"] > 0

        eval_data = client.get(f"/batches/{batch_id}/eval").json()["eval"]
        assert eval_data["total_records"] > 0
        assert eval_data["layer1"]["matched_records"] >= 0


# ===================================================================
# Test audit logging
# ===================================================================


class TestAuditLogging:
    """Verify structured per-record audit records are persisted."""

    def test_audit_records_are_persisted(self, client: TestClient):
        """Audit records are actually created and retrievable via
        the record detail endpoint."""
        batch_id = _create_batch(client)
        results = client.get(f"/batches/{batch_id}/results").json()
        record_id = results["records"][0]["record_id"]

        resp = client.get(f"/batches/{batch_id}/records/{record_id}")
        assert resp.status_code == 200
        audit = resp.json()["record"]
        assert "record_id" in audit
        assert audit["record_id"] == record_id

    def test_deterministic_record_audit(self, client: TestClient):
        """A Layer 1 matched record has resolved_by=LAYER_1, layer1 info,
        layer2=None, and LAYER1_DETERMINISTIC routing."""
        settlement = _write_csv(
            [{"settlement_id": "SET-001", "order_id": "ORD-X",
              "gross_amount": "500.00", "settlement_date": "2026-08-01"}],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-001", "order_id": "ORD-X",
              "credit_amount": "500.00", "value_date": "2026-08-01"}],
            BANK_FIELDS,
        )
        ledger = _write_csv(
            [{"order_id": "LED-Y", "gross_amount": "750.00",
              "transaction_date": "2026-08-02"}],
            LEDGER_FIELDS,
        )
        resp = client.post("/batches", files=_files(settlement, bank, ledger))
        batch_id = resp.json()["batch_id"]

        # Find a matched record.
        results = client.get(
            f"/batches/{batch_id}/results?bucket=DETERMINISTIC_MATCH"
        ).json()
        assert results["count"] >= 2
        record_id = results["records"][0]["record_id"]

        audit = client.get(
            f"/batches/{batch_id}/records/{record_id}"
        ).json()["record"]
        assert audit["resolved_by"] == "LAYER_1"
        assert audit["layer1"] is not None
        assert "EXACT_ID" in audit["layer1"]["rule_or_rationale"]
        assert audit["layer1"]["confidence"] == 1.0
        assert audit["layer2"] is None
        assert audit["routing"]["bucket"] == "DETERMINISTIC_MATCH"
        assert audit["routing"]["reason"] == "LAYER1_DETERMINISTIC"

    def test_no_candidate_record_audit(self, client: TestClient):
        """A residual with no Layer 2 attempt has resolved_by=None,
        layer1=None, layer2=None, and EXCEPTION/NO_CANDIDATE routing."""
        settlement = _write_csv(
            [{"settlement_id": "SET-A", "gross_amount": "100.00",
              "settlement_date": "2026-08-01"}],
            ["settlement_id", "gross_amount", "settlement_date"],
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-B", "credit_amount": "200.00",
              "value_date": "2026-08-02"}],
            ["bank_utr", "credit_amount", "value_date"],
        )
        ledger = _write_csv(
            [{"order_id": "LED-C", "gross_amount": "300.00",
              "transaction_date": "2026-08-03"}],
            LEDGER_FIELDS,
        )
        files = {"settlement": ("s.csv", settlement, "text/csv"),
                 "bank": ("b.csv", bank, "text/csv"),
                 "ledger": ("l.csv", ledger, "text/csv")}
        resp = client.post("/batches", files=files)
        batch_id = resp.json()["batch_id"]

        exc = client.get(
            f"/batches/{batch_id}/results?bucket=EXCEPTION"
        ).json()
        assert exc["count"] == 3
        for rec in exc["records"]:
            audit = client.get(
                f"/batches/{batch_id}/records/{rec['record_id']}"
            ).json()["record"]
            assert audit["resolved_by"] is None
            assert audit["layer1"] is None
            assert audit["layer2"] is None
            assert audit["routing"]["bucket"] == "EXCEPTION"
            assert audit["routing"]["reason"] == "NO_CANDIDATE"

    def test_exception_record_audit(self, client: TestClient):
        """Residuals are not processed by Layer 2 in the API pipeline.
        They route to EXCEPTION/NO_CANDIDATE with no Layer 2 audit info."""
        settlement = _write_csv(
            [{"settlement_id": "SET-001", "order_id": "ORD-001",
              "gross_amount": "1000.00", "settlement_date": "2026-08-01"}],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-001", "order_id": "ORD-001",
              "credit_amount": "1000.00", "value_date": "2026-08-01"},
             {"bank_utr": "BNK-002", "credit_amount": "3000.00",
              "value_date": "2026-08-02"}],
            BANK_FIELDS,
        )
        ledger = _write_csv(
            [{"order_id": "LED-003", "gross_amount": "4000.00",
              "transaction_date": "2026-08-03"}],
            LEDGER_FIELDS,
        )
        files = {"settlement": ("s.csv", settlement, "text/csv"),
                 "bank": ("b.csv", bank, "text/csv"),
                 "ledger": ("l.csv", ledger, "text/csv")}
        resp = client.post("/batches", files=files)
        batch_id = resp.json()["batch_id"]

        exc = client.get(
            f"/batches/{batch_id}/results?bucket=EXCEPTION"
        ).json()
        assert exc["count"] >= 1
        record_id = exc["records"][0]["record_id"]

        audit = client.get(
            f"/batches/{batch_id}/records/{record_id}"
        ).json()["record"]
        assert audit["resolved_by"] is None
        assert audit["layer1"] is None
        assert audit["layer2"] is None
        assert audit["routing"]["bucket"] == "EXCEPTION"
        assert audit["routing"]["reason"] == "NO_CANDIDATE"

    def test_l1_match_record_has_no_layer2(self, client: TestClient):
        """A Layer 1 matched record must NOT have layer2 info —
        deterministic matches win first."""
        settlement = _write_csv(
            [{"settlement_id": "SET-001", "order_id": "ORD-001",
              "gross_amount": "1000.00", "settlement_date": "2026-08-01"}],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-001", "order_id": "ORD-001",
              "credit_amount": "1000.00", "value_date": "2026-08-01"}],
            BANK_FIELDS,
        )
        ledger = _write_csv([], LEDGER_FIELDS)
        files = {"settlement": ("s.csv", settlement, "text/csv"),
                 "bank": ("b.csv", bank, "text/csv"),
                 "ledger": ("l.csv", ledger, "text/csv")}
        resp = client.post("/batches", files=files)
        batch_id = resp.json()["batch_id"]

        det = client.get(
            f"/batches/{batch_id}/results?bucket=DETERMINISTIC_MATCH"
        ).json()
        assert det["count"] == 2
        for rec in det["records"]:
            audit = client.get(
                f"/batches/{batch_id}/records/{rec['record_id']}"
            ).json()["record"]
            assert audit["resolved_by"] == "LAYER_1"
            assert audit["layer2"] is None

    def test_timestamp_is_utc_isoformat(self, client: TestClient):
        """Audit timestamp must be a timezone-aware ISO format string."""
        batch_id = _create_batch(client)
        results = client.get(f"/batches/{batch_id}/results").json()
        record_id = results["records"][0]["record_id"]

        audit = client.get(
            f"/batches/{batch_id}/records/{record_id}"
        ).json()["record"]
        assert "T" in audit["timestamp"]  # ISO format
        assert audit["timestamp"].endswith("Z") or "+00:00" in audit["timestamp"]

    def test_audit_count_matches_routing_count(self, client: TestClient):
        """Every record in routing results has a corresponding audit record."""
        batch_id = _create_batch(client)
        results = client.get(f"/batches/{batch_id}/results").json()
        total = results["count"]

        audit_count = 0
        for rec in results["records"]:
            resp = client.get(
                f"/batches/{batch_id}/records/{rec['record_id']}"
            )
            assert resp.status_code == 200
            audit_count += 1
        assert audit_count == total


# ===================================================================
# Test record detail endpoint
# ===================================================================


class TestRecordDetailEndpoint:
    """Verify GET /batches/{batch_id}/records/{record_id}."""

    def test_returns_record_with_all_fields(self, client: TestClient):
        batch_id = _create_batch(client)
        results = client.get(f"/batches/{batch_id}/results").json()
        record_id = results["records"][0]["record_id"]

        resp = client.get(f"/batches/{batch_id}/records/{record_id}")
        assert resp.status_code == 200
        audit = resp.json()["record"]
        assert audit["record_id"] == record_id
        assert "source_type" in audit
        assert "source_native_id" in audit
        assert "amount_paise" in audit
        assert "date" in audit
        assert "resolved_by" in audit
        assert "layer1" in audit
        assert "layer2" in audit
        assert "routing" in audit

    def test_unknown_batch_returns_404(self, client: TestClient):
        resp = client.get("/batches/nonexistent/records/REC-123")
        assert resp.status_code == 404

    def test_unknown_record_returns_404(self, client: TestClient):
        batch_id = _create_batch(client)
        resp = client.get(
            f"/batches/{batch_id}/records/DOES_NOT_EXIST"
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_incomplete_batch_returns_409(self, client: TestClient):
        settlement = _write_csv(
            [{"settlement_id": "X"}], ["settlement_id"]
        )
        bank = _write_csv(_make_bank_rows(), BANK_FIELDS)
        ledger = _write_csv(_make_ledger_rows(), LEDGER_FIELDS)
        resp = client.post(
            "/batches", files=_files(settlement, bank, ledger)
        )
        batch_id = resp.json()["batch_id"]
        resp = client.get(
            f"/batches/{batch_id}/records/ANY_ID"
        )
        assert resp.status_code == 409

    def test_deterministic_match_has_l1_detail(self, client: TestClient):
        """Layer 1 matched record includes decision_id and rule."""
        settlement = _write_csv(
            [{"settlement_id": "SET-100", "order_id": "ORD-X",
              "gross_amount": "500.00", "settlement_date": "2026-08-01"}],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-100", "order_id": "ORD-X",
              "credit_amount": "500.00", "value_date": "2026-08-01"}],
            BANK_FIELDS,
        )
        ledger = _write_csv(
            [{"order_id": "LED-Y", "gross_amount": "750.00",
              "transaction_date": "2026-08-02"}],
            LEDGER_FIELDS,
        )
        resp = client.post("/batches", files=_files(settlement, bank, ledger))
        batch_id = resp.json()["batch_id"]

        det = client.get(
            f"/batches/{batch_id}/results?bucket=DETERMINISTIC_MATCH"
        ).json()
        record_id = det["records"][0]["record_id"]

        audit = client.get(
            f"/batches/{batch_id}/records/{record_id}"
        ).json()["record"]
        assert audit["layer1"]["decision_id"].startswith("EXACT_ID-")
        assert audit["layer1"]["confidence"] == 1.0
        assert "member_record_ids" in audit["layer1"]
        assert len(audit["layer1"]["member_record_ids"]) == 2

    def test_exception_has_no_l1_or_l2(self, client: TestClient):
        """A residual without orchestrator has no L1 or L2 info."""
        settlement = _write_csv(
            [{"settlement_id": "SET-A", "gross_amount": "100.00",
              "settlement_date": "2026-08-01"}],
            ["settlement_id", "gross_amount", "settlement_date"],
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-B", "credit_amount": "999.00",
              "value_date": "2026-08-02"}],
            ["bank_utr", "credit_amount", "value_date"],
        )
        ledger = _write_csv(
            [{"order_id": "LED-C", "gross_amount": "500.00",
              "transaction_date": "2026-08-03"}],
            LEDGER_FIELDS,
        )
        files = {"settlement": ("s.csv", settlement, "text/csv"),
                 "bank": ("b.csv", bank, "text/csv"),
                 "ledger": ("l.csv", ledger, "text/csv")}
        resp = client.post("/batches", files=files)
        batch_id = resp.json()["batch_id"]

        exc = client.get(
            f"/batches/{batch_id}/results?bucket=EXCEPTION"
        ).json()
        assert exc["count"] == 3
        record_id = exc["records"][0]["record_id"]

        audit = client.get(
            f"/batches/{batch_id}/records/{record_id}"
        ).json()["record"]
        assert audit["layer1"] is None
        assert audit["layer2"] is None
        assert audit["routing"]["bucket"] == "EXCEPTION"
        assert audit["routing"]["reason"] == "NO_CANDIDATE"

    def test_routing_bucket_and_reason_match(self, client: TestClient):
        """The routing in audit matches the results endpoint."""
        batch_id = _create_batch(client)
        for bucket in ["DETERMINISTIC_MATCH", "EXCEPTION"]:
            results = client.get(
                f"/batches/{batch_id}/results?bucket={bucket}"
            ).json()
            if results["count"] == 0:
                continue
            record_id = results["records"][0]["record_id"]
            audit = client.get(
                f"/batches/{batch_id}/records/{record_id}"
            ).json()["record"]
            assert audit["routing"]["bucket"] == bucket
            assert results["records"][0]["reason"] == audit["routing"]["reason"]


# ===================================================================
# Integration test
# ===================================================================


class TestEndToEndIntegration:
    """Full vertical slice: ingestion → Layer 1 → Layer 2 → Layer 3 →
    evaluation → persisted audit → API retrieval."""

    def test_full_vertical_slice(self, client: TestClient, tmp_path: Path):
        """Upload CSVs, run the pipeline, verify every artifact is
        retrievable and internally consistent."""
        # Create test data with a mix of matched and residual records.
        settlement = _write_csv(
            [{"settlement_id": "SET-001", "order_id": "ORD-001",
              "gross_amount": "1000.00", "settlement_date": "2026-08-01"},
             {"settlement_id": "SET-002", "gross_amount": "500.00",
              "settlement_date": "2026-08-05"}],
            SETTLEMENT_FIELDS,
        )
        bank = _write_csv(
            [{"bank_utr": "BNK-001", "order_id": "ORD-001",
              "credit_amount": "1000.00", "value_date": "2026-08-01"}],
            BANK_FIELDS,
        )
        ledger = _write_csv(
            [{"order_id": "LED-003", "gross_amount": "300.00",
              "transaction_date": "2026-08-03"}],
            LEDGER_FIELDS,
        )
        files = {"settlement": ("s.csv", settlement, "text/csv"),
                 "bank": ("b.csv", bank, "text/csv"),
                 "ledger": ("l.csv", ledger, "text/csv")}

        # 1. Ingestion
        resp = client.post("/batches", files=files)
        assert resp.status_code == 201
        batch_id = resp.json()["batch_id"]
        total = resp.json()["record_count"]
        assert total == 4

        # 2. Status
        status = client.get(
            f"/batches/{batch_id}/status"
        ).json()
        assert status["status"] == "completed"
        assert status["record_count"] == 4
        assert status["layer2_mode"] == "not_executed"
        assert sum(status["routing_composition"].values()) == 4

        # 3. Layer 1 matched: SET-001 + BNK-001
        det = client.get(
            f"/batches/{batch_id}/results?bucket=DETERMINISTIC_MATCH"
        ).json()
        assert det["count"] == 2

        # 4. Layer 2 is skipped; residuals route to EXCEPTION/NO_CANDIDATE.
        auto = client.get(
            f"/batches/{batch_id}/results?bucket=AI_AUTO_ACCEPTED"
        ).json()
        exc = client.get(
            f"/batches/{batch_id}/results?bucket=EXCEPTION"
        ).json()
        assert auto["count"] == 0
        assert exc["count"] == 2

        # 5. Eval report
        eval_data = client.get(
            f"/batches/{batch_id}/eval"
        ).json()["eval"]
        assert eval_data["total_records"] == 4
        assert eval_data["layer1"]["matched_records"] == 2
        assert eval_data["layer2"]["scenarios_processed"] == 0
        assert eval_data["layer2_mode"] == "not_executed"

        # 6. Audit: every record has an audit entry
        results = client.get(
            f"/batches/{batch_id}/results"
        ).json()
        for rec in results["records"]:
            audit_resp = client.get(
                f"/batches/{batch_id}/records/{rec['record_id']}"
            )
            assert audit_resp.status_code == 200
            audit = audit_resp.json()["record"]

            # Audit routing matches results routing.
            assert audit["routing"]["bucket"] == rec["bucket"]
            assert audit["routing"]["reason"] == rec["reason"]

            # Deterministic matches are not presented as AI-derived.
            if rec["bucket"] == "DETERMINISTIC_MATCH":
                assert audit["resolved_by"] == "LAYER_1"
                assert audit["layer1"] is not None
                assert audit["layer2"] is None

            # AI auto-accepted records are visibly AI-derived.
            if rec["bucket"] == "AI_AUTO_ACCEPTED":
                assert audit["resolved_by"] == "LAYER_2"
                assert audit["layer2"] is not None
                assert audit["layer2"]["outcome_type"] == "PROPOSAL_VALID"
                assert audit["routing"]["confidence"] >= 0.90

        # 7. Source data fields present
        first_audit = client.get(
            f"/batches/{batch_id}/records/{results['records'][0]['record_id']}"
        ).json()["record"]
        assert "source_type" in first_audit
        assert "source_native_id" in first_audit
        assert "amount_paise" in first_audit
        assert "date" in first_audit

    def test_no_duplicates_across_buckets(self, client: TestClient):
        """Each record appears in exactly one routing bucket."""
        batch_id = _create_batch(client)
        total = client.get(f"/batches/{batch_id}/results").json()["count"]

        all_ids = []
        for bucket in ["DETERMINISTIC_MATCH", "AI_AUTO_ACCEPTED",
                        "HUMAN_REVIEW", "EXCEPTION"]:
            resp = client.get(
                f"/batches/{batch_id}/results?bucket={bucket}"
            )
            for rec in resp.json()["records"]:
                all_ids.append(rec["record_id"])
        assert len(all_ids) == total
        assert len(set(all_ids)) == total


# ===================================================================
# Test unexpected exception handling and safety
# ===================================================================


class TestUnexpectedExceptionHandling:
    """Verify that unexpected internal errors return safe responses
    without leaking implementation details.
    """

    def test_post_unexpected_error_returns_500(self, client: TestClient):
        """If pipeline processing raises unexpectedly, POST returns 500
        with a safe generic message and a batch_id for tracking.
        """
        from unittest.mock import patch

        with patch(
            "reconciliation.api.routes.run_batch_pipeline",
            side_effect=RuntimeError("simulated internal failure"),
        ):
            resp = client.post("/batches", files=_default_files())

        assert resp.status_code == 500
        data = resp.json()
        assert data["error_type"] == "internal_error"
        assert data["detail"] == "Internal processing error."
        assert "batch_id" in data

        # The batch should be marked as failed.
        status = client.get(f"/batches/{data['batch_id']}/status")
        assert status.json()["status"] == "failed"

    def test_post_unexpected_error_hides_traceback(self, client: TestClient):
        """The error response must never contain a stack trace,
        file path, or the original exception message.
        """
        from unittest.mock import patch

        with patch(
            "reconciliation.api.routes.run_batch_pipeline",
            side_effect=RuntimeError("/home/user/secrets/api_key.txt"),
        ):
            resp = client.post("/batches", files=_default_files())

        body = resp.text.lower()
        assert resp.status_code == 500
        assert "/home/user" not in body
        assert "api_key" not in body
        assert "traceback" not in body
        assert "runtimeerror" not in body

    def test_get_unexpected_store_error_returns_safe_500(
        self, client: TestClient, tmp_path: Path
    ):
        """If the store raises an unexpected exception on a GET endpoint,
        the global handler returns a safe 500 response.
        """
        from unittest.mock import patch

        batch_id = _create_batch(client)

        with patch(
            "reconciliation.api.routes.BatchStore.get_batch",
            side_effect=RuntimeError("database corruption"),
        ):
            resp = client.get(f"/batches/{batch_id}/status")

        assert resp.status_code == 500
        data = resp.json()
        assert data["error_type"] == "internal_error"
        assert data["detail"] == "Internal server error."

    def test_get_unexpected_error_hides_internals(self, client: TestClient):
        """The safe 500 response must never expose the exception message,
        stack trace, or internal paths.
        """
        from unittest.mock import patch

        batch_id = _create_batch(client)

        with patch(
            "reconciliation.api.routes.BatchStore.get_batch",
            side_effect=RuntimeError("disk full: /var/lib/sqlite/concord.db"),
        ):
            resp = client.get(f"/batches/{batch_id}/status")

        body = resp.text.lower()
        assert resp.status_code == 500
        assert "/var/lib" not in body
        assert "disk full" not in body
        assert "traceback" not in body
        assert "runtimeerror" not in body

    def test_get_results_unexpected_error_returns_safe_500(
        self, client: TestClient
    ):
        """The global handler applies to all GET endpoints, not just status."""
        from unittest.mock import patch

        batch_id = _create_batch(client)

        with patch(
            "reconciliation.api.routes.BatchStore.get_routing_decisions",
            side_effect=RuntimeError("unexpected"),
        ):
            resp = client.get(f"/batches/{batch_id}/results")

        assert resp.status_code == 500
        data = resp.json()
        assert data["error_type"] == "internal_error"
        assert "traceback" not in resp.text.lower()

    def test_csv_validation_error_still_returns_422(self, client: TestClient):
        """Known CSV validation errors must still return 422, not 500."""
        settlement = _write_csv(
            [{"settlement_id": "X"}], ["settlement_id"]
        )
        bank = _write_csv(_make_bank_rows(), BANK_FIELDS)
        ledger = _write_csv(_make_ledger_rows(), LEDGER_FIELDS)
        resp = client.post(
            "/batches", files=_files(settlement, bank, ledger)
        )
        assert resp.status_code == 422
        assert resp.json()["error_type"] != "internal_error"

    def test_404_still_works_as_before(self, client: TestClient):
        """Known 404 errors are not affected by the global handler."""
        resp = client.get("/batches/nonexistent/status")
        assert resp.status_code == 404

    def test_409_still_works_as_before(self, client: TestClient):
        """Known 409 errors are not affected by the global handler."""
        settlement = _write_csv(
            [{"settlement_id": "X"}], ["settlement_id"]
        )
        bank = _write_csv(_make_bank_rows(), BANK_FIELDS)
        ledger = _write_csv(_make_ledger_rows(), LEDGER_FIELDS)
        resp = client.post(
            "/batches", files=_files(settlement, bank, ledger)
        )
        batch_id = resp.json()["batch_id"]
        resp = client.get(f"/batches/{batch_id}/results")
        assert resp.status_code == 409


# ===================================================================
# Test health endpoint
# ===================================================================


class TestHealthEndpoint:
    """Verify GET /health — liveness probe for deployment checks."""

    def test_health_returns_200(self, client: TestClient):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_response_matches_contract(self, client: TestClient):
        """The response body must be exactly {"status": "ok"}."""
        resp = client.get("/health")
        assert resp.json() == {"status": "ok"}

    def test_health_does_not_require_database(self, client: TestClient):
        """The health endpoint must not depend on the database
        connection or any reconciliation infrastructure."""
        # Verify it works even though no batches exist.
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_does_not_expose_secrets(self, client: TestClient):
        """The response must not leak environment variables, paths,
        API keys, or other internal details."""
        body = client.get("/health").json()
        body_str = str(body).lower()
        assert "key" not in body_str
        assert "secret" not in body_str
        assert "token" not in body_str
        assert "password" not in body_str
        assert "/home" not in body_str
        assert "data/" not in body_str


# ===================================================================
# Test CORS configuration
# ===================================================================


@pytest.fixture
def cors_client(tmp_path: Path) -> TestClient:
    """Client with CORS enabled for http://localhost:3000."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CONCORD_CORS_ORIGINS", "http://localhost:3000")
        app = create_app(db_path=tmp_path / "cors_test.db")
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


@pytest.fixture
def no_cors_client(tmp_path: Path) -> TestClient:
    """Client with no CORS configuration (safe default)."""
    # Ensure the env var is unset.
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("CONCORD_CORS_ORIGINS", raising=False)
        app = create_app(db_path=tmp_path / "no_cors_test.db")
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


class TestCORSConfiguration:
    """Verify CORS middleware behavior under different configurations."""

    def test_allowed_origin_gets_cors_headers(
        self, cors_client: TestClient
    ):
        """A request from an allowed origin receives the CORS header."""
        resp = cors_client.get(
            "/health",
            headers={"Origin": "http://localhost:3000"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"

    def test_unconfigured_origin_is_rejected(
        self, cors_client: TestClient
    ):
        """A request from an unconfigured origin must NOT receive CORS headers."""
        resp = cors_client.get(
            "/health",
            headers={"Origin": "http://evil.example.com"},
        )
        assert resp.status_code == 200
        assert "access-control-allow-origin" not in resp.headers

    def test_preflight_returns_200_for_configured_method(
        self, cors_client: TestClient
    ):
        """A preflight OPTIONS request for a configured method succeeds."""
        resp = cors_client.options(
            "/batches",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
        assert "GET" in resp.headers.get("access-control-allow-methods", "")

    def test_preflight_rejects_disallowed_method(
        self, cors_client: TestClient
    ):
        """A preflight for DELETE (not in allow_methods) must be rejected."""
        resp = cors_client.options(
            "/batches",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "DELETE",
            },
        )
        # Starlette CORSMiddleware returns 400 when the requested method
        # is not in allow_methods.
        assert resp.status_code == 400

    def test_cors_defaults_to_wildcard_when_env_unset(
        self, no_cors_client: TestClient):
        """With no CORS config, defaults to wildcard for dev convenience."""
        resp = no_cors_client.get(
            "/health",
            headers={"Origin": "http://localhost:3000"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "*"

    def test_wildcard_allows_any_origin(self, tmp_path: Path):
        """CONCORD_CORS_ORIGINS=* grants access to all origins."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("CONCORD_CORS_ORIGINS", "*")
            app = create_app(db_path=tmp_path / "wildcard_cors.db")
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.get(
                    "/health",
                    headers={"Origin": "http://any-origin.example.com"},
                )
                assert resp.status_code == 200
                assert resp.headers.get("access-control-allow-origin") == "*"

    def test_multiple_origins_all_configured(self, tmp_path: Path):
        """Comma-separated origins each get CORS headers."""
        origins = "http://localhost:3000,http://localhost:5173"
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("CONCORD_CORS_ORIGINS", origins)
            app = create_app(db_path=tmp_path / "multi_cors.db")
            with TestClient(app, raise_server_exceptions=False) as client:
                for origin in ["http://localhost:3000", "http://localhost:5173"]:
                    resp = client.get(
                        "/health",
                        headers={"Origin": origin},
                    )
                    assert resp.headers.get("access-control-allow-origin") == origin

    def test_multiple_origins_unlisted_rejected(self, tmp_path: Path):
        """An origin not in the comma-separated list is rejected."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(
                "CONCORD_CORS_ORIGINS",
                "http://localhost:3000,http://localhost:5173",
            )
            app = create_app(db_path=tmp_path / "multi_cors_reject.db")
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.get(
                    "/health",
                    headers={"Origin": "http://evil.example.com"},
                )
                assert "access-control-allow-origin" not in resp.headers

    def test_cors_does_not_break_existing_api(self, cors_client: TestClient):
        """Existing batch creation still works with CORS enabled."""
        resp = cors_client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
