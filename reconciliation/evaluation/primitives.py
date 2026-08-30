"""
Evaluation primitives for Concord Day 5.

These reusable data structures and helper functions compute the fixed metrics
required by the evaluation harness. They do not import the matching engine
or ground-truth modules; instead they operate on plain ScenarioOutcome and
RecordOutcome records produced by the orchestration layer.

Zero denominators are represented explicitly as None rather than raising or
inventing percentages.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

from reconciliation.evaluation.dataset_generator import EdgeCaseCategory


# ---------------------------------------------------------------------------
# Safe math helpers
# ---------------------------------------------------------------------------


def safe_ratio(numerator: int, denominator: int) -> Optional[float]:
    """Return numerator/denominator, or None if denominator is zero."""
    if denominator == 0:
        return None
    return numerator / denominator


def safe_percentage(numerator: int, denominator: int) -> Optional[float]:
    """Return percentage, or None if denominator is zero."""
    if denominator == 0:
        return None
    return (numerator / denominator) * 100.0


# ---------------------------------------------------------------------------
# Evaluation integrity error
# ---------------------------------------------------------------------------


class EvaluationIntegrityError(ValueError):
    """Raised when record-to-scenario projection is incomplete or inconsistent."""
    pass


# ---------------------------------------------------------------------------
# Scenario-level outcome record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioOutcome:
    """
    Captures the scenario-level evaluation-relevant outcome for one scenario.

    Scenario-level is the correct unit for judging whether a proposed
    reconciliation relationship is correct against ground truth.
    """

    scenario_id: str
    category: EdgeCaseCategory
    is_true_orphan: bool
    has_real_match: bool

    baseline_matched: bool
    baseline_correct: Optional[bool]

    deterministic_matched: bool
    deterministic_correct: bool

    routing_bucket: Optional[str]
    ai_confidence: Optional[float]
    ai_proposal_ids: Tuple[str, ...]
    ai_correct: Optional[bool]

    # Layer 2 outcome type as a string value of ProposalOutcomeType
    # (e.g. "PROPOSAL_VALID", "NO_PROPOSAL", "API_ERROR").  None when
    # no Layer 2 outcome was produced for this scenario.
    layer2_outcome_type: Optional[str] = None

    layer1_time_ms: Optional[float] = None
    layer2_time_ms: Optional[float] = None


# ---------------------------------------------------------------------------
# Record-level outcome record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordOutcome:
    """
    Captures the record-level evaluation-relevant outcome for one record.

    Record-level is the correct unit for operational routing metrics
    (false-accept rate, review-queue composition, exception composition)
    because Layer 3 produces exactly one RoutingDecision per input record
    and a scenario may contain multiple records.
    """

    record_id: str
    scenario_id: str
    category: EdgeCaseCategory
    is_true_orphan: bool
    has_real_match: bool

    routing_bucket: str
    ai_confidence: Optional[float]

    is_false_accept: bool
    exception_correctly_refused: bool
    exception_should_have_been_caught: bool

    layer2_time_ms: Optional[float] = None


# ---------------------------------------------------------------------------
# Metric result data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeterministicMetrics:
    total_scenarios: int
    matched: int
    correct: int
    match_rate: Optional[float]
    precision: Optional[float]


@dataclass(frozen=True)
class AiPrecisionAtThreshold:
    threshold: float
    precision: Optional[float]
    true_positives: int
    false_positives: int
    total: int


@dataclass(frozen=True)
class AiRecallResult:
    recall: Optional[float]
    true_positives: int
    denominator: int
    recall_attempted: Optional[float] = None
    denominator_attempted: int = 0


@dataclass(frozen=True)
class FalseAcceptResult:
    rate: Optional[float]
    count: int
    total_auto_accepted: int


@dataclass(frozen=True)
class ReviewQueueComposition:
    counts: Dict[EdgeCaseCategory, int]
    percentages: Dict[EdgeCaseCategory, Optional[float]]
    total: int


@dataclass(frozen=True)
class ExceptionComposition:
    correctly_refused: int
    should_have_been_caught: int
    correctly_refused_pct: Optional[float]
    should_have_been_caught_pct: Optional[float]
    total: int


@dataclass(frozen=True)
class ThroughputMetrics:
    total_batch_time_ms: float
    layer1_time_ms: float
    layer2_time_ms: float


@dataclass(frozen=True)
class BaselineComparison:
    baseline_match_rate: Optional[float]
    deterministic_match_rate: Optional[float]
    baseline_precision: Optional[float]
    deterministic_precision: Optional[float]
    match_rate_delta: Optional[float]
    precision_delta: Optional[float]


# ---------------------------------------------------------------------------
# Scenario-level metric computation functions
# ---------------------------------------------------------------------------


def compute_deterministic_metrics(
    outcomes: List[ScenarioOutcome],
) -> DeterministicMetrics:
    total = len(outcomes)
    matched = sum(1 for o in outcomes if o.deterministic_matched)
    correct = sum(1 for o in outcomes if o.deterministic_matched and o.deterministic_correct)
    return DeterministicMetrics(
        total_scenarios=total,
        matched=matched,
        correct=correct,
        match_rate=safe_ratio(matched, total),
        precision=safe_ratio(correct, matched),
    )


def compute_ai_precision_at_threshold(
    outcomes: List[ScenarioOutcome],
    threshold: float,
) -> AiPrecisionAtThreshold:
    included = [o for o in outcomes if o.ai_confidence is not None and o.ai_confidence >= threshold]
    true_positives = sum(1 for o in included if o.ai_correct)
    false_positives = sum(1 for o in included if not o.ai_correct)
    return AiPrecisionAtThreshold(
        threshold=threshold,
        precision=safe_ratio(true_positives, true_positives + false_positives),
        true_positives=true_positives,
        false_positives=false_positives,
        total=len(included),
    )


def compute_ai_recall(outcomes: List[ScenarioOutcome]) -> AiRecallResult:
    """Compute AI recall over residual scenarios with real matches.

    Two complementary metrics are returned:

    * **System-wide recall** — denominator is *all* residual scenarios with
      ``has_real_match=True``, regardless of ``layer2_outcome_type``.
      This is the headline operational safety metric: under a total Layer 2
      outage (every residual returns API_ERROR) it reports 0 %, never N/A.

    * **Attempted-only recall** — denominator excludes provider-level
      failures (``API_ERROR``) and only counts scenarios where Layer 2
      actually ran (``PROPOSAL_VALID``, ``NO_PROPOSAL``,
      ``VALIDATION_FAILED``).  This answers "how good is the model when
      it's actually asked" but must never replace the system-wide number.
    """
    residuals_with_real_match = [
        o for o in outcomes
        if not o.deterministic_matched
        and o.has_real_match
    ]
    attempted_outcomes = {"PROPOSAL_VALID", "NO_PROPOSAL", "VALIDATION_FAILED"}
    attempted_with_real_match = [
        o for o in residuals_with_real_match
        if o.layer2_outcome_type in attempted_outcomes
    ]
    true_positives = sum(1 for o in residuals_with_real_match if o.ai_correct)
    return AiRecallResult(
        recall=safe_ratio(true_positives, len(residuals_with_real_match)),
        true_positives=true_positives,
        denominator=len(residuals_with_real_match),
        recall_attempted=safe_ratio(true_positives, len(attempted_with_real_match)),
        denominator_attempted=len(attempted_with_real_match),
    )


def compute_baseline_comparison(
    outcomes: List[ScenarioOutcome],
) -> BaselineComparison:
    total = len(outcomes)
    base_total = sum(1 for o in outcomes if o.baseline_matched)
    base_correct = sum(1 for o in outcomes if o.baseline_matched and o.baseline_correct)
    det_total = sum(1 for o in outcomes if o.deterministic_matched)
    det_correct = sum(1 for o in outcomes if o.deterministic_matched and o.deterministic_correct)

    base_match_rate = safe_ratio(base_total, total)
    det_match_rate = safe_ratio(det_total, total)
    base_precision = safe_ratio(base_correct, base_total)
    det_precision = safe_ratio(det_correct, det_total)

    if base_match_rate is None or det_match_rate is None:
        match_rate_delta = None
    else:
        match_rate_delta = det_match_rate - base_match_rate

    if base_precision is None or det_precision is None:
        precision_delta = None
    else:
        precision_delta = det_precision - base_precision

    return BaselineComparison(
        baseline_match_rate=base_match_rate,
        deterministic_match_rate=det_match_rate,
        baseline_precision=base_precision,
        deterministic_precision=det_precision,
        match_rate_delta=match_rate_delta,
        precision_delta=precision_delta,
    )


# ---------------------------------------------------------------------------
# Record-level projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MissingScenarioOutcome:
    """
    Represents a record whose scenario_id has no corresponding ScenarioOutcome.

    This must never be silently ignored. The evaluation harness must call
    validate_record_outcomes after projection to ensure no records are hidden.
    """

    record_id: str
    scenario_id: str


def build_record_outcomes(
    scenario_outcomes: List[ScenarioOutcome],
    record_scenario_map: Dict[str, str],
    record_routing_map: Dict[str, str],
    record_confidence_map: Optional[Dict[str, Optional[float]]] = None,
    record_layer2_time_ms: Optional[Dict[str, Optional[float]]] = None,
) -> List[Union[RecordOutcome, MissingScenarioOutcome]]:
    """
    Project scenario-level outcomes into per-record outcomes for operational
    routing metrics.

    Returns a list of RecordOutcome and MissingScenarioOutcome. Missing
    scenarios are surfaced explicitly and must be validated before metrics
    are computed.

    The engine must never import this function or the evaluation package.
    """
    scenario_map = {o.scenario_id: o for o in scenario_outcomes}
    record_confidence_map = record_confidence_map or {}
    record_layer2_time_ms = record_layer2_time_ms or {}

    record_outcomes: List[Union[RecordOutcome, MissingScenarioOutcome]] = []
    for record_id, scenario_id in record_scenario_map.items():
        scenario = scenario_map.get(scenario_id)
        if scenario is None:
            record_outcomes.append(MissingScenarioOutcome(record_id=record_id, scenario_id=scenario_id))
            continue

        routing_bucket = record_routing_map.get(record_id, "EXCEPTION")
        ai_confidence = record_confidence_map.get(record_id)
        layer2_ms = record_layer2_time_ms.get(record_id)

        is_false_accept = (
            routing_bucket == "AI_AUTO_ACCEPTED"
            and scenario.ai_correct is False
        )

        is_exception = routing_bucket == "EXCEPTION"
        correctly_refused = is_exception and not scenario.has_real_match
        should_have_been_caught = is_exception and scenario.has_real_match

        record_outcomes.append(
            RecordOutcome(
                record_id=record_id,
                scenario_id=scenario_id,
                category=scenario.category,
                is_true_orphan=scenario.is_true_orphan,
                has_real_match=scenario.has_real_match,
                routing_bucket=routing_bucket,
                ai_confidence=ai_confidence,
                is_false_accept=is_false_accept,
                exception_correctly_refused=correctly_refused,
                exception_should_have_been_caught=should_have_been_caught,
                layer2_time_ms=layer2_ms,
            )
        )

    return record_outcomes


def validate_record_outcomes(
    record_outcomes: List[Union[RecordOutcome, MissingScenarioOutcome]],
) -> None:
    """
    Validate that all projected records have a corresponding ScenarioOutcome.

    Raises EvaluationIntegrityError if any MissingScenarioOutcome is present.
    """
    missing = [o for o in record_outcomes if isinstance(o, MissingScenarioOutcome)]
    if missing:
        details = ", ".join(f"{o.record_id}->{o.scenario_id}" for o in missing)
        raise EvaluationIntegrityError(
            f"Missing ScenarioOutcome for {len(missing)} record(s): {details}"
        )


# ---------------------------------------------------------------------------
# Record-level metric computation functions
# ---------------------------------------------------------------------------


def compute_false_accept_metrics(record_outcomes: List[RecordOutcome]) -> FalseAcceptResult:
    """
    False-accept rate computed at the record level.

    A false accept is an individual record in AI_AUTO_ACCEPTED whose
    originating scenario's AI proposal was wrong against ground truth.
    """
    auto_accepted = [o for o in record_outcomes if o.routing_bucket == "AI_AUTO_ACCEPTED"]
    total_auto_accepted = len(auto_accepted)
    false_accepts = sum(1 for o in auto_accepted if o.is_false_accept)
    return FalseAcceptResult(
        rate=safe_ratio(false_accepts, total_auto_accepted),
        count=false_accepts,
        total_auto_accepted=total_auto_accepted,
    )


def compute_review_queue_composition(record_outcomes: List[RecordOutcome]) -> ReviewQueueComposition:
    """
    Review-queue composition computed at the record level.

    Counts/percentages are over individual records in HUMAN_REVIEW,
    not scenarios.
    """
    review_outcomes = [o for o in record_outcomes if o.routing_bucket == "HUMAN_REVIEW"]
    counts: Dict[EdgeCaseCategory, int] = {cat: 0 for cat in EdgeCaseCategory}
    for o in review_outcomes:
        counts[o.category] += 1
    total = len(review_outcomes)
    percentages = {cat: safe_percentage(counts[cat], total) for cat in EdgeCaseCategory}
    return ReviewQueueComposition(counts=counts, percentages=percentages, total=total)


def compute_exception_composition(record_outcomes: List[RecordOutcome]) -> ExceptionComposition:
    """
    Exception composition computed at the record level.

    Counts/percentages are over individual records in EXCEPTION,
    not scenarios.
    """
    exception_outcomes = [o for o in record_outcomes if o.routing_bucket == "EXCEPTION"]
    correctly_refused = sum(1 for o in exception_outcomes if o.exception_correctly_refused)
    should_have_been_caught = sum(1 for o in exception_outcomes if o.exception_should_have_been_caught)
    total = len(exception_outcomes)
    return ExceptionComposition(
        correctly_refused=correctly_refused,
        should_have_been_caught=should_have_been_caught,
        correctly_refused_pct=safe_percentage(correctly_refused, total),
        should_have_been_caught_pct=safe_percentage(should_have_been_caught, total),
        total=total,
    )


# ---------------------------------------------------------------------------
# Throughput metric computation
# ---------------------------------------------------------------------------


def compute_throughput_metrics(outcomes: List[ScenarioOutcome]) -> ThroughputMetrics:
    """
    Compute throughput metrics.

    Layer 1 is a batch-level operation: if layer1_time_ms is present on any
    outcome, it represents the single batch-level duration and is not summed
    across scenarios.

    Layer 2 is genuinely per-scenario: layer2_time_ms values are summed across
    scenarios to produce the total Layer 2 time.
    """
    layer1_times = [o.layer1_time_ms for o in outcomes if o.layer1_time_ms is not None]
    layer1 = layer1_times[0] if layer1_times else 0.0

    layer2 = sum(o.layer2_time_ms for o in outcomes if o.layer2_time_ms is not None)

    total = layer1 + layer2

    return ThroughputMetrics(
        total_batch_time_ms=total,
        layer1_time_ms=layer1,
        layer2_time_ms=layer2,
    )
