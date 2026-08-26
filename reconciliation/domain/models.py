"""
Domain models for Concord — Intelligent Settlement Reconciliation.

These types represent what the reconciliation engine is allowed to know.
They are frozen, immutable, and independent of evaluation concerns.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Tuple


class SourceType(str, Enum):
    SETTLEMENT = "SETTLEMENT"
    BANK = "BANK"
    LEDGER = "LEDGER"


@dataclass(frozen=True)
class NormalizedRecord:
    """
    A canonical financial record from any supported source — settlement,
    bank, or ledger — after normalization.

    amount_paise is produced by the normalization layer and should not be
    parsed ad hoc by reconciliation logic. All internal money arithmetic
    must operate on this integer paise value.
    """

    record_id: str
    source_type: SourceType
    source_native_id: str
    order_id_hint: str | None
    amount_paise: int
    date: date
    narration: str | None
    raw_payload: Any

    def __post_init__(self) -> None:
        if not self.record_id:
            raise ValueError("record_id must be a non-empty string.")
        if not self.source_native_id:
            raise ValueError("source_native_id must be a non-empty string.")


class ResolutionLayer(str, Enum):
    LAYER_1 = "LAYER_1"
    LAYER_2 = "LAYER_2"


class MatchRule(str, Enum):
    EXACT_ID = "EXACT_ID"
    AMOUNT_AND_DATE = "AMOUNT_AND_DATE"
    SPLIT_AGGREGATION = "SPLIT_AGGREGATION"


@dataclass(frozen=True)
class ReconciliationDecision:
    """
    A decision represents a positive reconciliation claim only. A record
    without a decision remains unresolved at that layer and is derived by
    the orchestration layer.
    """

    decision_id: str
    member_record_ids: Tuple[str, ...]
    resolution_layer: ResolutionLayer
    rule_or_rationale: str
    confidence: float

    def __post_init__(self) -> None:
        if len(self.member_record_ids) < 2:
            raise ValueError(
                "member_record_ids must contain at least 2 IDs."
            )
        if len(self.member_record_ids) != len(set(self.member_record_ids)):
            raise ValueError(
                "member_record_ids must not contain duplicates."
            )
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                "confidence must be between 0.0 and 1.0 inclusive."
            )
