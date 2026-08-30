"""
Day 5.2 tests: evaluation primitives.
"""
from __future__ import annotations

from datetime import date

import pytest

from reconciliation.baseline.naive_matcher import NaiveMatchResult, match
from reconciliation.domain.models import SourceType
from reconciliation.evaluation.dataset_generator import (
    EdgeCaseCategory,
    ExpectedLayer1Outcome,
    GroundTruthScenario,
    ScenarioRecordSpec,
    _compute_record_id,
    generate_dataset,
)
from reconciliation.evaluation.ground_truth import GroundTruthUnit
from reconciliation.evaluation.primitives import (
    AiPrecisionAtThreshold,
    AiRecallResult,
    BaselineComparison,
    DeterministicMetrics,
    EvaluationIntegrityError,
    ExceptionComposition,
    FalseAcceptResult,
    MissingScenarioOutcome,
    RecordOutcome,
    ReviewQueueComposition,
    ScenarioOutcome,
    ThroughputMetrics,
    build_record_outcomes,
    compute_ai_precision_at_threshold,
    compute_ai_recall,
    compute_baseline_comparison,
    compute_deterministic_metrics,
    compute_exception_composition,
    compute_false_accept_metrics,
    compute_review_queue_composition,
    compute_throughput_metrics,
    safe_percentage,
    safe_ratio,
    validate_record_outcomes,
)
from reconciliation.matcher_config import MatcherConfig
from tests.conftest import make_record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_outcome(
    scenario_id: str,
    category: EdgeCaseCategory,
    *,
    is_true_orphan: bool = False,
    has_real_match: bool = True,
    baseline_matched: bool = False,
    baseline_correct: Optional[bool] = None,
    deterministic_matched: bool = False,
    deterministic_correct: bool = False,
    routing_bucket: Optional[str] = None,
    ai_confidence: Optional[float] = None,
    ai_proposal_ids: Tuple[str, ...] = (),
    ai_correct: Optional[bool] = None,
    layer1_time_ms: Optional[float] = None,
    layer2_time_ms: Optional[float] = None,
) -> ScenarioOutcome:
    return ScenarioOutcome(
        scenario_id=scenario_id,
        category=category,
        is_true_orphan=is_true_orphan,
        has_real_match=has_real_match,
        baseline_matched=baseline_matched,
        baseline_correct=baseline_correct,
        deterministic_matched=deterministic_matched,
        deterministic_correct=deterministic_correct,
        routing_bucket=routing_bucket,
        ai_confidence=ai_confidence,
        ai_proposal_ids=ai_proposal_ids,
        ai_correct=ai_correct,
        layer1_time_ms=layer1_time_ms,
        layer2_time_ms=layer2_time_ms,
    )


def _make_record_outcome(
    record_id: str,
    scenario_id: str,
    category: EdgeCaseCategory,
    *,
    is_true_orphan: bool = False,
    has_real_match: bool = True,
    routing_bucket: str = "EXCEPTION",
    ai_confidence: Optional[float] = None,
    is_false_accept: bool = False,
    exception_correctly_refused: bool = False,
    exception_should_have_been_caught: bool = False,
    layer2_time_ms: Optional[float] = None,
) -> RecordOutcome:
    return RecordOutcome(
        record_id=record_id,
        scenario_id=scenario_id,
        category=category,
        is_true_orphan=is_true_orphan,
        has_real_match=has_real_match,
        routing_bucket=routing_bucket,
        ai_confidence=ai_confidence,
        is_false_accept=is_false_accept,
        exception_correctly_refused=exception_correctly_refused,
        exception_should_have_been_caught=exception_should_have_been_caught,
        layer2_time_ms=layer2_time_ms,
    )


# ---------------------------------------------------------------------------
# Safe math tests
# ---------------------------------------------------------------------------


class TestSafeRatio:
    def test_normal_division(self):
        assert safe_ratio(3, 4) == 0.75

    def test_zero_numerator(self):
        assert safe_ratio(0, 4) == 0.0

    def test_zero_denominator_returns_none(self):
        assert safe_ratio(3, 0) is None

    def test_zero_both_returns_none(self):
        assert safe_ratio(0, 0) is None


class TestSafePercentage:
    def test_normal_percentage(self):
        assert safe_percentage(25, 100) == 25.0

    def test_zero_denominator_returns_none(self):
        assert safe_percentage(0, 0) is None


