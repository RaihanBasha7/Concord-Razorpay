"""
Metrics computation for Layer 1 evaluation.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from reconciliation.domain.models import MatchRule, ReconciliationDecision
from reconciliation.evaluation.dataset_generator import (
    CATEGORY_QUOTAS,
    EdgeCaseCategory,
    ExpectedLayer1Outcome,
    GroundTruthScenario,
)
from reconciliation.evaluation.ground_truth import GroundTruthUnit


@dataclass(frozen=True)
class ScenarioEvaluation:
    scenario_id: str
    category: EdgeCaseCategory
    expected_outcome: ExpectedLayer1Outcome
    layer1_decision: Optional[ReconciliationDecision]
    matched: bool
    correct: bool
    incorrect_reason: Optional[str] = None


@dataclass(frozen=True)
class CategoryMetrics:
    category: EdgeCaseCategory
    total: int
    correctly_resolved: int
    coverage: float
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    precision: Optional[float]
    recall: float


@dataclass(frozen=True)
class EvaluationReport:
    total_scenarios: int
    overall_coverage: float
    overall_precision: Optional[float]
    category_metrics: Tuple[CategoryMetrics, ...]
    scenario_evaluations: Tuple[ScenarioEvaluation, ...]
    incorrect_matches: Tuple[ScenarioEvaluation, ...]
    layer1_false_negative_ids: Tuple[str, ...]
    pipeline_residual_scenario_ids: Tuple[str, ...]
    true_exception_scenario_ids: Tuple[str, ...]


def evaluate(
    scenarios: List[GroundTruthScenario],
    decisions: List[ReconciliationDecision],
    residual_record_ids: List[str],
    scenario_units: List[GroundTruthUnit],
) -> EvaluationReport:
    # Map record_id -> scenario_id via ground truth units
    record_to_scenario: Dict[str, str] = {}
    scenario_to_records: Dict[str, List[str]] = defaultdict(list)
    for unit in scenario_units:
        for rid in unit.member_record_ids:
            record_to_scenario[rid] = unit.scenario_id
            scenario_to_records[unit.scenario_id].append(rid)

    # Map scenario_id -> decision
    scenario_to_decision: Dict[str, ReconciliationDecision] = {}
    for decision in decisions:
        for rid in decision.member_record_ids:
            scen_id = record_to_scenario.get(rid)
            if scen_id:
                scenario_to_decision[scen_id] = decision
                break

    scenario_map = {s.scenario_id: s for s in scenarios}
    evaluations: List[ScenarioEvaluation] = []

    tp = fp = tn = fn = 0
    cat_stats: Dict[EdgeCaseCategory, Dict[str, int]] = defaultdict(
        lambda: {"total": 0, "tp": 0, "fp": 0, "tn": 0, "fn": 0, "correct": 0}
    )

    incorrect: List[ScenarioEvaluation] = []
    layer1_false_negatives: List[str] = []
    pipeline_residual_scenarios: List[str] = []
    true_exception_scenarios: List[str] = []

    for unit in scenario_units:
        scen = scenario_map.get(unit.scenario_id)
        if not scen:
            continue
        expected = scen.expected_outcome
        decision = scenario_to_decision.get(unit.scenario_id)
        matched = decision is not None

        if expected == ExpectedLayer1Outcome.MATCH_EXACT_ID:
            correct = matched and decision.rule_or_rationale == MatchRule.EXACT_ID.value
        elif expected == ExpectedLayer1Outcome.MATCH_AMOUNT_DATE:
            correct = matched and decision.rule_or_rationale == MatchRule.AMOUNT_AND_DATE.value
        else:
            correct = not matched

        reason = None
        if matched and not correct:
            if expected == ExpectedLayer1Outcome.MATCH_EXACT_ID:
                reason = f"Expected EXACT_ID but got {decision.rule_or_rationale}"
            elif expected == ExpectedLayer1Outcome.MATCH_AMOUNT_DATE:
                reason = f"Expected AMOUNT_AND_DATE but got {decision.rule_or_rationale}"
            else:
                reason = f"Expected no match but got {decision.rule_or_rationale}"
            incorrect.append(
                ScenarioEvaluation(
                    scenario_id=unit.scenario_id,
                    category=scen.category,
                    expected_outcome=expected,
                    layer1_decision=decision,
                    matched=matched,
                    correct=False,
                    incorrect_reason=reason,
                )
            )

        if expected in (
            ExpectedLayer1Outcome.MATCH_EXACT_ID,
            ExpectedLayer1Outcome.MATCH_AMOUNT_DATE,
        ):
            if matched and correct:
                tp += 1
                cat_stats[scen.category]["tp"] += 1
                cat_stats[scen.category]["correct"] += 1
            elif matched and not correct:
                fp += 1
                cat_stats[scen.category]["fp"] += 1
            else:
                fn += 1
                cat_stats[scen.category]["fn"] += 1
                layer1_false_negatives.append(unit.scenario_id)
                pipeline_residual_scenarios.append(unit.scenario_id)
        else:
            if not matched:
                tn += 1
                cat_stats[scen.category]["tn"] += 1
                cat_stats[scen.category]["correct"] += 1
                pipeline_residual_scenarios.append(unit.scenario_id)
                if unit.is_true_orphan:
                    true_exception_scenarios.append(unit.scenario_id)
            else:
                fp += 1
                cat_stats[scen.category]["fp"] += 1
                incorrect.append(
                    ScenarioEvaluation(
                        scenario_id=unit.scenario_id,
                        category=scen.category,
                        expected_outcome=expected,
                        layer1_decision=decision,
                        matched=matched,
                        correct=False,
                        incorrect_reason=reason or "Expected no match but Layer 1 matched",
                    )
                )

        cat_stats[scen.category]["total"] += 1

        evaluations.append(
            ScenarioEvaluation(
                scenario_id=unit.scenario_id,
                category=scen.category,
                expected_outcome=expected,
                layer1_decision=decision,
                matched=matched,
                correct=correct,
                incorrect_reason=reason,
            )
        )

    # Build per-category metrics
    category_metrics = []
    for cat in EdgeCaseCategory:
        stats = cat_stats[cat]
        total = stats["total"]
        correct = stats["correct"]
        coverage = correct / total if total > 0 else 0.0
        cat_tp = stats["tp"]
        cat_fp = stats["fp"]
        cat_tn = stats["tn"]
        cat_fn = stats["fn"]
        cat_precision = cat_tp / (cat_tp + cat_fp) if (cat_tp + cat_fp) > 0 else None
        cat_recall = cat_tp / (cat_tp + cat_fn) if (cat_tp + cat_fn) > 0 else 0.0
        category_metrics.append(
            CategoryMetrics(
                category=cat,
                total=total,
                correctly_resolved=correct,
                coverage=coverage,
                true_positives=cat_tp,
                false_positives=cat_fp,
                true_negatives=cat_tn,
                false_negatives=cat_fn,
                precision=cat_precision,
                recall=cat_recall,
            )
        )

    overall_coverage = sum(m.correctly_resolved for m in category_metrics) / len(evaluations) if evaluations else 0.0
    overall_precision = tp / (tp + fp) if (tp + fp) > 0 else None

    return EvaluationReport(
        total_scenarios=len(evaluations),
        overall_coverage=overall_coverage,
        overall_precision=overall_precision,
        category_metrics=tuple(category_metrics),
        scenario_evaluations=tuple(evaluations),
        incorrect_matches=tuple(incorrect),
        layer1_false_negative_ids=tuple(sorted(layer1_false_negatives)),
        pipeline_residual_scenario_ids=tuple(sorted(pipeline_residual_scenarios)),
        true_exception_scenario_ids=tuple(sorted(true_exception_scenarios)),
    )
