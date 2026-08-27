"""
Leakage detection checks for synthetic datasets.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from reconciliation.evaluation.dataset_generator import (
    EdgeCaseCategory,
    GroundTruthScenario,
    ScenarioRecordSpec,
    check_leakage,
)


__all__ = ["LeakageReport", "check_leakage"]
