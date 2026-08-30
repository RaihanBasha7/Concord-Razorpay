"""
Day 4.6 tests: Layer 2 evaluation harness.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

import pytest

from reconciliation.domain.models import SourceType
from reconciliation.evaluation.dataset_generator import (
    EdgeCaseCategory,
    ExpectedLayer1Outcome,
    GeneratedDataset,
    GroundTruthScenario,
    ScenarioRecordSpec,
    _compute_record_id,
    generate_dataset,
    write_dataset,
)
from reconciliation.evaluation.ground_truth import GroundTruthUnit
from reconciliation.evaluation.layer2_harness import (
    Layer2EvaluationReport,
    Layer2RoutingEvaluation,
    _score_routing,
    run_layer2_evaluation,
)
from reconciliation.groq_provider import StructuredCompletionProvider
from reconciliation.layer2 import Layer2Case, reconstruct_layer2_case
from reconciliation.loader import load_normalized_records
from reconciliation.matcher import reconcile
from reconciliation.matcher_config import MatcherConfig
from reconciliation.normalizer import normalize_record
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_orchestration import ProposalOutcome
from reconciliation.proposal_validation import ProposalOutcomeType
from reconciliation.retrieval import RetrievalResult
from reconciliation.routing import ProposalVerdict


class _FakeProvider(StructuredCompletionProvider):
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload

    def complete_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._payload


class _FakeLayer2Orchestrator:
    def __init__(self, outcomes: Dict[str, ProposalOutcome]) -> None:
        self._outcomes = outcomes

    def resolve(self, case: Layer2Case, retrieval_result: RetrievalResult) -> ProposalOutcome:
        return self._outcomes.get(
            case.scenario_id,
            ProposalOutcome(
                outcome=ProposalOutcomeType.NO_PROPOSAL,
                proposal=None,
                presented_record_ids=tuple(r.record_id for r in case.member_records),
                reason="default",
            ),
        )


def _make_dataset() -> GeneratedDataset:
    return generate_dataset(seed=42)


def test_layer2_harness_produces_report(tmp_path: Path):
    dataset = _make_dataset()
    write_dataset(dataset, tmp_path)

    outcomes: Dict[str, ProposalOutcome] = {}
    for scen in dataset.scenarios:
        member_ids = tuple(_compute_record_id(spec) for spec in scen.record_specs)
        outcomes[scen.scenario_id] = ProposalOutcome(
            outcome=ProposalOutcomeType.NO_PROPOSAL,
            proposal=None,
            presented_record_ids=member_ids,
            reason="No proposal.",
        )

    orchestrator = _FakeLayer2Orchestrator(outcomes)
    report = run_layer2_evaluation(dataset, orchestrator, tmp_path)

    assert isinstance(report, Layer2EvaluationReport)
    assert report.total_scenarios > 0
    assert len(report.routing_evaluations) == report.total_scenarios
    assert len(report.false_accepts) == 0
    assert report.false_accept_rate == 0.0


def test_layer2_harness_detects_false_accepts(tmp_path: Path):
    dataset = _make_dataset()
    write_dataset(dataset, tmp_path)

    outcomes: Dict[str, ProposalOutcome] = {}
    false_accept_scenarios = set()
    for scen in dataset.scenarios:
        member_ids = tuple(_compute_record_id(spec) for spec in scen.record_specs)
        if scen.category == EdgeCaseCategory.TRUE_ORPHAN and len(false_accept_scenarios) < 3:
            false_accept_scenarios.add(scen.scenario_id)
            outcomes[scen.scenario_id] = ProposalOutcome(
                outcome=ProposalOutcomeType.PROPOSAL_VALID,
                proposal=MatchProposal(
                    proposed_match_ids=list(member_ids),
                    confidence=0.95,
                    rationale="overconfident",
                ),
                presented_record_ids=member_ids,
                reason="Valid proposal.",
            )
        else:
            outcomes[scen.scenario_id] = ProposalOutcome(
                outcome=ProposalOutcomeType.NO_PROPOSAL,
                proposal=None,
                presented_record_ids=member_ids,
                reason="No proposal.",
            )

    orchestrator = _FakeLayer2Orchestrator(outcomes)
    report = run_layer2_evaluation(dataset, orchestrator, tmp_path)

    auto_accept_evs = [ev for ev in report.routing_evaluations if ev.routing_decision == ProposalVerdict.AUTO_ACCEPT]
    assert len(auto_accept_evs) == len(false_accept_scenarios)
    assert report.false_accept_rate > 0.0
    assert len(report.false_accepts) == len(false_accept_scenarios)
    for fa in report.false_accepts:
        assert fa.scenario_id in false_accept_scenarios


def test_layer2_harness_api_error_never_auto_accepts(tmp_path: Path):
    dataset = _make_dataset()
    write_dataset(dataset, tmp_path)

    outcomes: Dict[str, ProposalOutcome] = {}
    for scen in dataset.scenarios:
        member_ids = tuple(_compute_record_id(spec) for spec in scen.record_specs)
        outcomes[scen.scenario_id] = ProposalOutcome(
            outcome=ProposalOutcomeType.API_ERROR,
            proposal=MatchProposal(
                proposed_match_ids=list(member_ids),
                confidence=0.99,
                rationale="stray",
            ),
            presented_record_ids=member_ids,
            reason="API error.",
        )

    orchestrator = _FakeLayer2Orchestrator(outcomes)
    report = run_layer2_evaluation(dataset, orchestrator, tmp_path)

    assert report.false_accept_rate == 0.0
    for ev in report.routing_evaluations:
        assert ev.routing_decision != ProposalVerdict.AUTO_ACCEPT


def test_score_routing_auto_accept_no_match_correct_ids_is_correct():
    scenario = GroundTruthScenario(
        scenario_id="FEE-001",
        category=EdgeCaseCategory.FEE_DEDUCTED,
        record_specs=(),
        expected_outcome=ExpectedLayer1Outcome.NO_MATCH,
        has_real_match=True,
                description="Fee deducted scenario.",
    )
    unit = GroundTruthUnit(
        scenario_id="FEE-001",
        member_record_ids=("id-1", "id-2"),
        true_category=EdgeCaseCategory.FEE_DEDUCTED,
        is_true_orphan=False,
    )
    raw_eval = Layer2RoutingEvaluation(
        scenario_id="FEE-001",
        category=EdgeCaseCategory.FEE_DEDUCTED,
        is_true_orphan=False,
        layer1_expected=ExpectedLayer1Outcome.NO_MATCH,
        routing_decision=ProposalVerdict.AUTO_ACCEPT,
        outcome_type=ProposalOutcomeType.PROPOSAL_VALID,
        confidence=0.95,
        proposal_match_ids=("id-1", "id-2"),
        correct=False,
        incorrect_reason=None,
    )

    result = _score_routing(raw_eval, scenario, unit)
    assert result.correct is True
    assert result.incorrect_reason is None


def test_score_routing_auto_accept_no_match_wrong_ids_is_incorrect():
    scenario = GroundTruthScenario(
        scenario_id="FEE-001",
        category=EdgeCaseCategory.FEE_DEDUCTED,
        record_specs=(),
        expected_outcome=ExpectedLayer1Outcome.NO_MATCH,
        has_real_match=True,
                description="Fee deducted scenario.",
    )
    unit = GroundTruthUnit(
        scenario_id="FEE-001",
        member_record_ids=("id-1", "id-2"),
        true_category=EdgeCaseCategory.FEE_DEDUCTED,
        is_true_orphan=False,
    )
    raw_eval = Layer2RoutingEvaluation(
        scenario_id="FEE-001",
        category=EdgeCaseCategory.FEE_DEDUCTED,
        is_true_orphan=False,
        layer1_expected=ExpectedLayer1Outcome.NO_MATCH,
        routing_decision=ProposalVerdict.AUTO_ACCEPT,
        outcome_type=ProposalOutcomeType.PROPOSAL_VALID,
        confidence=0.95,
        proposal_match_ids=("id-1",),
        correct=False,
        incorrect_reason=None,
    )

    result = _score_routing(raw_eval, scenario, unit)
    assert result.correct is False
    assert "Auto-accepted with wrong IDs" in result.incorrect_reason


def test_score_routing_validation_failed_matchable_scenario_is_incorrect():
    scenario = GroundTruthScenario(
        scenario_id="FEE-001",
        category=EdgeCaseCategory.FEE_DEDUCTED,
        record_specs=(),
        expected_outcome=ExpectedLayer1Outcome.NO_MATCH,
        has_real_match=True,
                description="Fee deducted scenario.",
    )
    unit = GroundTruthUnit(
        scenario_id="FEE-001",
        member_record_ids=("id-1", "id-2"),
        true_category=EdgeCaseCategory.FEE_DEDUCTED,
        is_true_orphan=False,
    )
    raw_eval = Layer2RoutingEvaluation(
        scenario_id="FEE-001",
        category=EdgeCaseCategory.FEE_DEDUCTED,
        is_true_orphan=False,
        layer1_expected=ExpectedLayer1Outcome.NO_MATCH,
        routing_decision=ProposalVerdict.EXCEPTION,
        outcome_type=ProposalOutcomeType.VALIDATION_FAILED,
        confidence=None,
        proposal_match_ids=(),
        correct=False,
        incorrect_reason=None,
    )

    result = _score_routing(raw_eval, scenario, unit)
    assert result.correct is False
    assert result.incorrect_reason == (
        "Correctly rejected an invalid proposal but missed the match opportunity."
    )


def test_score_routing_no_proposal_matchable_scenario_is_incorrect():
    scenario = GroundTruthScenario(
        scenario_id="FEE-001",
        category=EdgeCaseCategory.FEE_DEDUCTED,
        record_specs=(),
        expected_outcome=ExpectedLayer1Outcome.NO_MATCH,
        has_real_match=True,
                description="Fee deducted scenario.",
    )
    unit = GroundTruthUnit(
        scenario_id="FEE-001",
        member_record_ids=("id-1", "id-2"),
        true_category=EdgeCaseCategory.FEE_DEDUCTED,
        is_true_orphan=False,
    )
    raw_eval = Layer2RoutingEvaluation(
        scenario_id="FEE-001",
        category=EdgeCaseCategory.FEE_DEDUCTED,
        is_true_orphan=False,
        layer1_expected=ExpectedLayer1Outcome.NO_MATCH,
        routing_decision=ProposalVerdict.EXCEPTION,
        outcome_type=ProposalOutcomeType.NO_PROPOSAL,
        confidence=None,
        proposal_match_ids=(),
        correct=False,
        incorrect_reason=None,
    )

    result = _score_routing(raw_eval, scenario, unit)
    assert result.correct is False
    assert result.incorrect_reason == (
        "Missed opportunity: Layer 1 expected match but Layer 2 returned no proposal."
    )


def test_score_routing_needs_review_no_match_wrong_ids_is_incorrect():
    scenario = GroundTruthScenario(
        scenario_id="FEE-002",
        category=EdgeCaseCategory.FEE_DEDUCTED,
        record_specs=(),
        expected_outcome=ExpectedLayer1Outcome.NO_MATCH,
        has_real_match=True,
                description="Fee deducted scenario.",
    )
    unit = GroundTruthUnit(
        scenario_id="FEE-002",
        member_record_ids=("id-1", "id-2"),
        true_category=EdgeCaseCategory.FEE_DEDUCTED,
        is_true_orphan=False,
    )
    raw_eval = Layer2RoutingEvaluation(
        scenario_id="FEE-002",
        category=EdgeCaseCategory.FEE_DEDUCTED,
        is_true_orphan=False,
        layer1_expected=ExpectedLayer1Outcome.NO_MATCH,
        routing_decision=ProposalVerdict.NEEDS_REVIEW,
        outcome_type=ProposalOutcomeType.PROPOSAL_VALID,
        confidence=0.75,
        proposal_match_ids=("id-1",),
        correct=False,
        incorrect_reason=None,
    )

    result = _score_routing(raw_eval, scenario, unit)
    assert result.correct is False
    assert "NEEDS_REVIEW with wrong IDs" in result.incorrect_reason


def test_score_routing_duplicate_subset_proposal_is_correct():
    from reconciliation.evaluation.dataset_generator import (
        _build_duplicate,
        _SeededRandom,
        _compute_record_id,
    )
    from reconciliation.domain.models import SourceType

    rng = _SeededRandom(42)
    dup_scen = _build_duplicate(rng, EdgeCaseCategory.DUPLICATE, 1, 0)
    settlement_ids = tuple(
        _compute_record_id(spec)
        for spec in dup_scen.record_specs
        if spec.source_type == SourceType.SETTLEMENT
    )
    all_ids = tuple(
        _compute_record_id(spec) for spec in dup_scen.record_specs
    )

    unit = GroundTruthUnit(
        scenario_id=dup_scen.scenario_id,
        member_record_ids=all_ids,
        true_category=EdgeCaseCategory.DUPLICATE,
        is_true_orphan=False,
        has_real_match=True,
    )
    raw_eval = Layer2RoutingEvaluation(
        scenario_id=dup_scen.scenario_id,
        category=EdgeCaseCategory.DUPLICATE,
        is_true_orphan=False,
        layer1_expected=ExpectedLayer1Outcome.NO_MATCH,
        routing_decision=ProposalVerdict.AUTO_ACCEPT,
        outcome_type=ProposalOutcomeType.PROPOSAL_VALID,
        confidence=0.9,
        proposal_match_ids=settlement_ids,
        correct=False,
        incorrect_reason=None,
    )

    result = _score_routing(raw_eval, dup_scen, unit)
    assert result.correct is True
    assert result.incorrect_reason is None
