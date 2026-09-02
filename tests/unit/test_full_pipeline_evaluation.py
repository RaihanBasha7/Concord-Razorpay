"""
Day 5.3 tests: full pipeline evaluation harness.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from reconciliation.baseline.naive_matcher import NaiveMatchResult, match as baseline_match
from reconciliation.domain.models import SourceType
from reconciliation.evaluation.dataset_generator import (
    EdgeCaseCategory,
    ExpectedLayer1Outcome,
    GroundTruthScenario,
    ScenarioRecordSpec,
    _compute_record_id,
    generate_dataset,
)
from reconciliation.evaluation.dataset_fingerprint import (
    manifest_path,
    read_manifest,
)
from reconciliation.evaluation.full_pipeline_evaluation import (
    FullPipelineReport,
    _MockLayer2Orchestrator,
    _ArtifactBackedOrchestrator,
    _build_ground_truth_units,
    run_full_pipeline,
    serialize_report,
    write_human_readable_report,
)
from reconciliation.evaluation.ground_truth import GroundTruthUnit
from reconciliation.evaluation.layer2_harness import (
    Layer2RoutingEvaluation,
    ProposalVerdict,
    ProposalOutcomeType,
)
from reconciliation.evaluation.primitives import (
    AiPrecisionAtThreshold,
    AiRecallResult,
    BaselineComparison,
    DeterministicMetrics,
    ExceptionComposition,
    FalseAcceptResult,
    RecordOutcome,
    ReviewQueueComposition,
    ScenarioOutcome,
    ThroughputMetrics,
)
from reconciliation.matcher_config import MatcherConfig
from tests.conftest import make_record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scenario(
    scenario_id: str,
    category: EdgeCaseCategory,
    member_specs: list[ScenarioRecordSpec],
    expected_outcome: Any,
    has_real_match: bool = True,
) -> GroundTruthScenario:
    return GroundTruthScenario(
        scenario_id=scenario_id,
        category=category,
        record_specs=tuple(member_specs),
        expected_outcome=expected_outcome,
        has_real_match=has_real_match,
        description="test",
    )


def _make_unit(
    scenario_id: str,
    member_record_ids: tuple[str, ...],
    category: EdgeCaseCategory,
    is_true_orphan: bool = False,
    has_real_match: bool = True,
) -> GroundTruthUnit:
    return GroundTruthUnit(
        scenario_id=scenario_id,
        member_record_ids=member_record_ids,
        true_category=category,
        is_true_orphan=is_true_orphan,
        has_real_match=has_real_match,
    )


# ---------------------------------------------------------------------------
# Test all required report sections exist
# ---------------------------------------------------------------------------


class TestReportSections:
    def test_serialize_report_contains_all_required_sections(self):
        report = FullPipelineReport(
            dataset_seed=42,
            total_scenarios=10,
            total_records=20,
            run_timestamp="2026-01-01T00:00:00",
            layer2_mode="deterministic_mock",
            throughput=ThroughputMetrics(
                total_batch_time_ms=100.0,
                layer1_time_ms=50.0,
                layer2_time_ms=50.0,
            ),
            deterministic_metrics=DeterministicMetrics(
                total_scenarios=10,
                matched=5,
                correct=5,
                match_rate=0.5,
                precision=1.0,
            ),
            ai_precision_090=AiPrecisionAtThreshold(
                threshold=0.9,
                precision=0.9,
                true_positives=9,
                false_positives=1,
                total=10,
            ),
            ai_precision_075=AiPrecisionAtThreshold(
                threshold=0.75,
                precision=0.8,
                true_positives=8,
                false_positives=2,
                total=10,
            ),
            ai_precision_060=AiPrecisionAtThreshold(
                threshold=0.6,
                precision=0.7,
                true_positives=7,
                false_positives=3,
                total=10,
            ),
            ai_recall=AiRecallResult(
                recall=0.8,
                true_positives=8,
                denominator=10,
            ),
            false_accept=FalseAcceptResult(
                rate=0.1,
                count=1,
                total_auto_accepted=10,
            ),
            review_queue=ReviewQueueComposition(
                counts={cat: 0 for cat in EdgeCaseCategory},
                percentages={cat: None for cat in EdgeCaseCategory},
                total=0,
            ),
            exception_composition=ExceptionComposition(
                correctly_refused=0,
                should_have_been_caught=0,
                correctly_refused_pct=None,
                should_have_been_caught_pct=None,
                total=0,
            ),
            baseline_comparison=BaselineComparison(
                baseline_match_rate=0.5,
                deterministic_match_rate=0.5,
                baseline_precision=0.8,
                deterministic_precision=0.9,
                match_rate_delta=0.0,
                precision_delta=0.1,
            ),
            final_system_composition={
                "total_records": 20,
                "counts": {
                    "DETERMINISTIC_MATCH": 5,
                    "AI_AUTO_ACCEPTED": 2,
                    "HUMAN_REVIEW": 3,
                    "EXCEPTION": 10,
                },
                "rates": {
                    "DETERMINISTIC_MATCH": 0.25,
                    "AI_AUTO_ACCEPTED": 0.1,
                    "HUMAN_REVIEW": 0.15,
                    "EXCEPTION": 0.5,
                },
                "note": "test",
            },
            scenario_outcomes=(),
            record_outcomes=(),
            auto_accept_threshold=0.90,
            review_threshold=0.60,
            layer2_provenance="deterministic_mock (test fixture)",
            baseline_config={"amount_tolerance_paise": 100, "date_window_days": 2},
            dataset_fingerprint="fixed-test-fingerprint",
        )

        payload = serialize_report(report)

        assert "metadata" in payload
        assert "throughput" in payload
        assert "deterministic_match_rate" in payload
        assert "ai_precision_at_thresholds" in payload
        assert "confidence_>=_0.90" in payload["ai_precision_at_thresholds"]
        assert "confidence_>=_0.75" in payload["ai_precision_at_thresholds"]
        assert "confidence_>=_0.60" in payload["ai_precision_at_thresholds"]
        assert "ai_recall" in payload
        assert "false_accept_rate" in payload
        assert "review_queue_composition" in payload
        assert "exception_composition" in payload
        assert "baseline_comparison" in payload
        assert "final_system_composition" in payload
        assert "layer2_provenance" in payload["metadata"]
        assert "dataset_fingerprint" in payload["metadata"]
        assert payload["metadata"]["dataset_fingerprint"] == "fixed-test-fingerprint"
        assert "raw_outcomes" in payload


# ---------------------------------------------------------------------------
# Test headline false-accept calculation
# ---------------------------------------------------------------------------


class TestFalseAcceptCalculation:
    def test_false_accept_rate_from_record_outcomes(self):
        records = [
            _make_record_outcome("R1", "S1", EdgeCaseCategory.TRUE_ORPHAN, routing_bucket="AI_AUTO_ACCEPTED", is_false_accept=True),
            _make_record_outcome("R2", "S2", EdgeCaseCategory.EXACT_MATCH, routing_bucket="AI_AUTO_ACCEPTED", is_false_accept=False),
            _make_record_outcome("R3", "S3", EdgeCaseCategory.EXACT_MATCH, routing_bucket="AI_AUTO_ACCEPTED", is_false_accept=False),
        ]
        from reconciliation.evaluation.primitives import compute_false_accept_metrics
        metrics = compute_false_accept_metrics(records)
        assert metrics.count == 1
        assert metrics.total_auto_accepted == 3
        assert metrics.rate == pytest.approx(1 / 3)

    def test_false_accept_zero_denominator(self):
        records = [
            _make_record_outcome("R1", "S1", EdgeCaseCategory.EXACT_MATCH, routing_bucket="HUMAN_REVIEW"),
        ]
        from reconciliation.evaluation.primitives import compute_false_accept_metrics
        metrics = compute_false_accept_metrics(records)
        assert metrics.rate is None
        assert metrics.count == 0
        assert metrics.total_auto_accepted == 0


# ---------------------------------------------------------------------------
# Test baseline is actually invoked
# ---------------------------------------------------------------------------


class TestBaselineInvoked:
    def test_baseline_is_called_during_run(self, tmp_path: Path):
        from reconciliation.evaluation.dataset_generator import write_dataset
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        write_dataset(dataset, tmp_path)

        from reconciliation.evaluation.full_pipeline_evaluation import run_full_pipeline
        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )

        assert report.baseline_comparison.baseline_match_rate is not None
        assert report.baseline_comparison.baseline_precision is not None
        assert report.baseline_comparison.deterministic_match_rate is not None
        assert report.baseline_comparison.deterministic_precision is not None


# ---------------------------------------------------------------------------
# Test timing fields are present
# ---------------------------------------------------------------------------


class TestTimingFields:
    def test_layer1_timing_populated(self, tmp_path: Path):
        from reconciliation.evaluation.dataset_generator import write_dataset
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        write_dataset(dataset, tmp_path)

        from reconciliation.evaluation.full_pipeline_evaluation import run_full_pipeline
        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )

        assert report.throughput.layer1_time_ms > 0

    def test_layer2_timing_populated(self, tmp_path: Path):
        from reconciliation.evaluation.dataset_generator import write_dataset
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        write_dataset(dataset, tmp_path)

        from reconciliation.evaluation.full_pipeline_evaluation import run_full_pipeline
        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )

        assert report.throughput.layer2_time_ms > 0

    def test_total_batch_time_equals_sum(self, tmp_path: Path):
        from reconciliation.evaluation.dataset_generator import write_dataset
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        write_dataset(dataset, tmp_path)

        from reconciliation.evaluation.full_pipeline_evaluation import run_full_pipeline
        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )

        expected_total = report.throughput.layer1_time_ms + report.throughput.layer2_time_ms
        assert report.throughput.total_batch_time_ms == pytest.approx(expected_total)


# ---------------------------------------------------------------------------
# Test zero denominator handling
# ---------------------------------------------------------------------------


class TestZeroDenominators:
    def test_precision_none_when_no_matches(self):
        from reconciliation.evaluation.primitives import compute_deterministic_metrics
        outcomes = [
            _make_scenario_outcome("S1", EdgeCaseCategory.TRUE_ORPHAN, deterministic_matched=False, deterministic_correct=True),
        ]
        metrics = compute_deterministic_metrics(outcomes)
        assert metrics.precision is None

    def test_recall_none_when_no_residuals_with_real_match(self):
        from reconciliation.evaluation.primitives import compute_ai_recall
        outcomes = [
            _make_scenario_outcome("S1", EdgeCaseCategory.TRUE_ORPHAN, deterministic_matched=False, has_real_match=False),
        ]
        recall = compute_ai_recall(outcomes)
        assert recall.recall is None

    def test_false_accept_rate_none_when_no_auto_accepted(self):
        from reconciliation.evaluation.primitives import compute_false_accept_metrics
        records = [
            _make_record_outcome("R1", "S1", EdgeCaseCategory.EXACT_MATCH, routing_bucket="HUMAN_REVIEW"),
        ]
        metrics = compute_false_accept_metrics(records)
        assert metrics.rate is None

    def test_review_queue_percentages_none_when_empty(self):
        from reconciliation.evaluation.primitives import compute_review_queue_composition
        records = [
            _make_record_outcome("R1", "S1", EdgeCaseCategory.EXACT_MATCH, routing_bucket="AI_AUTO_ACCEPTED"),
        ]
        composition = compute_review_queue_composition(records)
        assert composition.total == 0
        assert all(v is None for v in composition.percentages.values())

    def test_exception_percentages_none_when_empty(self):
        from reconciliation.evaluation.primitives import compute_exception_composition
        records = [
            _make_record_outcome("R1", "S1", EdgeCaseCategory.EXACT_MATCH, routing_bucket="AI_AUTO_ACCEPTED"),
        ]
        composition = compute_exception_composition(records)
        assert composition.total == 0
        assert composition.correctly_refused_pct is None
        assert composition.should_have_been_caught_pct is None


# ---------------------------------------------------------------------------
# Test mock orchestrator determinism
# ---------------------------------------------------------------------------


class TestMockOrchestrator:
    def test_same_scenario_same_outcome(self):
        from reconciliation.layer2 import Layer2Case
        from reconciliation.retrieval import RetrievalResult

        scenario = _make_scenario(
            "EXACT-001",
            EdgeCaseCategory.EXACT_MATCH,
            [
                ScenarioRecordSpec(
                    synthetic_ref="REC-SET-001",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="SET-EXACT-001-001",
                    order_id_hint="ORD-EXACT-001",
                    amount_paise=100000,
                    record_date=date(2026, 8, 1),
                    narration="test",
                ),
                ScenarioRecordSpec(
                    synthetic_ref="REC-BNK-001",
                    source_type=SourceType.BANK,
                    source_native_id="BNK-EXACT-001-001",
                    order_id_hint="ORD-EXACT-001",
                    amount_paise=100000,
                    record_date=date(2026, 8, 1),
                    narration="test",
                ),
            ],
            ExpectedLayer1Outcome.MATCH_EXACT_ID,
        )
        unit = _make_unit("EXACT-001", ("SET-001", "BNK-001"), EdgeCaseCategory.EXACT_MATCH)

        orchestrator = _MockLayer2Orchestrator({"EXACT-001": scenario}, {"EXACT-001": unit})

        case = Layer2Case(
            scenario_id="EXACT-001",
            member_records=(
                make_record(record_id="SET-001", source_type=SourceType.SETTLEMENT, source_native_id="SET-EXACT-001-001", order_id_hint="ORD-EXACT-001", amount_paise=100000, date=date(2026, 8, 1)),
            ),
            record_count=1,
        )

        retrieval = RetrievalResult(
            scenario_id="EXACT-001",
            candidates=(),
            candidate_count=0,
            max_candidates=10,
            retrieval_signals_used=(),
        )

        outcome1 = orchestrator.resolve(case, retrieval)
        outcome2 = orchestrator.resolve(case, retrieval)

        assert outcome1.outcome == outcome2.outcome
        assert outcome1.proposal == outcome2.proposal or (outcome1.proposal is None and outcome2.proposal is None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scenario_outcome(
    scenario_id: str,
    category: EdgeCaseCategory,
    *,
    is_true_orphan: bool = False,
    has_real_match: bool = True,
    baseline_matched: bool = False,
    baseline_correct: bool = False,
    deterministic_matched: bool = False,
    deterministic_correct: bool = False,
    routing_bucket: str | None = None,
    ai_confidence: float | None = None,
    ai_proposal_ids: tuple[str, ...] = (),
    ai_correct: bool | None = None,
    layer1_time_ms: float | None = None,
    layer2_time_ms: float | None = None,
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
    ai_confidence: float | None = None,
    is_false_accept: bool = False,
    exception_correctly_refused: bool = False,
    exception_should_have_been_caught: bool = False,
    layer2_time_ms: float | None = None,
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
# Focused evaluation-validity tests
# ---------------------------------------------------------------------------


class TestArtifactBackedProvenance:
    def test_artifact_path_produces_artifact_backed_report(self, tmp_path):
        from reconciliation.evaluation.full_pipeline_evaluation import (
            _ArtifactBackedOrchestrator,
            load_layer2_artifacts,
            run_full_pipeline,
        )

        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        # Find two same-source settlement records from a DUPLICATE scenario.
        # These are residuals (Layer 1 only matches cross-source) and
        # share identical amount/date, satisfying the Layer 3 evidence gate.
        from reconciliation.domain.models import SourceType
        dup_settlement_ids = None
        for scen in dataset.scenarios:
            if scen.category.value != "DUPLICATE":
                continue
            settlements = [s for s in scen.record_specs
                           if s.source_type == SourceType.SETTLEMENT]
            if len(settlements) == 2:
                dup_settlement_ids = [_compute_record_id(s) for s in settlements]
                break
        assert dup_settlement_ids is not None, "No DUPLICATE scenario with 2 settlements found"

        artifact = tmp_path / "day4_audit.jsonl"
        artifact.write_text(
            json.dumps({
                "correlation_id": "test-1",
                "timestamp": "2026-08-27T11:00:00+00:00",
                "presented_record_ids": dup_settlement_ids,
                "outcome": "PROPOSAL_VALID",
                "proposal": {"proposed_match_ids": dup_settlement_ids, "confidence": 0.9},
                "confidence": 0.9,
                "reason": "Valid proposal from Layer 2 artifact.",
            }) + "\n",
            encoding="utf-8",
        )

        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
            layer2_artifact_path=artifact,
        )
        assert report.layer2_mode == "artifact-backed"
        assert "day4_audit.jsonl" in report.layer2_provenance

    def test_no_artifact_path_falls_back_to_mock(self, tmp_path):
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        assert report.layer2_mode == "deterministic_mock"
        assert "No real Layer 2 artifacts" in report.layer2_provenance

    def test_mock_mode_not_confused_with_real_evaluation(self, tmp_path):
        from reconciliation.evaluation.full_pipeline_evaluation import _MockLayer2Orchestrator

        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        assert report.layer2_mode == "deterministic_mock"
        assert "smoke-test" in report.layer2_provenance.lower()


class TestGroundTruthHeuristicRemoval:
    def test_has_real_match_from_ground_truth_not_scenario_name(self):
        from reconciliation.evaluation.dataset_generator import (
            _build_exact_match,
            _build_true_orphan,
            _SeededRandom,
        )
        from reconciliation.evaluation.ground_truth import EdgeCaseCategory

        rng = _SeededRandom(42)
        exact = _build_exact_match(rng, EdgeCaseCategory.EXACT_MATCH, 1, 0)
        orphan = _build_true_orphan(rng, EdgeCaseCategory.TRUE_ORPHAN, 1, 50)

        assert exact.has_real_match is True
        assert orphan.has_real_match is False

    def test_rounding_difference_has_real_match_flag(self):
        from reconciliation.evaluation.dataset_generator import (
            _build_rounding_difference,
            _SeededRandom,
        )
        from reconciliation.evaluation.ground_truth import EdgeCaseCategory

        rng = _SeededRandom(42)
        low = _build_rounding_difference(rng, EdgeCaseCategory.ROUNDING_DIFFERENCE, 1, 0)
        high = _build_rounding_difference(rng, EdgeCaseCategory.ROUNDING_DIFFERENCE, 9, 9)

        assert low.has_real_match is True
        assert high.has_real_match is False


class TestBaselineComparisonSemantics:
    def test_baseline_compare_labels_reflect_layer1_only(self, tmp_path):
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        payload = serialize_report(report)
        note = payload["baseline_comparison"]["comparable_metrics_note"]
        assert "Layer 2/AI" in note
        assert "directly comparable" in note

    def test_final_system_composition_distinguishes_routing_buckets(self, tmp_path):
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        composition = report.final_system_composition
        assert "DETERMINISTIC_MATCH" in composition["counts"]
        assert "AI_AUTO_ACCEPTED" in composition["counts"]
        assert "HUMAN_REVIEW" in composition["counts"]
        assert "EXCEPTION" in composition["counts"]
        total = sum(composition["counts"].values())
        assert total == report.total_records


# ---------------------------------------------------------------------------
# Dataset provenance tests
# ---------------------------------------------------------------------------


def _artifact_line(
    *,
    correlation_id: str = "c1",
    fingerprint: str | None = None,
    presented: list[str] | None = None,
    outcome: str = "PROPOSAL_VALID",
    proposed: list[str] | None = None,
    confidence: float | None = 0.9,
) -> str:
    import json as _json
    raw = {
        "correlation_id": correlation_id,
        "timestamp": "2026-08-29T00:00:00+00:00",
        "presented_record_ids": presented or [],
        "outcome": outcome,
        "proposal": {
            "proposed_match_ids": proposed or [],
            "confidence": confidence if confidence is not None else 0.0,
            "rationale": "artifact",
        },
        "confidence": confidence,
        "reason": "Valid proposal from Layer 2 artifact.",
    }
    if fingerprint is not None:
        raw["dataset_fingerprint"] = fingerprint
    return _json.dumps(raw)


class TestDatasetProvenance:
    def test_full_pipeline_freezes_manifest_and_embeds_fingerprint(self, tmp_path):
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
        from reconciliation.evaluation.dataset_fingerprint import (
            manifest_path,
            read_manifest,
        )

        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )

        assert manifest_path(tmp_path).exists()
        manifest = read_manifest(tmp_path)
        assert manifest is not None
        assert report.dataset_fingerprint == manifest.fingerprint()
        assert len(report.dataset_fingerprint) == 64

    def test_full_pipeline_rejects_dataset_drift(self, tmp_path):
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        # First run: freeze the dataset on disk.
        run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )

        # Tamper with a frozen file on disk to simulate drift.
        (tmp_path / "settlements.csv").write_text("tampered\n", encoding="utf-8")

        # Second run: must detect the drift before rewriting anything.
        with pytest.raises(ValueError, match="Dataset drift detected"):
            run_full_pipeline(
                dataset=dataset,
                config=config,
                output_dir=tmp_path,
                baseline_config=config,
            )

    def test_artifact_backed_verifies_correct_fingerprint(self, tmp_path):
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
        # Prime the dataset + manifest on disk so we know the fingerprint.
        run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        manifest = read_manifest(tmp_path)
        fingerprint = manifest.fingerprint()
        # Use real member IDs from a DUPLICATE scenario so artifact mapping succeeds.
        dup_scen = next(
            s for s in dataset.scenarios if s.category.value == "DUPLICATE"
        )
        member_ids = [_compute_record_id(spec) for spec in dup_scen.record_specs]

        artifact = tmp_path / "day4_audit.jsonl"
        artifact.write_text(
            _artifact_line(
                fingerprint=fingerprint,
                presented=list(member_ids),
                proposed=list(member_ids),
            )
            + "\n",
            encoding="utf-8",
        )

        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
            layer2_artifact_path=artifact,
        )
        assert report.layer2_mode == "artifact-backed"
        assert report.dataset_fingerprint == fingerprint
        assert fingerprint[:12] in report.layer2_provenance

    def test_artifact_backed_rejects_mismatched_fingerprint(self, tmp_path):
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
        run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        dup_scen = next(
            s for s in dataset.scenarios if s.category.value == "DUPLICATE"
        )
        member_ids = [_compute_record_id(spec) for spec in dup_scen.record_specs]
        artifact = tmp_path / "day4_audit.jsonl"
        artifact.write_text(
            _artifact_line(
                fingerprint="0" * 64,
                presented=list(member_ids),
                proposed=list(member_ids),
            )
            + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="provenance mismatch"):
            run_full_pipeline(
                dataset=dataset,
                config=config,
                output_dir=tmp_path,
                baseline_config=config,
                layer2_artifact_path=artifact,
            )

    def test_artifact_backed_rejects_record_id_drift(self, tmp_path):
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
        run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        # Legacy artifact (no fingerprint) referencing a record not in dataset.
        artifact = tmp_path / "day4_audit.jsonl"
        artifact.write_text(
            _artifact_line(
                fingerprint=None,
                presented=["SETTLEMENT-deadbeef0001"],
                proposed=["SETTLEMENT-deadbeef0001"],
            )
            + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="provenance mismatch"):
            run_full_pipeline(
                dataset=dataset,
                config=config,
                output_dir=tmp_path,
                baseline_config=config,
                layer2_artifact_path=artifact,
            )


class TestArtifactMappingRegression:
    def test_prefix_matching_beats_subset_collision(self, tmp_path):
        from reconciliation.evaluation.full_pipeline_evaluation import (
            _ArtifactBackedOrchestrator,
            _dataset_record_ids,
            load_layer2_artifacts,
            _map_artifacts_to_scenarios,
        )

        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        # Prime the dataset + manifest on disk.
        run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        manifest = read_manifest(tmp_path)
        fingerprint = manifest.fingerprint()

        fee_scen = next(
            s for s in dataset.scenarios
            if s.category.value == "FEE_DEDUCTED"
        )
        dup_scen = next(
            s for s in dataset.scenarios
            if s.category.value == "DUPLICATE"
        )
        fee_ids = [_compute_record_id(spec) for spec in fee_scen.record_specs]
        dup_ids = [_compute_record_id(spec) for spec in dup_scen.record_specs]

        # Use a real ledger record ID from the dataset so provenance passes.
        real_ledger_id = next(
            rid for rid in _dataset_record_ids(dataset)
            if rid.startswith("LEDGER-")
        )

        # Artifact starts with fee_ids (prefix match for fee), but also
        # contains dup_ids so subset matching would see both.
        presented = fee_ids + dup_ids + [real_ledger_id]
        artifact = tmp_path / "day4_audit.jsonl"
        artifact.write_text(
            _artifact_line(
                fingerprint=fingerprint,
                presented=presented,
                proposed=fee_ids,
            )
            + "\n",
            encoding="utf-8",
        )

        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
            layer2_artifact_path=artifact,
        )

        fee_outcome = next(
            o for o in report.scenario_outcomes
            if o.scenario_id == fee_scen.scenario_id
        )
        dup_outcome = next(
            o for o in report.scenario_outcomes
            if o.scenario_id == dup_scen.scenario_id
        )

        assert fee_outcome.ai_proposal_ids == tuple(fee_ids)
        assert dup_outcome.ai_proposal_ids == ()


class TestDuplicateEvaluationSemantics:
    def test_duplicate_subset_proposal_is_correct(self, tmp_path):
        from reconciliation.evaluation.layer2_harness import _score_routing
        from reconciliation.evaluation.dataset_generator import (
            _build_duplicate,
            _SeededRandom,
        )

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


# ===================================================================
# Regression: API_ERROR artifact preservation (P0 evaluation integrity)
# ===================================================================


class TestAPIErrorArtifactPreservation:
    """Verify that API_ERROR artifacts are preserved through scenario
    mapping and evaluation rather than becoming NO_PROPOSAL or missing.

    Bug: _map_artifacts_to_scenarios() silently dropped all API_ERROR
    artifacts, causing them to be indistinguishable from missing artifacts
    and counted as AI misses in recall metrics.
    """

    def test_api_error_preserved_through_mapping(self, tmp_path):
        """An API_ERROR artifact must appear in the scenario map
        rather than being silently dropped."""
        from reconciliation.evaluation.full_pipeline_evaluation import (
            _ArtifactBackedOrchestrator,
            _dataset_record_ids,
            load_layer2_artifacts,
            _map_artifacts_to_scenarios,
        )

        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        # Prime the dataset + manifest on disk.
        run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        manifest = read_manifest(tmp_path)
        fingerprint = manifest.fingerprint()

        # Find a residual scenario (not matched by Layer 1).
        from reconciliation.loader import load_residuals
        residuals = load_residuals(tmp_path)
        assert len(residuals) > 0
        target = residuals[0]

        unit_map = {
            u.scenario_id: u
            for u in _build_ground_truth_units(list(dataset.scenarios))
        }

        # Create an API_ERROR artifact for the target scenario.
        artifact = tmp_path / "day4_audit.jsonl"
        artifact.write_text(
            _artifact_line(
                correlation_id="api-err-1",
                fingerprint=fingerprint,
                presented=list(target.member_record_ids),
                outcome="API_ERROR",
                proposed=[],
                confidence=None,
            )
            + "\n",
            encoding="utf-8",
        )

        artifacts = load_layer2_artifacts(artifact)
        scenario_map = _map_artifacts_to_scenarios(artifacts, residuals, unit_map)

        # The API_ERROR artifact MUST be in the map, not dropped.
        assert target.scenario_id in scenario_map
        assert scenario_map[target.scenario_id].outcome.value == "API_ERROR"

    def test_api_error_not_collapsed_to_no_proposal(self, tmp_path):
        """An API_ERROR scenario must produce an API_ERROR outcome
        in the final report, not NO_PROPOSAL."""
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        # Prime the dataset.
        run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        manifest = read_manifest(tmp_path)
        fingerprint = manifest.fingerprint()

        from reconciliation.loader import load_residuals
        residuals = load_residuals(tmp_path)

        # Create an API_ERROR artifact for every residual scenario.
        lines = []
        for i, res in enumerate(residuals):
            lines.append(
                _artifact_line(
                    correlation_id=f"api-err-{i}",
                    fingerprint=fingerprint,
                    presented=list(res.member_record_ids),
                    outcome="API_ERROR",
                    proposed=[],
                    confidence=None,
                )
            )
        artifact = tmp_path / "day4_audit.jsonl"
        artifact.write_text("\n".join(lines) + "\n", encoding="utf-8")

        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
            layer2_artifact_path=artifact,
        )

        residual_scenarios = [
            o for o in report.scenario_outcomes
            if not o.deterministic_matched
        ]
        assert len(residual_scenarios) > 0

        for o in residual_scenarios:
            # Every residual must show API_ERROR, not NO_PROPOSAL or None.
            assert o.layer2_outcome_type == "API_ERROR", (
                f"{o.scenario_id}: expected API_ERROR, got {o.layer2_outcome_type}"
            )
            # ai_correct must be None (can't judge), not False (AI miss).
            assert o.ai_correct is None, (
                f"{o.scenario_id}: ai_correct should be None for API_ERROR, "
                f"got {o.ai_correct}"
            )
            # Must route to EXCEPTION (not AI_AUTO_ACCEPTED or HUMAN_REVIEW).
            assert o.routing_bucket == "EXCEPTION"

    def test_api_error_visible_in_serialized_report(self, tmp_path):
        """API_ERROR must be visible in the serialized JSON report."""
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        manifest = read_manifest(tmp_path)
        fingerprint = manifest.fingerprint()

        from reconciliation.loader import load_residuals
        residuals = load_residuals(tmp_path)

        # One API_ERROR artifact.
        artifact = tmp_path / "day4_audit.jsonl"
        artifact.write_text(
            _artifact_line(
                correlation_id="api-err-1",
                fingerprint=fingerprint,
                presented=list(residuals[0].member_record_ids),
                outcome="API_ERROR",
                proposed=[],
                confidence=None,
            )
            + "\n",
            encoding="utf-8",
        )

        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
            layer2_artifact_path=artifact,
        )

        serialized = serialize_report(report)
        outcomes = serialized["raw_outcomes"]["scenario_outcomes"]

        # Find the residual scenario outcome.
        target = residuals[0].scenario_id
        target_outcome = next(o for o in outcomes if o["scenario_id"] == target)
        assert target_outcome["layer2_outcome_type"] == "API_ERROR"

    def test_api_error_in_systemwide_recall_but_excluded_from_attempted(self):
        """System-wide recall includes API_ERROR in denominator; attempted-only excludes it."""
        from reconciliation.evaluation.primitives import compute_ai_recall

        # Scenario with real match, API_ERROR outcome.
        api_error = ScenarioOutcome(
            scenario_id="S-1",
            category=EdgeCaseCategory.EXACT_MATCH,
            is_true_orphan=False,
            has_real_match=True,
            baseline_matched=False,
            baseline_correct=None,
            deterministic_matched=False,
            deterministic_correct=False,
            routing_bucket="EXCEPTION",
            ai_confidence=None,
            ai_proposal_ids=(),
            ai_correct=None,
            layer2_outcome_type="API_ERROR",
        )
        # Scenario with real match, AI correctly matched.
        success = ScenarioOutcome(
            scenario_id="S-2",
            category=EdgeCaseCategory.EXACT_MATCH,
            is_true_orphan=False,
            has_real_match=True,
            baseline_matched=False,
            baseline_correct=None,
            deterministic_matched=False,
            deterministic_correct=False,
            routing_bucket="AI_AUTO_ACCEPTED",
            ai_confidence=0.95,
            ai_proposal_ids=("A", "B"),
            ai_correct=True,
            layer2_outcome_type="PROPOSAL_VALID",
        )
        # Scenario with real match, AI missed.
        miss = ScenarioOutcome(
            scenario_id="S-3",
            category=EdgeCaseCategory.EXACT_MATCH,
            is_true_orphan=False,
            has_real_match=True,
            baseline_matched=False,
            baseline_correct=None,
            deterministic_matched=False,
            deterministic_correct=False,
            routing_bucket="EXCEPTION",
            ai_confidence=None,
            ai_proposal_ids=(),
            ai_correct=False,
            layer2_outcome_type="NO_PROPOSAL",
        )

        result = compute_ai_recall([api_error, success, miss])
        # System-wide: denominator = 3 (all have has_real_match=True), tp = 1.
        assert result.denominator == 3
        assert result.true_positives == 1
        assert result.recall == pytest.approx(1 / 3)
        # Attempted-only: denominator = 2 (API_ERROR excluded), tp = 1.
        assert result.denominator_attempted == 2
        assert result.recall_attempted == 0.5

    def test_total_api_error_coverage_systemwide_zero_attempted_none(self):
        """100%% API_ERROR: system-wide recall must be 0.0 (not N/A),
        attempted-only must be None (0/0, correctly undefined)."""
        from reconciliation.evaluation.primitives import compute_ai_recall

        outcomes = []
        for i in range(5):
            outcomes.append(
                ScenarioOutcome(
                    scenario_id=f"S-{i}",
                    category=EdgeCaseCategory.EXACT_MATCH,
                    is_true_orphan=False,
                    has_real_match=True,
                    baseline_matched=False,
                    baseline_correct=None,
                    deterministic_matched=False,
                    deterministic_correct=False,
                    routing_bucket="EXCEPTION",
                    ai_confidence=None,
                    ai_proposal_ids=(),
                    ai_correct=None,
                    layer2_outcome_type="API_ERROR",
                )
            )

        result = compute_ai_recall(outcomes)
        # System-wide: denominator = 5, tp = 0, recall = 0.0.
        assert result.denominator == 5
        assert result.true_positives == 0
        assert result.recall == 0.0
        # Attempted-only: denominator = 0, recall = None.
        assert result.denominator_attempted == 0
        assert result.recall_attempted is None

    def test_mixed_coverage_both_recalls_differ(self):
        """Mixed API_ERROR + attempted: system-wide and attempted-only recall
