"""
Pipeline orchestration for the Concord API.

Thin layer that wires existing engine components (normalizer, Layer 1
matcher, Layer 2 reconstruction/retrieval, Layer 3 routing) into a
single callable for the API endpoints.

No reconciliation logic lives here — only sequencing and serialization.
The engine code in reconciliation.matcher, reconciliation.layer3, etc.
remains completely independent of FastAPI.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from reconciliation.domain.models import NormalizedRecord, SourceType
from reconciliation.layer3 import route as layer3_route
from reconciliation.matcher import reconcile
from reconciliation.matcher_config import MatcherConfig
from reconciliation.normalizer import NormalizationError, normalize_record
from reconciliation.proposal_validation import ProposalOutcome, ProposalOutcomeType


# ---------------------------------------------------------------------------
# Required CSV columns per source type.
# At least one key from each group must be present in the CSV header.
# ---------------------------------------------------------------------------

_REQUIRED_COLUMN_GROUPS: Dict[SourceType, Dict[str, Tuple[str, ...]]] = {
    SourceType.SETTLEMENT: {
        "id": ("settlement_id",),
        "amount": ("gross_amount", "amount"),
        "date": ("settlement_date",),
    },
    SourceType.BANK: {
        "id": ("bank_utr",),
        "amount": ("credit_amount", "amount"),
        "date": ("value_date",),
    },
    SourceType.LEDGER: {
        "id": ("order_id",),
        "amount": ("gross_amount", "amount"),
        "date": ("transaction_date",),
    },
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CSVValidationError(ValueError):
    """Raised when a CSV input fails validation or normalization."""

    def __init__(
        self,
        message: str,
        error_type: str = "validation_error",
        details: Dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.details = details or {}


# ---------------------------------------------------------------------------
# Pipeline result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchPipelineResult:
    """Everything the API needs to persist after a successful pipeline run."""

    record_count: int
    enriched_routing: List[Dict[str, Any]]
    l1_decisions_json: Any
    l2_outcomes_json: List[Any]
    eval_report: Dict[str, Any]
    records_by_source: Dict[str, int]
    layer2_mode: str
    audit_records: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# CSV parsing and validation
# ---------------------------------------------------------------------------


def read_csv_content(
    content: bytes, source_type: SourceType
) -> List[Dict[str, str]]:
    """Decode, parse, and validate the structure of a CSV upload.

    Returns a list of raw row dictionaries.  Raises CSVValidationError
    for unreadable content, malformed CSV, or missing required columns.
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CSVValidationError(
            f"{source_type.value} CSV is not valid UTF-8 text.",
            error_type="unreadable_csv",
        ) from exc

    try:
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
    except csv.Error as exc:
        raise CSVValidationError(
            f"{source_type.value} CSV is malformed: {exc}",
            error_type="malformed_csv",
        ) from exc

    if reader.fieldnames is None:
        raise CSVValidationError(
            f"{source_type.value} CSV is empty or has no headers.",
            error_type="malformed_csv",
        )

    # Validate required columns (case-sensitive, matching normalizer convention).
    header_set = {f.strip() for f in reader.fieldnames}
    groups = _REQUIRED_COLUMN_GROUPS[source_type]
    missing: Dict[str, Tuple[str, ...]] = {}
    for group_name, keys in groups.items():
        if not any(k in header_set for k in keys):
            missing[group_name] = keys

    if missing:
        raise CSVValidationError(
            f"{source_type.value} CSV is missing required columns.",
            error_type="missing_columns",
            details={
                "missing_groups": {k: list(v) for k, v in missing.items()},
                "found_columns": list(reader.fieldnames),
            },
        )

    return rows


def normalize_rows(
    rows: List[Dict[str, str]], source_type: SourceType
) -> List[NormalizedRecord]:
    """Normalize every row through the existing normalizer.

    Raises CSVValidationError on the first normalization failure so the
    caller gets a precise row-level error message.
    """
    records: List[NormalizedRecord] = []
    for i, row in enumerate(rows):
        try:
            records.append(normalize_record(row, source_type))
        except NormalizationError as exc:
            raise CSVValidationError(
                f"{source_type.value} CSV row {i + 2} failed normalization: {exc}",
                error_type="normalization_error",
                details={"row_number": i + 2, "source_type": source_type.value},
            ) from exc
    return records


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_l1_decisions(decisions: tuple) -> List[Dict[str, Any]]:
    return [
        {
            "decision_id": d.decision_id,
            "member_record_ids": list(d.member_record_ids),
            "resolution_layer": d.resolution_layer.value,
            "rule_or_rationale": d.rule_or_rationale,
            "confidence": d.confidence,
        }
        for d in decisions
    ]


def _serialize_l2_outcome(outcome: ProposalOutcome) -> Dict[str, Any]:
    proposal = None
    if outcome.proposal is not None:
        proposal = {
            "proposed_match_ids": list(outcome.proposal.proposed_match_ids),
            "confidence": outcome.proposal.confidence,
            "rationale": outcome.proposal.rationale,
        }
    return {
        "outcome": outcome.outcome.value,
        "proposal": proposal,
        "presented_record_ids": list(outcome.presented_record_ids),
        "reason": outcome.reason,
        "invalid_ids": list(outcome.invalid_ids),
        "diagnostic": outcome.diagnostic,
    }


