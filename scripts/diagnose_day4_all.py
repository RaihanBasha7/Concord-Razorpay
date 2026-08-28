"""
Day 4 full diagnostic: analyze all residual scenarios through Layer 2
reconstruction and retrieval without modifying production behavior.

Classifies each scenario into relationship shapes using ground truth
only as an offline oracle. No production code is changed.
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from reconciliation.domain.models import NormalizedRecord, SourceType
from reconciliation.layer2 import reconstruct_layer2_case
from reconciliation.loader import load_normalized_records
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


@dataclass(frozen=True)
class ShapeReport:
    scenario_id: str
    category: str
    description: str
    has_valid_relationship: bool
    is_true_exception: bool
    shape: str
    member_record_ids: Tuple[str, ...]
    candidate_count: int
    candidate_ids: Tuple[str, ...]
    true_counterpart_in_candidates: Optional[bool]
    top_candidates: Tuple[Dict[str, Any], ...]


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


def _infer_true_counterpart_ids(
    raw: RawResidual,
    lookup: Dict[str, NormalizedRecord],
) -> Tuple[str, ...]:
    """
    Infer the set of record IDs that constitute the true counterpart(s)
    according to synthetic ground truth.

    For TRUE_ORPHAN there is no counterpart.
    For other categories the true relationship is among the member records
    themselves (e.g. duplicate pair, fee-deducted pair, split settlement triple).
    """
    if raw.is_true_exception or not raw.has_valid_relationship:
        return ()

    member_ids = set(raw.member_record_ids)
    members = [lookup[rid] for rid in raw.member_record_ids if rid in lookup]

    if raw.category == "DUPLICATE":
        if len(members) == 2:
            return (members[1].record_id,)
        if len(members) == 3:
            return (members[1].record_id, members[2].record_id)
        return ()

    if raw.category == "SPLIT_SETTLEMENT":
        if len(members) >= 2:
            return tuple(m.record_id for m in members[1:])
        return ()

    if raw.category in {
        "FEE_DEDUCTED",
        "LATE_ARRIVING",
        "INCONSISTENT_NARRATION",
        "PARTIAL_REFUND",
        "ROUNDING_DIFFERENCE",
    }:
        if len(members) == 2:
            return (members[1].record_id,)
        return ()

    return ()


def _classify(
    raw: RawResidual,
    retrieval_candidate_ids: Tuple[str, ...],
    lookup: Dict[str, NormalizedRecord],
) -> Tuple[str, Optional[bool]]:
    """
    Return (shape, true_counterpart_in_candidates).

    Shapes:
      TRUE_EXCEPTION: no valid reconciliation relationship exists.
      INTERNAL_CASE: required relationship can be determined entirely among
        Layer2Case.member_records.
      EXTERNAL_CANDIDATE: requires one or more relevant records outside
        member_records, and we check whether retrieval top-10 contains them.
      MIXED: reconciliation requires reasoning over both member records and
        external candidates.
    """
    if raw.is_true_exception or not raw.has_valid_relationship:
        return "TRUE_EXCEPTION", None

    true_counterpart_ids = set(_infer_true_counterpart_ids(raw, lookup))
    member_ids = set(raw.member_record_ids)
    candidate_ids = set(retrieval_candidate_ids)

    external_true_counterparts = true_counterpart_ids - member_ids
    if not external_true_counterparts:
        return "INTERNAL_CASE", None

    in_candidates = external_true_counterparts & candidate_ids
    if in_candidates == external_true_counterparts:
        return "EXTERNAL_CANDIDATE", True
    if in_candidates:
        return "MIXED", True

    return "EXTERNAL_CANDIDATE", False


def _top_candidates(
    retrieval_result: Any,
    lookup: Dict[str, NormalizedRecord],
) -> Tuple[Dict[str, Any], ...]:
    items = []
    for c in retrieval_result.candidates:
        record = lookup.get(c.record.record_id)
        summary = _summarize(record) if record else {}
        items.append(
            {
                "rank": c.rank,
                "record_id": c.record.record_id,
                "score": c.score,
                "signals": c.match_signals,
                "source_type": c.source_type.value,
                "amount_paise": summary.get("amount_paise"),
                "date": summary.get("date"),
                "order_id_hint": summary.get("order_id_hint"),
            }
        )
    return tuple(items)


def main() -> None:
    data_dir = Path("data")
    raw_residuals = _load_raw_residuals(data_dir)
    normalized = load_normalized_records(data_dir)
    lookup = _record_lookup(normalized)
    config = RetrievalConfig()

    reports: List[ShapeReport] = []
    for raw in raw_residuals:
        member_ids = raw.member_record_ids
        member_records = tuple(lookup[rid] for rid in member_ids if rid in lookup)

        case = reconstruct_layer2_case(
            scenario_id=raw.scenario_id,
            member_record_ids=member_ids,
            normalized_records=normalized,
        )
        retrieval = retrieve_candidates(case, normalized, config)
        candidate_ids = tuple(c.record.record_id for c in retrieval.candidates)

        shape, in_candidates = _classify(raw, candidate_ids, lookup)
        reports.append(
            ShapeReport(
                scenario_id=raw.scenario_id,
                category=raw.category,
                description=raw.description,
                has_valid_relationship=raw.has_valid_relationship,
                is_true_exception=raw.is_true_exception,
                shape=shape,
                member_record_ids=member_ids,
                candidate_count=retrieval.candidate_count,
                candidate_ids=candidate_ids,
                true_counterpart_in_candidates=in_candidates,
                top_candidates=_top_candidates(retrieval, lookup),
            )
        )

    shape_counts = Counter(r.shape for r in reports)
    by_category = defaultdict(list)
    for r in reports:
        by_category[r.category].append(r)

    external_or_mixed = [r for r in reports if r.shape in {"EXTERNAL_CANDIDATE", "MIXED"}]
    external_or_mixed_recall = (
        sum(1 for r in external_or_mixed if r.true_counterpart_in_candidates)
        / len(external_or_mixed)
        if external_or_mixed
        else None
    )

    print("=" * 72)
    print("DAY 4 FULL RESIDUAL SHAPE DIAGNOSIS")
    print("=" * 72)
    print(f"Total scenarios analyzed: {len(reports)}")
    print()
    print("Shape counts:")
    for shape, count in sorted(shape_counts.items()):
        print(f"  {shape:20s} {count}")
    print()
    print("Retrieval recall (EXTERNAL_CANDIDATE + MIXED):")
    if external_or_mixed_recall is None:
        print("  N/A (no EXTERNAL_CANDIDATE or MIXED scenarios)")
    else:
        print(f"  {external_or_mixed_recall:.2%} ({sum(1 for r in external_or_mixed if r.true_counterpart_in_candidates)}/{len(external_or_mixed)})")
    print()

    print("Breakdown by taxonomy category:")
    for category in sorted(by_category.keys()):
        items = by_category[category]
        shapes = Counter(r.shape for r in items)
        print(f"  {category:25s} {len(items):3d} scenarios -> {dict(shapes)}")
    print()

    print("Representative examples:")
    for shape in sorted(shape_counts.keys()):
        examples = [r for r in reports if r.shape == shape][:3]
        print(f"\n  {shape}:")
        for ex in examples:
            print(f"    {ex.scenario_id} ({ex.category})")
            print(f"      members: {', '.join(ex.member_record_ids)}")
            print(f"      candidates: {ex.candidate_count}")
            if ex.true_counterpart_in_candidates is not None:
                print(f"      true counterpart in candidates: {ex.true_counterpart_in_candidates}")
    print()

    if external_or_mixed:
        print("EXTERNAL_CANDIDATE / MIXED details:")
        for r in external_or_mixed:
            print(f"  {r.scenario_id} ({r.category}) shape={r.shape}")
            print(f"    members: {', '.join(r.member_record_ids)}")
            print(f"    true counterpart in candidates: {r.true_counterpart_in_candidates}")
            for c in r.top_candidates[:5]:
                print(
                    f"      #{c['rank']} {c['record_id']} score={c['score']} "
                    f"signals={c['signals']} src={c['source_type']} "
                    f"amt={c['amount_paise']} date={c['date']}"
                )
            print()

    print("=" * 72)


if __name__ == "__main__":
    main()
