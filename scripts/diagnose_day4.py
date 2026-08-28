"""
Day 4 diagnostic: inspect first 4 residual scenarios through Layer 2
reconstruction and retrieval without modifying production behavior.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from reconciliation.domain.models import NormalizedRecord, SourceType
from reconciliation.layer2 import reconstruct_layer2_case
from reconciliation.loader import load_normalized_records, load_residuals, ResidualScenario
from reconciliation.normalizer import normalize_record
from reconciliation.retrieval import retrieve_candidates, RetrievalConfig


@dataclass(frozen=True)
class RawResidual:
    scenario_id: str
    category: str
    record_count: int
    member_record_ids: Tuple[str, ...]
    has_valid_relationship: bool
    is_true_exception: bool
    description: str


def _load_raw_residuals(data_dir: Path) -> List[RawResidual]:
    path = data_dir / "residuals.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        out = []
        for row in reader:
            out.append(
                RawResidual(
                    scenario_id=row["scenario_id"],
                    category=row["category"],
                    record_count=int(row["record_count"]),
                    member_record_ids=tuple(json.loads(row["member_record_ids"])),
                    has_valid_relationship=row["has_valid_relationship"].strip() == "True",
                    is_true_exception=row["is_true_exception"].strip() == "True",
                    description=row["description"],
                )
            )
        return out


def _record_lookup(
    normalized: Tuple[NormalizedRecord, ...],
) -> Dict[str, NormalizedRecord]:
    return {r.record_id: r for r in normalized}


def _summarize(record: NormalizedRecord) -> Dict[str, Any]:
    return {
        "record_id": record.record_id,
        "source_type": record.source_type.value,
        "source_native_id": record.source_native_id,
        "order_id_hint": record.order_id_hint,
        "amount_paise": record.amount_paise,
        "date": record.date.isoformat(),
        "narration": record.narration,
    }


def main() -> None:
    data_dir = Path("data")
    raw_residuals = _load_raw_residuals(data_dir)
    normalized = load_normalized_records(data_dir)
    lookup = _record_lookup(normalized)

    config = RetrievalConfig()
    rows: List[Dict[str, Any]] = []

    for raw in raw_residuals[:4]:
        scenario_id = raw.scenario_id
        member_ids = raw.member_record_ids
        member_records = tuple(lookup[rid] for rid in member_ids)

        case = reconstruct_layer2_case(
            scenario_id=scenario_id,
            member_record_ids=member_ids,
            normalized_records=normalized,
        )

        retrieval = retrieve_candidates(case, normalized, config)
        candidate_ids = [c.record.record_id for c in retrieval.candidates]

        rows.append(
            {
                "scenario_id": scenario_id,
                "category": raw.category,
                "description": raw.description,
                "has_valid_relationship": raw.has_valid_relationship,
                "is_true_exception": raw.is_true_exception,
                "member_records": [_summarize(r) for r in member_records],
                "candidate_count": retrieval.candidate_count,
                "candidate_ids": candidate_ids,
                "top_candidates": [
                    {
                        "rank": c.rank,
                        "record_id": c.record.record_id,
                        "score": c.score,
                        "signals": c.match_signals,
                        "source_type": c.source_type.value,
                        "record": _summarize(c.record),
                    }
                    for c in retrieval.candidates
                ],
            }
        )

    # Print concise report
    for row in rows:
        print(f"=== {row['scenario_id']} ({row['category']}) ===")
        print(f"  Description: {row['description']}")
        print(f"  has_valid_relationship={row['has_valid_relationship']}, is_true_exception={row['is_true_exception']}")
        print(f"  Member records ({len(row['member_records'])}):")
        for rec in row["member_records"]:
            print(
                f"    {rec['record_id']} | {rec['source_type']} | "
                f"amt={rec['amount_paise']} | date={rec['date']} | "
                f"order={rec['order_id_hint']} | narr={rec['narration']}"
            )
        print(f"  Top {row['candidate_count']} candidates:")
        for cand in row["top_candidates"]:
            print(
                f"    #{cand['rank']} {cand['record_id']} (score={cand['score']}, "
                f"signals={cand['signals']}) | {cand['record']['source_type']} | "
                f"amt={cand['record']['amount_paise']} | date={cand['record']['date']} | "
                f"order={cand['record']['order_id_hint']}"
            )
        print()


if __name__ == "__main__":
    main()