def _build_enriched_routing(
    routing_decisions: list,
    record_map: Dict[str, NormalizedRecord],
) -> List[Dict[str, Any]]:
    """Pair each RoutingDecision with the corresponding record details."""
    enriched: List[Dict[str, Any]] = []
    for rd in routing_decisions:
        rec = record_map[rd.record_id]
        enriched.append(
            {
                "record_id": rd.record_id,
                "source_type": rec.source_type.value,
                "source_native_id": rec.source_native_id,
                "order_id_hint": rec.order_id_hint,
                "amount_paise": rec.amount_paise,
                "date": rec.date.isoformat(),
                "narration": rec.narration,
                "bucket": rd.bucket.value,
                "reason": rd.reason.value,
                "confidence": rd.confidence,
                "source_decision_id": rd.source_decision_id,
                "source_outcome": (
                    rd.source_outcome.value if rd.source_outcome else None
                ),
            }
        )
    return enriched


def _build_audit_records(
    routing_decisions: list,
    record_map: Dict[str, NormalizedRecord],
    l1_result: Any,
    l2_outcomes: List[ProposalOutcome],
) -> List[Dict[str, Any]]:
    """Build a structured JSON audit record for every normalized record.

    The audit record captures the complete decision path: which layer
    resolved the record, what rule or AI proposal was applied, and the
    final routing verdict.  No secrets or API keys are included.
    """
    # Build lookup: record_id -> Layer 1 decision info.
    l1_by_record: Dict[str, Dict[str, Any]] = {}
    for d in l1_result.decisions:
        for rid in d.member_record_ids:
            l1_by_record[rid] = {
                "decision_id": d.decision_id,
                "member_record_ids": list(d.member_record_ids),
                "rule_or_rationale": d.rule_or_rationale,
                "confidence": d.confidence,
            }

    # Build lookup: record_id -> Layer 2 outcome info.
    # A record may appear in multiple outcomes as a candidate; the first
    # outcome that claims it (per Layer 3) is the authoritative one.
    l2_by_record: Dict[str, Dict[str, Any]] = {}
    for outcome in l2_outcomes:
        proposed_ids = (
            set(outcome.proposal.proposed_match_ids)
            if outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID
            and outcome.proposal is not None
            else set()
        )
        for rid in outcome.presented_record_ids:
            if rid not in l2_by_record:
                l2_by_record[rid] = {
                    "scenario_id": None,
                    "outcome_type": outcome.outcome.value,
                    "proposed_match_ids": list(proposed_ids),
                    "confidence": (
                        outcome.proposal.confidence
                        if outcome.proposal is not None
                        else None
                    ),
                    "rationale": (
                        outcome.proposal.rationale
                        if outcome.proposal is not None
                        else None
                    ),
                    "reason": outcome.reason,
                    "invalid_ids": list(outcome.invalid_ids),
                }

    # Build the final audit records.
    audit_records: List[Dict[str, Any]] = []
    for rd in routing_decisions:
        rec = record_map[rd.record_id]

        l1_info = l1_by_record.get(rd.record_id)
        l2_info = l2_by_record.get(rd.record_id)

        # Determine which layer resolved this record.
        # Layer 1 deterministic matches take priority and must NOT
        # carry Layer 2 info — deterministic matches are never
        # presented as AI-derived.
        if l1_info is not None:
            resolved_by = "LAYER_1"
            l2_for_audit = None
        elif l2_info is not None:
            resolved_by = "LAYER_2"
            l2_for_audit = l2_info
        else:
            resolved_by = None
            l2_for_audit = None

        audit_records.append({
            "record_id": rd.record_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source_type": rec.source_type.value,
            "source_native_id": rec.source_native_id,
            "order_id_hint": rec.order_id_hint,
            "amount_paise": rec.amount_paise,
            "date": rec.date.isoformat(),
            "narration": rec.narration,
            "resolved_by": resolved_by,
            "layer1": l1_info,
            "layer2": l2_for_audit,
            "routing": {
                "bucket": rd.bucket.value,
                "reason": rd.reason.value,
                "confidence": rd.confidence,
                "source_decision_id": rd.source_decision_id,
                "source_outcome": (
                    rd.source_outcome.value if rd.source_outcome else None
                ),
            },
        })

    return audit_records


