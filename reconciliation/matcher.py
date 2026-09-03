from __future__ import annotations

from collections import defaultdict
from typing import Iterable, List, Optional, Set, Tuple

from reconciliation.config import DEFAULT_TOLERANCES
from reconciliation.domain.models import (
    MatchRule,
    NormalizedRecord,
    ReconciliationDecision,
    ResolutionLayer,
)
from reconciliation.matcher_config import MatcherConfig
from reconciliation.result import ReconciliationResult


def reconcile(
    records: Iterable[NormalizedRecord],
    config: Optional[MatcherConfig] = None,
) -> ReconciliationResult:
    """
    Run the deterministic reconciliation matcher against the given records.

    Matching order:
    1. Exact identifier matching (highest priority)
    2. Amount-plus-date matching (on residuals from step 1)
    """
    if config is None:
        config = MatcherConfig.from_tolerances(DEFAULT_TOLERANCES)

    record_list = list(records)

    id_counts: dict[str, int] = defaultdict(int)
    for record in record_list:
        id_counts[record.record_id] += 1

    duplicate_ids: Set[str] = {rid for rid, count in id_counts.items() if count > 1}
    excluded_ids: Set[str] = set(duplicate_ids)

    matched_ids: Set[str] = set()
    decisions: List[ReconciliationDecision] = []

    _apply_exact_id_matching(record_list, matched_ids, decisions, excluded_ids)
    _apply_amount_date_matching(record_list, matched_ids, decisions, config, excluded_ids)

    residual_ids = tuple(
        sorted(r.record_id for r in record_list if r.record_id not in matched_ids)
    )

    return ReconciliationResult(
        decisions=tuple(sorted(decisions, key=lambda d: d.decision_id)),
        residual_record_ids=residual_ids,
    )


def _apply_exact_id_matching(
    records: List[NormalizedRecord],
    matched_ids: Set[str],
    decisions: List[ReconciliationDecision],
    excluded_ids: Set[str],
) -> None:
    groups: dict[str, List[NormalizedRecord]] = defaultdict(list)
    for record in records:
        if record.order_id_hint:
            groups[record.order_id_hint].append(record)

    for _hint, group in groups.items():
        if any(r.record_id in matched_ids or r.record_id in excluded_ids for r in group):
            continue

        if len(group) == 2:
            source_types = {r.source_type for r in group}
            if len(source_types) == 2:
                member_ids = tuple(sorted(r.record_id for r in group))
                decision = ReconciliationDecision(
                    decision_id=f"EXACT_ID-{'-'.join(member_ids)}",
                    member_record_ids=member_ids,
                    resolution_layer=ResolutionLayer.LAYER_1,
                    rule_or_rationale=MatchRule.EXACT_ID.value,
                    confidence=1.0,
                )
                decisions.append(decision)
                matched_ids.update(member_ids)


def _apply_amount_date_matching(
    records: List[NormalizedRecord],
    matched_ids: Set[str],
    decisions: List[ReconciliationDecision],
    config: MatcherConfig,
    excluded_ids: Set[str],
) -> None:
    residuals = [
        r for r in records
        if r.record_id not in matched_ids and r.record_id not in excluded_ids
    ]

    eligible_pairs: List[Tuple[NormalizedRecord, NormalizedRecord]] = []
    for i, a in enumerate(residuals):
        for b in residuals[i + 1 :]:
            if a.source_type == b.source_type:
                continue
            if abs(a.amount_paise - b.amount_paise) > config.amount_tolerance_paise:
                continue
            if abs((a.date - b.date).days) > config.date_window_days:
                continue
            eligible_pairs.append((a, b))

    participation = defaultdict(int)
    for a, b in eligible_pairs:
        participation[a.record_id] += 1
        participation[b.record_id] += 1

    for a, b in eligible_pairs:
        if participation[a.record_id] == 1 and participation[b.record_id] == 1:
            member_ids = tuple(sorted((a.record_id, b.record_id)))
            decision = ReconciliationDecision(
                decision_id=f"AMOUNT_DATE-{'-'.join(member_ids)}",
                member_record_ids=member_ids,
                resolution_layer=ResolutionLayer.LAYER_1,
                rule_or_rationale=MatchRule.AMOUNT_AND_DATE.value,
                confidence=1.0,
            )
            decisions.append(decision)
            matched_ids.update(member_ids)