# ---------------------------------------------------------------------------
# Scenario-level metric tests
# ---------------------------------------------------------------------------


class TestDeterministicMetrics:
    def test_perfect_coverage(self):
        outcomes = [
            _make_outcome("S1", EdgeCaseCategory.EXACT_MATCH, deterministic_matched=True, deterministic_correct=True),
            _make_outcome("S2", EdgeCaseCategory.T_PLUS_DELAY, deterministic_matched=True, deterministic_correct=True),
        ]
        metrics = compute_deterministic_metrics(outcomes)
        assert metrics.total_scenarios == 2
        assert metrics.matched == 2
        assert metrics.correct == 2
        assert metrics.match_rate == 1.0
        assert metrics.precision == 1.0

    def test_zero_denominator_for_precision(self):
        outcomes = [
            _make_outcome("S1", EdgeCaseCategory.TRUE_ORPHAN, deterministic_matched=False, deterministic_correct=True),
        ]
        metrics = compute_deterministic_metrics(outcomes)
        assert metrics.match_rate == 0.0
        assert metrics.precision is None

    def test_incorrect_deterministic_match_is_counted(self):
        outcomes = [
            _make_outcome("S1", EdgeCaseCategory.EXACT_MATCH, deterministic_matched=True, deterministic_correct=False),
        ]
        metrics = compute_deterministic_metrics(outcomes)
        assert metrics.matched == 1
        assert metrics.correct == 0
        assert metrics.match_rate == 1.0
        assert metrics.precision == 0.0


class TestAiPrecisionAtThreshold:
    def test_threshold_boundary_inclusion(self):
        outcomes = [
            _make_outcome("S1", EdgeCaseCategory.EXACT_MATCH, routing_bucket="AI_AUTO_ACCEPTED", ai_confidence=0.90, ai_correct=True),
            _make_outcome("S2", EdgeCaseCategory.EXACT_MATCH, routing_bucket="AI_AUTO_ACCEPTED", ai_confidence=0.89, ai_correct=True),
        ]
        at_090 = compute_ai_precision_at_threshold(outcomes, 0.90)
        assert at_090.threshold == 0.90
        assert at_090.precision == 1.0
        assert at_090.true_positives == 1
        assert at_090.false_positives == 0
        assert at_090.total == 1

    def test_zero_denominator(self):
        outcomes = [
            _make_outcome("S1", EdgeCaseCategory.EXACT_MATCH, routing_bucket="HUMAN_REVIEW", ai_confidence=0.75, ai_correct=True),
        ]
        at_090 = compute_ai_precision_at_threshold(outcomes, 0.90)
        assert at_090.precision is None
        assert at_090.total == 0


class TestAiRecall:
    def test_recall_counts_residuals_with_real_match(self):
        outcomes = [
            _make_outcome("S1", EdgeCaseCategory.EXACT_MATCH, deterministic_matched=True, has_real_match=True),
            _make_outcome("S2", EdgeCaseCategory.TRUE_ORPHAN, deterministic_matched=False, has_real_match=False),
            _make_outcome("S3", EdgeCaseCategory.FEE_DEDUCTED, deterministic_matched=False, has_real_match=True, ai_correct=True),
        ]
        recall = compute_ai_recall(outcomes)
        assert recall.denominator == 1
        assert recall.true_positives == 1
        assert recall.recall == 1.0

    def test_recall_zero_denominator(self):
        outcomes = [
            _make_outcome("S1", EdgeCaseCategory.TRUE_ORPHAN, deterministic_matched=False, has_real_match=False),
        ]
        recall = compute_ai_recall(outcomes)
        assert recall.recall is None
        assert recall.denominator == 0
        assert recall.true_positives == 0


class TestBaselineComparison:
    def test_deltas_computed(self):
        outcomes = [
            _make_outcome("S1", EdgeCaseCategory.EXACT_MATCH, baseline_matched=True, baseline_correct=True, deterministic_matched=True, deterministic_correct=True),
            _make_outcome("S2", EdgeCaseCategory.TRUE_ORPHAN, baseline_matched=False, deterministic_matched=False),
        ]
        comparison = compute_baseline_comparison(outcomes)
        assert comparison.baseline_match_rate == 0.5
        assert comparison.deterministic_match_rate == 0.5
        assert comparison.baseline_precision == 1.0
        assert comparison.deterministic_precision == 1.0
        assert comparison.match_rate_delta == 0.0
        assert comparison.precision_delta == 0.0

    def test_zero_denominator_for_baseline_precision(self):
        outcomes = [
            _make_outcome("S1", EdgeCaseCategory.TRUE_ORPHAN, baseline_matched=False, deterministic_matched=False),
        ]
        comparison = compute_baseline_comparison(outcomes)
        assert comparison.baseline_precision is None
        assert comparison.precision_delta is None


