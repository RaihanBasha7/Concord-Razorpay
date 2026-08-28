"""
Day 4.1 — Layer 2 input contract and deterministic residual record reconstruction.

Reconstructs normalized source records from residual scenario metadata
without leaking evaluation-only fields into the production contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from reconciliation.domain.models import NormalizedRecord


class ReconstructionError(ValueError):
    """Raised when a referenced record cannot be found during reconstruction."""


@dataclass(frozen=True)
class Layer2Case:
    """
    A reconstructed unresolved Layer 2 case.

    Contains only production-available information derived from normalized
    source records. Evaluation-only metadata such as category,
    has_valid_relationship, is_true_exception, and description are
    intentionally excluded.
    """

    scenario_id: str
    member_records: Tuple[NormalizedRecord, ...]
    record_count: int

    def __post_init__(self) -> None:
        if not self.scenario_id:
            raise ValueError("scenario_id must be a non-empty string.")
        if self.record_count < 1:
            raise ValueError("record_count must be at least 1.")
        if self.record_count != len(self.member_records):
            raise ValueError(
                "record_count must match the number of member_records."
            )
        record_ids = [r.record_id for r in self.member_records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError(
                "member_records must not contain duplicate record IDs."
            )


def reconstruct_layer2_case(
    *,
    scenario_id: str,
    member_record_ids: Tuple[str, ...],
    normalized_records: Tuple[NormalizedRecord, ...],
) -> Layer2Case:
    """
    Deterministically reconstruct a Layer2Case from residual scenario
    metadata and a lookup of normalized records.

    The order of member_record_ids is preserved in member_records.

    Raises ReconstructionError if any member_record_id cannot be found.
    """
    if not member_record_ids:
        raise ReconstructionError("member_record_ids must not be empty.")

    record_map = {r.record_id: r for r in normalized_records}
    missing = [rid for rid in member_record_ids if rid not in record_map]
    if missing:
        raise ReconstructionError(
            f"Referenced record(s) not found: {', '.join(missing)}"
        )

    member_records = tuple(record_map[rid] for rid in member_record_ids)
    return Layer2Case(
        scenario_id=scenario_id,
        member_records=member_records,
        record_count=len(member_record_ids),
    )
