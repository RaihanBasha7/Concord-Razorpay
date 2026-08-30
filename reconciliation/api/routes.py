"""
FastAPI route handlers for the Concord reconciliation API.

The routes are a thin orchestration layer: they validate inputs, call
run_batch_pipeline, and persist results via BatchStore.  No reconciliation
logic lives in route handlers.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from reconciliation.api.pipeline import (
    CSVValidationError,
    read_csv_content,
    run_batch_pipeline,
)
from reconciliation.api.store import BatchStore
from reconciliation.domain.models import SourceType
from reconciliation.layer3 import RoutingBucket

_VALID_BUCKETS = {b.value for b in RoutingBucket}

logger = logging.getLogger("concord.api")


def create_router(
    store: BatchStore,
    *,
    layer2_mode: str = "not_executed",
) -> APIRouter:
    """Create an APIRouter with all Concord endpoints bound to *store*.

    Parameters
    ----------
    layer2_mode : str
        Label recorded in the eval report so consumers can distinguish
        real AI processing from the default API path.  Currently always
        ``not_executed`` because the API does not execute Layer 2.
    """

    router = APIRouter()

    # ------------------------------------------------------------------
    # POST /batches
    # ------------------------------------------------------------------

    @router.post("", status_code=201)
    async def create_batch(
        settlement: UploadFile = File(..., description="Settlement CSV file"),
        bank: UploadFile = File(..., description="Bank CSV file"),
        ledger: UploadFile = File(..., description="Ledger CSV file"),
    ):
        """Accept three CSV inputs, run the reconciliation pipeline,
        persist results, and return a batch ID."""
        batch_id = store.create_batch()

        try:
            # Read raw bytes from uploads.
            settlement_content = await settlement.read()
            bank_content = await bank.read()
            ledger_content = await ledger.read()

            # Parse and validate CSV structure.
            settlement_rows = read_csv_content(
                settlement_content, SourceType.SETTLEMENT
            )
            bank_rows = read_csv_content(bank_content, SourceType.BANK)
            ledger_rows = read_csv_content(ledger_content, SourceType.LEDGER)

            # Run the full reconciliation pipeline.
            result = run_batch_pipeline(
                settlement_rows, bank_rows, ledger_rows,
                layer2_mode=layer2_mode,
            )

            # Persist all artifacts.
            store.store_routing_decisions(batch_id, result.enriched_routing)
            store.store_json(batch_id, "layer1", result.l1_decisions_json)
            store.store_json(batch_id, "layer2_outcomes", result.l2_outcomes_json)
            store.store_json(batch_id, "eval", result.eval_report)
            store.store_audit_records(batch_id, result.audit_records)
            store.complete_batch(batch_id, result.record_count)

            return {
                "batch_id": batch_id,
                "status": "completed",
                "record_count": result.record_count,
            }

        except CSVValidationError as exc:
            store.fail_batch(batch_id, str(exc))
            return JSONResponse(
                status_code=422,
                content={
                    "batch_id": batch_id,
                    "detail": str(exc),
                    "error_type": exc.error_type,
                    **exc.details,
                },
            )
        except Exception:
            logger.exception("Unexpected error processing batch %s", batch_id)
            store.fail_batch(batch_id, "Internal processing error")
            return JSONResponse(
                status_code=500,
                content={
                    "batch_id": batch_id,
                    "detail": "Internal processing error.",
                    "error_type": "internal_error",
                },
            )

    # ------------------------------------------------------------------
    # GET /batches/{batch_id}/status
    # ------------------------------------------------------------------

    @router.get("/{batch_id}/status")
    async def get_batch_status(batch_id: str):
        """Return the processing state and high-level metrics for a batch."""
        batch = store.get_batch(batch_id)
        if batch is None:
            raise HTTPException(
                status_code=404, detail=f"Batch {batch_id} not found."
            )

        composition: dict = {}
        l1_decision_count = 0
        layer2_mode: str | None = None
        if batch["status"] == "completed":
            composition = store.get_routing_composition(batch_id)
            l1 = store.get_json(batch_id, "layer1")
            l1_decision_count = len(l1) if l1 else 0
            eval_report = store.get_json(batch_id, "eval")
            if eval_report:
                layer2_mode = eval_report.get("layer2_mode")

        return {
            "batch_id": batch["id"],
            "status": batch["status"],
            "created_at": batch["created_at"],
            "completed_at": batch.get("completed_at"),
            "error_message": batch.get("error_message"),
            "record_count": batch["record_count"],
            "layer1_decision_count": l1_decision_count,
            "layer2_mode": layer2_mode,
            "routing_composition": composition,
        }

    # ------------------------------------------------------------------
    # GET /batches/{batch_id}/results
    # ------------------------------------------------------------------

    @router.get("/{batch_id}/results")
    async def get_batch_results(
        batch_id: str,
        bucket: Optional[str] = Query(
            None,
            description="Filter by routing bucket. Valid: DETERMINISTIC_MATCH, AI_AUTO_ACCEPTED, HUMAN_REVIEW, EXCEPTION",
        ),
    ):
        """Return routing results, optionally filtered by bucket."""
        batch = store.get_batch(batch_id)
        if batch is None:
            raise HTTPException(
                status_code=404, detail=f"Batch {batch_id} not found."
            )
        if batch["status"] != "completed":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Batch {batch_id} has status '{batch['status']}', "
                    "not 'completed'."
                ),
            )

        if bucket is not None and bucket not in _VALID_BUCKETS:
            valid = sorted(_VALID_BUCKETS)
            raise HTTPException(
                status_code=422,
                detail=f"Invalid bucket '{bucket}'. Valid values: {valid}",
            )

        decisions = store.get_routing_decisions(batch_id, bucket)
        return {
            "batch_id": batch_id,
            "bucket": bucket or "ALL",
            "count": len(decisions),
            "records": [
                {
                    "record_id": d["record_id"],
                    "source_type": d["source_type"],
                    "source_native_id": d["source_native_id"],
                    "order_id_hint": d["order_id_hint"],
                    "amount_paise": d["amount_paise"],
                    "date": d["record_date"],
                    "narration": d["narration"],
                    "bucket": d["bucket"],
                    "reason": d["reason"],
                    "confidence": d["confidence"],
                    "source_decision_id": d["source_decision_id"],
                    "source_outcome": d["source_outcome"],
                }
                for d in decisions
            ],
        }

    # ------------------------------------------------------------------
    # GET /batches/{batch_id}/eval
    # ------------------------------------------------------------------

    @router.get("/{batch_id}/eval")
    async def get_batch_eval(batch_id: str):
        """Return the evaluation report generated for this batch."""
        batch = store.get_batch(batch_id)
        if batch is None:
            raise HTTPException(
                status_code=404, detail=f"Batch {batch_id} not found."
            )
        if batch["status"] != "completed":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Batch {batch_id} has status '{batch['status']}', "
                    "not 'completed'."
                ),
            )

        eval_report = store.get_json(batch_id, "eval")
        if eval_report is None:
            raise HTTPException(
                status_code=404,
                detail=f"Evaluation report not found for batch {batch_id}.",
            )

        return {"batch_id": batch_id, "eval": eval_report}

    # ------------------------------------------------------------------
    # GET /batches/{batch_id}/records/{record_id}
    # ------------------------------------------------------------------

    @router.get("/{batch_id}/records/{record_id}")
    async def get_record_detail(batch_id: str, record_id: str):
        """Return the full decision context for one record, including
        source data, Layer 1/Layer 2 information, and audit trail."""
        batch = store.get_batch(batch_id)
        if batch is None:
            raise HTTPException(
                status_code=404, detail=f"Batch {batch_id} not found."
            )
        if batch["status"] != "completed":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Batch {batch_id} has status '{batch['status']}', "
                    "not 'completed'."
                ),
            )

        audit = store.get_audit_record(batch_id, record_id)
        if audit is None:
            raise HTTPException(
                status_code=404,
                detail=f"Record {record_id} not found in batch {batch_id}.",
            )

        return {"batch_id": batch_id, "record": audit}

    return router