# ---------------------------------------------------------------------------
# Record-level metric tests
# ---------------------------------------------------------------------------


class TestRecordLevelFalseAccept:
    def test_false_accept_counted_single_record(self):
        records = [
            _make_record_outcome("R1", "S1", EdgeCaseCategory.TRUE_ORPHAN, routing_bucket="AI_AUTO_ACCEPTED", is_false_accept=True),
            _make_record_outcome("R2", "S2", EdgeCaseCategory.EXACT_MATCH, routing_bucket="AI_AUTO_ACCEPTED", is_false_accept=False),
        ]
        metrics = compute_false_accept_metrics(records)
        assert metrics.count == 1
        assert metrics.total_auto_accepted == 2
        assert metrics.rate == 0.5

    def test_false_accept_multi_record_scenario_not_double_counted(self):
        """
        A multi-record scenario where the model proposed 3 records and the
        proposal was wrong. All 3 are false accepts, but the scenario should
        not be counted 3 times in a scenario-level metric. At the record level
        each record is counted exactly once.
        """
        records = [
            _make_record_outcome("R1", "S1", EdgeCaseCategory.SPLIT_SETTLEMENT, routing_bucket="AI_AUTO_ACCEPTED", is_false_accept=True),
            _make_record_outcome("R2", "S1", EdgeCaseCategory.SPLIT_SETTLEMENT, routing_bucket="AI_AUTO_ACCEPTED", is_false_accept=True),
            _make_record_outcome("R3", "S1", EdgeCaseCategory.SPLIT_SETTLEMENT, routing_bucket="AI_AUTO_ACCEPTED", is_false_accept=True),
            _make_record_outcome("R4", "S1", EdgeCaseCategory.SPLIT_SETTLEMENT, routing_bucket="EXCEPTION", is_false_accept=False),
        ]
        metrics = compute_false_accept_metrics(records)
        assert metrics.total_auto_accepted == 3
        assert metrics.count == 3
        assert metrics.rate == 1.0

    def test_zero_denominator_for_false_accept_rate(self):
        records = [
            _make_record_outcome("R1", "S1", EdgeCaseCategory.EXACT_MATCH, routing_bucket="HUMAN_REVIEW"),
        ]
        metrics = compute_false_accept_metrics(records)
        assert metrics.rate is None
        assert metrics.count == 0
        assert metrics.total_auto_accepted == 0


class TestRecordLevelReviewQueueComposition:
    def test_counts_and_percentages_reconcile(self):
        records = [
            _make_record_outcome("R1", "S1", EdgeCaseCategory.EXACT_MATCH, routing_bucket="HUMAN_REVIEW"),
            _make_record_outcome("R2", "S2", EdgeCaseCategory.TRUE_ORPHAN, routing_bucket="HUMAN_REVIEW"),
            _make_record_outcome("R3", "S2", EdgeCaseCategory.TRUE_ORPHAN, routing_bucket="HUMAN_REVIEW"),
        ]
        composition = compute_review_queue_composition(records)
        assert composition.total == 3
        assert composition.counts[EdgeCaseCategory.TRUE_ORPHAN] == 2
        assert composition.counts[EdgeCaseCategory.EXACT_MATCH] == 1
        assert composition.percentages[EdgeCaseCategory.TRUE_ORPHAN] == pytest.approx(2 / 3 * 100)
        assert composition.percentages[EdgeCaseCategory.EXACT_MATCH] == pytest.approx(1 / 3 * 100)

    def test_multi_record_scenario_contributions_counted_individually(self):
        """
        A scenario with 3 records where 2 are in HUMAN_REVIEW must contribute
        2 to the review queue, not 1.
        """
        records = [
            _make_record_outcome("R1", "S1", EdgeCaseCategory.FEE_DEDUCTED, routing_bucket="AI_AUTO_ACCEPTED"),
            _make_record_outcome("R2", "S1", EdgeCaseCategory.FEE_DEDUCTED, routing_bucket="HUMAN_REVIEW"),
            _make_record_outcome("R3", "S1", EdgeCaseCategory.FEE_DEDUCTED, routing_bucket="HUMAN_REVIEW"),
        ]
        composition = compute_review_queue_composition(records)
        assert composition.total == 2
        assert composition.counts[EdgeCaseCategory.FEE_DEDUCTED] == 2

    def test_empty_review_queue_returns_none_percentages(self):
        records = [
            _make_record_outcome("R1", "S1", EdgeCaseCategory.EXACT_MATCH, routing_bucket="AI_AUTO_ACCEPTED"),
        ]
        composition = compute_review_queue_composition(records)
        assert composition.total == 0
        assert all(v is None for v in composition.percentages.values())