def _build_eval_report(
    l1_result: Any,
    l2_outcomes: List[ProposalOutcome],
    routing_decisions: list,
    records: List[NormalizedRecord],
    records_by_source: Dict[str, int],
) -> Dict[str, Any]:
    """Compute an operational evaluation report without ground truth.

    The report captures Layer 1 match statistics, Layer 2 outcome
    summary, and Layer 3 routing composition.  Ground-truth-dependent
    metrics (precision, recall, false-accept rate) are intentionally
    absent because the API has no labelled data.
    """
    l1_decision_count = len(l1_result.decisions)

    matched_record_ids: set = set()
    for d in l1_result.decisions:
        matched_record_ids.update(d.member_record_ids)

    l1_decisions_by_rule: Dict[str, int] = {}
    for d in l1_result.decisions:
        l1_decisions_by_rule[d.rule_or_rationale] = (
            l1_decisions_by_rule.get(d.rule_or_rationale, 0) + 1
        )

    l2_outcomes_by_type: Dict[str, int] = {}
    for o in l2_outcomes:
        l2_outcomes_by_type[o.outcome.value] = (
            l2_outcomes_by_type.get(o.outcome.value, 0) + 1
        )

    routing_composition: Dict[str, int] = {}
    for rd in routing_decisions:
        bucket = rd.bucket.value
        routing_composition[bucket] = routing_composition.get(bucket, 0) + 1

    return {
        "total_records": len(records),
        "records_by_source": records_by_source,
        "layer1": {
            "total_records": len(records),
            "decisions_count": l1_decision_count,
            "matched_records": len(matched_record_ids),
            "residual_records": len(l1_result.residual_record_ids),
            "decisions_by_rule": l1_decisions_by_rule,
        },
        "layer2": {
            "scenarios_processed": len(l2_outcomes),
            "outcomes_by_type": l2_outcomes_by_type,
        },
        "layer3_routing_composition": routing_composition,
        "thresholds": {
            "auto_accept": 0.90,
            "review": 0.60,
        },
    }


# ---------------------------------------------------------------------------
# Main pipeline entry point
# ---------------------------------------------------------------------------


def run_batch_pipeline(
    settlement_rows: List[Dict[str, str]],
    bank_rows: List[Dict[str, str]],
    ledger_rows: List[Dict[str, str]],
    *,
    layer2_mode: str = "not_executed",
) -> BatchPipelineResult:
    """Execute the full Concord pipeline on pre-parsed CSV rows.

    Flow: normalize → Layer 1 → Layer 3 routing → evaluation summary.

    The API does not construct artificial Layer 2 scenarios from
    arbitrary unmatched records.  Layer 2 outcomes are always empty.
    Unmatched records fall through to Layer 3 as EXCEPTION / NO_CANDIDATE.

    Parameters
    ----------
    layer2_mode : str
        A label recorded in the eval report so consumers can distinguish
        real AI processing from the default API path.  Currently always
        ``not_executed`` because the API does not execute Layer 2.

    Raises CSVValidationError if normalization fails for any row.
    """
    # 1. Normalize all source records.
    settlement_records = normalize_rows(settlement_rows, SourceType.SETTLEMENT)
    bank_records = normalize_rows(bank_rows, SourceType.BANK)
    ledger_records = normalize_rows(ledger_rows, SourceType.LEDGER)

    all_records = settlement_records + bank_records + ledger_records
    records_by_source = {
        "SETTLEMENT": len(settlement_records),
        "BANK": len(bank_records),
        "LEDGER": len(ledger_records),
    }

    if not all_records:
        raise CSVValidationError(
            "All three CSV files contain no data rows.",
            error_type="empty_input",
        )

    # 2. Layer 1 — deterministic matching.
    config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
    l1_result = reconcile(all_records, config)
    residual_ids = set(l1_result.residual_record_ids)

    # 3. Layer 2 residual processing.
    #
    #    The API does not construct artificial Layer 2 scenarios from
    #    arbitrary unmatched records.  Layer 2 outcomes remain empty
    #    unless a valid production-safe case construction mechanism
    #    exists outside this pipeline.  Unmatched records fall through
    #    to Layer 3's EXCEPTION / NO_CANDIDATE fallback.
    l2_outcomes: List[ProposalOutcome] = []

    # 4. Layer 3 — deterministic guardrail routing.
    routing_decisions = layer3_route(
        layer1_decisions=l1_result.decisions,
        layer2_outcomes=l2_outcomes,
        all_records=all_records,
    )

    # 5. Build enriched results for storage.
    record_map = {r.record_id: r for r in all_records}
    enriched_routing = _build_enriched_routing(routing_decisions, record_map)

    # 6. Compute operational evaluation report.
    eval_report = _build_eval_report(
        l1_result, l2_outcomes, routing_decisions, all_records, records_by_source
    )
    eval_report["layer2_mode"] = layer2_mode

    # 7. Build structured audit records.
    audit_records = _build_audit_records(
        routing_decisions, record_map, l1_result, l2_outcomes
    )

    return BatchPipelineResult(
        record_count=len(all_records),
        enriched_routing=enriched_routing,
        l1_decisions_json=_serialize_l1_decisions(l1_result.decisions),
        l2_outcomes_json=[_serialize_l2_outcome(o) for o in l2_outcomes],
        eval_report=eval_report,
        records_by_source=records_by_source,
        layer2_mode=layer2_mode,
        audit_records=audit_records,
    )