correctly diverge."""
        from reconciliation.evaluation.primitives import compute_ai_recall

        # 2 API_ERROR (has_real_match=True) — cannot judge
        api1 = ScenarioOutcome(
            scenario_id="S-A1", category=EdgeCaseCategory.EXACT_MATCH,
            is_true_orphan=False, has_real_match=True,
            baseline_matched=False, baseline_correct=None,
            deterministic_matched=False, deterministic_correct=False,
            routing_bucket="EXCEPTION", ai_confidence=None,
            ai_proposal_ids=(), ai_correct=None,
            layer2_outcome_type="API_ERROR",
        )
        api2 = ScenarioOutcome(
            scenario_id="S-A2", category=EdgeCaseCategory.EXACT_MATCH,
            is_true_orphan=False, has_real_match=True,
            baseline_matched=False, baseline_correct=None,
            deterministic_matched=False, deterministic_correct=False,
            routing_bucket="EXCEPTION", ai_confidence=None,
            ai_proposal_ids=(), ai_correct=None,
            layer2_outcome_type="API_ERROR",
        )
        # 1 correct proposal
        correct = ScenarioOutcome(
            scenario_id="S-C", category=EdgeCaseCategory.EXACT_MATCH,
            is_true_orphan=False, has_real_match=True,
            baseline_matched=False, baseline_correct=None,
            deterministic_matched=False, deterministic_correct=False,
            routing_bucket="AI_AUTO_ACCEPTED", ai_confidence=0.95,
            ai_proposal_ids=("A", "B"), ai_correct=True,
            layer2_outcome_type="PROPOSAL_VALID",
        )
        # 2 AI misses
        miss1 = ScenarioOutcome(
            scenario_id="S-M1", category=EdgeCaseCategory.EXACT_MATCH,
            is_true_orphan=False, has_real_match=True,
            baseline_matched=False, baseline_correct=None,
            deterministic_matched=False, deterministic_correct=False,
            routing_bucket="EXCEPTION", ai_confidence=None,
            ai_proposal_ids=(), ai_correct=False,
            layer2_outcome_type="NO_PROPOSAL",
        )
        miss2 = ScenarioOutcome(
            scenario_id="S-M2", category=EdgeCaseCategory.EXACT_MATCH,
            is_true_orphan=False, has_real_match=True,
            baseline_matched=False, baseline_correct=None,
            deterministic_matched=False, deterministic_correct=False,
            routing_bucket="EXCEPTION", ai_confidence=None,
            ai_proposal_ids=(), ai_correct=False,
            layer2_outcome_type="NO_PROPOSAL",
        )

        result = compute_ai_recall([api1, api2, correct, miss1, miss2])
        # System-wide: denominator = 5, tp = 1, recall = 20%%.
        assert result.denominator == 5
        assert result.true_positives == 1
        assert result.recall == pytest.approx(0.2)
        # Attempted-only: denominator = 3 (correct + miss1 + miss2), tp = 1, recall = 33.3%%.
        assert result.denominator_attempted == 3
        assert result.recall_attempted == pytest.approx(1 / 3)
        # They must differ.
        assert result.recall != result.recall_attempted

    def test_missing_artifact_distinct_from_api_error(self, tmp_path):
        """A genuinely missing artifact (no Layer 2 attempt) must remain
        distinguishable from an API_ERROR."""
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        manifest = read_manifest(tmp_path)
        fingerprint = manifest.fingerprint()

        from reconciliation.loader import load_residuals
        residuals = load_residuals(tmp_path)
        assert len(residuals) >= 2

        # Only create an artifact for the first residual.
        # The second residual has no artifact (genuinely missing).
        artifact = tmp_path / "day4_audit.jsonl"
        artifact.write_text(
            _artifact_line(
                correlation_id="valid-1",
                fingerprint=fingerprint,
                presented=list(residuals[0].member_record_ids),
                outcome="PROPOSAL_VALID",
                proposed=list(residuals[0].member_record_ids),
                confidence=0.95,
            )
            + "\n",
            encoding="utf-8",
        )

        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
            layer2_artifact_path=artifact,
        )

        # First residual: has a valid proposal.
        outcome_with_artifact = next(
            o for o in report.scenario_outcomes
            if o.scenario_id == residuals[0].scenario_id
        )
        assert outcome_with_artifact.layer2_outcome_type == "PROPOSAL_VALID"

        # Second residual: no artifact at all (missing).
        outcome_missing = next(
            o for o in report.scenario_outcomes
            if o.scenario_id == residuals[1].scenario_id
        )
        # Missing artifact: the orchestrator returns NO_PROPOSAL ("no artifact
        # available").  This is distinct from API_ERROR ("provider failed").
        assert outcome_missing.layer2_outcome_type == "NO_PROPOSAL"
        assert outcome_missing.routing_bucket == "EXCEPTION"

    def test_api_error_and_valid_proposal_distinguishable(self, tmp_path):
        """A valid proposal and an API_ERROR for different scenarios
        remain distinguishable in the final report."""
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        manifest = read_manifest(tmp_path)
        fingerprint = manifest.fingerprint()

        from reconciliation.loader import load_residuals
        residuals = load_residuals(tmp_path)
        assert len(residuals) >= 2

        # First residual: valid proposal.  Second: API_ERROR.
        artifact = tmp_path / "day4_audit.jsonl"
        artifact.write_text(
            _artifact_line(
                correlation_id="valid-1",
                fingerprint=fingerprint,
                presented=list(residuals[0].member_record_ids),
                outcome="PROPOSAL_VALID",
                proposed=list(residuals[0].member_record_ids),
                confidence=0.92,
            )
            + "\n"
            + _artifact_line(
                correlation_id="api-err-1",
                fingerprint=fingerprint,
                presented=list(residuals[1].member_record_ids),
                outcome="API_ERROR",
                proposed=[],
                confidence=None,
            )
            + "\n",
            encoding="utf-8",
        )

        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
            layer2_artifact_path=artifact,
        )

        valid = next(
            o for o in report.scenario_outcomes
            if o.scenario_id == residuals[0].scenario_id
        )
        error = next(
            o for o in report.scenario_outcomes
            if o.scenario_id == residuals[1].scenario_id
        )

        # Valid proposal: routed to AI_AUTO_ACCEPTED (confidence >= 0.90).
        assert valid.routing_bucket == "AI_AUTO_ACCEPTED"
        assert valid.layer2_outcome_type == "PROPOSAL_VALID"
        assert valid.ai_confidence == 0.92
        assert valid.ai_correct is True

        # API_ERROR: routed to EXCEPTION, outcome type is API_ERROR.
        assert error.routing_bucket == "EXCEPTION"
        assert error.layer2_outcome_type == "API_ERROR"
        assert error.ai_correct is None  # Can't judge, not False.

    def test_multiple_attempts_latest_success_wins(self, tmp_path):
        """When a scenario has both a PROPOSAL_VALID and a later API_ERROR,
        the successful evaluation is preserved (not overwritten)."""
        from reconciliation.evaluation.full_pipeline_evaluation import (
            _dataset_record_ids,
            load_layer2_artifacts,
            _map_artifacts_to_scenarios,
        )

        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        manifest = read_manifest(tmp_path)
        fingerprint = manifest.fingerprint()

        from reconciliation.loader import load_residuals
        residuals = load_residuals(tmp_path)
        target = residuals[0]

        unit_map = {
            u.scenario_id: u
            for u in _build_ground_truth_units(list(dataset.scenarios))
        }

        # Two artifacts for the same scenario:
        # 1. PROPOSAL_VALID at t1 (earlier)
        # 2. API_ERROR at t2 (later)
        artifact = tmp_path / "day4_audit.jsonl"
        artifact.write_text(
            _artifact_line(
                correlation_id="valid-1",
                fingerprint=fingerprint,
                presented=list(target.member_record_ids),
                outcome="PROPOSAL_VALID",
                proposed=list(target.member_record_ids),
                confidence=0.95,
            )
            + "\n"
            + _artifact_line(
                correlation_id="api-err-1",
                fingerprint=fingerprint,
                presented=list(target.member_record_ids),
                outcome="API_ERROR",
                proposed=[],
                confidence=None,
            )
            + "\n",
            encoding="utf-8",
        )

        artifacts = load_layer2_artifacts(artifact)
        scenario_map = _map_artifacts_to_scenarios(artifacts, residuals, unit_map)

        # The PROPOSAL_VALID must win — it was successful and the API_ERROR
        # should not erase it.
        assert target.scenario_id in scenario_map
        assert scenario_map[target.scenario_id].outcome.value == "PROPOSAL_VALID"

    def test_multiple_attempts_api_error_fills_empty_slot(self, tmp_path):
        """When only API_ERROR artifacts exist for a scenario, the API_ERROR
        must be preserved (not dropped into missing)."""
        from reconciliation.evaluation.full_pipeline_evaluation import (
            _dataset_record_ids,
            load_layer2_artifacts,
            _map_artifacts_to_scenarios,
        )

        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        manifest = read_manifest(tmp_path)
        fingerprint = manifest.fingerprint()

        from reconciliation.loader import load_residuals
        residuals = load_residuals(tmp_path)
        target = residuals[0]

        unit_map = {
            u.scenario_id: u
            for u in _build_ground_truth_units(list(dataset.scenarios))
        }

        # Two API_ERROR artifacts for the same scenario.
        artifact = tmp_path / "day4_audit.jsonl"
        artifact.write_text(
            _artifact_line(
                correlation_id="api-err-1",
                fingerprint=fingerprint,
                presented=list(target.member_record_ids),
                outcome="API_ERROR",
                proposed=[],
                confidence=None,
            )
            + "\n"
            + _artifact_line(
                correlation_id="api-err-2",
                fingerprint=fingerprint,
                presented=list(target.member_record_ids),
                outcome="API_ERROR",
                proposed=[],
                confidence=None,
            )
            + "\n",
            encoding="utf-8",
        )

        artifacts = load_layer2_artifacts(artifact)
        scenario_map = _map_artifacts_to_scenarios(artifacts, residuals, unit_map)

        # API_ERROR must be in the map (not dropped).
        assert target.scenario_id in scenario_map
        assert scenario_map[target.scenario_id].outcome.value == "API_ERROR"

    def test_end_to_end_api_error_evaluation(self, tmp_path):
        """End-to-end: API_ERROR artifacts flow through the full pipeline
        and produce correct, distinguishable evaluation outcomes."""
        dataset = generate_dataset(seed=42)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

        run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
        )
        manifest = read_manifest(tmp_path)
        fingerprint = manifest.fingerprint()

        from reconciliation.loader import load_residuals
        residuals = load_residuals(tmp_path)
        assert len(residuals) >= 3

        # Create a mix: valid, API_ERROR, and missing (no artifact).
        artifact = tmp_path / "day4_audit.jsonl"
        artifact.write_text(
            _artifact_line(
                correlation_id="valid-1",
                fingerprint=fingerprint,
                presented=list(residuals[0].member_record_ids),
                outcome="PROPOSAL_VALID",
                proposed=list(residuals[0].member_record_ids),
                confidence=0.95,
            )
            + "\n"
            + _artifact_line(
                correlation_id="api-err-1",
                fingerprint=fingerprint,
                presented=list(residuals[1].member_record_ids),
                outcome="API_ERROR",
                proposed=[],
                confidence=None,
            )
            + "\n",
            # residuals[2] has no artifact (genuinely missing).
            encoding="utf-8",
        )

        report = run_full_pipeline(
            dataset=dataset,
            config=config,
            output_dir=tmp_path,
            baseline_config=config,
            layer2_artifact_path=artifact,
        )

        valid = next(
            o for o in report.scenario_outcomes
            if o.scenario_id == residuals[0].scenario_id
        )
        error = next(
            o for o in report.scenario_outcomes
            if o.scenario_id == residuals[1].scenario_id
        )
        missing = next(
            o for o in report.scenario_outcomes
            if o.scenario_id == residuals[2].scenario_id
        )

        # Valid proposal: AI_AUTO_ACCEPTED.
        assert valid.routing_bucket == "AI_AUTO_ACCEPTED"
        assert valid.layer2_outcome_type == "PROPOSAL_VALID"
        assert valid.ai_correct is True

        # API_ERROR: EXCEPTION with distinct outcome type.
        assert error.routing_bucket == "EXCEPTION"
        assert error.layer2_outcome_type == "API_ERROR"
        assert error.ai_correct is None

        # Missing: EXCEPTION with NO_PROPOSAL outcome type (distinct from
        # API_ERROR — the orchestrator fills NO_PROPOSAL when no artifact exists).
        assert missing.routing_bucket == "EXCEPTION"
        assert missing.layer2_outcome_type == "NO_PROPOSAL"
        all_units = _build_ground_truth_units(list(dataset.scenarios))
        missing_unit = next(
            u for u in all_units if u.scenario_id == residuals[2].scenario_id
        )
        assert missing.ai_correct == (not missing_unit.has_real_match)

        # All three are distinguishable.
        assert valid.layer2_outcome_type != error.layer2_outcome_type
        assert error.layer2_outcome_type != missing.layer2_outcome_type
        assert valid.layer2_outcome_type != missing.layer2_outcome_type