class TestRecordLevelExceptionComposition:
    def test_correctly_refused_counted(self):
        records = [
            _make_record_outcome("R1", "S1", EdgeCaseCategory.TRUE_ORPHAN, routing_bucket="EXCEPTION", exception_correctly_refused=True),
            _make_record_outcome("R2", "S2", EdgeCaseCategory.LATE_ARRIVING, routing_bucket="EXCEPTION", exception_correctly_refused=True),
            _make_record_outcome("R3", "S3", EdgeCaseCategory.FEE_DEDUCTED, has_real_match=True, routing_bucket="EXCEPTION", exception_should_have_been_caught=True),
        ]
        composition = compute_exception_composition(records)
        assert composition.correctly_refused == 2
        assert composition.should_have_been_caught == 1
        assert composition.total == 3
        assert composition.correctly_refused_pct == pytest.approx(2 / 3 * 100)
        assert composition.should_have_been_caught_pct == pytest.approx(1 / 3 * 100)

    def test_multi_record_scenario_exception_contributions_counted_individually(self):
        """
        A scenario with 3 records all in EXCEPTION must contribute 3 to the
        exception count, not 1.
        """
        records = [
            _make_record_outcome("R1", "S1", EdgeCaseCategory.SPLIT_SETTLEMENT, has_real_match=True, routing_bucket="EXCEPTION", exception_should_have_been_caught=True),
            _make_record_outcome("R2", "S1", EdgeCaseCategory.SPLIT_SETTLEMENT, has_real_match=True, routing_bucket="EXCEPTION", exception_should_have_been_caught=True),
            _make_record_outcome("R3", "S1", EdgeCaseCategory.SPLIT_SETTLEMENT, has_real_match=True, routing_bucket="EXCEPTION", exception_should_have_been_caught=True),
        ]
        composition = compute_exception_composition(records)
        assert composition.total == 3
        assert composition.should_have_been_caught == 3

    def test_zero_denominator(self):
        records = [
            _make_record_outcome("R1", "S1", EdgeCaseCategory.EXACT_MATCH, routing_bucket="AI_AUTO_ACCEPTED"),
        ]
        composition = compute_exception_composition(records)
        assert composition.total == 0
        assert composition.correctly_refused_pct is None
        assert composition.should_have_been_caught_pct is None


class TestThroughputMetrics:
    def test_layer1_not_double_counted_across_scenarios(self):
        """
        Layer 1 is batch-level. If the same duration is attached to every
        scenario outcome, it must not be summed.
        """
        outcomes = [
            _make_outcome("S1", EdgeCaseCategory.EXACT_MATCH, layer1_time_ms=10.0, layer2_time_ms=20.0),
            _make_outcome("S2", EdgeCaseCategory.TRUE_ORPHAN, layer1_time_ms=10.0, layer2_time_ms=15.0),
        ]
        metrics = compute_throughput_metrics(outcomes)
        assert metrics.layer1_time_ms == 10.0
        assert metrics.layer2_time_ms == 35.0
        assert metrics.total_batch_time_ms == 45.0

    def test_layer2_summed_across_scenarios(self):
        outcomes = [
            _make_outcome("S1", EdgeCaseCategory.EXACT_MATCH, layer2_time_ms=20.0),
            _make_outcome("S2", EdgeCaseCategory.TRUE_ORPHAN, layer2_time_ms=15.0),
        ]
        metrics = compute_throughput_metrics(outcomes)
        assert metrics.layer2_time_ms == 35.0

    def test_no_layer1_time_uses_zero(self):
        outcomes = [
            _make_outcome("S1", EdgeCaseCategory.EXACT_MATCH, layer2_time_ms=20.0),
        ]
        metrics = compute_throughput_metrics(outcomes)
        assert metrics.layer1_time_ms == 0.0
        assert metrics.total_batch_time_ms == 20.0

    def test_empty_outcomes_uses_zero(self):
        metrics = compute_throughput_metrics([])
        assert metrics.layer1_time_ms == 0.0
        assert metrics.layer2_time_ms == 0.0
        assert metrics.total_batch_time_ms == 0.0


