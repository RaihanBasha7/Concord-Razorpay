"""
Day 5.2 — Deliberately naive baseline matcher.

This module provides a deliberately simple, single-pass deterministic matcher
that serves as a lower-bound reference for the full reconciliation engine.

It has NO knowledge of Layer 1 rules, Layer 2 proposals, routing buckets,
confidence scores, or semantic narration matching. Its only intelligence is
a closest-amount/date greedy scan.

Algorithm:
  1. Iterate records in input order.
  2. For each unmatched record, scan all unmatched records from a different
     source type.
  3. Filter candidates by amount tolerance and date window.
  4. Pair with the closest candidate (smallest amount difference, then
     smallest date difference as tiebreaker).
  5. Mark both records as matched.
  6. Unmatched records are returned in input order.

Single-pass means:
  - Records are processed in input order exactly once.
  - Once a record is matched, it is never reconsidered as a candidate for
    later records.
  - A record can never be paired with more than one other record.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Tuple

from reconciliation.domain.models import NormalizedRecord
from reconciliation.matcher_config import MatcherConfig


@dataclass(frozen=True)
class NaiveMatchResult:
    """
    Result of the naive baseline matcher.

    matches contains sorted pairs of record IDs (lower ID first) to make the
    result order-independent.
    """

    matches: Tuple[Tuple[str, str], ...]
    unmatched_record_ids: Tuple[str, ...]


def match(
    records: Iterable[NormalizedRecord],
    config: MatcherConfig,
) -> NaiveMatchResult:
    """
    Run the naive baseline matcher.

    The provided records collection is never mutated.
    """
    record_list = list(records)
    unmatched: List[NormalizedRecord] = list(record_list)

    pairs: List[Tuple[str, str]] = []

    i = 0
    while i < len(unmatched):
        a = unmatched[i]
        best_j = None
        best_score = None

        j = i + 1
        while j < len(unmatched):
            b = unmatched[j]
            if a.source_type == b.source_type:
                j += 1
                continue

            if abs(a.amount_paise - b.amount_paise) > config.amount_tolerance_paise:
                j += 1
                continue

            if abs((a.date - b.date).days) > config.date_window_days:
                j += 1
                continue

            amt_diff = abs(a.amount_paise - b.amount_paise)
            date_diff = abs((a.date - b.date).days)
            score = (amt_diff, date_diff)

            if best_score is None or score < best_score:
                best_score = score
                best_j = j

            j += 1

        if best_j is not None:
            b = unmatched[best_j]
            pair = tuple(sorted((a.record_id, b.record_id)))
            pairs.append(pair)
            # Remove the higher index first, then the lower index.
            if best_j > i:
                del unmatched[best_j]
                del unmatched[i]
            else:
                del unmatched[i]
                del unmatched[best_j]
            # Do not advance i; the next record shifted into position i.
        else:
            i += 1

    return NaiveMatchResult(
        matches=tuple(pairs),
        unmatched_record_ids=tuple(r.record_id for r in unmatched),
    )
