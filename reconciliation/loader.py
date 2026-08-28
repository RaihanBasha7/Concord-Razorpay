"""
Day 4.5 — Data loading for the Layer 2 runner.

Loads the normalized source datasets and the residual scenario manifest. The
residual manifest contains evaluation-only metadata (category, has_valid_
relationship, is_true_exception, description, and category-encoded scenario_id
prefixes). This loader surfaces only what the runner needs to reconstruct a
Layer2Case; ground-truth/evaluation fields are never passed downstream to the
LLM.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from reconciliation.domain.models import NormalizedRecord, SourceType
from reconciliation.normalizer import normalize_record


@dataclass(frozen=True)
class ResidualScenario:
    """A residual scenario stripped of evaluation-only fields."""

    scenario_id: str
    record_count: int
    member_record_ids: Tuple[str, ...]


_SOURCE_FILES = (
    ("settlements.csv", SourceType.SETTLEMENT),
    ("bank.csv", SourceType.BANK),
    ("ledger.csv", SourceType.LEDGER),
)


def load_normalized_records(data_dir: Path | str) -> Tuple[NormalizedRecord, ...]:
    """Normalize every source row across the three supported datasets."""
    data_dir = Path(data_dir)
    records: list[NormalizedRecord] = []
    for filename, source_type in _SOURCE_FILES:
        path = data_dir / filename
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                records.append(normalize_record(row, source_type))
    return tuple(records)


def load_residuals(data_dir: Path | str) -> Tuple[ResidualScenario, ...]:
    """Load residual scenarios, keeping only the fields needed for reconstruction."""
    data_dir = Path(data_dir)
    scenarios: list[ResidualScenario] = []
    with (data_dir / "residuals.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            member_ids = tuple(json.loads(row["member_record_ids"]))
            scenarios.append(
                ResidualScenario(
                    scenario_id=row["scenario_id"],
                    record_count=int(row["record_count"]),
                    member_record_ids=member_ids,
                )
            )
    return tuple(scenarios)
