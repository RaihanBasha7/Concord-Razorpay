"""
Residual persistence for Day 3 evaluation output.

Produces a scenario-level pipeline residual artifact for Day 4 consumption.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from reconciliation.domain.models import SourceType
from reconciliation.evaluation.dataset_generator import (
    GroundTruthScenario,
    ScenarioRecordSpec,
    _compute_record_id,
)
from reconciliation.evaluation.ground_truth import EdgeCaseCategory


@dataclass(frozen=True)
class PipelineResidualRecord:
    scenario_id: str
    category: Optional[str]
    record_count: int
    member_record_ids: str
    has_valid_relationship: bool
    is_true_exception: bool
    description: str


def persist_residuals(
    scenarios: List[GroundTruthScenario],
    pipeline_residual_scenario_ids: List[str],
    output_dir: Path,
) -> Path:
    scenario_map = {s.scenario_id: s for s in scenarios}
    residuals: List[PipelineResidualRecord] = []
    for scen_id in pipeline_residual_scenario_ids:
        scen = scenario_map.get(scen_id)
        if scen is None:
            continue
        record_ids = tuple(_compute_record_id(spec) for spec in scen.record_specs)
        is_true_orphan = len(scen.record_specs) == 1 and scen.category == EdgeCaseCategory.TRUE_ORPHAN
        residuals.append(
            PipelineResidualRecord(
                scenario_id=scen.scenario_id,
                category=scen.category.value if scen.category else None,
                record_count=len(record_ids),
                member_record_ids=json.dumps(record_ids),
                has_valid_relationship=not is_true_orphan,
                is_true_exception=is_true_orphan,
                description=scen.description,
            )
        )

    path = output_dir / "residuals.csv"
    if residuals:
        fieldnames = [
            "scenario_id",
            "category",
            "record_count",
            "member_record_ids",
            "has_valid_relationship",
            "is_true_exception",
            "description",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in residuals:
                writer.writerow({
                    "scenario_id": r.scenario_id,
                    "category": r.category,
                    "record_count": r.record_count,
                    "member_record_ids": r.member_record_ids,
                    "has_valid_relationship": r.has_valid_relationship,
                    "is_true_exception": r.is_true_exception,
                    "description": r.description,
                })
    else:
        path.write_text("")
    return path
