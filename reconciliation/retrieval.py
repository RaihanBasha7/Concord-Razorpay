"""
Day 4.2 — Deterministic candidate retrieval for Layer 2.

Given a Layer2Case containing unresolved normalized records, retrieve a
small, bounded, deterministic set of plausible candidate records from the
available normalized dataset using transparent, explainable signals.

This component receives only Layer2Case and normalized records. It does
not use or inspect scenario category, ground-truth labels,
has_valid_relationship, is_true_exception, or synthetic descriptions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Tuple

from reconciliation.domain.models import NormalizedRecord, SourceType


class RetrievalError(ValueError):
    """Raised when retrieval cannot proceed due to invalid input."""


@dataclass(frozen=True)
class RetrievalConfig:
    """
    Configuration for deterministic candidate retrieval.

    Scores are computed with deterministic arithmetic on the
    production-visible fields of NormalizedRecord. Lower score = better match.
    """

    max_candidates: int = 10
    date_weight: float = 1.0
    amount_weight: float = 1.0
    order_id_weight: float = 2.0
    cross_source_bonus: float = 1.0

    def __post_init__(self) -> None:
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be at least 1.")
        if self.date_weight < 0:
            raise ValueError("date_weight must be non-negative.")
        if self.amount_weight < 0:
            raise ValueError("amount_weight must be non-negative.")
        if self.order_id_weight < 0:
            raise ValueError("order_id_weight must be non-negative.")
        if self.cross_source_bonus < 0:
            raise ValueError("cross_source_bonus must be non-negative.")


@dataclass(frozen=True)
class CandidateRecord:
    """
    A ranked candidate record with retrieval metadata explaining its rank.
    """

    record: NormalizedRecord
    score: float
    rank: int
    match_signals: Tuple[str, ...]
    source_type: SourceType


@dataclass(frozen=True)
class RetrievalResult:
    """
    The result of deterministic candidate retrieval for a Layer2Case.
    """

    scenario_id: str
    candidates: Tuple[CandidateRecord, ...]
    candidate_count: int
    max_candidates: int
    retrieval_signals_used: Tuple[str, ...]


def _date_proximity_days(a: date, b: date) -> int:
    """Return absolute day distance between two dates."""
    return abs((a - b).days)


def _amount_proximity_relative(a_paise: int, b_paise: int) -> float:
    """Return scale-aware relative amount difference in [0.0, 1.0].

    0.0 means exact match. 1.0 means one amount is zero while the other is
    non-zero. For two non-zero amounts the value is the absolute difference
    divided by the larger amount, so proportional differences are treated
    equally regardless of transaction scale.
    """
    if a_paise == 0 and b_paise == 0:
        return 0.0
    if a_paise == 0 or b_paise == 0:
        return 1.0
    return abs(a_paise - b_paise) / max(a_paise, b_paise)


def _order_id_match_score(
    a_hint: str | None,
    b_hint: str | None,
) -> tuple[float, bool, bool]:
    """
    Return (bonus_factor, exact_match, partial_match) for two order_id_hints.

    bonus_factor is 1.0 for exact match, 0.5 for partial match, 0.0 otherwise.
    """
    if a_hint is None or b_hint is None:
        return 0.0, False, False
    if a_hint == b_hint:
        return 1.0, True, False
    if a_hint in b_hint or b_hint in a_hint:
        return 0.5, False, True
    prefix_len = 0
    for ca, cb in zip(a_hint, b_hint):
        if ca == cb:
            prefix_len += 1
        else:
            break
    if prefix_len >= 4:
        return 0.5, False, True
    return 0.0, False, False


def _compute_compatibility(
    member: NormalizedRecord,
    candidate: NormalizedRecord,
    config: RetrievalConfig,
) -> tuple[float, tuple[str, ...]]:
    """
    Compute a compatibility score between a single case member and a candidate.

    Lower score = better match. The score is a weighted sum of penalties
    minus bonuses for positive signals.
    """
    signals: list[str] = []
    score = 0.0

    date_delta = _date_proximity_days(member.date, candidate.date)
    if date_delta > 0:
        signals.append("date_proximity")
    score += date_delta * config.date_weight

    amount_delta = _amount_proximity_relative(member.amount_paise, candidate.amount_paise)
    if amount_delta > 0:
        signals.append("amount_proximity")
    score += amount_delta * config.amount_weight

    order_bonus, exact, partial = _order_id_match_score(
        member.order_id_hint, candidate.order_id_hint
    )
    if exact:
        signals.append("order_id_exact_match")
        score -= config.order_id_weight * order_bonus
    elif partial:
        signals.append("order_id_partial_match")
        score -= config.order_id_weight * order_bonus

    if member.source_type != candidate.source_type:
        signals.append("cross_source")
        score -= config.cross_source_bonus

    return score, tuple(signals)


def retrieve_candidates(
    layer2_case: "Layer2Case",
    normalized_records: Tuple[NormalizedRecord, ...],
    config: RetrievalConfig | None = None,
) -> RetrievalResult:
    """
    Retrieve a deterministic set of plausible candidate records for a Layer2Case.

    Candidates are scored against each member record using transparent signals:
    date proximity, amount proximity, order ID matching, and cross-source
    compatibility. A candidate's overall score is its best (lowest) score
    across all members. Results are ranked by ascending score, with record_id
    as a tiebreaker for determinism.

    The case's own member records are excluded from the candidate pool.

    Args:
        layer2_case: The unresolved Layer 2 case.
        normalized_records: All available normalized records.
        config: Retrieval configuration. Uses defaults if omitted.

    Returns:
        RetrievalResult with ranked candidates and retrieval metadata.
    """
    if config is None:
        config = RetrievalConfig()

    member_ids = {r.record_id for r in layer2_case.member_records}
    candidate_pool = [
        r for r in normalized_records if r.record_id not in member_ids
    ]

    retrieval_signals = (
        "date_proximity",
        "amount_proximity",
        "order_id_exact_match",
        "order_id_partial_match",
        "cross_source",
    )

    if not candidate_pool:
        return RetrievalResult(
            scenario_id=layer2_case.scenario_id,
            candidates=(),
            candidate_count=0,
            max_candidates=config.max_candidates,
            retrieval_signals_used=retrieval_signals,
        )

    scored: list[tuple[float, NormalizedRecord, tuple[str, ...]]] = []
    for candidate in candidate_pool:
        best_score = float("inf")
        best_signals: tuple[str, ...] = ()
        for member in layer2_case.member_records:
            score, signals = _compute_compatibility(member, candidate, config)
            if score < best_score:
                best_score = score
                best_signals = signals
        scored.append((best_score, candidate, best_signals))

    scored.sort(key=lambda x: (x[0], x[1].record_id))

    top = scored[: config.max_candidates]

    candidates = []
    for rank, (score, candidate, signals) in enumerate(top, start=1):
        candidates.append(
            CandidateRecord(
                record=candidate,
                score=round(score, 4),
                rank=rank,
                match_signals=signals,
                source_type=candidate.source_type,
            )
        )

    return RetrievalResult(
        scenario_id=layer2_case.scenario_id,
        candidates=tuple(candidates),
        candidate_count=len(candidates),
        max_candidates=config.max_candidates,
        retrieval_signals_used=retrieval_signals,
    )
