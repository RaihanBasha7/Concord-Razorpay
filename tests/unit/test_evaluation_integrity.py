"""
Tests for evaluation integrity — prevents metric contamination from
partial results and the ai_correct=None bug.

These tests ensure:
- compute_ai_precision_at_threshold does not count ai_correct=None as FP
- compute_ai_recall does not count ai_correct=None as TP
- false accepts are only from AI_AUTO_ACCEPTED scenarios
- guardrail-rejected proposals are excluded from model-quality metrics
- PARTIALLY_OBSERVED scenarios do not inflate precision/recall
"""
from __future__ import annotations

import pytest
from reconciliation.evaluation.dataset_generator import EdgeCaseCategory
from reconciliation.evaluation.primitives import (
    ScenarioOutcome,
    RecordOutcome,
    compute_ai_precision_at_threshold,
    compute_ai_recall,
    compute_false_accept_metrics,
    build_record_outcomes,
    validate_record_outcomes,
)


def _make_scenario(
    scenario_id: str,
    *,
    category: EdgeCaseCategory = EdgeCaseCategory.DUPLICATE,
    has_real_match: bool = True,
    deterministic_matched: bool = False,
    deterministic_correct: bool = False,
    routing_bucket: str = "EXCEPTION",
    ai_confidence: float | None = None,
    ai_proposal_ids: tuple[str, ...] = (),
    ai_correct: bool | None = None,
    layer2_outcome_type: str | None = None,
) -> ScenarioOutcome:
    return ScenarioOutcome(
        scenario_id=scenario_id,
        category=category,
        is_true_orphan=False,
        has_real_match=has_real_match,
        baseline_matched=False,
        baseline_correct=None,
        deterministic_matched=deterministic_matched,
        deterministic_correct=deterministic_correct,
        routing_bucket=routing_bucket,
        ai_confidence=ai_confidence,
        ai_proposal_ids=ai_proposal_ids,
        ai_correct=ai_correct,
        layer2_outcome_type=layer2_outcome_type,
    )


# ────────────────────────────────────────────────────────────────
# 1. Precision: None excluded from FP
# ────────────────────────────────────────────────────────────────

class TestPrecisionExcludesNone:
    """ai_correct=None must NOT be counted as false positive."""

    def test_none_excluded_from_false_positives(self):
        outcomes = [
            _make_scenario("S1", ai_confidence=0.95, ai_correct=True),
            _make_scenario("S2", ai_confidence=0.92, ai_correct=None),
        ]
        result = compute_ai_precision_at_threshold(outcomes, 0.90)
        assert result.true_positives == 1
        assert result.false_positives == 0
        assert result.unknown == 1
        assert result.precision == 1.0  # 1/1, not 1/2

    def test_false_still_counted_as_false_positive(self):
        outcomes = [
            _make_scenario("S1", ai_confidence=0.95, ai_correct=True),
            _make_scenario("S2", ai_confidence=0.92, ai_correct=False),
            _make_scenario("S3", ai_confidence=0.91, ai_correct=None),
        ]
        result = compute_ai_precision_at_threshold(outcomes, 0.90)
        assert result.true_positives == 1
        assert result.false_positives == 1
        assert result.unknown == 1
        assert result.precision == 0.5  # 1/2

    def test_all_none_gives_none_precision(self):
        outcomes = [
            _make_scenario("S1", ai_confidence=0.95, ai_correct=None),
            _make_scenario("S2", ai_confidence=0.92, ai_correct=None),
        ]
        result = compute_ai_precision_at_threshold(outcomes, 0.90)
        assert result.true_positives == 0
        assert result.false_positives == 0
        assert result.unknown == 2
        assert result.precision is None  # 0/0 → None

    def test_enriched_fields_present(self):
        outcomes = [
            _make_scenario("S1", ai_confidence=0.95, ai_correct=True),
            _make_scenario("S2", ai_confidence=0.92, ai_correct=None),
        ]
        result = compute_ai_precision_at_threshold(outcomes, 0.90)
        assert hasattr(result, "evaluated")
        assert hasattr(result, "unknown")
        assert result.evaluated == 1
        assert result.unknown == 1


# ────────────────────────────────────────────────────────────────
# 2. Recall: None excluded from TP
# ────────────────────────────────────────────────────────────────

