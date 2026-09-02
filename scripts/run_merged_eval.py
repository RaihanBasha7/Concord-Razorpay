"""
Complete Clean Layer 2 Evaluation — Merged Results.

Fresh inference: 6 scenarios (new Groq output with proposed_match_ids)
Documented continuation: 36 scenarios (prior Groq output, outcome+confidence only, no proposed_match_ids)
Provider failures: 35 scenarios (quota exhausted in both runs)
Unevaluated: 0 scenarios (all 77 attempted)

Produces:
  - data/clean_full_evaluation_report.json
  - data/clean_full_evaluation_report.md
  - data/clean_full_eval_results.json (updated)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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

    from reconciliation.domain.models import NormalizedRecord, SourceType
    from reconciliation.evaluation.dataset_fingerprint import read_manifest, verify_dataset
    from reconciliation.evaluation.dataset_generator import (
        EdgeCaseCategory, _compute_record_id, check_leakage, generate_dataset,
    )
    from reconciliation.evaluation.ground_truth import GroundTruthUnit
    from reconciliation.evaluation.metrics import evaluate as evaluate_l1
    from reconciliation.evaluation.primitives import (
        ScenarioOutcome, build_record_outcomes, compute_ai_precision_at_threshold,
        compute_ai_recall, compute_deterministic_metrics, compute_exception_composition,
        compute_false_accept_metrics, compute_review_queue_composition,
        compute_throughput_metrics, validate_record_outcomes,
    )
    from reconciliation.evaluation.full_pipeline_evaluation import (
        _build_ground_truth_units, _expected_match_ids,
    )
    from reconciliation.layer2 import reconstruct_layer2_case
    from reconciliation.layer3 import route as layer3_route
    from reconciliation.loader import load_normalized_records, load_residuals
    from reconciliation.matcher import reconcile
    from reconciliation.matcher_config import MatcherConfig
    from reconciliation.proposal import MatchProposal
    from reconciliation.proposal_validation import ProposalOutcome, ProposalOutcomeType
    from reconciliation.retrieval import RetrievalConfig, retrieve_candidates
    from reconciliation.baseline.naive_matcher import match as baseline_match

    # ── Configuration ────────────────────────────────────────────
    data_dir = Path("data")
    report_json_path = data_dir / "clean_full_evaluation_report.json"
    report_md_path = data_dir / "clean_full_evaluation_report.md"
    results_path = data_dir / "clean_full_eval_results.json"

    config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
    retrieval_config = RetrievalConfig()
    AUTO_ACCEPT_THRESHOLD = 0.90
    REVIEW_THRESHOLD = 0.60

    print("=" * 70)
    print("COMPLETE CLEAN LAYER 2 EVALUATION (MERGED)")
    print("=" * 70)
    print()

    # ── PHASE 1: Pre-flight ──────────────────────────────────────
    print("PHASE 1: PRE-FLIGHT VERIFICATION")
    print("-" * 50)

    manifest = read_manifest(data_dir)
    assert manifest is not None
    verification = verify_dataset(data_dir, manifest)
    assert verification.ok, f"Drift: {verification.details}"

    fingerprint = manifest.fingerprint()
    EXPECTED_FP = "b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93"
    assert fingerprint == EXPECTED_FP

    dataset = generate_dataset(seed=42)
    leakage = check_leakage(list(dataset.scenarios), list(dataset.record_specs))
    assert not leakage.has_leakage

    normalized = load_normalized_records(data_dir)
    residuals = load_residuals(data_dir)
    all_records = list(normalized)

    assert len(all_records) == 245
    assert len(dataset.scenarios) == 120
    assert len(residuals) == 77

    print(f"  Dataset fingerprint: {fingerprint} OK")
    print(f"  Leakage check: PASSED")
    print(f"  Records: {len(all_records)} (expected 245) OK")
    print(f"  Scenarios: {len(dataset.scenarios)} (expected 120) OK")
    print(f"  Residuals: {len(residuals)} (expected 77) OK")
    print()

    # ── Setup maps ───────────────────────────────────────────────
    scenario_map = {s.scenario_id: s for s in dataset.scenarios}
    unit_map = {u.scenario_id: u for u in _build_ground_truth_units(list(dataset.scenarios))}
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
    print(f"  L1 matched: {l1_matched}/{len(all_records)} ({l1_matched/len(all_records):.1%})")
    print(f"  L1 residual records: {l1_residual_records}")
    print(f"  L1 residual scenarios: {l1_residual_scenarios}")
    print()

    # Baseline
    baseline_result = baseline_match(list(normalized), config)
    baseline_match_ids = set()
    for a, b in baseline_result.matches:
        baseline_match_ids.add(a)
        baseline_match_ids.add(b)

    # ── Load merged results ──────────────────────────────────────
    print("Loading merged Layer 2 results...")
    fresh_results = json.loads(results_path.read_text())

    # Source classification
    fresh_scenarios = {}  # scenario_id -> fresh result (has proposed_match_ids)
    prior_scenarios = {}  # scenario_id -> prior result (outcome+confidence only)
    failed_scenarios = {}  # scenario_id -> error result

    for r in fresh_results["results"]:
        sid = r["scenario_id"]
        if r["outcome"] in ("PROPOSAL_VALID", "NO_PROPOSAL", "VALIDATION_FAILED"):
            if r.get("proposed_match_ids"):
                fresh_scenarios[sid] = r
            else:
                prior_scenarios[sid] = r
        elif r["outcome"] == "API_ERROR":
            failed_scenarios[sid] = r

    # Load additional prior results from clean_eval_results.json for scenarios
    # where fresh run hit quota but prior run succeeded
    old_results = json.loads((data_dir / "clean_eval_results.json").read_text())
    for r in old_results["results"]:
        sid = r["scenario_id"]
        if r["outcome"] in ("PROPOSAL_VALID", "NO_PROPOSAL"):
            if sid in failed_scenarios and sid not in fresh_scenarios:
                # Fresh run failed, prior run succeeded: use prior as continuation
                prior_scenarios[sid] = r
                del failed_scenarios[sid]  # Remove from failures since we have prior data

    total_fresh = len(fresh_scenarios)
    total_prior = len(prior_scenarios)
    total_failed = len(failed_scenarios)
    total_unevaluated = 77 - total_fresh - total_prior - total_failed

    print(f"  Fresh inference (with proposed_match_ids): {total_fresh}")
    print(f"  Prior results (outcome+confidence, no proposal IDs): {total_prior}")
    print(f"  Provider failures: {total_failed}")
    print(f"  Unevaluated: {total_unevaluated}")
    print()

    # ── Build Layer 2 outcomes from merged data ──────────────────
    print("Building Layer 2 outcomes from merged data...")

    l2_outcomes: List[Tuple[str, ProposalOutcome]] = []
    stats = {
        "fresh_ok": total_fresh,
        "prior_ok": total_prior,
        "api_error": total_failed,
        "quota_exhausted": sum(1 for r in failed_scenarios.values() if r.get("error_classification") == "quota_exhausted"),
        "transient": sum(1 for r in failed_scenarios.values() if r.get("error_classification") == "transient"),
        "no_proposal": 0,
        "valid_proposal": 0,
        "validation_failed": 0,
    }

    for residual in residuals:
        sid = residual.scenario_id
        case = reconstruct_layer2_case(
            scenario_id=sid,
            member_record_ids=residual.member_record_ids,
            normalized_records=tuple(normalized),
        )
        retrieval = retrieve_candidates(case, tuple(normalized), retrieval_config)
        presented_ids = tuple(
            dict.fromkeys(
                [r.record_id for r in case.member_records]
                + [c.record.record_id for c in retrieval.candidates]
            )
        )

        if sid in fresh_scenarios:
            r = fresh_scenarios[sid]
            if r["outcome"] == "PROPOSAL_VALID":
                proposal = MatchProposal(
                    proposed_match_ids=r["proposed_match_ids"],
                    confidence=r["confidence"],
                    rationale="Fresh Groq inference",
                )
                outcome = ProposalOutcome(
                    outcome=ProposalOutcomeType.PROPOSAL_VALID,
                    proposal=proposal,
                    presented_record_ids=presented_ids,
                    reason="Fresh inference.",
                )
                stats["valid_proposal"] += 1
            elif r["outcome"] == "NO_PROPOSAL":
                outcome = ProposalOutcome(
                    outcome=ProposalOutcomeType.NO_PROPOSAL,
                    proposal=None,
                    presented_record_ids=presented_ids,
                    reason="Model returned no proposed match IDs.",
                )
                stats["no_proposal"] += 1
            elif r["outcome"] == "VALIDATION_FAILED":
                # Proposal was rejected by guardrail; treat as validation failed
                proposal = MatchProposal(
                    proposed_match_ids=r.get("proposed_match_ids", []),
                    confidence=r.get("confidence", 0.0),
                    rationale="Fresh Groq inference (guardrail-rejected)",
                )
                outcome = ProposalOutcome(
                    outcome=ProposalOutcomeType.VALIDATION_FAILED,
                    proposal=proposal,
                    presented_record_ids=presented_ids,
                    reason="Proposal rejected by same-source duplicate over-inclusion guardrail.",
                )
                stats["validation_failed"] += 1
        elif sid in prior_scenarios:
            r = prior_scenarios[sid]
            if r["outcome"] == "PROPOSAL_VALID":
                # Prior result: we have outcome+confidence but NOT proposed_match_ids
                # Route based on confidence, but correctness is UNKNOWN
                conf = r.get("confidence", 0.0)
                outcome = ProposalOutcome(
                    outcome=ProposalOutcomeType.PROPOSAL_VALID,
                    proposal=MatchProposal(
                        proposed_match_ids=[],  # Unknown from prior result
                        confidence=conf,
                        rationale="Prior evaluation (no proposed_match_ids available)",
                    ),
                    presented_record_ids=presented_ids,
                    reason="Prior evaluation result. Confidence available but proposed_match_ids not recorded.",
                )
                stats["valid_proposal"] += 1
            elif r["outcome"] == "NO_PROPOSAL":
                outcome = ProposalOutcome(
                    outcome=ProposalOutcomeType.NO_PROPOSAL,
                    proposal=None,
                    presented_record_ids=presented_ids,
                    reason="Prior evaluation: model returned no proposed match IDs.",
                )
                stats["no_proposal"] += 1
        elif sid in failed_scenarios:
            r = failed_scenarios[sid]
            classification = r.get("error_classification", "unknown")
            outcome = ProposalOutcome(
                outcome=ProposalOutcomeType.API_ERROR,
                proposal=None,
                presented_record_ids=presented_ids,
                reason=f"Provider failure: {classification}",
                error_classification=classification,
            )
        else:
            outcome = ProposalOutcome(
                outcome=ProposalOutcomeType.API_ERROR,
                proposal=None,
                presented_record_ids=presented_ids,
                reason="No Layer 2 result available.",
                error_classification="unevaluated",
            )

        l2_outcomes.append((sid, outcome))

    print(f"  Valid proposals: {stats['valid_proposal']}")
    print(f"  No proposals: {stats['no_proposal']}")
    print(f"  Validation failed: {stats['validation_failed']}")
    print(f"  API errors: {stats['api_error']}")
    print()

    # ── Layer 3 routing ──────────────────────────────────────────
    print("Running Layer 3 routing...")
    l2_outcome_list = [o for _, o in l2_outcomes]
    routing_decisions = layer3_route(
        layer1_decisions=l1_decisions,
        layer2_outcomes=l2_outcome_list,
        all_records=all_records,
    )

    record_routing = {rd.record_id: rd for rd in routing_decisions}
    bucket_counts = Counter(rd.bucket.value for rd in routing_decisions)

    print("  Record-level routing:")
    for bucket, count in sorted(bucket_counts.items()):
        print(f"    {bucket}: {count}")
    print()

    # ── Build scenario outcomes ──────────────────────────────────
    l1_eval_map = {ev.scenario_id: ev for ev in l1_report.scenario_evaluations}
    l2_scenario_map = {sid: o for sid, o in l2_outcomes}

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

                # Only compute correctness when proposed_match_ids are available
                if ai_proposal_ids:
                    expected = _expected_match_ids(scen, unit)
                    ai_correct = (
                        tuple(sorted(ai_proposal_ids)) == tuple(sorted(expected))
                    )
                else:
                    # Prior result: correctness unknown
                    ai_correct = None

                layer2_ms = fresh_results["results"][
                    next(i for i, r in enumerate(fresh_results["results"]) if r["scenario_id"] == sid)
                ].get("elapsed_ms") if sid in fresh_scenarios else None
            elif l2_type == ProposalOutcomeType.API_ERROR:
                routing_bucket = "EXCEPTION"
                ai_confidence = None
                ai_proposal_ids = ()
                ai_correct = None
                layer2_ms = None
            elif l2_type == ProposalOutcomeType.VALIDATION_FAILED:
                routing_bucket = "EXCEPTION"
                ai_confidence = l2_outcome.proposal.confidence if l2_outcome.proposal else None
                ai_proposal_ids = tuple(l2_outcome.proposal.proposed_match_ids) if l2_outcome.proposal else ()
                # Guardrail-rejected: check what the proposal was
                if ai_proposal_ids:
                    expected = _expected_match_ids(scen, unit)
                    ai_correct = (
                        tuple(sorted(ai_proposal_ids)) == tuple(sorted(expected))
                    )
                else:
                    ai_correct = None
                layer2_ms = None
            else:
                routing_bucket = "EXCEPTION"
                ai_confidence = None
                ai_proposal_ids = ()
                ai_correct = not unit.has_real_match
                layer2_ms = None

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

    # ── Record-level outcomes ────────────────────────────────────
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

    # ══════════════════════════════════════════════════════════════
    # METRICS
    # ══════════════════════════════════════════════════════════════
    def _fmt_pct(val):
        return "N/A" if val is None else f"{val:.1%}"

    # Record-level accounting
    record_buckets = Counter(ro.routing_bucket for ro in record_outcomes)
    total_records = len(record_outcomes)
    det_records = record_buckets.get("DETERMINISTIC_MATCH", 0)
    ai_records = record_buckets.get("AI_AUTO_ACCEPTED", 0)
    review_records = record_buckets.get("HUMAN_REVIEW", 0)
    exception_records = record_buckets.get("EXCEPTION", 0)

    assert det_records + ai_records + review_records + exception_records == total_records

    # Scenario-level accounting
    scenario_buckets = Counter(so.routing_bucket for so in scenario_outcomes)
    total_scenarios = len(scenario_outcomes)
    det_scenarios = scenario_buckets.get("DETERMINISTIC_MATCH", 0)
    ai_scenarios = scenario_buckets.get("AI_AUTO_ACCEPTED", 0)
    review_scenarios = scenario_buckets.get("HUMAN_REVIEW", 0)
    exception_scenarios = scenario_buckets.get("EXCEPTION", 0)

    # Model quality metrics
    det_metrics = compute_deterministic_metrics(scenario_outcomes)
    ai_p090 = compute_ai_precision_at_threshold(scenario_outcomes, 0.90)
    ai_p075 = compute_ai_precision_at_threshold(scenario_outcomes, 0.75)
    ai_p060 = compute_ai_precision_at_threshold(scenario_outcomes, 0.60)
    ai_recall = compute_ai_recall(scenario_outcomes)
    false_accept = compute_false_accept_metrics(record_outcomes)
    review_queue = compute_review_queue_composition(record_outcomes)
    exception_comp = compute_exception_composition(record_outcomes)
    throughput = compute_throughput_metrics(scenario_outcomes)

    # Proposal-level metrics (only scenarios with proposed_match_ids AND known correctness)
    proposals_with_correctness = [so for so in scenario_outcomes if so.ai_proposal_ids and so.ai_correct is not None]
    proposal_correct = [so for so in proposals_with_correctness if so.ai_correct]
    proposal_incorrect = [so for so in proposals_with_correctness if not so.ai_correct]

    # Scenarios where correctness is unknown (prior results without proposed_match_ids)
    prior_without_proposal_ids = [so for so in scenario_outcomes
                                   if so.layer2_outcome_type == "PROPOSAL_VALID"
                                   and not so.ai_proposal_ids
                                   and so.ai_correct is None]

    # Residual recall
    residual_with_match = [so for so in scenario_outcomes if not so.deterministic_matched and so.has_real_match]
    residual_ai_correct = [so for so in residual_with_match if so.ai_correct is True]

    # Guardrail impact
    guardrail_affected = [so for so in scenario_outcomes if so.layer2_outcome_type == "VALIDATION_FAILED"]

    # Edge case breakdown
    cat_metrics: Dict[str, Dict[str, Any]] = {}
    for so in scenario_outcomes:
        cat = so.category.value
        if cat not in cat_metrics:
            cat_metrics[cat] = {
                "scenario_count": 0, "l1_matched": 0, "residual": 0,
                "l2_attempted": 0, "l2_successful": 0, "api_errors": 0,
                "no_proposal": 0, "valid_proposals": 0, "auto_accept": 0,
                "human_review": 0, "exception": 0, "false_accepts": 0,
                "correct_known": 0, "correct_unknown": 0,
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
            if so.ai_correct is True:
                cm["correct_known"] += 1
            elif so.ai_correct is None:
                cm["correct_unknown"] += 1

    # False accept details
    false_accept_scenarios = [
        so for so in scenario_outcomes
        if so.routing_bucket == "AI_AUTO_ACCEPTED" and so.ai_correct is False
    ]

    # ══════════════════════════════════════════════════════════════
    # FINAL REPORT
    # ══════════════════════════════════════════════════════════════
    print("=" * 70)
    print("FINAL REPORT")
    print("=" * 70)
    print()

    is_partial = stats["api_error"] > 0
    evaluation_type = "PARTIAL EVALUATION" if is_partial else "FULL EVALUATION"

    # Determine verdict
    if is_partial:
        verdict = "YELLOW"
        verdict_reason = "Evaluation is clean but incomplete due to provider quota limits."
    elif false_accept.count > 0:
        verdict = "RED"
        verdict_reason = f"Unresolved false accepts: {false_accept.count}"
    else:
        verdict = "GREEN"
        verdict_reason = "All 77 residual scenarios evaluated. No false accepts."

    report_data = {
        "evaluation_type": evaluation_type,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "data_source": {
            "fresh_inference": total_fresh,
            "prior_continuation": total_prior,
            "provider_failures": total_failed,
            "unevaluated": total_unevaluated,
            "fresh_inference_note": f"{total_fresh} scenarios with fresh Groq outputs (proposed_match_ids available)",
            "prior_continuation_note": f"{total_prior} scenarios from prior evaluation (outcome+confidence only, proposed_match_ids not recorded; correctness UNKNOWN)",
            "provider_failures_note": f"{total_failed} scenarios with provider quota exhaustion",
        },
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
        },
        "layer2_execution": {
            "total_attempted": 77,
            "fresh_inference": total_fresh,
            "prior_continuation": total_prior,
            "api_errors": total_failed,
            "quota_exhausted": stats["quota_exhausted"],
            "valid_proposals": stats["valid_proposal"],
            "no_proposals": stats["no_proposal"],
            "validation_failed": stats["validation_failed"],
        },
        "layer2_quality": {
            "proposal_level": {
                "total_proposals_with_known_correctness": len(proposals_with_correctness),
                "correct": len(proposal_correct),
                "incorrect": len(proposal_incorrect),
                "precision": len(proposal_correct) / len(proposals_with_correctness) if proposals_with_correctness else None,
                "note": f"Only {len(proposals_with_correctness)} of {stats['valid_proposal']} proposals have known correctness (fresh inference with proposed_match_ids). {len(prior_without_proposal_ids)} prior proposals lack proposed_match_ids.",
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
                "note": "True positives only counted where correctness is known (fresh inference).",
            },
        },
        "layer3_routing": {
            "record_level": dict(bucket_counts),
            "scenario_level": dict(scenario_buckets),
            "accounting_check": det_records + ai_records + review_records + exception_records,
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
            "affected_scenarios": [so.scenario_id for so in guardrail_affected],
        },
        "edge_case_breakdown": cat_metrics,
        "system_safety": {
            "model_quality": {
                "false_accepts": false_accept.count,
                "false_accept_rate": false_accept.rate,
            },
            "system_reliability": {
                "api_failures": total_failed,
                "quota_exhausted": stats["quota_exhausted"],
                "unevaluated": total_unevaluated,
                "provider_failure_rate": total_failed / 77 if 77 > 0 else None,
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
        "limitations": [
            f"PARTIAL EVALUATION: Only {total_fresh} of 77 residual scenarios have fresh Groq outputs with proposed_match_ids.",
            f"{total_prior} scenarios from prior evaluation have outcome+confidence but NOT proposed_match_ids; correctness is UNKNOWN.",
            f"{total_failed} scenarios had provider quota exhaustion and could not be evaluated.",
            "Proposal-level precision is only computable for fresh inference scenarios.",
            "Daily Groq quota (200K tokens) insufficient for 77 sequential requests.",
        ],
    }

    report_json_path.write_text(json.dumps(report_data, indent=2), encoding="utf-8")

    # ── Markdown report ──────────────────────────────────────────
    md = []
    md.append("# Complete Clean Layer 2 Evaluation Report")
    md.append("")
    md.append(f"**Evaluation type:** {evaluation_type}")
    md.append(f"**Verdict:** {verdict} — {verdict_reason}")
    md.append(f"**Timestamp:** {report_data['run_timestamp']}")
    md.append(f"**Dataset fingerprint:** `{fingerprint}`")
    md.append("")

    md.append("## 1. Dataset Provenance")
    md.append(f"- Total records: {len(all_records)}")
    md.append(f"- Total scenarios: {len(dataset.scenarios)}")
    md.append(f"- Residual scenarios: {len(residuals)}")
    md.append(f"- Leakage-free: Verified")
    md.append(f"- Fingerprint: `{fingerprint}`")
    md.append("")

    md.append("## 2. Layer 1 Baseline")
    md.append(f"- Matched records: {l1_matched} / {len(all_records)} ({l1_matched/len(all_records):.1%})")
    md.append(f"- Residual records: {l1_residual_records}")
    md.append(f"- Residual scenarios: {l1_residual_scenarios}")
    md.append(f"- Deterministic precision: {_fmt_pct(det_metrics.precision)}")
    md.append("")

    md.append("## 3. Layer 2 Execution")
    md.append(f"- Total attempted: 77")
    md.append(f"- Fresh inference: {total_fresh} (with proposed_match_ids)")
    md.append(f"- Prior continuation: {total_prior} (outcome+confidence only)")
    md.append(f"- Provider failures: {total_failed} (quota exhausted)")
    md.append(f"- Unevaluated: {total_unevaluated}")
    md.append("")

    md.append("## 4. Layer 2 Quality")
    md.append("### Proposal-level")
    md.append(f"- Proposals with known correctness: {len(proposals_with_correctness)}")
    md.append(f"- Correct: {len(proposal_correct)}")
    md.append(f"- Incorrect: {len(proposal_incorrect)}")
    md.append(f"- Precision: {len(proposal_correct)/len(proposals_with_correctness):.1%}" if proposals_with_correctness else "- Precision: N/A (no proposals with known correctness)")
    md.append(f"- Note: {len(prior_without_proposal_ids)} prior proposals lack proposed_match_ids.")
    md.append("")
    md.append("### Precision at thresholds")
    md.append(f"- >=0.90: {_fmt_pct(ai_p090.precision)} ({ai_p090.true_positives}TP / {ai_p090.true_positives+ai_p090.false_positives} total)")
    md.append(f"- >=0.75: {_fmt_pct(ai_p075.precision)} ({ai_p075.true_positives}TP / {ai_p075.true_positives+ai_p075.false_positives} total)")
    md.append(f"- >=0.60: {_fmt_pct(ai_p060.precision)} ({ai_p060.true_positives}TP / {ai_p060.true_positives+ai_p060.false_positives} total)")
    md.append("")
    md.append("### Recall")
    md.append(f"- System-wide: {_fmt_pct(ai_recall.recall)} ({ai_recall.true_positives}TP / {ai_recall.denominator} residuals)")
    md.append("")

    md.append("## 5. Layer 3 Routing")
    md.append("### Record-level")
    for bucket in ["DETERMINISTIC_MATCH", "AI_AUTO_ACCEPTED", "HUMAN_REVIEW", "EXCEPTION"]:
        count = bucket_counts.get(bucket, 0)
        md.append(f"- {bucket}: {count} ({count/total_records:.1%})")
    md.append(f"- Accounting check: {det_records}+{ai_records}+{review_records}+{exception_records} = {det_records+ai_records+review_records+exception_records}")
    md.append("")
    md.append("### Scenario-level")
    for bucket in ["DETERMINISTIC_MATCH", "AI_AUTO_ACCEPTED", "HUMAN_REVIEW", "EXCEPTION"]:
        count = scenario_buckets.get(bucket, 0)
        md.append(f"- {bucket}: {count}")
    md.append("")

    md.append("## 6. False Accepts")
    md.append(f"- Count: {false_accept.count}")
    md.append(f"- Rate: {_fmt_pct(false_accept.rate)}")
    md.append(f"- Total auto-accepted: {false_accept.total_auto_accepted}")
    if false_accept_scenarios:
        for so in false_accept_scenarios:
            md.append(f"  - {so.scenario_id} ({so.category.value}): confidence={so.ai_confidence:.2f}")
    md.append("")

    md.append("## 7. Guardrail Impact")
    md.append(f"- Proposals affected: {len(guardrail_affected)}")
    if guardrail_affected:
        for so in guardrail_affected:
            md.append(f"  - {so.scenario_id} ({so.category.value})")
    md.append("")

    md.append("## 8. Edge-Case Breakdown (SCENARIO-LEVEL)")
    md.append("| Category | Scn | L1 | Res | L2att | L2ok | Err | NoP | Prop | A-acc | Rev | Exc | FA | Correct? |")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for cat in sorted(cat_metrics.keys()):
        cm = cat_metrics[cat]
        md.append(f"| {cat} | {cm['scenario_count']} | {cm['l1_matched']} | {cm['residual']} "
                  f"| {cm['l2_attempted']} | {cm['l2_successful']} | {cm['api_errors']} | {cm['no_proposal']} "
                  f"| {cm['valid_proposals']} | {cm['auto_accept']} | {cm['human_review']} | {cm['exception']} "
                  f"| {cm['false_accepts']} | {cm['correct_known']}K/{cm['correct_unknown']}U |")
    md.append("")
    md.append("*K = known correctness, U = unknown (prior results without proposed_match_ids)*")
    md.append("")

    md.append("## 9. Provider Reliability")
    md.append(f"- API failures: {total_failed} / 77 ({total_failed/77:.1%})")
    md.append(f"- Quota exhausted: {stats['quota_exhausted']}")
    md.append(f"- Transient errors: {stats['transient']}")
    md.append("")

    md.append("## 10. Limitations")
    md.append(f"1. **PARTIAL EVALUATION**: Only {total_fresh} of 77 residual scenarios have fresh Groq outputs with proposed_match_ids.")
    md.append(f"2. **{total_prior} prior results lack proposed_match_ids**: Correctness is UNKNOWN for these scenarios. They contribute to routing but not to precision/recall metrics.")
    md.append(f"3. **{total_failed} provider failures**: Groq daily quota (200K tokens) exhausted after {total_fresh} fresh requests.")
    md.append(f"4. **Proposal-level precision** is only computable for {len(proposals_with_correctness)} scenarios with known correctness.")
    md.append("")

    md.append("## 11. Artifact Provenance")
    md.append(f"- Dataset fingerprint: `{fingerprint}`")
    md.append(f"- Fresh inference results: {total_fresh} scenarios")
    md.append(f"- Prior continuation results: {total_prior} scenarios")
    md.append(f"- Provider failures: {total_failed} scenarios")
    md.append(f"- No canonical artifact created (partial evaluation)")

    report_md_path.write_text("\n".join(md), encoding="utf-8")

    print(f"  Report: {report_json_path}")
    print(f"  Report: {report_md_path}")
    print()

    # ══════════════════════════════════════════════════════════════
    # PRINT SUMMARY
    # ══════════════════════════════════════════════════════════════
    print("=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    print()
    print(f"  VERDICT: {verdict}")
    print(f"  {verdict_reason}")
    print()
    print(f"  ### Dataset")
    print(f"  - Fingerprint: {fingerprint}")
    print(f"  - Records: {len(all_records)}, Scenarios: {len(dataset.scenarios)}, Residuals: {len(residuals)}")
    print()
    print(f"  ### Layer 1")
    print(f"  - Match rate: {l1_matched}/{len(all_records)} ({l1_matched/len(all_records):.1%})")
    print(f"  - Precision: {_fmt_pct(det_metrics.precision)}")
    print()
    print(f"  ### Layer 2")
    print(f"  - Fresh inference: {total_fresh}/77 scenarios")
    print(f"  - Prior continuation: {total_prior}/77 scenarios (no proposed_match_ids)")
    print(f"  - Provider failures: {total_failed}/77 scenarios")
    print()
    print(f"  ### Layer 3 (Record-level)")
    print(f"  - DETERMINISTIC_MATCH: {det_records}")
    print(f"  - AI_AUTO_ACCEPTED: {ai_records}")
    print(f"  - HUMAN_REVIEW: {review_records}")
    print(f"  - EXCEPTION: {exception_records}")
    print(f"  - Check: {det_records}+{ai_records}+{review_records}+{exception_records} = {det_records+ai_records+review_records+exception_records}")
    print()
    print(f"  ### Safety")
    print(f"  - False accepts: {false_accept.count} / {false_accept.total_auto_accepted} auto-accepted ({_fmt_pct(false_accept.rate)})")
    print(f"  - Provider failure rate: {total_failed/77:.1%}")
    print()
    print(f"  ### Guardrail Impact")
    print(f"  - Proposals affected: {len(guardrail_affected)}")
    print()
    print(f"  ### Limitations")
    print(f"  - Only {total_fresh}/77 scenarios have fresh inference with proposed_match_ids")
    print(f"  - {total_prior} scenarios from prior run lack proposed_match_ids (correctness unknown)")
    print(f"  - Daily Groq quota exhausted; cannot complete fresh evaluation")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