# ---------------------------------------------------------------------------
# Record outcome projection tests
# ---------------------------------------------------------------------------


class TestBuildRecordOutcomes:
    def test_projection_preserves_scenario_flags(self):
        scenario_outcomes = [
            _make_outcome(
                "S1",
                EdgeCaseCategory.TRUE_ORPHAN,
                is_true_orphan=True,
                has_real_match=False,
                routing_bucket="EXCEPTION",
                ai_correct=False,
            ),
        ]
        record_scenario_map = {"R1": "S1"}
        record_routing_map = {"R1": "EXCEPTION"}

        records = build_record_outcomes(scenario_outcomes, record_scenario_map, record_routing_map)
        validate_record_outcomes(records)
        assert len(records) == 1
        r = records[0]
        assert isinstance(r, RecordOutcome)
        assert r.is_true_orphan is True
        assert r.has_real_match is False
        assert r.exception_correctly_refused is True
        assert r.exception_should_have_been_caught is False
        assert r.is_false_accept is False

    def test_false_accept_projected_from_scenario_ai_correct(self):
        scenario_outcomes = [
            _make_outcome(
                "S1",
                EdgeCaseCategory.EXACT_MATCH,
                routing_bucket="AI_AUTO_ACCEPTED",
                ai_correct=False,
            ),
        ]
        record_scenario_map = {"R1": "S1"}
        record_routing_map = {"R1": "AI_AUTO_ACCEPTED"}

        records = build_record_outcomes(scenario_outcomes, record_scenario_map, record_routing_map)
        validate_record_outcomes(records)
        assert records[0].is_false_accept is True

    def test_missing_scenario_produces_explicit_missing_outcome(self):
        """
        A record whose scenario_id has no ScenarioOutcome must not be silently
        skipped. It must be surfaced as MissingScenarioOutcome and cause
        validation to fail.
        """
        scenario_outcomes = [
            _make_outcome("S1", EdgeCaseCategory.EXACT_MATCH),
        ]
        record_scenario_map = {"R1": "S1", "R2": "MISSING"}
        record_routing_map = {"R1": "AI_AUTO_ACCEPTED", "R2": "AI_AUTO_ACCEPTED"}

        records = build_record_outcomes(scenario_outcomes, record_scenario_map, record_routing_map)
        assert len(records) == 2
        assert isinstance(records[0], RecordOutcome)
        assert isinstance(records[1], MissingScenarioOutcome)
        assert records[1].record_id == "R2"
        assert records[1].scenario_id == "MISSING"

    def test_missing_scenario_cannot_silently_reduce_evaluated_count(self):
        """
        validate_record_outcomes must raise EvaluationIntegrityError when any
        MissingScenarioOutcome is present, preventing silent denominator
        reduction.
        """
        scenario_outcomes = [
            _make_outcome("S1", EdgeCaseCategory.EXACT_MATCH),
        ]
        record_scenario_map = {"R1": "S1", "R2": "MISSING"}
        record_routing_map = {"R1": "AI_AUTO_ACCEPTED", "R2": "AI_AUTO_ACCEPTED"}

        records = build_record_outcomes(scenario_outcomes, record_scenario_map, record_routing_map)
        with pytest.raises(EvaluationIntegrityError, match="R2->MISSING"):
            validate_record_outcomes(records)

    def test_validation_error_identifies_all_missing_records(self):
        scenario_outcomes = [
            _make_outcome("S1", EdgeCaseCategory.EXACT_MATCH),
        ]
        record_scenario_map = {
            "R1": "S1",
            "R2": "MISSING_A",
            "R3": "MISSING_B",
        }
        record_routing_map = {
            "R1": "AI_AUTO_ACCEPTED",
            "R2": "AI_AUTO_ACCEPTED",
            "R3": "AI_AUTO_ACCEPTED",
        }

        records = build_record_outcomes(scenario_outcomes, record_scenario_map, record_routing_map)
        with pytest.raises(EvaluationIntegrityError, match="R2->MISSING_A, R3->MISSING_B"):
            validate_record_outcomes(records)

    def test_multi_record_scenario_produces_multiple_record_outcomes(self):
        scenario_outcomes = [
            _make_outcome(
                "S1",
                EdgeCaseCategory.SPLIT_SETTLEMENT,
                has_real_match=True,
                routing_bucket="EXCEPTION",
                ai_correct=False,
            ),
        ]
        record_scenario_map = {
            "R1": "S1",
            "R2": "S1",
            "R3": "S1",
        }
        record_routing_map = {
            "R1": "AI_AUTO_ACCEPTED",
            "R2": "HUMAN_REVIEW",
            "R3": "EXCEPTION",
        }

        records = build_record_outcomes(scenario_outcomes, record_scenario_map, record_routing_map)
        validate_record_outcomes(records)
        assert len(records) == 3
        buckets = {r.record_id: r.routing_bucket for r in records if isinstance(r, RecordOutcome)}
        assert buckets == {"R1": "AI_AUTO_ACCEPTED", "R2": "HUMAN_REVIEW", "R3": "EXCEPTION"}
        assert records[0].is_false_accept is True
        assert records[2].exception_should_have_been_caught is True

    def test_exception_composition_categories_sum_to_total(self):
        """
        Regression: correctly_refused + should_have_been_caught must equal
        total EXCEPTION records. Previously, INCONSISTENT_NARRATION and
        ROUNDING_DIFFERENCE residual scenarios (has_real_match=False) were
        misclassified as neither, creating a 30-record gap.
        """
        scenario_outcomes = [
            # TRUE_ORPHAN: has_real_match=False → correctly_refused
            _make_outcome(
                "S1", EdgeCaseCategory.TRUE_ORPHAN,
                has_real_match=False, routing_bucket="EXCEPTION",
            ),
            # INCONSISTENT_NARRATION: has_real_match=False → correctly_refused
            _make_outcome(
                "S2", EdgeCaseCategory.INCONSISTENT_NARRATION,
                has_real_match=False, routing_bucket="EXCEPTION",
            ),
            # FEE_DEDUCTED: has_real_match=True → should_have_been_caught
            _make_outcome(
                "S3", EdgeCaseCategory.FEE_DEDUCTED,
                has_real_match=True, routing_bucket="EXCEPTION",
                ai_correct=False,
            ),
            # SPLIT_SETTLEMENT: has_real_match=True → should_have_been_caught
            _make_outcome(
                "S4", EdgeCaseCategory.SPLIT_SETTLEMENT,
                has_real_match=True, routing_bucket="EXCEPTION",
                ai_correct=False,
            ),
        ]
        record_scenario_map = {
            "R1": "S1",
            "R2": "S2", "R3": "S2",
            "R4": "S3", "R5": "S3",
            "R6": "S4", "R7": "S4", "R8": "S4",
        }
        record_routing_map = {
            "R1": "EXCEPTION",
            "R2": "EXCEPTION", "R3": "EXCEPTION",
            "R4": "EXCEPTION", "R5": "EXCEPTION",
            "R6": "EXCEPTION", "R7": "EXCEPTION", "R8": "EXCEPTION",
        }

        records = build_record_outcomes(
            scenario_outcomes, record_scenario_map, record_routing_map,
        )
        validate_record_outcomes(records)
        exc_records = [r for r in records if isinstance(r, RecordOutcome) and r.routing_bucket == "EXCEPTION"]
        assert len(exc_records) == 8

        cr = sum(1 for r in exc_records if r.exception_correctly_refused)
        shbc = sum(1 for r in exc_records if r.exception_should_have_been_caught)
        assert cr + shbc == len(exc_records), (
            f"Exception composition gap: {len(exc_records) - cr - shbc} records "
            f"are neither correctly_refused ({cr}) nor should_have_been_caught ({shbc})"
        )
        # S1 (orphan, 1 record) + S2 (narration, 2 records) = 3 correctly refused
        assert cr == 3
        # S3 (fee, 2 records) + S4 (split, 3 records) = 5 should_have_been_caught
        assert shbc == 5
