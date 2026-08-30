"""
Ground-truth types for Concord evaluation.

Ground truth exists only for evaluation and must never be imported by the
matching engine. These types define expected categories and true orphan
status for labelled reconciliation examples.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Tuple


class EdgeCaseCategory(str, Enum):
    EXACT_MATCH = "EXACT_MATCH"
    T_PLUS_DELAY = "T_PLUS_DELAY"
    FEE_DEDUCTED = "FEE_DEDUCTED"
    PARTIAL_REFUND = "PARTIAL_REFUND"
    SPLIT_SETTLEMENT = "SPLIT_SETTLEMENT"
    ROUNDING_DIFFERENCE = "ROUNDING_DIFFERENCE"
    INCONSISTENT_NARRATION = "INCONSISTENT_NARRATION"
    DUPLICATE = "DUPLICATE"
    TRUE_ORPHAN = "TRUE_ORPHAN"
    LATE_ARRIVING = "LATE_ARRIVING"


@dataclass(frozen=True)
class GroundTruthUnit:
    scenario_id: str
    member_record_ids: Tuple[str, ...]
    true_category: EdgeCaseCategory
    is_true_orphan: bool
    has_real_match: bool = False
