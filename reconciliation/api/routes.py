"""
FastAPI route handlers for the Concord reconciliation API.

The routes are a thin orchestration layer: they validate inputs, call
run_batch_pipeline, and persist results via BatchStore.  No reconciliation
logic lives in route handlers.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from reconciliation.api.pipeline import (
    CSVValidationError,
    compute_batch_fingerprint,
    read_csv_content,
    run_batch_pipeline,
)
from reconciliation.api.store import BatchStore
from reconciliation.domain.models import SourceType
from reconciliation.frozen_dataset import compute_upload_fingerprint
from reconciliation.layer3 import RoutingBucket

_VALID_BUCKETS = {b.value for b in RoutingBucket}

logger = logging.getLogger("concord.api")


def create_router(
    store: BatchStore,
    *,
    layer2_mode: str = "not_executed",
    data_dir: str | None = None,
) -> APIRouter:
    """Create an APIRouter with all Concord endpoints bound to *store*.

    Parameters
    ----------
    layer2_mode : str
        Label recorded in the eval report so consumers can distinguish
        real AI processing from the default API path.
    data_dir : str, optional
        Path to the data directory containing the frozen dataset manifest
        and Layer 2 audit artifacts.  When provided, the pipeline will
        check uploads against the frozen dataset fingerprint.
    """

    router = APIRouter()

    def _duplicate_response(batch: dict):
        """Response for an idempotent re-upload of an already-processed batch.

        Uses the same shape as a fresh success so the client contract is
        unchanged, plus ``duplicate``/``message`` so the UI can tell users
        the upload was a no-op pointing at the existing batch.
        """
        return JSONResponse(
            status_code=201,
            content={
                "batch_id": batch["id"],
                "status": "completed",
                "record_count": batch["record_count"],
                "duplicate": True,
                "message": (
                    "These files were already processed — returning the "
                    "existing batch."
                ),
            },
        )

    # ------------------------------------------------------------------
    # GET /batches/summary
    # ------------------------------------------------------------------

    @router.get("/summary")
    async def get_summary():
        """Aggregate statistics across all persisted, completed batches.

        Reconstructed from the database on every request, so dashboard
        totals survive refreshes and backend restarts.
        """
        return store.get_summary()

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
        persist results, and return a batch ID.

        Idempotency: the same three-file content (verified by a SHA-256
        fingerprint over the raw CSV bytes) maps to exactly one logical
        batch.  Re-uploading an already-processed batch returns the
        existing batch with ``duplicate: true`` instead of creating a
        second one; genuinely new content creates a new batch.
        """
        # Read raw bytes from uploads first so the fingerprint is computed
        # from actual file content, not filenames or stream metadata.
        settlement_content = await settlement.read()
        bank_content = await bank.read()
        ledger_content = await ledger.read()

        batch_fingerprint = compute_batch_fingerprint(
            settlement_content, bank_content, ledger_content
        )

        # Idempotency pre-check against persisted batches.
        existing = store.get_batch_by_fingerprint(batch_fingerprint)
        if existing is not None and existing["status"] == "completed":
            return _duplicate_response(existing)

        if existing is not None and existing["status"] == "failed":
            # Retry of a previously failed upload: reuse the same row so the
            # unique fingerprint index stays consistent and no residue from
            # the failed attempt is left behind.
            batch_id = existing["id"]
            store.reset_failed_batch(batch_id)
        elif existing is not None:
            # A batch with this content is already processing (concurrent
            # duplicate upload). Do not create another one.
            return JSONResponse(
                status_code=409,
                content={
                    "batch_id": existing["id"],
                    "detail": "A batch with these files is already being processed.",
                    "error_type": "duplicate_in_progress",
                },
            )
        else:
            try:
                batch_id = store.create_batch(batch_fingerprint)
            except sqlite3.IntegrityError:
                # Lost a concurrent insert race; the unique index picked the
                # winner. Re-fetch and return that batch.
                winner = store.get_batch_by_fingerprint(batch_fingerprint)
                if winner is not None and winner["status"] == "completed":
                    return _duplicate_response(winner)
                if winner is not None:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "batch_id": winner["id"],
                            "detail": (
                                "A batch with these files is already being "
                                "processed."
                            ),
                            "error_type": "duplicate_in_progress",
                        },
                    )
                raise

        try:
            # Parse and validate CSV structure.
            settlement_rows = read_csv_content(
                settlement_content, SourceType.SETTLEMENT
            )
            bank_rows = read_csv_content(bank_content, SourceType.BANK)
            ledger_rows = read_csv_content(ledger_content, SourceType.LEDGER)

            # Compute fingerprint from raw CSV bytes for frozen dataset
            # detection.  This is content-based (SHA-256 of byte content),
            # not filename-based.
            upload_fingerprint = compute_upload_fingerprint(
                settlement_content, bank_content, ledger_content
            )

            # Run the full reconciliation pipeline.
            result = run_batch_pipeline(
                settlement_rows, bank_rows, ledger_rows,
                layer2_mode=layer2_mode,
                upload_fingerprint=upload_fingerprint,
                data_dir=data_dir,
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