class TestRecallExcludesNone:
    """ai_correct=None must NOT be counted as true positive in recall."""

    def test_none_not_counted_as_tp(self):
        outcomes = [
            _make_scenario("S1", has_real_match=True, ai_correct=True,
                           layer2_outcome_type="PROPOSAL_VALID"),
            _make_scenario("S2", has_real_match=True, ai_correct=None,
                           layer2_outcome_type="PROPOSAL_VALID"),
        ]
        result = compute_ai_recall(outcomes)
        assert result.true_positives == 1
        assert result.denominator == 2
        assert result.unevaluated_positives == 1
        assert result.evaluated_positives == 1

    def test_api_error_not_in_attempted_denominator(self):
        outcomes = [
            _make_scenario("S1", has_real_match=True, ai_correct=True,
                           layer2_outcome_type="PROPOSAL_VALID"),
            _make_scenario("S2", has_real_match=True, ai_correct=None,
                           layer2_outcome_type="API_ERROR"),
        ]
        result = compute_ai_recall(outcomes)
        assert result.denominator_attempted == 1

    def test_incorrect_counted_separately(self):
        outcomes = [
            _make_scenario("S1", has_real_match=True, ai_correct=True,
                           layer2_outcome_type="PROPOSAL_VALID"),
            _make_scenario("S2", has_real_match=True, ai_correct=False,
                           layer2_outcome_type="PROPOSAL_VALID"),
            _make_scenario("S3", has_real_match=True, ai_correct=None,
                           layer2_outcome_type="NO_PROPOSAL"),
        ]
        result = compute_ai_recall(outcomes)
        assert result.true_positives == 1
        assert result.incorrect_positives == 1
        assert result.unevaluated_positives == 1
        assert result.evaluated_positives == 2


# ────────────────────────────────────────────────────────────────
# 3. False accepts: only AI_AUTO_ACCEPTED
# ────────────────────────────────────────────────────────────────

class TestFalseAcceptOnlyFromAutoAccepted:

    def test_review_incorrect_not_false_accept(self):
        scenarios = [_make_scenario("S1", ai_correct=False, ai_confidence=0.75,
                                     routing_bucket="HUMAN_REVIEW",
                                     layer2_outcome_type="PROPOSAL_VALID")]
        records = [RecordOutcome(record_id="R1", scenario_id="S1",
                                  category=EdgeCaseCategory.DUPLICATE,
                                  is_true_orphan=False, has_real_match=True,
                                  routing_bucket="HUMAN_REVIEW", ai_confidence=0.75,
                                  is_false_accept=False,
                                  exception_correctly_refused=False,
                                  exception_should_have_been_caught=False)]
        result = compute_false_accept_metrics(records)
        assert result.count == 0
        assert result.total_auto_accepted == 0

    def test_exception_incorrect_not_false_accept(self):
        records = [RecordOutcome(record_id="R1", scenario_id="S1",
                                  category=EdgeCaseCategory.DUPLICATE,
                                  is_true_orphan=False, has_real_match=True,
                                  routing_bucket="EXCEPTION", ai_confidence=None,
                                  is_false_accept=False,
                                  exception_correctly_refused=False,
                                  exception_should_have_been_caught=True)]
        result = compute_false_accept_metrics(records)
        assert result.count == 0
        assert result.total_auto_accepted == 0

    def test_auto_accept_correct_not_false_accept(self):
        records = [RecordOutcome(record_id="R1", scenario_id="S1",
                                  category=EdgeCaseCategory.DUPLICATE,
                                  is_true_orphan=False, has_real_match=True,
                                  routing_bucket="AI_AUTO_ACCEPTED", ai_confidence=0.95,
                                  is_false_accept=False,
                                  exception_correctly_refused=False,
                                  exception_should_have_been_caught=False)]
        result = compute_false_accept_metrics(records)
        assert result.count == 0
        assert result.total_auto_accepted == 1

    def test_auto_accept_incorrect_is_false_accept(self):
        records = [RecordOutcome(record_id="R1", scenario_id="S1",
                                  category=EdgeCaseCategory.DUPLICATE,
                                  is_true_orphan=False, has_real_match=True,
                                  routing_bucket="AI_AUTO_ACCEPTED", ai_confidence=0.95,
                                  is_false_accept=True,
                                  exception_correctly_refused=False,
                                  exception_should_have_been_caught=False)]
        result = compute_false_accept_metrics(records)
        assert result.count == 1
        assert result.total_auto_accepted == 1


# ────────────────────────────────────────────────────────────────
# 4. Guardrail-rejected proposals
# ────────────────────────────────────────────────────────────────

