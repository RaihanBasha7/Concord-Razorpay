"""
Day 4.6 — Layer 2 evaluation harness.

Runs the full Layer 2 pipeline (proposal + validation + routing) against the
residual set from Layer 1 and scores routing decisions against ground truth.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from reconciliation.domain.models import SourceType
from reconciliation.evaluation.dataset_generator import (
    EdgeCaseCategory,
    ExpectedLayer1Outcome,
    GeneratedDataset,
    GroundTruthScenario,
    _compute_record_id,
    generate_dataset,
    write_dataset,
)
from reconciliation.evaluation.evaluation_harness import build_ground_truth_units
from reconciliation.evaluation.ground_truth import GroundTruthUnit
from reconciliation.layer2 import Layer2Case, reconstruct_layer2_case
from reconciliation.loader import load_normalized_records, load_residuals
from reconciliation.matcher import reconcile
from reconciliation.matcher_config import MatcherConfig
from reconciliation.proposal_orchestration import ProposalOutcome, ProposalOrchestrator
from reconciliation.proposal_validation import ProposalOutcomeType
from reconciliation.retrieval import RetrievalConfig, RetrievalResult, retrieve_candidates
from reconciliation.routing import RoutingDecision, route


@dataclass(frozen=True)
class Layer2RoutingEvaluation:
    scenario_id: str
    category: EdgeCaseCategory
    is_true_orphan: bool
    layer1_expected: ExpectedLayer1Outcome
    routing_decision: RoutingDecision
    outcome_type: ProposalOutcomeType
    confidence: Optional[float]
    proposal_match_ids: Tuple[str, ...]
    correct: bool
    incorrect_reason: Optional[str]


@dataclass(frozen=True)
class Layer2CategoryMetrics:
    category: EdgeCaseCategory
    total: int
    auto_accept: int
    needs_review: int
    exception_count: int
    correct: int
    coverage: float
    precision: Optional[float]
    false_accepts: int


@dataclass(frozen=True)
class Layer2EvaluationReport:
    total_scenarios: int
    overall_coverage: float
    overall_precision: Optional[float]
    false_accept_rate: float
    category_metrics: Tuple[Layer2CategoryMetrics, ...]
    routing_evaluations: Tuple[Layer2RoutingEvaluation, ...]
    false_accepts: Tuple[Layer2RoutingEvaluation, ...]


def _load_normalized_records(
    settlement_path: Path,
    bank_path: Path,
    ledger_path: Path,
) -> List[Any]:
    records = []
    for path, source_type in [
        (settlement_path, SourceType.SETTLEMENT),
        (bank_path, SourceType.BANK),
        (ledger_path, SourceType.LEDGER),
    ]:
        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                clean_row = {k: v for k, v in row.items() if k not in ("scenario_ref", "synthetic_ref")}
                from reconciliation.normalizer import normalize_record
                record = normalize_record(clean_row, source_type)
                records.append(record)
    return records


def _score_routing(
    eval: Layer2RoutingEvaluation,
    scenario: GroundTruthScenario,
    unit: GroundTruthUnit,
) -> Layer2RoutingEvaluation:
    correct = False
    reason = None

    if eval.routing_decision == RoutingDecision.AUTO_ACCEPT:
        if unit.is_true_orphan:
            correct = False
            reason = "Auto-accepted a true orphan."
        else:
            if eval.proposal_match_ids == unit.member_record_ids:
                correct = True
            else:
                correct = False
                reason = (
                    f"Auto-accepted with wrong IDs: "
                    f"{eval.proposal_match_ids} != {unit.member_record_ids}"
                )
    else:
        if unit.is_true_orphan:
            correct = True
        elif eval.routing_decision == RoutingDecision.NEEDS_REVIEW:
            if eval.proposal_match_ids == unit.member_record_ids:
                correct = True
            else:
                correct = False
                reason = (
                    f"NEEDS_REVIEW with wrong IDs: "
                    f"{eval.proposal_match_ids} != {unit.member_record_ids}"
                )
        else:
            if eval.outcome_type == ProposalOutcomeType.VALIDATION_FAILED:
                correct = False
                reason = "Correctly rejected an invalid proposal but missed the match opportunity."
            elif eval.outcome_type == ProposalOutcomeType.NO_PROPOSAL:
                correct = False
                reason = "Missed opportunity: Layer 1 expected match but Layer 2 returned no proposal."
            else:
                correct = False
                reason = f"Provider failure on a matchable scenario: {eval.outcome_type.value}"

    return Layer2RoutingEvaluation(
        scenario_id=eval.scenario_id,
        category=eval.category,
        is_true_orphan=eval.is_true_orphan,
        layer1_expected=eval.layer1_expected,
        routing_decision=eval.routing_decision,
        outcome_type=eval.outcome_type,
        confidence=eval.confidence,
        proposal_match_ids=eval.proposal_match_ids,
        correct=correct,
        incorrect_reason=reason,
    )


def _process_residual(
    residual: Any,
    normalized: List[Any],
    orchestrator: ProposalOrchestrator,
    retrieval_config: RetrievalConfig,
    scenario_map: Dict[str, GroundTruthScenario],
    unit_map: Dict[str, GroundTruthUnit],
) -> Layer2RoutingEvaluation:
    scenario_id = residual.scenario_id
    member_ids = residual.member_record_ids
    normalized_records = tuple(normalized)
    member_records = tuple(r for r in normalized_records if r.record_id in member_ids)

    case = reconstruct_layer2_case(
        scenario_id=scenario_id,
        member_record_ids=member_ids,
        normalized_records=normalized_records,
    )
    retrieval = retrieve_candidates(case, normalized_records, retrieval_config)

    outcome = orchestrator.resolve(case, retrieval)
    decision = route(outcome)

    confidence = outcome.proposal.confidence if outcome.proposal else None
    match_ids = tuple(outcome.proposal.proposed_match_ids) if outcome.proposal else ()

    scenario = scenario_map[scenario_id]
    unit = unit_map[scenario_id]

    raw_eval = Layer2RoutingEvaluation(
        scenario_id=scenario_id,
        category=scenario.category,
        is_true_orphan=unit.is_true_orphan,
        layer1_expected=scenario.expected_outcome,
        routing_decision=decision,
        outcome_type=outcome.outcome,
        confidence=confidence,
        proposal_match_ids=match_ids,
        correct=False,
        incorrect_reason=None,
    )

    return _score_routing(raw_eval, scenario, unit)


def run_layer2_evaluation(
    dataset: GeneratedDataset,
    orchestrator: ProposalOrchestrator,
    output_dir: Path,
    config: Optional[MatcherConfig] = None,
    retrieval_config: Optional[RetrievalConfig] = None,
) -> Layer2EvaluationReport:
    from reconciliation.evaluation.evaluation_harness import run_evaluation as run_l1_evaluation

    config = config or MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
    retrieval_config = retrieval_config or RetrievalConfig()

    write_dataset(dataset, output_dir)
    l1_result = run_l1_evaluation(dataset, config, output_dir)

    residuals = load_residuals(output_dir)
    normalized = _load_normalized_records(
        output_dir / "settlements.csv",
        output_dir / "bank.csv",
        output_dir / "ledger.csv",
    )

    scenario_map = {s.scenario_id: s for s in dataset.scenarios}
    unit_map = {u.scenario_id: u for u in build_ground_truth_units(list(dataset.scenarios))}

    evaluations: List[Layer2RoutingEvaluation] = []
    for residual in residuals:
        evaluations.append(
            _process_residual(
                residual, normalized, orchestrator, retrieval_config, scenario_map, unit_map
            )
        )

    return _build_report(evaluations)


def _build_report(
    evaluations: List[Layer2RoutingEvaluation],
) -> Layer2EvaluationReport:
    total = len(evaluations)
    cat_stats: Dict[EdgeCaseCategory, Dict[str, int]] = {}
    for cat in EdgeCaseCategory:
        cat_stats[cat] = {
            "total": 0,
            "auto_accept": 0,
            "needs_review": 0,
            "exception": 0,
            "correct": 0,
            "false_accepts": 0,
        }

    false_accepts: List[Layer2RoutingEvaluation] = []
    for ev in evaluations:
        stats = cat_stats[ev.category]
        stats["total"] += 1
        if ev.routing_decision == RoutingDecision.AUTO_ACCEPT:
            stats["auto_accept"] += 1
            if not ev.correct:
                stats["false_accepts"] += 1
                false_accepts.append(ev)
        elif ev.routing_decision == RoutingDecision.NEEDS_REVIEW:
            stats["needs_review"] += 1
        else:
            stats["exception"] += 1

        if ev.correct:
            stats["correct"] += 1

    category_metrics = []
    total_auto_accept = 0
    total_correct = 0
    total_false_accepts = 0
    for cat in EdgeCaseCategory:
        stats = cat_stats[cat]
        total_auto_accept += stats["auto_accept"]
        total_correct += stats["correct"]
        total_false_accepts += stats["false_accepts"]
        precision = (
            (stats["auto_accept"] - stats["false_accepts"]) / stats["auto_accept"]
            if stats["auto_accept"] > 0
            else None
        )
        coverage = stats["correct"] / stats["total"] if stats["total"] > 0 else 0.0
        category_metrics.append(
            Layer2CategoryMetrics(
                category=cat,
                total=stats["total"],
                auto_accept=stats["auto_accept"],
                needs_review=stats["needs_review"],
                exception_count=stats["exception"],
                correct=stats["correct"],
                coverage=coverage,
                precision=precision,
                false_accepts=stats["false_accepts"],
            )
        )

    overall_coverage = total_correct / total if total > 0 else 0.0
    overall_precision = (
        (total_auto_accept - total_false_accepts) / total_auto_accept
        if total_auto_accept > 0
        else None
    )
    false_accept_rate = total_false_accepts / total_auto_accept if total_auto_accept > 0 else 0.0

    return Layer2EvaluationReport(
        total_scenarios=total,
        overall_coverage=overall_coverage,
        overall_precision=overall_precision,
        false_accept_rate=false_accept_rate,
        category_metrics=tuple(category_metrics),
        routing_evaluations=tuple(evaluations),
        false_accepts=tuple(false_accepts),
    )
