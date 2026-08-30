"""
Day 5.3 — Full pipeline evaluation harness.

Wires Layer 1, Layer 2 (via injectable orchestrator), Layer 3, and the naive
baseline together and computes every required metric using the primitives in
reconciliation.evaluation.primitives.

Layer 2 is intentionally abstracted behind ProposalOrchestrator so the harness
can use a real provider or a deterministic mock.  For the first official run
the harness defaults to a deterministic mock that produces reproducible
realistic outcomes; the report explicitly records the Layer 2 mode.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from reconciliation.baseline.naive_matcher import NaiveMatchResult, match as baseline_match
from reconciliation.domain.models import SourceType
from reconciliation.evaluation.dataset_generator import (
    CATEGORY_QUOTAS,
    EdgeCaseCategory,
    ExpectedLayer1Outcome,
    GeneratedDataset,
    GroundTruthScenario,
    _compute_record_id,
)
from reconciliation.evaluation.dataset_fingerprint import (
    DatasetManifest,
    freeze_or_verify_dataset,
)
from reconciliation.evaluation.ground_truth import GroundTruthUnit
from reconciliation.evaluation.metrics import EvaluationReport, evaluate as evaluate_l1
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
    build_record_outcomes,
    compute_ai_precision_at_threshold,
    compute_ai_recall,
    compute_baseline_comparison,
    compute_deterministic_metrics,
    compute_exception_composition,
    compute_false_accept_metrics,
    compute_review_queue_composition,
    compute_throughput_metrics,
    validate_record_outcomes,
)
from reconciliation.evaluation.residuals import persist_residuals
from reconciliation.layer2 import Layer2Case, reconstruct_layer2_case
from reconciliation.layer3 import route as layer3_route
from reconciliation.loader import load_normalized_records, load_residuals, ResidualScenario
from reconciliation.matcher import reconcile
from reconciliation.matcher_config import MatcherConfig
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_orchestration import ProposalOrchestrator
from reconciliation.proposal_validation import (
    ProposalOutcome,
    ProposalOutcomeType,
)
from reconciliation.retrieval import RetrievalConfig, retrieve_candidates


# ---------------------------------------------------------------------------
# Public report dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FullPipelineReport:
    dataset_seed: int
    total_scenarios: int
    total_records: int
    run_timestamp: str
    layer2_mode: str
    layer2_provenance: str

    throughput: ThroughputMetrics

    deterministic_metrics: DeterministicMetrics

    ai_precision_090: AiPrecisionAtThreshold
    ai_precision_075: AiPrecisionAtThreshold
    ai_precision_060: AiPrecisionAtThreshold

    ai_recall: AiRecallResult

    false_accept: FalseAcceptResult

    review_queue: ReviewQueueComposition

    exception_composition: ExceptionComposition

    baseline_comparison: BaselineComparison
    final_system_composition: Dict[str, Any]

    scenario_outcomes: Tuple[ScenarioOutcome, ...]
    record_outcomes: Tuple[RecordOutcome, ...]

    auto_accept_threshold: float
    review_threshold: float
    baseline_config: Dict[str, Any]
    dataset_fingerprint: str = ""


# ---------------------------------------------------------------------------
# Layer 2 artifact loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Layer2Artifact:
    correlation_id: str
    timestamp: str
    scenario_id: Optional[str]
    outcome: ProposalOutcomeType
    confidence: Optional[float]
    proposed_match_ids: Tuple[str, ...]
    presented_record_ids: Tuple[str, ...]
    dataset_fingerprint: Optional[str] = None


def load_layer2_artifacts(path: Path) -> Tuple[Layer2Artifact, ...]:
    artifacts: List[Layer2Artifact] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            presented = raw.get("presented_record_ids") or raw.get("candidate_ids_presented") or []
            proposal = raw.get("proposal") or {}
            artifacts.append(
                Layer2Artifact(
                    correlation_id=raw["correlation_id"],
                    timestamp=raw["timestamp"],
                    scenario_id=None,
                    outcome=ProposalOutcomeType(raw["outcome"]),
                    confidence=raw.get("confidence"),
                    proposed_match_ids=tuple(proposal.get("proposed_match_ids", [])),
                    presented_record_ids=tuple(presented),
                    dataset_fingerprint=raw.get("dataset_fingerprint"),
                )
            )
    return tuple(artifacts)


@dataclass(frozen=True)
class Layer2Provenance:
    """Summary of dataset/artifact provenance verification for artifact-backed runs.

    ``ok`` is True only when every artifact that carries a fingerprint matches the
    current dataset fingerprint, and every legacy artifact (without a fingerprint)
    references record IDs that all exist in the current dataset.
    """

    dataset_fingerprint: str
    dataset_seed: int
    total_artifacts: int
    artifacts_with_fingerprint: int
    fingerprints_matched: int
    fingerprints_mismatched: int
    legacy_artifacts: int
    record_id_coverage: float
    ok: bool
    mismatches: Tuple[str, ...]


def _dataset_record_ids(dataset: GeneratedDataset) -> set:
    return {_compute_record_id(spec) for spec in dataset.record_specs}


def verify_layer2_artifact_provenance(
    dataset: GeneratedDataset,
    artifacts: Tuple[Layer2Artifact, ...],
    manifest: DatasetManifest,
) -> Layer2Provenance:
    """Verify that Layer 2 artifacts correspond to the current dataset.

    Two complementary signals are used:
      * Direct fingerprint match for artifacts that were stamped by a
        fingerprint-aware Layer 2 runner.  These are provenance-verified:
        the artifact is cryptographically proven to have been produced from
        the exact dataset version identified by ``manifest``.
      * Record-ID coverage for legacy artifacts (no fingerprint): every record
        ID referenced by the artifact must exist in the current dataset.  This
        establishes compatibility, not provenance.  Legacy artifacts are
        provenance-unverified even when all referenced IDs are present.
    """
    dataset_fingerprint = manifest.fingerprint()
    dataset_ids = _dataset_record_ids(dataset)

    with_fingerprint = 0
    matched = 0
    mismatched = 0
    legacy = 0
    mismatches: List[str] = []
    referenced_ids: set = set()

    for artifact in artifacts:
        referenced_ids.update(artifact.presented_record_ids)
        referenced_ids.update(artifact.proposed_match_ids)
        if artifact.dataset_fingerprint:
            with_fingerprint += 1
            if artifact.dataset_fingerprint == dataset_fingerprint:
                matched += 1
            else:
                mismatched += 1
                mismatches.append(
                    f"artifact {artifact.correlation_id} fingerprint "
                    f"{artifact.dataset_fingerprint[:12]}… does not match dataset"
                )
        else:
            legacy += 1

    missing_ids = referenced_ids - dataset_ids
    total_referenced = len(referenced_ids)
    coverage = (
        (total_referenced - len(missing_ids)) / total_referenced
        if total_referenced > 0
        else 1.0
    )
    if missing_ids:
        mismatches.append(
            f"{len(missing_ids)} record ID(s) referenced by artifacts are not "
            f"present in the current dataset (e.g. {list(missing_ids)[:3]})."
        )

    ok = mismatched == 0 and len(missing_ids) == 0

    return Layer2Provenance(
        dataset_fingerprint=dataset_fingerprint,
        dataset_seed=manifest.dataset_seed,
        total_artifacts=len(artifacts),
        artifacts_with_fingerprint=with_fingerprint,
        fingerprints_matched=matched,
        fingerprints_mismatched=mismatched,
        legacy_artifacts=legacy,
        record_id_coverage=coverage,
        ok=ok,
        mismatches=tuple(mismatches),
    )


def _map_artifacts_to_scenarios(
    artifacts: Tuple[Layer2Artifact, ...],
    residuals: Tuple[ResidualScenario, ...],
    unit_map: Dict[str, GroundTruthUnit],
) -> Dict[str, Layer2Artifact]:
    scenario_map: Dict[str, Layer2Artifact] = {}

    member_lists: Dict[str, Tuple[str, ...]] = {
        rs.scenario_id: rs.member_record_ids for rs in residuals
    }

    for artifact in artifacts:
        best_scenario_id = None
        best_overlap = 0
        for scen_id, member_ids in member_lists.items():
            presented = artifact.presented_record_ids
            n = len(member_ids)
            if n <= len(presented) and tuple(presented[:n]) == member_ids:
                overlap = n
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_scenario_id = scen_id

        if best_scenario_id is not None:
            existing = scenario_map.get(best_scenario_id)
            if existing is None or artifact.timestamp > existing.timestamp:
                # Prefer non-API_ERROR outcomes: a successful evaluation is
                # never erased by a later provider failure.  An API_ERROR
                # only fills an empty slot so it remains visible in metrics
                # rather than collapsing into "missing artifact" / NO_PROPOSAL.
                if existing is None or artifact.outcome != ProposalOutcomeType.API_ERROR:
                    scenario_map[best_scenario_id] = artifact

    return scenario_map


class _ArtifactBackedOrchestrator:
    def __init__(
        self,
        scenario_outcomes: Dict[str, Layer2Artifact],
        unit_map: Dict[str, GroundTruthUnit],
    ) -> None:
        self._scenario_outcomes = scenario_outcomes
        self._unit_map = unit_map

    def resolve(
        self,
        case: Layer2Case,
        retrieval_result: Any,
    ) -> ProposalOutcome:
        artifact = self._scenario_outcomes.get(case.scenario_id)
        presented_ids = tuple(
            dict.fromkeys(
                [r.record_id for r in case.member_records]
                + [c.record.record_id for c in retrieval_result.candidates]
            )
        )

        if artifact is None:
            return ProposalOutcome(
                outcome=ProposalOutcomeType.NO_PROPOSAL,
                proposal=None,
                presented_record_ids=presented_ids,
                reason="No Layer 2 artifact available for this scenario.",
            )

        if artifact.outcome == ProposalOutcomeType.PROPOSAL_VALID:
            proposal = MatchProposal(
                proposed_match_ids=list(artifact.proposed_match_ids),
                confidence=artifact.confidence or 0.0,
                rationale="artifact",
            )
            return ProposalOutcome(
                outcome=ProposalOutcomeType.PROPOSAL_VALID,
                proposal=proposal,
                presented_record_ids=presented_ids,
                reason="Valid proposal from Layer 2 artifact.",
            )

        if artifact.outcome == ProposalOutcomeType.NO_PROPOSAL:
            return ProposalOutcome(
                outcome=ProposalOutcomeType.NO_PROPOSAL,
                proposal=None,
                presented_record_ids=presented_ids,
                reason="Model returned no proposed match IDs (from artifact).",
            )

        return ProposalOutcome(
            outcome=ProposalOutcomeType.API_ERROR,
            proposal=None,
            presented_record_ids=presented_ids,
            reason=f"Provider failure recorded in artifact: {artifact.outcome.value}",
        )


# ---------------------------------------------------------------------------
# Deterministic mock orchestrator (test/smoke-test only)
# ---------------------------------------------------------------------------


class _MockLayer2Orchestrator:
    """
    Deterministic mock that simulates Layer 2 outcomes for every residual
    scenario.  Outcomes are selected via a hash of scenario_id so results are
    fully reproducible across runs and platforms.

    The mock deliberately includes:
      * correct proposals at various confidence levels,
      * NO_PROPOSAL on a realistic fraction of residuals,
      * a small number of false-accept proposals (wrong IDs, high confidence),
      * a small number of wrong proposals at review confidence.

    It does NOT call any external service.
    """

    def __init__(
        self,
        scenario_map: Dict[str, GroundTruthScenario],
        unit_map: Dict[str, GroundTruthUnit],
        seed: int = 42,
    ) -> None:
        self._scenario_map = scenario_map
        self._unit_map = unit_map
        self._seed = seed

    @staticmethod
    def _bucket(scenario_id: str) -> int:
        h = hashlib.md5(f"{scenario_id}:mock-layer2".encode()).hexdigest()
        return int(h, 16) % 100

    def resolve(
        self,
        case: Layer2Case,
        retrieval_result: Any,
    ) -> ProposalOutcome:
        scenario = self._scenario_map.get(case.scenario_id)
        unit = self._unit_map.get(case.scenario_id)

        presented_ids = tuple(
            dict.fromkeys(
                [r.record_id for r in case.member_records]
                + [c.record.record_id for c in retrieval_result.candidates]
            )
        )

        if scenario is None or unit is None:
            return ProposalOutcome(
                outcome=ProposalOutcomeType.NO_PROPOSAL,
                proposal=None,
                presented_record_ids=presented_ids,
                reason="Unknown scenario.",
            )

        bucket = self._bucket(case.scenario_id)

        is_orphan = scenario.category == EdgeCaseCategory.TRUE_ORPHAN
        is_rounding_high = (
            scenario.category == EdgeCaseCategory.ROUNDING_DIFFERENCE
            and case.scenario_id >= "RND-009"
        )
        has_real_match = not is_orphan and not is_rounding_high
        member_ids = list(unit.member_record_ids)

        if has_real_match:
            return self._real_match_outcome(bucket, member_ids, presented_ids)
        return self._no_match_outcome(bucket, member_ids, presented_ids)

    def _real_match_outcome(
        self,
        bucket: int,
        member_ids: List[str],
        presented_ids: Tuple[str, ...],
    ) -> ProposalOutcome:
        if bucket < 35:
            conf = 0.90 + (bucket / 35) * 0.05
            return self._valid(member_ids, conf, presented_ids)
        if bucket < 55:
            conf = 0.75 + ((bucket - 35) / 20) * 0.14
            return self._valid(member_ids, conf, presented_ids)
        if bucket < 70:
            conf = 0.60 + ((bucket - 55) / 15) * 0.14
            return self._valid(member_ids, conf, presented_ids)
        if bucket < 85:
            conf = 0.40 + ((bucket - 70) / 15) * 0.19
            return self._valid(member_ids, conf, presented_ids)
        if bucket < 95:
            return ProposalOutcome(
                outcome=ProposalOutcomeType.NO_PROPOSAL,
                proposal=None,
                presented_record_ids=presented_ids,
                reason="Model returned no proposed match IDs.",
            )
        if bucket < 98:
            conf = 0.90 + ((bucket - 95) / 3) * 0.05
            wrong = member_ids[:-1] if len(member_ids) > 1 else presented_ids[:1]
            return self._valid(wrong, conf, presented_ids)
        conf = 0.75 + ((bucket - 98) / 2) * 0.14
        wrong = member_ids[:-1] if len(member_ids) > 1 else presented_ids[:1]
        return self._valid(wrong, conf, presented_ids)

    def _no_match_outcome(
        self,
        bucket: int,
        member_ids: List[str],
        presented_ids: Tuple[str, ...],
    ) -> ProposalOutcome:
        if bucket < 65:
            return ProposalOutcome(
                outcome=ProposalOutcomeType.NO_PROPOSAL,
                proposal=None,
                presented_record_ids=presented_ids,
                reason="Model returned no proposed match IDs.",
            )
        if bucket < 80:
            conf = 0.40 + ((bucket - 65) / 15) * 0.19
            wrong = presented_ids[:1] if presented_ids else []
            return self._valid(wrong, conf, presented_ids)
        if bucket < 90:
            conf = 0.60 + ((bucket - 80) / 10) * 0.14
            wrong = presented_ids[:1] if presented_ids else []
            return self._valid(wrong, conf, presented_ids)
        if bucket < 95:
            conf = 0.75 + ((bucket - 90) / 5) * 0.14
            wrong = presented_ids[:1] if presented_ids else []
            return self._valid(wrong, conf, presented_ids)
        if bucket < 98:
            conf = 0.90 + ((bucket - 95) / 3) * 0.05
            wrong = presented_ids[:1] if presented_ids else []
            return self._valid(wrong, conf, presented_ids)
        return ProposalOutcome(
            outcome=ProposalOutcomeType.NO_PROPOSAL,
            proposal=None,
            presented_record_ids=presented_ids,
            reason="Model returned no proposed match IDs.",
        )

    @staticmethod
    def _valid(
        proposed_ids: List[str],
        confidence: float,
        presented_ids: Tuple[str, ...],
    ) -> ProposalOutcome:
        return ProposalOutcome(
            outcome=ProposalOutcomeType.PROPOSAL_VALID,
            proposal=MatchProposal(
                proposed_match_ids=proposed_ids,
                confidence=confidence,
                rationale="mock",
            ),
            presented_record_ids=presented_ids,
            reason="Valid proposal.",
        )


# ---------------------------------------------------------------------------
# Public harness API
# ---------------------------------------------------------------------------


def run_full_pipeline(
    dataset: GeneratedDataset,
    config: MatcherConfig,
    output_dir: Path,
    *,
    baseline_config: Optional[MatcherConfig] = None,
    retrieval_config: Optional[RetrievalConfig] = None,
    layer2_artifact_path: Optional[Path] = None,
    orchestrator_factory: Optional[
        callable[[Dict[str, GroundTruthScenario], Dict[str, GroundTruthUnit]], ProposalOrchestrator]
    ] = None,
) -> FullPipelineReport:
    """
    Execute the full Concord pipeline (Layer 1 + baseline + Layer 2 + Layer 3)
    against the fixed dataset and return a complete evaluation report.

    Layer 2 data source resolution:
      1. If ``orchestrator_factory`` is provided, it is used directly.
      2. Else if ``layer2_artifact_path`` exists, load real Day 4 audit artifacts
         from that path and use them to drive Layer 2 outcomes.
      3. Else fall back to the deterministic mock orchestrator (smoke-test only).
    """
    baseline_config = baseline_config or config
    retrieval_config = retrieval_config or RetrievalConfig()

    output_dir.mkdir(parents=True, exist_ok=True)

    from reconciliation.evaluation.dataset_fingerprint import read_manifest, verify_dataset
    existing_manifest = read_manifest(output_dir)
    if existing_manifest is not None:
        verification = verify_dataset(output_dir, existing_manifest)
        if not verification.ok:
            raise ValueError(
                "Dataset drift detected relative to the frozen manifest: "
                + "; ".join(verification.details)
                + f". Mismatched files: {verification.mismatches}; "
                f"Missing files: {verification.missing}"
            )
        dataset_manifest = existing_manifest
        manifest_status = "verified"
    else:
        from reconciliation.evaluation.dataset_generator import write_dataset
        write_dataset(dataset, output_dir)
        manifest_status = "unfrozen"

    normalized = load_normalized_records(output_dir)

    scenario_map = {s.scenario_id: s for s in dataset.scenarios}
    unit_map = {
        u.scenario_id: u
        for u in _build_ground_truth_units(list(dataset.scenarios))
    }

    record_to_scenario: Dict[str, str] = {}
    scenario_to_records: Dict[str, List[str]] = {}
    for unit in unit_map.values():
        for rid in unit.member_record_ids:
            record_to_scenario[rid] = unit.scenario_id
        scenario_to_records[unit.scenario_id] = list(unit.member_record_ids)

    all_record_ids = [r.record_id for r in normalized]

    layer1_start = time.perf_counter()
    l1_result = reconcile(list(normalized), config)
    layer1_time_ms = (time.perf_counter() - layer1_start) * 1000.0

    l1_decisions = list(l1_result.decisions)
    l1_residual_ids = set(l1_result.residual_record_ids)

    l1_report = evaluate_l1(
        scenarios=list(dataset.scenarios),
        decisions=l1_decisions,
        residual_record_ids=list(l1_result.residual_record_ids),
        scenario_units=list(unit_map.values()),
    )

    if manifest_status != "verified":
        persist_residuals(
            scenarios=list(dataset.scenarios),
            pipeline_residual_scenario_ids=list(l1_report.pipeline_residual_scenario_ids),
            output_dir=output_dir,
        )
        dataset_manifest, manifest_status = freeze_or_verify_dataset(
            output_dir, dataset_seed=42
        )

    dataset_fingerprint = dataset_manifest.fingerprint()

    l1_eval_map = {ev.scenario_id: ev for ev in l1_report.scenario_evaluations}

    baseline_start = time.perf_counter()
    baseline_result = baseline_match(list(normalized), baseline_config)
    baseline_time_ms = (time.perf_counter() - baseline_start) * 1000.0

    baseline_match_ids: set = set()
    for a, b in baseline_result.matches:
        baseline_match_ids.add(a)
        baseline_match_ids.add(b)

    residuals = load_residuals(output_dir)

    if orchestrator_factory is not None:
        orchestrator = orchestrator_factory(scenario_map, unit_map)
        layer2_mode = "injected_orchestrator"
        layer2_provenance = "Custom orchestrator factory injected by caller."
    elif layer2_artifact_path is not None and layer2_artifact_path.exists():
        artifacts = load_layer2_artifacts(layer2_artifact_path)
        provenance = verify_layer2_artifact_provenance(
            dataset=dataset,
            artifacts=artifacts,
            manifest=dataset_manifest,
        )
        if not provenance.ok:
            raise ValueError(
                "Refusing to evaluate Layer 2 artifacts against a dataset with a "
                "provenance mismatch. This prevents trusting Layer 2 metrics that "
                f"were produced from a different dataset version. "
                f"Detail: {'; '.join(provenance.mismatches)}"
            )
        artifact_scenario_map = _map_artifacts_to_scenarios(artifacts, residuals, unit_map)
        orchestrator = _ArtifactBackedOrchestrator(artifact_scenario_map, unit_map)
        layer2_mode = "artifact-backed"
        covered = len(artifact_scenario_map)
        api_error_count = sum(
            1 for a in artifact_scenario_map.values()
            if a.outcome == ProposalOutcomeType.API_ERROR
        )
        provenance_summary = (
            f"Dataset fingerprint {dataset_fingerprint[:12]}… verified; "
            f"{covered} of {len(residuals)} residual scenarios have Layer 2 "
            f"artifacts ({api_error_count} API_ERROR); "
            f"record-id coverage {provenance.record_id_coverage:.0%}; "
            f"{provenance.fingerprints_matched} artifact(s) provenance-verified "
            f"(fingerprint-matched), "
            f"{provenance.legacy_artifacts} legacy artifact(s) provenance-unverified "
            f"(record-id compatibility checked only)."
        )
        layer2_provenance = (
            f"Real Layer 2 artifacts from {layer2_artifact_path.name}. "
            + provenance_summary
        )
    else:
        orchestrator = _MockLayer2Orchestrator(scenario_map, unit_map)
        layer2_mode = "deterministic_mock"
        layer2_provenance = (
            "No real Layer 2 artifacts available. "
            "Deterministic mock orchestrator used for smoke-testing only."
        )

    layer2_outcomes: List[ProposalOutcome] = []
    layer2_scenario_map: Dict[str, ProposalOutcome] = {}
    layer2_total_ms = 0.0

    for residual in residuals:
        case = reconstruct_layer2_case(
            scenario_id=residual.scenario_id,
            member_record_ids=residual.member_record_ids,
            normalized_records=tuple(normalized),
        )
        retrieval = retrieve_candidates(case, tuple(normalized), retrieval_config)

        t0 = time.perf_counter()
        outcome = orchestrator.resolve(case, retrieval)
        elapsed = (time.perf_counter() - t0) * 1000.0
        layer2_total_ms += elapsed

        layer2_outcomes.append(outcome)
        layer2_scenario_map[residual.scenario_id] = outcome

    routing_decisions = layer3_route(
        layer1_decisions=l1_decisions,
        layer2_outcomes=layer2_outcomes,
        all_records=list(normalized),
    )

    record_routing_map: Dict[str, str] = {}
    record_confidence_map: Dict[str, Optional[float]] = {}
    record_layer2_time_map: Dict[str, Optional[float]] = {}

    for rd in routing_decisions:
        record_routing_map[rd.record_id] = rd.bucket.value
        record_confidence_map[rd.record_id] = rd.confidence
        scen_id = record_to_scenario.get(rd.record_id)
        if scen_id and scen_id in layer2_scenario_map:
            record_layer2_time_map[rd.record_id] = layer2_total_ms / len(layer2_scenario_map) if layer2_scenario_map else None
        else:
            record_layer2_time_map[rd.record_id] = None

    scenario_outcomes: List[ScenarioOutcome] = []

    for scen in dataset.scenarios:
        scen_id = scen.scenario_id
        member_ids = scenario_to_records[scen_id]
        unit = unit_map[scen_id]
        l1_eval = l1_eval_map.get(scen_id)

        deterministic_matched = l1_eval.matched if l1_eval else False
        deterministic_correct = l1_eval.correct if l1_eval else False

        baseline_matched = any(rid in baseline_match_ids for rid in member_ids)
        baseline_correct = any(
            tuple(sorted(pair)) == tuple(sorted(member_ids))
            for pair in baseline_result.matches
        )

        l2_outcome = layer2_scenario_map.get(scen_id)
        l2_outcome_type: Optional[str] = (
            l2_outcome.outcome.value if l2_outcome is not None else None
        )

        if deterministic_matched or l2_outcome is None:
            routing_bucket = "DETERMINISTIC_MATCH" if deterministic_matched else "EXCEPTION"
            ai_confidence = None
            ai_proposal_ids: Tuple[str, ...] = ()
            if deterministic_matched:
                ai_correct = deterministic_correct
            else:
                ai_correct = not unit.has_real_match
        else:
            if l2_outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID and l2_outcome.proposal:
                conf = l2_outcome.proposal.confidence
                if conf >= 0.90:
                    routing_bucket = "AI_AUTO_ACCEPTED"
                elif conf >= 0.60:
                    routing_bucket = "HUMAN_REVIEW"
                else:
                    routing_bucket = "EXCEPTION"
                ai_confidence = conf
                ai_proposal_ids = tuple(l2_outcome.proposal.proposed_match_ids)
                ai_correct = (
                    tuple(sorted(l2_outcome.proposal.proposed_match_ids))
                    == tuple(sorted(_expected_match_ids(scen, unit)))
                )
            elif l2_outcome.outcome == ProposalOutcomeType.API_ERROR:
                # Provider/infrastructure failure: the AI never ran, so we
                # cannot judge its correctness.  Route to EXCEPTION and
                # leave ai_correct as None to distinguish from AI miss.
                routing_bucket = "EXCEPTION"
                ai_confidence = None
                ai_proposal_ids = ()
                ai_correct = None
            else:
                routing_bucket = "EXCEPTION"
                ai_confidence = None
                ai_proposal_ids = ()
                ai_correct = not unit.has_real_match

        scenario_outcomes.append(
            ScenarioOutcome(
                scenario_id=scen_id,
                category=scen.category,
                is_true_orphan=unit.is_true_orphan,
                has_real_match=unit.has_real_match,
                baseline_matched=baseline_matched,
                baseline_correct=baseline_correct,
                deterministic_matched=deterministic_matched,
                deterministic_correct=deterministic_correct,
                routing_bucket=routing_bucket,
                ai_confidence=ai_confidence,
                ai_proposal_ids=ai_proposal_ids,
                ai_correct=ai_correct,
                layer2_outcome_type=l2_outcome_type,
                layer1_time_ms=layer1_time_ms,
                layer2_time_ms=layer2_total_ms if scen_id in layer2_scenario_map else None,
            )
        )

    record_outcomes_list = build_record_outcomes(
        scenario_outcomes=scenario_outcomes,
        record_scenario_map=record_to_scenario,
        record_routing_map=record_routing_map,
        record_confidence_map=record_confidence_map,
        record_layer2_time_ms=record_layer2_time_map,
    )
    validate_record_outcomes(record_outcomes_list)

    record_outcomes = tuple(
        ro for ro in record_outcomes_list if isinstance(ro, RecordOutcome)
    )

    deterministic_metrics = compute_deterministic_metrics(scenario_outcomes)
    ai_precision_090 = compute_ai_precision_at_threshold(scenario_outcomes, 0.90)
    ai_precision_075 = compute_ai_precision_at_threshold(scenario_outcomes, 0.75)
    ai_precision_060 = compute_ai_precision_at_threshold(scenario_outcomes, 0.60)
    ai_recall = compute_ai_recall(scenario_outcomes)
    false_accept = compute_false_accept_metrics(record_outcomes)
    review_queue = compute_review_queue_composition(record_outcomes)
    exception_composition = compute_exception_composition(record_outcomes)
    baseline_comparison = compute_baseline_comparison(scenario_outcomes)
    throughput = compute_throughput_metrics(scenario_outcomes)

    final_composition = _compute_final_system_composition(record_outcomes)

    return FullPipelineReport(
        dataset_seed=42,
        total_scenarios=len(dataset.scenarios),
        total_records=len(normalized),
        run_timestamp=datetime.now().isoformat(),
        layer2_mode=layer2_mode,
        layer2_provenance=layer2_provenance,
        throughput=throughput,
        deterministic_metrics=deterministic_metrics,
        ai_precision_090=ai_precision_090,
        ai_precision_075=ai_precision_075,
        ai_precision_060=ai_precision_060,
        ai_recall=ai_recall,
        false_accept=false_accept,
        review_queue=review_queue,
        exception_composition=exception_composition,
        baseline_comparison=baseline_comparison,
        final_system_composition=final_composition,
        scenario_outcomes=tuple(scenario_outcomes),
        record_outcomes=record_outcomes,
        auto_accept_threshold=0.90,
        review_threshold=0.60,
        baseline_config={
            "amount_tolerance_paise": baseline_config.amount_tolerance_paise,
            "date_window_days": baseline_config.date_window_days,
        },
        dataset_fingerprint=dataset_fingerprint,
    )


# ---------------------------------------------------------------------------
# Helper (duplicated from evaluation_harness to avoid circular imports)
# ---------------------------------------------------------------------------


def _build_ground_truth_units(
    scenarios: List[GroundTruthScenario],
) -> List[GroundTruthUnit]:
    units = []
    for scen in scenarios:
        record_ids = tuple(_compute_record_id(spec) for spec in scen.record_specs)
        units.append(
            GroundTruthUnit(
                scenario_id=scen.scenario_id,
                member_record_ids=record_ids,
                true_category=scen.category,
                is_true_orphan=len(record_ids) == 1,
                has_real_match=scen.has_real_match,
            )
        )
    return units


def _expected_match_ids(
    scenario: GroundTruthScenario,
    unit: GroundTruthUnit,
) -> Tuple[str, ...]:
    if scenario.category == EdgeCaseCategory.DUPLICATE:
        return tuple(
            _compute_record_id(spec)
            for spec in scenario.record_specs
            if spec.source_type == SourceType.SETTLEMENT
        )
    return unit.member_record_ids


def _compute_final_system_composition(
    record_outcomes: Tuple[RecordOutcome, ...],
) -> Dict[str, Any]:
    total = len(record_outcomes)
    buckets: Dict[str, int] = {
        "DETERMINISTIC_MATCH": 0,
        "AI_AUTO_ACCEPTED": 0,
        "HUMAN_REVIEW": 0,
        "EXCEPTION": 0,
    }
    for ro in record_outcomes:
        buckets[ro.routing_bucket] = buckets.get(ro.routing_bucket, 0) + 1

    return {
        "total_records": total,
        "counts": buckets,
        "rates": {
            k: (v / total if total > 0 else None)
            for k, v in buckets.items()
        },
        "note": (
            "Final system routing composition at the record level. "
            "DETERMINISTIC_MATCH = Layer 1 exact/amount-date match. "
            "AI_AUTO_ACCEPTED = Layer 2 proposal accepted automatically. "
            "HUMAN_REVIEW = Layer 2 proposal needs human review. "
            "EXCEPTION = no valid Layer 2 outcome or low confidence."
        ),
    }


# ---------------------------------------------------------------------------
# Report serialization
# ---------------------------------------------------------------------------


def serialize_report(report: FullPipelineReport) -> Dict[str, Any]:
    """Convert a FullPipelineReport to a JSON-serializable dict."""
    return {
        "metadata": {
            "dataset_seed": report.dataset_seed,
            "total_scenarios": report.total_scenarios,
            "total_records": report.total_records,
            "run_timestamp": report.run_timestamp,
            "layer2_mode": report.layer2_mode,
            "layer2_provenance": report.layer2_provenance,
            "auto_accept_threshold": report.auto_accept_threshold,
            "review_threshold": report.review_threshold,
            "baseline_config": report.baseline_config,
            "dataset_fingerprint": report.dataset_fingerprint,
        },
        "throughput": {
            "total_batch_time_ms": report.throughput.total_batch_time_ms,
            "layer1_time_ms": report.throughput.layer1_time_ms,
            "layer2_time_ms": report.throughput.layer2_time_ms,
        },
        "deterministic_match_rate": {
            "total_scenarios": report.deterministic_metrics.total_scenarios,
            "matched": report.deterministic_metrics.matched,
            "match_rate": report.deterministic_metrics.match_rate,
            "precision": report.deterministic_metrics.precision,
            "correct": report.deterministic_metrics.correct,
        },
        "ai_precision_at_thresholds": {
            "confidence_>=_0.90": {
                "threshold": report.ai_precision_090.threshold,
                "precision": report.ai_precision_090.precision,
                "true_positives": report.ai_precision_090.true_positives,
                "false_positives": report.ai_precision_090.false_positives,
                "total": report.ai_precision_090.total,
            },
            "confidence_>=_0.75": {
                "threshold": report.ai_precision_075.threshold,
                "precision": report.ai_precision_075.precision,
                "true_positives": report.ai_precision_075.true_positives,
                "false_positives": report.ai_precision_075.false_positives,
                "total": report.ai_precision_075.total,
            },
            "confidence_>=_0.60": {
                "threshold": report.ai_precision_060.threshold,
                "precision": report.ai_precision_060.precision,
                "true_positives": report.ai_precision_060.true_positives,
                "false_positives": report.ai_precision_060.false_positives,
                "total": report.ai_precision_060.total,
            },
        },
        "ai_recall": {
            "recall": report.ai_recall.recall,
            "true_positives": report.ai_recall.true_positives,
            "denominator": report.ai_recall.denominator,
            "recall_attempted": report.ai_recall.recall_attempted,
            "denominator_attempted": report.ai_recall.denominator_attempted,
            "note": (
                "'recall' is system-wide (all residuals with real match). "
                "'recall_attempted' excludes API_ERROR provider failures "
                "(only scenarios where Layer 2 actually ran)."
            ),
        },
        "false_accept_rate": {
            "rate": report.false_accept.rate,
            "count": report.false_accept.count,
            "total_auto_accepted": report.false_accept.total_auto_accepted,
        },
        "review_queue_composition": {
            "total": report.review_queue.total,
            "percentage_of_batch": (
                report.review_queue.total / report.total_records * 100.0
                if report.total_records > 0
                else None
            ),
            "counts_by_category": {
                cat.value: report.review_queue.counts[cat] for cat in EdgeCaseCategory
            },
            "percentages_by_category": {
                cat.value: report.review_queue.percentages[cat] for cat in EdgeCaseCategory
            },
            "denominator_note": "Percentages are over total HUMAN_REVIEW records, not total batch.",
        },
        "exception_composition": {
            "total": report.exception_composition.total,
            "percentage_of_batch": (
                report.exception_composition.total / report.total_records * 100.0
                if report.total_records > 0
                else None
            ),
            "correctly_refused": report.exception_composition.correctly_refused,
            "correctly_refused_pct": report.exception_composition.correctly_refused_pct,
            "should_have_been_caught": report.exception_composition.should_have_been_caught,
            "should_have_been_caught_pct": report.exception_composition.should_have_been_caught_pct,
            "correctly_refused_note": "No real match exists in ground truth (has_real_match=False).",
            "should_have_been_caught_note": "Exception bucket on a scenario that has a real match.",
        },
        "baseline_comparison": {
            "baseline_match_rate": report.baseline_comparison.baseline_match_rate,
            "deterministic_match_rate": report.baseline_comparison.deterministic_match_rate,
            "match_rate_delta": report.baseline_comparison.match_rate_delta,
            "baseline_precision": report.baseline_comparison.baseline_precision,
            "deterministic_precision": report.baseline_comparison.deterministic_precision,
            "precision_delta": report.baseline_comparison.precision_delta,
            "comparable_metrics_note": "Match rate and precision are directly comparable between baseline and Layer 1 only. Baseline does not implement Layer 2/AI metrics, so this table does not compare against the full layered system.",
        },
        "final_system_composition": report.final_system_composition,
        "raw_outcomes": {
            "scenario_outcomes": [
                {
                    "scenario_id": o.scenario_id,
                    "category": o.category.value,
                    "is_true_orphan": o.is_true_orphan,
                    "has_real_match": o.has_real_match,
                    "baseline_matched": o.baseline_matched,
                    "baseline_correct": o.baseline_correct,
                    "deterministic_matched": o.deterministic_matched,
                    "deterministic_correct": o.deterministic_correct,
                    "routing_bucket": o.routing_bucket,
                    "ai_confidence": o.ai_confidence,
                    "ai_proposal_ids": list(o.ai_proposal_ids),
                    "ai_correct": o.ai_correct,
                    "layer2_outcome_type": o.layer2_outcome_type,
                }
                for o in report.scenario_outcomes
            ],
            "record_outcomes": [
                {
                    "record_id": o.record_id,
                    "scenario_id": o.scenario_id,
                    "category": o.category.value,
                    "routing_bucket": o.routing_bucket,
                    "ai_confidence": o.ai_confidence,
                    "is_false_accept": o.is_false_accept,
                    "exception_correctly_refused": o.exception_correctly_refused,
                    "exception_should_have_been_caught": o.exception_should_have_been_caught,
                }
                for o in report.record_outcomes
            ],
        },
    }


def write_human_readable_report(report: FullPipelineReport, path: Path) -> None:
    """Write a Markdown summary of the evaluation report."""
    lines = [
        "# Concord Day 5 — Full Pipeline Evaluation Report",
        "",
        f"**Run timestamp:** {report.run_timestamp}",
        f"**Dataset seed:** {report.dataset_seed}",
        f"**Total scenarios:** {report.total_scenarios}",
        f"**Total records:** {report.total_records}",
        f"**Layer 2 mode:** {report.layer2_mode}",
        f"**Layer 2 provenance:** {report.layer2_provenance}",
        f"**Auto-accept threshold:** {report.auto_accept_threshold}",
        f"**Review threshold:** {report.review_threshold}",
        f"**Baseline config:** {report.baseline_config}",
        "",
        "---",
        "",
        "## 1. Deterministic Match Rate",
        "",
        f"- **Total scenarios:** {report.deterministic_metrics.total_scenarios}",
        f"- **Matched:** {report.deterministic_metrics.matched}",
        f"- **Match rate:** {_fmt_pct(report.deterministic_metrics.match_rate)}",
        "",
        "## 2. Deterministic Match Precision",
        "",
        f"- **Correct:** {report.deterministic_metrics.correct}",
        f"- **Precision:** {_fmt_pct(report.deterministic_metrics.precision)}",
        "",
        "---",
        "",
        "## 3. AI-Proposal Precision at Three Analysis Cuts",
        "",
        _fmt_threshold("confidence >= 0.90", report.ai_precision_090),
        _fmt_threshold("confidence >= 0.75", report.ai_precision_075),
        _fmt_threshold("confidence >= 0.60", report.ai_precision_060),
        "",
        "---",
        "",
        "## 4. AI-Proposal Recall",
        "",
        "### AI recall (system-wide)",
        "",
        "*Denominator: all residual scenarios with a real match, regardless of Layer 2 outcome.*",
        "",
        f"- **True positives:** {report.ai_recall.true_positives}",
        f"- **Denominator:** {report.ai_recall.denominator}",
        f"- **Recall:** {_fmt_pct(report.ai_recall.recall)}",
        "",
        "### AI recall (of attempted scenarios)",
        "",
        "*Denominator: only residuals where Layer 2 actually ran (excludes API_ERROR provider failures).*",
        "",
        f"- **True positives:** {report.ai_recall.true_positives}",
        f"- **Denominator:** {report.ai_recall.denominator_attempted}",
        f"- **Recall:** {_fmt_pct(report.ai_recall.recall_attempted)}",
        "",
        "---",
        "",
        "## 5. False-Accept Rate",
        "",
        "> **This is the headline safety metric.**",
        "",
        f"- **Total auto-accepted:** {report.false_accept.total_auto_accepted}",
        f"- **False accepts:** {report.false_accept.count}",
        f"- **False-accept rate:** {_fmt_pct(report.false_accept.rate)}",
        "",
        "---",
        "",
        "## 6. Review-Queue Composition (HUMAN_REVIEW)",
        "",
        f"- **Total count:** {report.review_queue.total}",
        f"- **Percentage of batch:** {_fmt_pct(report.review_queue.total / report.total_records) if report.total_records else 'N/A'}",
        "",
        "### By category",
        "",
        "| Category | Count | % of review queue |",
        "|----------|-------|-------------------|",
    ]
    for cat in EdgeCaseCategory:
        count = report.review_queue.counts[cat]
        pct = report.review_queue.percentages[cat]
        lines.append(
            f"| {cat.value} | {count} | {_fmt_already_pct(pct)} |"
        )
    lines.extend([
        "",
        "*Note: Percentages are over total HUMAN_REVIEW records (denominator = review queue total), not total batch.*",
        "",
        "---",
        "",
        "## 7. Exception Composition",
        "",
        f"- **Total count:** {report.exception_composition.total}",
        f"- **Percentage of batch:** {_fmt_pct(report.exception_composition.total / report.total_records) if report.total_records else 'N/A'}",
        f"- **Correctly refused (no real match exists):** {report.exception_composition.correctly_refused} ({_fmt_already_pct(report.exception_composition.correctly_refused_pct)})",
        f"- **Should have been caught (real match existed):** {report.exception_composition.should_have_been_caught} ({_fmt_already_pct(report.exception_composition.should_have_been_caught_pct)})",
        "",
        "---",
        "",
        "## 8. Throughput",
        "",
        f"- **Total batch time:** {report.throughput.total_batch_time_ms:.2f} ms",
        f"- **Layer 1 time:** {report.throughput.layer1_time_ms:.2f} ms",
        f"- **Layer 2 time:** {report.throughput.layer2_time_ms:.2f} ms",
        "",
        "*Layer 2 time is summed across all residual scenarios. Layer 1 is batch-level.*",
        "",
        "---",
        "",
        "## 9. Baseline Comparison (Layer 1 only)",
        "",
        "| Metric | Baseline | Layered System (Layer 1) | Delta |",
        "|--------|----------|--------------------------|-------|",
        f"| Match rate | {_fmt_pct(report.baseline_comparison.baseline_match_rate)} | {_fmt_pct(report.baseline_comparison.deterministic_match_rate)} | {_fmt_delta(report.baseline_comparison.match_rate_delta)} |",
        f"| Precision | {_fmt_pct(report.baseline_comparison.baseline_precision)} | {_fmt_pct(report.baseline_comparison.deterministic_precision)} | {_fmt_delta(report.baseline_comparison.precision_delta)} |",
        "",
        "*Note: This table compares baseline vs Layer 1 only. Baseline does not implement Layer 2/AI metrics, so a single match rate delta for the full system would be misleading.*",
        "",
        "---",
        "",
        "## 10. Final System Routing Composition (all layers)",
        "",
        f"- **Total records:** {report.final_system_composition['total_records']}",
        "",
        "| Routing bucket | Count | Rate |",
        "|----------------|-------|------|",
    ])
    for bucket in ("DETERMINISTIC_MATCH", "AI_AUTO_ACCEPTED", "HUMAN_REVIEW", "EXCEPTION"):
        count = report.final_system_composition["counts"].get(bucket, 0)
        rate = report.final_system_composition["rates"].get(bucket)
        lines.append(
            f"| {bucket} | {count} | {_fmt_pct(rate)} |"
        )
    lines.append("")
    lines.append(report.final_system_composition.get("note", ""))
    lines.extend([
        "",
        "---",
        "",
        "## Metadata",
        "",
        f"- **Dataset used:** Synthetic dataset with seed {report.dataset_seed}",
        f"- **Dataset fingerprint:** {report.dataset_fingerprint or '(none)'}",
        f"- **Evaluation run identity/time:** {report.run_timestamp}",
        f"- **Layer 2 provenance:** {report.layer2_provenance}",
        f"- **Configured routing thresholds:** auto_accept={report.auto_accept_threshold}, review={report.review_threshold}",
        f"- **Baseline configuration:** {report.baseline_config}",
        f"- **Layer 2 execution mode:** {report.layer2_mode}",
        f"- **Dataset fingerprint:** {report.dataset_fingerprint or '(none)'}",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.2f}%"


def _fmt_delta(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value * 100:.2f}pp"


def _fmt_already_pct(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}%"


def _fmt_threshold(label: str, result: AiPrecisionAtThreshold) -> str:
    return (
        f"### {label}\n"
        f"\n"
        f"- **Included:** {result.total}\n"
        f"- **True positives:** {result.true_positives}\n"
        f"- **False positives:** {result.false_positives}\n"
        f"- **Precision:** {_fmt_pct(result.precision)}\n"
    )