class TestGuardrailRejectedExcludedFromModelQuality:

    def test_validation_failed_not_in_correctness_count(self):
        outcomes = [
            _make_scenario("S1", ai_proposal_ids=("R1", "R2"), ai_correct=True,
                           routing_bucket="AI_AUTO_ACCEPTED",
                           layer2_outcome_type="PROPOSAL_VALID"),
            _make_scenario("S2", ai_proposal_ids=("R1", "R2", "R3"), ai_correct=False,
                           routing_bucket="EXCEPTION",
                           layer2_outcome_type="VALIDATION_FAILED"),
        ]
        # Manual filter matching how eval scripts should do it
        proposals_with_known_correctness = [
            so for so in outcomes
            if so.layer2_outcome_type == "PROPOSAL_VALID"
            and so.ai_proposal_ids
            and so.ai_correct is not None
        ]
        assert len(proposals_with_known_correctness) == 1
        assert proposals_with_known_correctness[0].scenario_id == "S1"

    def test_validation_failed_not_auto_accept(self):
        """VALIDATION_FAILED routes to EXCEPTION, never AI_AUTO_ACCEPTED."""
        outcomes = [
            _make_scenario("S1", ai_confidence=0.95, ai_correct=False,
                           routing_bucket="EXCEPTION",
                           layer2_outcome_type="VALIDATION_FAILED"),
        ]
        auto_accepted = [so for so in outcomes if so.routing_bucket == "AI_AUTO_ACCEPTED"]
        assert len(auto_accepted) == 0


# ────────────────────────────────────────────────────────────────
# 5. Partial results cannot enter precision
# ────────────────────────────────────────────────────────────────

class TestPartiallyObservedNotInflatingPrecision:

    def test_prior_results_excluded_from_precision(self):
        outcomes = [
            _make_scenario("S1", ai_confidence=0.95, ai_correct=True),
            _make_scenario("S2", ai_confidence=0.92, ai_correct=True),
            _make_scenario("S3", ai_confidence=0.90, ai_correct=True),
            # Prior results: high confidence but unknown correctness
            _make_scenario("S4", ai_confidence=0.95, ai_correct=None),
            _make_scenario("S5", ai_confidence=0.91, ai_correct=None),
        ]
        result = compute_ai_precision_at_threshold(outcomes, 0.90)
        # Should be 3/3 = 100%, not 3/5 = 60%
        assert result.precision == 1.0
        assert result.true_positives == 3
        assert result.false_positives == 0
        assert result.unknown == 2


# ────────────────────────────────────────────────────────────────
# 6. Partial results cannot enter recall
# ────────────────────────────────────────────────────────────────

class TestPartiallyObservedNotInflatingRecall:

    def test_prior_results_excluded_from_recall_numerator(self):
        outcomes = [
            _make_scenario("S1", has_real_match=True, ai_correct=True,
                           layer2_outcome_type="PROPOSAL_VALID"),
            _make_scenario("S2", has_real_match=True, ai_correct=None,
                           layer2_outcome_type="PROPOSAL_VALID"),
        ]
        result = compute_ai_recall(outcomes)
        # S2 should NOT count as TP
        assert result.true_positives == 1
        assert result.unevaluated_positives == 1

    def test_provider_failure_not_in_attempted(self):
        outcomes = [
            _make_scenario("S1", has_real_match=True, ai_correct=None,
                           layer2_outcome_type="API_ERROR"),
        ]
        result = compute_ai_recall(outcomes)
        assert result.denominator_attempted == 0
        assert result.unevaluated_positives == 1


# ────────────────────────────────────────────────────────────────
# 7. is_false_accept in build_record_outcomes
# ────────────────────────────────────────────────────────────────

class TestRecordOutcomeFalseAccept:

    def test_auto_accept_with_ai_correct_false(self):
        scenario = _make_scenario("S1", ai_correct=False,
                                   routing_bucket="AI_AUTO_ACCEPTED")
        records = build_record_outcomes(
            scenario_outcomes=[scenario],
            record_scenario_map={"R1": "S1"},
            record_routing_map={"R1": "AI_AUTO_ACCEPTED"},
        )
        assert len(records) == 1
        assert records[0].is_false_accept is True

    def test_auto_accept_with_ai_correct_true(self):
        scenario = _make_scenario("S1", ai_correct=True,
                                   routing_bucket="AI_AUTO_ACCEPTED")
        records = build_record_outcomes(
            scenario_outcomes=[scenario],
            record_scenario_map={"R1": "S1"},
            record_routing_map={"R1": "AI_AUTO_ACCEPTED"},
        )
        assert records[0].is_false_accept is False

    def test_auto_accept_with_ai_correct_none(self):
        """When ai_correct is None, is_false_accept must be False, not True."""
        scenario = _make_scenario("S1", ai_correct=None,
                                   routing_bucket="AI_AUTO_ACCEPTED")
        records = build_record_outcomes(
            scenario_outcomes=[scenario],
            record_scenario_map={"R1": "S1"},
            record_routing_map={"R1": "AI_AUTO_ACCEPTED"},
        )
        # is_false_accept uses `scenario.ai_correct is False`, so None → False
        assert records[0].is_false_accept is False
