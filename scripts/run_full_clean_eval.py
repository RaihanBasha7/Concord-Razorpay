"""
Complete Fresh Clean Layer 2 Evaluation — 77 residual scenarios.

Produces:
  - data/layer2_clean_eval.jsonl (canonical artifact)
  - data/clean_full_eval_results.json (machine-readable results)
  - data/clean_full_evaluation_report.json (comprehensive metrics)
  - data/clean_full_evaluation_report.md (human-readable report)
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── .env loader ─────────────────────────────────────────────────
def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def main() -> int:
    _load_dotenv()

    # ── Imports ──────────────────────────────────────────────────
    from reconciliation.audit import Auditor, make_audit_record
    from reconciliation.domain.models import NormalizedRecord, SourceType
    from reconciliation.evaluation.dataset_fingerprint import (
        DatasetManifest, read_manifest, verify_dataset,
    )
    from reconciliation.evaluation.dataset_generator import (
        CATEGORY_QUOTAS, EdgeCaseCategory, GeneratedDataset, GroundTruthScenario,
        _compute_record_id, check_leakage, generate_dataset,
    )
    from reconciliation.evaluation.ground_truth import GroundTruthUnit
    from reconciliation.evaluation.metrics import EvaluationReport, evaluate as evaluate_l1
    from reconciliation.evaluation.primitives import (
        ScenarioOutcome, build_record_outcomes, compute_ai_precision_at_threshold,
        compute_ai_recall, compute_deterministic_metrics, compute_exception_composition,
        compute_false_accept_metrics, compute_review_queue_composition,
        compute_throughput_metrics, validate_record_outcomes,
    )
    from reconciliation.evaluation.residuals import persist_residuals
    from reconciliation.evaluation.full_pipeline_evaluation import (
        _build_ground_truth_units, _expected_match_ids,
    )
    from reconciliation.groq_provider import (
        GroqProviderError, GroqStructuredProvider, safe_diagnostic,
    )
    from reconciliation.layer2 import Layer2Case, reconstruct_layer2_case
    from reconciliation.layer3 import route as layer3_route
    from reconciliation.loader import load_normalized_records, load_residuals
    from reconciliation.matcher import reconcile
    from reconciliation.matcher_config import MatcherConfig
    from reconciliation.proposal_orchestration import ProposalOrchestrator
    from reconciliation.proposal_service import ProposalService
    from reconciliation.proposal_validation import (
        ProposalOutcome, ProposalOutcomeType, validate_proposal,
    )
    from reconciliation.retrieval import RetrievalConfig, retrieve_candidates

    # ── Configuration ────────────────────────────────────────────
    data_dir = Path("data")
    artifact_path = data_dir / "layer2_clean_eval.jsonl"
    results_path = data_dir / "clean_full_eval_results.json"
    report_json_path = data_dir / "clean_full_evaluation_report.json"
    report_md_path = data_dir / "clean_full_evaluation_report.md"

    config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
    retrieval_config = RetrievalConfig()
    AUTO_ACCEPT_THRESHOLD = 0.90
    REVIEW_THRESHOLD = 0.60

    # Rate limit settings
    BASE_DELAY = 5.0          # Base delay between requests (seconds)
    MAX_DELAY = 60.0           # Max delay for backoff
    MAX_RETRIES = 3            # Max retries per scenario on transient errors
    QUOTA_RETRY_DELAY = 300.0  # 5 min wait when daily quota hit

    # ── Print header ─────────────────────────────────────────────
    print("=" * 70)
    print("COMPLETE FRESH CLEAN LAYER 2 EVALUATION")
    print("=" * 70)
    print()

    # ── PHASE 1: Pre-flight ──────────────────────────────────────
    print("PHASE 1: PRE-FLIGHT VERIFICATION")
    print("-" * 50)

    manifest = read_manifest(data_dir)
    if manifest is None:
        print("ERROR: No frozen dataset manifest found.")
        return 1

    verification = verify_dataset(data_dir, manifest)
    if not verification.ok:
        print(f"DRIFT DETECTED: {verification.details}")
        return 1

    fingerprint = manifest.fingerprint()
    print(f"  Dataset fingerprint: {fingerprint}")
    print(f"  Fingerprint loaded from data/dataset_manifest.json (canonical source of truth)")

    dataset = generate_dataset(seed=42)
    leakage = check_leakage(list(dataset.scenarios), list(dataset.record_specs))
    assert not leakage.has_leakage, f"Leakage detected: {leakage.issues}"
    print(f"  Leakage check: PASSED")

    normalized = load_normalized_records(data_dir)
    residuals = load_residuals(data_dir)
    all_records = list(normalized)

    assert len(all_records) == 245, f"Expected 245 records, got {len(all_records)}"
    assert len(dataset.scenarios) == 120, f"Expected 120 scenarios, got {len(dataset.scenarios)}"
    assert len(residuals) == 77, f"Expected 77 residuals, got {len(residuals)}"

    print(f"  Records: {len(all_records)} (expected 245)")
    print(f"  Scenarios: {len(dataset.scenarios)} (expected 120)")
    print(f"  Residuals: {len(residuals)} (expected 77)")
    print()

    # Verify guardrail tests pass
    print("  Verifying guardrail tests...")
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-x", "-q",
         "-k", "guardrail or duplicate_overinclusion or same_source"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        print(f"  GUARDRAIL TESTS FAILED: {result.stdout[-200:]}")
        return 1
    print(f"  Guardrail tests: PASSED")

    # Verify old artifact cannot load into current evaluation
    print("  Verifying old artifact rejection...")
    old_artifact = data_dir / "layer2_clean_audit.jsonl"
    if old_artifact.exists():
        from reconciliation.evaluation.full_pipeline_evaluation import (
            load_layer2_artifacts, verify_layer2_artifact_provenance
        )
        old_arts = load_layer2_artifacts(old_artifact)
        prov = verify_layer2_artifact_provenance(dataset, old_arts, manifest)
        if not prov.ok:
            print(f"  Old artifact correctly rejected: {prov.mismatches[0][:80]}...")
        else:
            print(f"  WARNING: Old artifact matches current dataset (unexpected)")
    else:
        print(f"  No old artifact to check")
    print()

    # ── Setup maps ───────────────────────────────────────────────
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

    # ── Layer 1 ──────────────────────────────────────────────────
    print("Running Layer 1...")
    t0 = time.perf_counter()
    l1_result = reconcile(all_records, config)
    layer1_time_ms = (time.perf_counter() - t0) * 1000.0

    l1_decisions = list(l1_result.decisions)
    l1_residual_ids = set(l1_result.residual_record_ids)

    l1_report = evaluate_l1(
        scenarios=list(dataset.scenarios),
        decisions=l1_decisions,
        residual_record_ids=list(l1_result.residual_record_ids),
        scenario_units=list(unit_map.values()),
    )

    l1_matched = len(l1_decisions)
    l1_residual_scenarios = len(l1_report.pipeline_residual_scenario_ids)
    l1_residual_records = len(l1_residual_ids)
    print(f"  L1 matched: {l1_matched} records ({l1_matched/len(all_records):.1%})")
    print(f"  L1 residual records: {l1_residual_records}")
    print(f"  L1 residual scenarios: {l1_residual_scenarios}")
    print(f"  L1 time: {layer1_time_ms:.1f}ms")
    print()

    # ── Layer 1 Baseline ─────────────────────────────────────────
    from reconciliation.baseline.naive_matcher import match as baseline_match
    baseline_result = baseline_match(list(normalized), config)
    baseline_match_ids = set()
    for a, b in baseline_result.matches:
        baseline_match_ids.add(a)
        baseline_match_ids.add(b)
    print(f"  Baseline (naive) matched: {len(baseline_result.matches)} pairs")
    print()

    # ══════════════════════════════════════════════════════════════
    # PHASE 2-3: FRESH GROQ INFERENCE — ALL 77 RESIDUALS
    # ══════════════════════════════════════════════════════════════
    print("=" * 70)
    print("PHASE 2-3: FRESH LAYER 2 GROQ EVALUATION (77 scenarios)")
    print("=" * 70)
    print()

    provider = GroqStructuredProvider()
    service = ProposalService(provider)
    orchestrator = ProposalOrchestrator(service)

    stats = {
        "attempted": 0,
        "successful": 0,
        "api_error": 0,
        "quota_exhausted": 0,
        "transient_error": 0,
        "timeout": 0,
        "validation_failed": 0,
        "no_proposal": 0,
        "valid_proposal": 0,
        "retries_used": 0,
    }
    outcomes: List[Tuple[str, ProposalOutcome, float]] = []
    error_details: List[Dict[str, Any]] = []

    for i, residual in enumerate(residuals):
        scenario_id = residual.scenario_id
        stats["attempted"] += 1

        case = reconstruct_layer2_case(
            scenario_id=scenario_id,
            member_record_ids=residual.member_record_ids,
            normalized_records=tuple(normalized),
        )
        retrieval = retrieve_candidates(case, tuple(normalized), retrieval_config)

        # Attempt with retry logic for transient errors
        outcome = None
        elapsed = 0.0
        for attempt in range(MAX_RETRIES):
            t0 = time.perf_counter()
            try:
                outcome = orchestrator.resolve(case, retrieval)
                elapsed = (time.perf_counter() - t0) * 1000.0
                break
            except Exception as exc:
                elapsed = (time.perf_counter() - t0) * 1000.0
                outcome = ProposalOutcome(
                    outcome=ProposalOutcomeType.API_ERROR,
                    proposal=None,
                    presented_record_ids=tuple(
                        [r.record_id for r in case.member_records]
                        + [c.record.record_id for c in retrieval.candidates]
                    ),
                    reason=f"Unexpected error: {exc}",
                    diagnostic=safe_diagnostic(exc),
                    error_classification="unknown",
                )
                break

            if outcome and outcome.outcome == ProposalOutcomeType.API_ERROR:
                classification = outcome.error_classification or "unknown"
                if classification == "quota_exhausted":
                    # Don't retry quota exhaustion; wait longer
                    print(f"  [{i+1}/{len(residuals)}] {scenario_id}: QUOTA EXHAUSTED - waiting {QUOTA_RETRY_DELAY}s...")
                    time.sleep(QUOTA_RETRY_DELAY)
                    stats["retries_used"] += 1
                    continue
                elif classification in ("transient", "timeout"):
                    # Exponential backoff with jitter
                    backoff = min(BASE_DELAY * (2 ** attempt) + random.uniform(0, 2), MAX_DELAY)
                    print(f"  [{i+1}/{len(residuals)}] {scenario_id}: retry {attempt+1} after {backoff:.1f}s ({classification})")
                    time.sleep(backoff)
                    stats["retries_used"] += 1
                    continue
                else:
                    break  # Permanent error, don't retry

        outcomes.append((scenario_id, outcome, elapsed))

        # Categorize outcome
        if outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID:
            stats["valid_proposal"] += 1
            stats["successful"] += 1
        elif outcome.outcome == ProposalOutcomeType.NO_PROPOSAL:
            stats["no_proposal"] += 1
            stats["successful"] += 1
        elif outcome.outcome == ProposalOutcomeType.VALIDATION_FAILED:
            stats["validation_failed"] += 1
            stats["successful"] += 1
        elif outcome.outcome == ProposalOutcomeType.API_ERROR:
            stats["api_error"] += 1
            classification = outcome.error_classification or "unknown"
            if classification == "quota_exhausted":
                stats["quota_exhausted"] += 1
            elif classification == "transient":
                stats["transient_error"] += 1
            elif classification == "timeout":
                stats["timeout"] += 1
            error_details.append({
                "scenario_id": scenario_id,
                "error_type": classification,
                "diagnostic": outcome.diagnostic or "",
            })
        elif outcome.outcome == ProposalOutcomeType.TIMEOUT:
            stats["timeout"] += 1
            stats["api_error"] += 1
            error_details.append({
                "scenario_id": scenario_id,
                "error_type": "timeout",
                "diagnostic": outcome.diagnostic or "",
            })

        # Progress
        progress = i + 1
        ok_count = stats["valid_proposal"] + stats["no_proposal"] + stats["validation_failed"]
        if progress % 5 == 0 or progress == len(residuals):
            print(f"  [{progress}/{len(residuals)}] OK={ok_count} ERR={stats['api_error']} "
                  f"VALID={stats['valid_proposal']} NOPROP={stats['no_proposal']} "
                  f"FAIL={stats['validation_failed']}")

        # Pacing delay between requests
        if i < len(residuals) - 1:
            delay = BASE_DELAY
            if stats["api_error"] > 0 and stats["transient_error"] > 0:
                delay = min(BASE_DELAY * 2, MAX_DELAY)  # Slow down after errors
            time.sleep(delay)

    # Check if evaluation is partial
    unevaluated = stats["attempted"] - stats["successful"] - stats["validation_failed"]
    is_partial = stats["api_error"] > 0

    print()
    print(f"Layer 2 execution complete:")
    print(f"  Attempted: {stats['attempted']}")
    print(f"  Successful (model responded): {stats['successful']}")
    print(f"  Valid proposals: {stats['valid_proposal']}")
    print(f"  No proposals: {stats['no_proposal']}")
    print(f"  Validation failed: {stats['validation_failed']}")
    print(f"  API errors: {stats['api_error']}")
    print(f"    - quota_exhausted: {stats['quota_exhausted']}")
    print(f"    - transient: {stats['transient_error']}")
    print(f"    - timeout: {stats['timeout']}")
    print(f"  Retries used: {stats['retries_used']}")
    print(f"  Evaluation: {'PARTIAL' if is_partial else 'COMPLETE'}")
    print()

    # ── Save raw results ─────────────────────────────────────────
    raw_results = {
        "fingerprint": fingerprint,
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "results": [
            {
                "scenario_id": sid,
                "outcome": o.outcome.value,
                "confidence": o.proposal.confidence if o.proposal else None,
                "proposed_match_ids": list(o.proposal.proposed_match_ids) if o.proposal else [],
                "error_classification": o.error_classification,
                "elapsed_ms": ms,
            }
            for sid, o, ms in outcomes
        ],
        "stats": {
            "ok": stats["successful"],
            "error": stats["api_error"],
            "valid_proposal": stats["valid_proposal"],
            "no_proposal": stats["no_proposal"],
            "validation_failed": stats["validation_failed"],
        },
        "total_attempted": stats["attempted"],
        "is_partial": is_partial,
    }
    results_path.write_text(json.dumps(raw_results, indent=2), encoding="utf-8")
    print(f"  Raw results saved to {results_path}")

    # ══════════════════════════════════════════════════════════════
    # PHASE 4: LAYER 3 ROUTING (with guardrail)
    # ══════════════════════════════════════════════════════════════
    print()
    print("=" * 70)
    print("PHASE 4: LAYER 3 ROUTING (with guardrail)")
    print("=" * 70)
    print()

    l2_outcomes_list = [outcome for _, outcome, _ in outcomes]
    routing_decisions = layer3_route(
        layer1_decisions=l1_decisions,
        layer2_outcomes=l2_outcomes_list,
        all_records=all_records,
    )

    record_routing = {rd.record_id: rd for rd in routing_decisions}
    bucket_counts = Counter(rd.bucket.value for rd in routing_decisions)
    print("  Layer 3 routing composition (record-level):")
    for bucket, count in sorted(bucket_counts.items()):
        print(f"    {bucket}: {count}")
    print()

    # ══════════════════════════════════════════════════════════════
    # PHASE 5: FINAL ACCOUNTING
    # ══════════════════════════════════════════════════════════════
    print("=" * 70)
    print("PHASE 5: FINAL ACCOUNTING")
    print("=" * 70)
    print()

    # Build scenario outcomes
    l1_eval_map = {ev.scenario_id: ev for ev in l1_report.scenario_evaluations}
    l2_scenario_map = {sid: outcome for sid, outcome, _ in outcomes}

    scenario_outcomes = []
    for scen in dataset.scenarios:
        sid = scen.scenario_id
        unit = unit_map[sid]
        l1_eval = l1_eval_map.get(sid)
        l2_outcome = l2_scenario_map.get(sid)

        det_matched = l1_eval.matched if l1_eval else False
        det_correct = l1_eval.correct if l1_eval else False
        baseline_matched = any(rid in baseline_match_ids for rid in scenario_to_records.get(sid, []))
        baseline_correct = any(
            tuple(sorted(pair)) == tuple(sorted(scenario_to_records.get(sid, [])))
            for pair in baseline_result.matches
        )

        layer1_time = layer1_time_ms if l1_eval else None

        if det_matched or l2_outcome is None:
            routing_bucket = "DETERMINISTIC_MATCH" if det_matched else "EXCEPTION"
            ai_confidence = None
            ai_proposal_ids: Tuple[str, ...] = ()
            ai_correct = det_correct if det_matched else (not unit.has_real_match)
            layer2_outcome_type = None
            layer2_ms = None
        else:
            l2_type = l2_outcome.outcome
            layer2_outcome_type = l2_type.value
            if l2_type == ProposalOutcomeType.PROPOSAL_VALID and l2_outcome.proposal:
                conf = l2_outcome.proposal.confidence
                if conf >= AUTO_ACCEPT_THRESHOLD:
                    routing_bucket = "AI_AUTO_ACCEPTED"
                elif conf >= REVIEW_THRESHOLD:
                    routing_bucket = "HUMAN_REVIEW"
                else:
                    routing_bucket = "EXCEPTION"
                ai_confidence = conf
                ai_proposal_ids = tuple(l2_outcome.proposal.proposed_match_ids)
                expected = _expected_match_ids(scen, unit)
                ai_correct = (
                    tuple(sorted(l2_outcome.proposal.proposed_match_ids))
                    == tuple(sorted(expected))
                )
                layer2_ms = next((m for s, _, m in outcomes if s == sid), None)
            elif l2_type == ProposalOutcomeType.API_ERROR:
                routing_bucket = "EXCEPTION"
                ai_confidence = None
                ai_proposal_ids = ()
                ai_correct = None
                layer2_ms = next((m for s, _, m in outcomes if s == sid), None)
            else:
                routing_bucket = "EXCEPTION"
                ai_confidence = None
                ai_proposal_ids = ()
                ai_correct = not unit.has_real_match
                layer2_ms = next((m for s, _, m in outcomes if s == sid), None)

        scenario_outcomes.append(ScenarioOutcome(
            scenario_id=sid,
            category=scen.category,
            is_true_orphan=unit.is_true_orphan,
            has_real_match=scen.has_real_match,
            baseline_matched=baseline_matched,
            baseline_correct=baseline_correct,
            deterministic_matched=det_matched,
            deterministic_correct=det_correct,
            routing_bucket=routing_bucket,
            ai_confidence=ai_confidence,
            ai_proposal_ids=ai_proposal_ids,
            ai_correct=ai_correct,
            layer2_outcome_type=layer2_outcome_type,
            layer1_time_ms=layer1_time,
            layer2_time_ms=layer2_ms,
        ))

    # Record-level outcomes
    record_routing_map = {}
    record_confidence_map = {}
    for rd in routing_decisions:
        record_routing_map[rd.record_id] = rd.bucket.value
        record_confidence_map[rd.record_id] = rd.confidence

    record_outcomes_list = build_record_outcomes(
        scenario_outcomes=scenario_outcomes,
        record_scenario_map=record_to_scenario,
        record_routing_map=record_routing_map,
        record_confidence_map=record_confidence_map,
    )
    validate_record_outcomes(record_outcomes_list)
    record_outcomes = [o for o in record_outcomes_list if hasattr(o, "is_false_accept")]

    # Record-level accounting
    record_buckets = Counter(ro.routing_bucket for ro in record_outcomes)
    total_records = len(record_outcomes)
    det_records = record_buckets.get("DETERMINISTIC_MATCH", 0)
    ai_records = record_buckets.get("AI_AUTO_ACCEPTED", 0)
    review_records = record_buckets.get("HUMAN_REVIEW", 0)
    exception_records = record_buckets.get("EXCEPTION", 0)

    assert det_records + ai_records + review_records + exception_records == total_records, \
        f"Accounting error: {det_records}+{ai_records}+{review_records}+{exception_records} != {total_records}"

    print("  RECORD-LEVEL ACCOUNTING:")
    print(f"    TOTAL RECORDS: {total_records}")
    print(f"    L1 DETERMINISTIC MATCH: {det_records} ({det_records/total_records:.1%})")
    print(f"    RESIDUAL RECORDS: {total_records - det_records}")
    print(f"    AI AUTO-ACCEPTED: {ai_records}")
    print(f"    HUMAN REVIEW: {review_records}")
    print(f"    EXCEPTION: {exception_records}")
    print(f"    CHECK: {det_records}+{ai_records}+{review_records}+{exception_records} = {det_records+ai_records+review_records+exception_records}")
    print()

    # Scenario-level accounting
    scenario_buckets = Counter(so.routing_bucket for so in scenario_outcomes)
    total_scenarios = len(scenario_outcomes)
    det_scenarios = scenario_buckets.get("DETERMINISTIC_MATCH", 0)
    ai_scenarios = scenario_buckets.get("AI_AUTO_ACCEPTED", 0)
    review_scenarios = scenario_buckets.get("HUMAN_REVIEW", 0)
    exception_scenarios = scenario_buckets.get("EXCEPTION", 0)

    print("  SCENARIO-LEVEL ACCOUNTING:")
    print(f"    TOTAL SCENARIOS: {total_scenarios}")
    print(f"    DETERMINISTIC MATCH: {det_scenarios}")
    print(f"    AI AUTO-ACCEPTED: {ai_scenarios}")
    print(f"    HUMAN REVIEW: {review_scenarios}")
    print(f"    EXCEPTION: {exception_scenarios}")
    print()

    # ══════════════════════════════════════════════════════════════
    # PHASE 6: MODEL QUALITY
    # ══════════════════════════════════════════════════════════════
    print("=" * 70)
    print("PHASE 6: MODEL QUALITY")
    print("=" * 70)
    print()

    det_metrics = compute_deterministic_metrics(scenario_outcomes)
    ai_p090 = compute_ai_precision_at_threshold(scenario_outcomes, 0.90)
    ai_p075 = compute_ai_precision_at_threshold(scenario_outcomes, 0.75)
    ai_p060 = compute_ai_precision_at_threshold(scenario_outcomes, 0.60)
    ai_recall = compute_ai_recall(scenario_outcomes)
    false_accept = compute_false_accept_metrics(record_outcomes)
    review_queue = compute_review_queue_composition(record_outcomes)
    exception_comp = compute_exception_composition(record_outcomes)
    throughput = compute_throughput_metrics(scenario_outcomes)

    def _fmt_pct(val):
        return "N/A" if val is None else f"{val:.1%}"

    print(f"  Deterministic match rate: {_fmt_pct(det_metrics.match_rate)} ({det_metrics.matched}/{det_metrics.total_scenarios})")
    print(f"  Deterministic precision: {_fmt_pct(det_metrics.precision)}")
    print()

    # Proposal-level metrics (among scenarios where model produced a proposal)
    proposals = [so for so in scenario_outcomes if so.ai_proposal_ids and so.ai_correct is not None]
    proposal_correct = [so for so in proposals if so.ai_correct]
    proposal_incorrect = [so for so in proposals if not so.ai_correct]

    print(f"  PROPOSAL-LEVEL METRICS:")
    print(f"    Total proposals: {len(proposals)}")
    print(f"    Correct: {len(proposal_correct)}")
    print(f"    Incorrect: {len(proposal_incorrect)}")
    if proposals:
        print(f"    Precision: {len(proposal_correct)/len(proposals):.1%}")
    print()

    # Record-level precision/recall
    # Precision: of all records routed to AI_AUTO_ACCEPTED, how many are correct?
    # Recall: of all records that should be matched (has_real_match=True and not L1), how many did AI find?
    residual_with_match = [so for so in scenario_outcomes if not so.deterministic_matched and so.has_real_match]
    residual_ai_correct = [so for so in residual_with_match if so.ai_correct is True]
    print(f"  RECORD-LEVEL (via scenarios):")
    print(f"    Residual scenarios with real match: {len(residual_with_match)}")
    print(f"    AI correctly resolved: {len(residual_ai_correct)}")
    if residual_with_match:
        print(f"    Recall (of matchable residuals): {len(residual_ai_correct)/len(residual_with_match):.1%}")
    print()

    print(f"  AI Precision at thresholds:")
    print(f"    >=0.90: {_fmt_pct(ai_p090.precision)} ({ai_p090.true_positives}TP / {ai_p090.true_positives+ai_p090.false_positives} total)")
    print(f"    >=0.75: {_fmt_pct(ai_p075.precision)} ({ai_p075.true_positives}TP / {ai_p075.true_positives+ai_p075.false_positives} total)")
    print(f"    >=0.60: {_fmt_pct(ai_p060.precision)} ({ai_p060.true_positives}TP / {ai_p060.true_positives+ai_p060.false_positives} total)")
    print()

    print(f"  AI Recall:")
    print(f"    System-wide: {_fmt_pct(ai_recall.recall)} ({ai_recall.true_positives}TP / {ai_recall.denominator} residuals)")
    if ai_recall.recall_attempted is not None:
        print(f"    Attempted-only: {_fmt_pct(ai_recall.recall_attempted)} ({ai_recall.true_positives}TP / {ai_recall.denominator_attempted} attempted)")
    print()

    # False accepts
    false_accept_scenarios = [
        so for so in scenario_outcomes
        if so.routing_bucket == "AI_AUTO_ACCEPTED" and so.ai_correct is False
    ]
    print(f"  FALSE ACCEPTS:")
    print(f"    Count: {false_accept.count}")
    print(f"    Rate: {_fmt_pct(false_accept.rate)}")
    print(f"    Total auto-accepted: {false_accept.total_auto_accepted}")
    if false_accept_scenarios:
        print(f"    Details:")
        for so in false_accept_scenarios:
            print(f"      - {so.scenario_id} ({so.category.value}): conf={so.ai_confidence:.2f}, proposed={list(so.ai_proposal_ids)}")
    print()

    # ══════════════════════════════════════════════════════════════
    # PHASE 7: SYSTEM SAFETY
    # ══════════════════════════════════════════════════════════════
    print("=" * 70)
    print("PHASE 7: SYSTEM SAFETY")
    print("=" * 70)
    print()

    print("  MODEL QUALITY:")
    print(f"    False accepts: {false_accept.count}")
    print(f"    False accept rate: {_fmt_pct(false_accept.rate)}")
    print()

    print("  SYSTEM RELIABILITY:")
    print(f"    API failures: {stats['api_error']}")
    print(f"    Rate limits (transient): {stats['transient_error']}")
    print(f"    Timeouts: {stats['timeout']}")
    print(f"    Quota exhausted: {stats['quota_exhausted']}")
    print(f"    Unevaluated scenarios: {stats['attempted'] - stats['successful'] - stats['validation_failed']}")
    print()

    print(f"  SAFE AUTO-ACCEPT RATE:")
    safe = false_accept.total_auto_accepted - false_accept.count
    print(f"    Safe auto-accepted: {safe} / {false_accept.total_auto_accepted}")
    if false_accept.total_auto_accepted > 0:
        print(f"    Safe auto-accept rate: {safe/false_accept.total_auto_accepted:.1%}")
    print(f"  FALSE ACCEPT RATE: {_fmt_pct(false_accept.rate)}")
    print()

    # ══════════════════════════════════════════════════════════════
    # PHASE 8: EDGE CASES
    # ══════════════════════════════════════════════════════════════
    print("=" * 70)
    print("PHASE 8: EDGE CASE BREAKDOWN")
    print("=" * 70)
    print()

    cat_metrics: Dict[str, Dict[str, Any]] = {}
    for so in scenario_outcomes:
        cat = so.category.value
        if cat not in cat_metrics:
            cat_metrics[cat] = {
                "scenario_count": 0,
                "l1_matched": 0,
                "residual": 0,
                "l2_attempted": 0,
                "l2_successful": 0,
                "api_errors": 0,
                "no_proposal": 0,
                "valid_proposals": 0,
                "auto_accept": 0,
                "human_review": 0,
                "exception": 0,
                "false_accepts": 0,
            }
        cm = cat_metrics[cat]
        cm["scenario_count"] += 1
        if so.deterministic_matched:
            cm["l1_matched"] += 1
        else:
            cm["residual"] += 1
            if so.layer2_outcome_type is not None:
                cm["l2_attempted"] += 1
                if so.layer2_outcome_type in ("PROPOSAL_VALID", "NO_PROPOSAL", "VALIDATION_FAILED"):
                    cm["l2_successful"] += 1
                if so.layer2_outcome_type == "API_ERROR":
                    cm["api_errors"] += 1
                if so.layer2_outcome_type == "NO_PROPOSAL":
                    cm["no_proposal"] += 1
                if so.layer2_outcome_type == "PROPOSAL_VALID":
                    cm["valid_proposals"] += 1
            if so.routing_bucket == "AI_AUTO_ACCEPTED":
                cm["auto_accept"] += 1
                if so.ai_correct is False:
                    cm["false_accepts"] += 1
            elif so.routing_bucket == "HUMAN_REVIEW":
                cm["human_review"] += 1
            elif so.routing_bucket == "EXCEPTION":
                cm["exception"] += 1

    # Print table
    print("  SCENARIO-LEVEL EDGE CASE TABLE:")
    print(f"  {'Category':<28} {'Scn':>3} {'L1':>3} {'Res':>3} {'L2att':>5} {'L2ok':>4} {'Err':>3} {'NoP':>3} {'Prop':>4} {'A-acc':>5} {'Rev':>3} {'Exc':>3} {'FA':>3}")
    print("  " + "-" * 95)
    for cat in sorted(cat_metrics.keys()):
        cm = cat_metrics[cat]
        print(f"  {cat:<28} {cm['scenario_count']:>3} {cm['l1_matched']:>3} {cm['residual']:>3} "
              f"{cm['l2_attempted']:>5} {cm['l2_successful']:>4} {cm['api_errors']:>3} {cm['no_proposal']:>3} "
              f"{cm['valid_proposals']:>4} {cm['auto_accept']:>5} {cm['human_review']:>3} {cm['exception']:>3} "
              f"{cm['false_accepts']:>3}")
    print()
    print("  All numbers above are SCENARIO-LEVEL.")
    print()

    # ══════════════════════════════════════════════════════════════
    # PHASE 9: GUARDRAIL IMPACT
    # ══════════════════════════════════════════════════════════════
    print("=" * 70)
    print("PHASE 9: GUARDRAIL IMPACT COMPARISON")
    print("=" * 70)
    print()

    # Re-run Layer 3 WITHOUT the guardrail for comparison
    # The guardrail is in validate_proposal's _check_same_source_duplicate_overinclusion
    # To simulate "before guardrail", we create proposals that bypass the guardrail
    # by setting records_by_id=None in validation

    # Instead of re-running, compare the outcomes
    # The guardrail only affects DUPLICATE scenarios with >=3 proposed IDs
    # Let's check which proposals were affected
    guardrail_affected = []
    for so in scenario_outcomes:
        if so.layer2_outcome_type == "VALIDATION_FAILED":
            # Check if this was due to guardrail
            for sid, outcome, _ in outcomes:
                if sid == so.scenario_id and outcome.proposal:
                    if len(outcome.proposal.proposed_match_ids) >= 3:
                        guardrail_affected.append(so)
                        break

    print(f"  Proposals affected by guardrail: {len(guardrail_affected)}")
    if guardrail_affected:
        for so in guardrail_affected:
            print(f"    - {so.scenario_id} ({so.category.value})")
    print()

    # Simulate before/after guardrail
    # BEFORE: All PROPOSAL_VALID outcomes pass validation (no guardrail check)
    # AFTER: Guardrail rejects same-source duplicate over-inclusion

    # Count scenarios where guardrail changed outcome
    before_auto_accept = 0
    before_review = 0
    before_exception = 0
    before_false_accept = 0

    after_auto_accept = false_accept.total_auto_accepted
    after_review = review_queue.total
    after_exception = exception_comp.total
    after_false_accept = false_accept.count

    # For scenarios where guardrail fired, compute what would have happened without it
    for so in guardrail_affected:
        # These were VALIDATION_FAILED -> routed to EXCEPTION
        # Without guardrail, they'd be PROPOSAL_VALID with their confidence
        for sid, outcome, _ in outcomes:
            if sid == so.scenario_id and outcome.proposal:
                conf = outcome.proposal.confidence
                if conf >= AUTO_ACCEPT_THRESHOLD:
                    before_auto_accept += 1
                    # Would this be a false accept?
                    expected = _expected_match_ids(scenario_map[sid], unit_map[sid])
                    correct = tuple(sorted(outcome.proposal.proposed_match_ids)) == tuple(sorted(expected))
                    if not correct:
                        before_false_accept += 1
                elif conf >= REVIEW_THRESHOLD:
                    before_review += 1
                else:
                    before_exception += 1
                break

    before_auto_accept += after_auto_accept
    before_review += after_review
    before_exception += after_exception

    print("  BEFORE GUARDRAIL (simulated):")
    print(f"    Auto-accepted: {before_auto_accept}")
    print(f"    Human review: {before_review}")
    print(f"    Exception: {before_exception}")
    print(f"    False accepts: {before_false_accept}")
    print()
    print("  AFTER GUARDRAIL (actual):")
    print(f"    Auto-accepted: {after_auto_accept}")
    print(f"    Human review: {after_review}")
    print(f"    Exception: {after_exception}")
    print(f"    False accepts: {after_false_accept}")
    print()

    if before_false_accept > after_false_accept:
        print(f"  Guardrail REMOVED {before_false_accept - after_false_accept} false accept(s)")
    if after_exception > before_exception:
        print(f"  Guardrail INCREASED exceptions by {after_exception - before_exception}")
    if before_auto_accept > after_auto_accept:
        print(f"  Guardrail DECREASED auto-accepts by {before_auto_accept - after_auto_accept}")
    print()

    # ══════════════════════════════════════════════════════════════
    # PHASE 10: BASELINE COMPARISON
    # ══════════════════════════════════════════════════════════════
    print("=" * 70)
    print("PHASE 10: BASELINE COMPARISON")
    print("=" * 70)
    print()

    # L1-only baseline
    l1_only_match = det_metrics.matched
    l1_only_correct = det_metrics.correct
    l1_only_total = det_metrics.total_scenarios

    # Full system (L1 + L2 + L3)
    full_resolved = det_scenarios + ai_scenarios
    full_correct = sum(1 for so in scenario_outcomes if so.deterministic_matched and so.deterministic_correct) + \
                   sum(1 for so in scenario_outcomes if so.routing_bucket == "AI_AUTO_ACCEPTED" and so.ai_correct)

    print(f"  Layer 1-only baseline:")
    print(f"    Match rate: {_fmt_pct(det_metrics.match_rate)} ({l1_only_match}/{l1_only_total})")
    print(f"    Precision: {_fmt_pct(det_metrics.precision)} ({l1_only_correct}/{l1_only_match})")
    print()
    print(f"  Full system (L1 + L2 + L3):")
    print(f"    Resolved: {full_resolved}/{l1_only_total} scenarios ({full_resolved/l1_only_total:.1%})")
    print(f"    Correctly resolved: {full_correct}/{l1_only_total}")
    if full_resolved > 0:
        print(f"    Precision of resolved: {full_correct/full_resolved:.1%}")
    print()
    print(f"  AI useful resolution of ambiguous residuals:")
    print(f"    Residuals with real match: {len(residual_with_match)}")
    print(f"    AI correctly resolved: {len(residual_ai_correct)}")
    if residual_with_match:
        print(f"    Useful resolution rate: {len(residual_ai_correct)/len(residual_with_match):.1%}")
    print()

    # ══════════════════════════════════════════════════════════════
    # PHASE 11: CANONICAL ARTIFACT
    # ══════════════════════════════════════════════════════════════
    print("=" * 70)
    print("PHASE 11: CANONICAL ARTIFACT")
    print("=" * 70)
    print()

    # Only create canonical artifact if evaluation is complete
    if is_partial:
        print("  Evaluation is PARTIAL due to provider errors.")
        print("  Canonical artifact will NOT be created.")
        print("  To create a canonical artifact, re-run when quota is refreshed.")
        print()
    else:
        print("  Creating canonical clean Layer 2 artifact...")

        auditor = Auditor(artifact_path, archive_existing=True, atomic_write=True)
        for scenario_id, outcome, elapsed_ms in outcomes:
            member_ids = [r.record_id for r in normalized if record_to_scenario.get(r.record_id) == scenario_id]
            presented_ids = list(dict.fromkeys(member_ids))
            if outcome.proposal:
                for pid in outcome.proposal.proposed_match_ids:
                    if pid not in presented_ids:
                        presented_ids.append(pid)

            record = make_audit_record(
                correlation_id=f"{scenario_id}-{fingerprint[:8]}",
                presented_record_ids=presented_ids,
                outcome=outcome.outcome.value,
                proposal=outcome.proposal,
                reason=outcome.reason,
                dataset_fingerprint=fingerprint,
                diagnostic=outcome.diagnostic or None,
            )
            auditor.write(record)

        auditor.finalize()

        artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        print(f"  Artifact: {artifact_path.name}")
        print(f"  SHA-256: {artifact_hash}")
        print(f"  Records: {auditor.write_count}")
        print(f"  Dataset fingerprint: {fingerprint}")
        print()

        # Update manifest
        manifest_data = json.loads(Path("data/dataset_manifest.json").read_text())
        manifest_data["canonical_layer2_artifact"] = {
            "filename": artifact_path.name,
            "sha256": artifact_hash,
            "generation_timestamp": datetime.now(timezone.utc).isoformat(),
            "record_count": auditor.write_count,
            "provider": "groq",
            "model": os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b"),
            "evaluation_type": "COMPLETE" if not is_partial else "PARTIAL",
            "note": f"Fresh clean evaluation. Dataset fingerprint: {fingerprint}.",
        }
        Path("data/dataset_manifest.json").write_text(
            json.dumps(manifest_data, indent=2), encoding="utf-8"
        )
        print("  Manifest updated.")

    # ══════════════════════════════════════════════════════════════
    # PHASE 12: FINAL REPORT
    # ══════════════════════════════════════════════════════════════
    print()
    print("=" * 70)
    print("PHASE 12: FINAL REPORT")
    print("=" * 70)
    print()

    # JSON report
    report_data = {
        "evaluation_type": "FULL EVALUATION" if not is_partial else "PARTIAL EVALUATION",
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "fingerprint": fingerprint,
            "schema_version": manifest.schema_version,
            "dataset_seed": manifest.dataset_seed,
            "total_records": len(all_records),
            "total_scenarios": len(dataset.scenarios),
            "residual_scenarios": len(residuals),
            "leakage_free": True,
        },
        "layer1_baseline": {
            "matched_records": l1_matched,
            "residual_records": l1_residual_records,
            "residual_scenarios": l1_residual_scenarios,
            "match_rate_records": l1_matched / len(all_records),
            "match_rate_scenarios": det_metrics.match_rate,
            "deterministic_precision": det_metrics.precision,
            "time_ms": layer1_time_ms,
        },
        "layer2_execution": {
            "provider": "groq",
            "model": os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b"),
            "attempted": stats["attempted"],
            "successful": stats["successful"],
            "api_errors": stats["api_error"],
            "quota_exhausted": stats["quota_exhausted"],
            "transient_errors": stats["transient_error"],
            "timeout_errors": stats["timeout"],
            "valid_proposals": stats["valid_proposal"],
            "no_proposals": stats["no_proposal"],
            "validation_failed": stats["validation_failed"],
            "retries_used": stats["retries_used"],
        },
        "layer2_quality": {
            "proposal_level": {
                "total_proposals": len(proposals),
                "correct": len(proposal_correct),
                "incorrect": len(proposal_incorrect),
                "precision": len(proposal_correct)/len(proposals) if proposals else None,
            },
            "precision_at_thresholds": {
                "0.90": {"precision": ai_p090.precision, "tp": ai_p090.true_positives, "fp": ai_p090.false_positives, "total": ai_p090.total},
                "0.75": {"precision": ai_p075.precision, "tp": ai_p075.true_positives, "fp": ai_p075.false_positives, "total": ai_p075.total},
                "0.60": {"precision": ai_p060.precision, "tp": ai_p060.true_positives, "fp": ai_p060.false_positives, "total": ai_p060.total},
            },
            "recall": {
                "system_wide": ai_recall.recall,
                "true_positives": ai_recall.true_positives,
                "denominator": ai_recall.denominator,
                "attempted_only": ai_recall.recall_attempted,
                "denominator_attempted": ai_recall.denominator_attempted,
            },
        },
        "layer3_routing": {
            "thresholds": {"auto_accept": AUTO_ACCEPT_THRESHOLD, "review": REVIEW_THRESHOLD},
            "record_level": dict(bucket_counts),
            "scenario_level": dict(scenario_buckets),
        },
        "false_accepts": {
            "count": false_accept.count,
            "rate": false_accept.rate,
            "total_auto_accepted": false_accept.total_auto_accepted,
            "details": [
                {"scenario_id": so.scenario_id, "category": so.category.value, "confidence": so.ai_confidence}
                for so in false_accept_scenarios
            ],
        },
        "guardrail_impact": {
            "proposals_affected": len(guardrail_affected),
            "before": {"auto_accept": before_auto_accept, "review": before_review, "exception": before_exception, "false_accepts": before_false_accept},
            "after": {"auto_accept": after_auto_accept, "review": after_review, "exception": after_exception, "false_accepts": after_false_accept},
        },
        "edge_case_breakdown": cat_metrics,
        "system_safety": {
            "model_quality": {"false_accepts": false_accept.count, "false_accept_rate": false_accept.rate},
            "system_reliability": {
                "api_failures": stats["api_error"],
                "rate_limits": stats["transient_error"],
                "timeouts": stats["timeout"],
                "quota_exhausted": stats["quota_exhausted"],
                "unevaluated": stats["attempted"] - stats["successful"] - stats["validation_failed"],
            },
        },
        "accounting": {
            "record_level": {
                "total": total_records,
                "l1_deterministic": det_records,
                "ai_auto_accepted": ai_records,
                "human_review": review_records,
                "exception": exception_records,
                "check": det_records + ai_records + review_records + exception_records,
            },
            "scenario_level": {
                "total": total_scenarios,
                "deterministic_match": det_scenarios,
                "ai_auto_accepted": ai_scenarios,
                "human_review": review_scenarios,
                "exception": exception_scenarios,
            },
        },
        "limitations": {
            "partial_evaluation": is_partial,
            "api_errors": stats["api_error"],
            "quota_limited": stats["quota_exhausted"] > 0,
        },
    }

    report_json_path.write_text(json.dumps(report_data, indent=2), encoding="utf-8")

    # Markdown report
    md = []
    md.append("# Complete Clean Layer 2 Evaluation Report")
    md.append("")
    md.append(f"**Evaluation type:** {report_data['evaluation_type']}")
    md.append(f"**Timestamp:** {report_data['run_timestamp']}")
    md.append(f"**Dataset fingerprint:** `{fingerprint}`")
    md.append("")

    md.append("## 1. Dataset Provenance")
    md.append(f"- Total records: {len(all_records)}")
    md.append(f"- Total scenarios: {len(dataset.scenarios)}")
    md.append(f"- Residual scenarios: {len(residuals)}")
    md.append(f"- Leakage-free: True")
    md.append("")

    md.append("## 2. Layer 1 Baseline")
    md.append(f"- Matched records: {l1_matched} / {len(all_records)} ({l1_matched/len(all_records):.1%})")
    md.append(f"- Residual records: {l1_residual_records}")
    md.append(f"- Residual scenarios: {l1_residual_scenarios}")
    md.append(f"- Deterministic precision: {_fmt_pct(det_metrics.precision)}")
    md.append("")

    md.append("## 3. Layer 2 Execution")
    md.append(f"- Attempted: {stats['attempted']}")
    md.append(f"- Successful: {stats['successful']}")
    md.append(f"- API errors: {stats['api_error']}")
    md.append(f"  - quota_exhausted: {stats['quota_exhausted']}")
    md.append(f"  - transient: {stats['transient_error']}")
    md.append(f"  - timeout: {stats['timeout']}")
    md.append(f"- Valid proposals: {stats['valid_proposal']}")
    md.append(f"- No proposals: {stats['no_proposal']}")
    md.append(f"- Validation failed: {stats['validation_failed']}")
    md.append("")

    md.append("## 4. Layer 2 Quality")
    md.append(f"### Proposal-level")
    md.append(f"- Total proposals: {len(proposals)}")
    md.append(f"- Correct: {len(proposal_correct)}")
    md.append(f"- Incorrect: {len(proposal_incorrect)}")
    md.append(f"- Precision: {len(proposal_correct)/len(proposals):.1%}" if proposals else "- Precision: N/A")
    md.append("")
    md.append(f"### Precision at thresholds")
    md.append(f"- >=0.90: {_fmt_pct(ai_p090.precision)} ({ai_p090.true_positives}TP / {ai_p090.true_positives+ai_p090.false_positives} total)")
    md.append(f"- >=0.75: {_fmt_pct(ai_p075.precision)} ({ai_p075.true_positives}TP / {ai_p075.true_positives+ai_p075.false_positives} total)")
    md.append(f"- >=0.60: {_fmt_pct(ai_p060.precision)} ({ai_p060.true_positives}TP / {ai_p060.true_positives+ai_p060.false_positives} total)")
    md.append("")
    md.append(f"### Recall")
    md.append(f"- System-wide: {_fmt_pct(ai_recall.recall)} ({ai_recall.true_positives}TP / {ai_recall.denominator} residuals)")
    if ai_recall.recall_attempted is not None:
        md.append(f"- Attempted-only: {_fmt_pct(ai_recall.recall_attempted)} ({ai_recall.true_positives}TP / {ai_recall.denominator_attempted} attempted)")
    md.append("")

    md.append("## 5. Layer 3 Routing")
    md.append("### Record-level")
    for bucket in ["DETERMINISTIC_MATCH", "AI_AUTO_ACCEPTED", "HUMAN_REVIEW", "EXCEPTION"]:
        count = bucket_counts.get(bucket, 0)
        md.append(f"- {bucket}: {count} ({count/total_records:.1%})")
    md.append("")

    md.append("## 6. False Accepts")
    md.append(f"- Count: {false_accept.count}")
    md.append(f"- Rate: {_fmt_pct(false_accept.rate)}")
    md.append(f"- Total auto-accepted: {false_accept.total_auto_accepted}")
    if false_accept_scenarios:
        md.append("")
        for so in false_accept_scenarios:
            md.append(f"  - {so.scenario_id} ({so.category.value}): confidence={so.ai_confidence:.2f}")
    md.append("")

    md.append("## 7. Guardrail Impact")
    md.append(f"- Proposals affected: {len(guardrail_affected)}")
    md.append(f"- Before: auto_accept={before_auto_accept}, review={before_review}, exception={before_exception}, false_accepts={before_false_accept}")
    md.append(f"- After:  auto_accept={after_auto_accept}, review={after_review}, exception={after_exception}, false_accepts={after_false_accept}")
    md.append("")

    md.append("## 8. Edge-Case Breakdown (SCENARIO-LEVEL)")
    md.append("| Category | Scn | L1 | Res | L2att | L2ok | Err | NoP | Prop | A-acc | Rev | Exc | FA |")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for cat in sorted(cat_metrics.keys()):
        cm = cat_metrics[cat]
        md.append(f"| {cat} | {cm['scenario_count']} | {cm['l1_matched']} | {cm['residual']} "
                  f"| {cm['l2_attempted']} | {cm['l2_successful']} | {cm['api_errors']} | {cm['no_proposal']} "
                  f"| {cm['valid_proposals']} | {cm['auto_accept']} | {cm['human_review']} | {cm['exception']} "
                  f"| {cm['false_accepts']} |")
    md.append("")

    md.append("## 9. Provider Reliability")
    md.append(f"- API failures: {stats['api_error']} / {stats['attempted']}")
    md.append(f"- Failure rate: {stats['api_error']/stats['attempted']:.1%}")
    md.append(f"- Rate limits: {stats['transient_error']}")
    md.append(f"- Timeouts: {stats['timeout']}")
    md.append(f"- Quota exhausted: {stats['quota_exhausted']}")
    md.append("")

    md.append("## 10. Limitations")
    if is_partial:
        md.append(f"- **PARTIAL EVALUATION**: {stats['api_error']} scenarios had provider errors")
        if stats["quota_exhausted"] > 0:
            md.append(f"- Groq daily token quota exhausted after {stats['quota_exhausted']} scenarios")
    else:
        md.append("- Full evaluation completed successfully")
    md.append("")

    md.append("## 11. Artifact Provenance")
    if not is_partial:
        md.append(f"- File: `layer2_clean_eval.jsonl`")
        md.append(f"- Dataset fingerprint: `{fingerprint}`")
        md.append(f"- Records: {stats['attempted']}")
    else:
        md.append("- No canonical artifact created (partial evaluation)")

    report_md_path.write_text("\n".join(md), encoding="utf-8")

    print(f"  Report JSON: {report_json_path}")
    print(f"  Report MD: {report_md_path}")
    print()

    # ══════════════════════════════════════════════════════════════
    # FINAL VERDICT
    # ══════════════════════════════════════════════════════════════
    print("=" * 70)
    print("FINAL VERDICT")
    print("=" * 70)
    print()

    if is_partial:
        verdict = "YELLOW"
        reason = "Evaluation is clean but incomplete due to provider limits."
    elif false_accept.count > 0:
        verdict = "RED"
        reason = f"Unresolved false accepts: {false_accept.count}"
    else:
        verdict = "GREEN"
        reason = "All 77 residual scenarios evaluated. No false accepts."

    print(f"  VERDICT: {verdict}")
    print(f"  Reason: {reason}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
