"""
Evaluation harness for running Layer 1 matcher against synthetic dataset.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from reconciliation.domain.models import SourceType
from reconciliation.evaluation.dataset_generator import (
    GeneratedDataset,
    GroundTruthScenario,
    _compute_record_id,
    generate_dataset,
    validate_quotas,
    write_dataset,
)
from reconciliation.evaluation.ground_truth import GroundTruthUnit
from reconciliation.evaluation.metrics import EvaluationReport, evaluate
from reconciliation.evaluation.residuals import persist_residuals
from reconciliation.matcher import reconcile
from reconciliation.matcher_config import MatcherConfig
from reconciliation.normalizer import normalize_record


@dataclass(frozen=True)
class HarnessResult:
    report: EvaluationReport
    residual_path: Path


def load_normalized_records(
    settlement_path: Path,
    bank_path: Path,
    ledger_path: Path,
) -> List[Any]:
    records = []
    for path, source_type in [
        (settlement_path, SourceType.SETTLEMENT),
        (bank_path, SourceType.BANK),
        (ledger_path, SourceType.LEDGER),
    ]:
        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Drop scenario_ref before normalization so it is not part of matching
                clean_row = {k: v for k, v in row.items() if k not in ("scenario_ref", "synthetic_ref")}
                record = normalize_record(clean_row, source_type)
                records.append(record)
    return records


def build_ground_truth_units(
    scenarios: List[GroundTruthScenario],
) -> List[GroundTruthUnit]:
    units = []
    for scen in scenarios:
        record_ids = tuple(_compute_record_id(spec) for spec in scen.record_specs)
        units.append(
            GroundTruthUnit(
                scenario_id=scen.scenario_id,
                member_record_ids=record_ids,
                true_category=scen.category,
                is_true_orphan=len(record_ids) == 1,
                has_real_match=scen.has_real_match,
            )
        )
    return units


def run_evaluation(
    dataset: GeneratedDataset,
    config: MatcherConfig,
    output_dir: Path,
) -> HarnessResult:
    validate_quotas(list(dataset.scenarios))

    # Leakage check
    from reconciliation.evaluation.leakage import check_leakage
    leakage = check_leakage(list(dataset.scenarios), list(dataset.record_specs))
    if leakage.has_leakage:
        raise ValueError(f"Leakage detected: {leakage.issues}")

    records = load_normalized_records(
        output_dir / "settlements.csv",
        output_dir / "bank.csv",
        output_dir / "ledger.csv",
    )
    result = reconcile(records, config)
    units = build_ground_truth_units(list(dataset.scenarios))
    report = evaluate(
        scenarios=list(dataset.scenarios),
        decisions=list(result.decisions),
        residual_record_ids=list(result.residual_record_ids),
        scenario_units=units,
    )
    residual_path = persist_residuals(
        scenarios=list(dataset.scenarios),
        pipeline_residual_scenario_ids=list(report.pipeline_residual_scenario_ids),
        output_dir=output_dir,
    )
    return HarnessResult(report=report, residual_path=residual_path)
