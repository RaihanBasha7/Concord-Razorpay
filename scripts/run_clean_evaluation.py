"""
Fresh clean Layer 2 evaluation against the leakage-remediated dataset.

Every result in this evaluation comes from the NEW clean dataset.
No old artifacts are reused.

Produces:
  - data/layer2_clean_audit.jsonl (canonical artifact)
  - data/clean_evaluation_report.json (full metrics)
  - data/clean_evaluation_report.md (human-readable report)
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ------------------------------------------------------------------
# .env loader
# ------------------------------------------------------------------

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


# ------------------------------------------------------------------
# Main evaluation
# ------------------------------------------------------------------

def main() -> int:
    _load_dotenv()

    from reconciliation.audit import Auditor, AuditRecord, make_audit_record
    from reconciliation.domain.models import NormalizedRecord, SourceType
    from reconciliation.evaluation.dataset_fingerprint import (
        DatasetManifest,
        compute_dataset_manifest,
        read_manifest,
        verify_dataset,
    )
    from reconciliation.evaluation.dataset_generator import (
        CATEGORY_QUOTAS,
        EdgeCaseCategory,
        GeneratedDataset,
        GroundTruthScenario,
        _compute_record_id,
        check_leakage,
        generate_dataset,
    )
    from reconciliation.evaluation.ground_truth import GroundTruthUnit
    from reconciliation.evaluation.metrics import (
        EvaluationReport,
        evaluate as evaluate_l1,
    )
    from reconciliation.evaluation.primitives import (
        ScenarioOutcome,
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
    from reconciliation.evaluation.full_pipeline_evaluation import (
        _build_ground_truth_units,
        _expected_match_ids,
    )
    from reconciliation.groq_provider import (
        GroqProviderError,
        GroqStructuredProvider,
        safe_diagnostic,
    )
    from reconciliation.layer2 import Layer2Case, reconstruct_layer2_case
    from reconciliation.layer3 import route as layer3_route
    from reconciliation.loader import load_normalized_records, load_residuals
    from reconciliation.matcher import reconcile
    from reconciliation.matcher_config import MatcherConfig
    from reconciliation.proposal_orchestration import ProposalOrchestrator
    from reconciliation.proposal_service import ProposalService
    from reconciliation.proposal_validation import (
        ProposalOutcome,
        ProposalOutcomeType,
    )
    from reconciliation.retrieval import RetrievalConfig, retrieve_candidates
    from reconciliation.routing import route as route_l2

    # ── Configuration ─────────────────────────────────────────────
    data_dir = Path("data")
    artifact_path = data_dir / "layer2_clean_audit.jsonl"
    config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
    retrieval_config = RetrievalConfig()
    AUTO_ACCEPT_THRESHOLD = 0.90
    REVIEW_THRESHOLD = 0.60

    # ── Dataset verification ──────────────────────────────────────
    print("=" * 70)
    print("FRESH CLEAN LAYER 2 EVALUATION")
    print("=" * 70)
    print()

    manifest = read_manifest(data_dir)
    if manifest is None:
        print("ERROR: No frozen dataset manifest found.")
        return 1

    verification = verify_dataset(data_dir, manifest)
    if not verification.ok:
        print(f"DRIFT DETECTED: {verification.details}")
        return 1

    fingerprint = manifest.fingerprint()
    print(f"Dataset fingerprint: {fingerprint}")
    print(f"Dataset verified: YES")

    # Generate dataset for ground truth
    dataset = generate_dataset(seed=42)
    leakage = check_leakage(list(dataset.scenarios), list(dataset.record_specs))
    assert not leakage.has_leakage, f"Leakage detected: {leakage.issues}"
    print(f"Leakage check: PASSED")

    # ── Load data ─────────────────────────────────────────────────
    normalized = load_normalized_records(data_dir)
    residuals = load_residuals(data_dir)
    all_records = list(normalized)

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

    print(f"Normalized records: {len(all_records)}")
    print(f"Residual scenarios: {len(residuals)}")
    print()

    # ── Layer 1 ───────────────────────────────────────────────────
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

    # Persist residuals if needed
    residuals_path = data_dir / "residuals.csv"
    if not residuals_path.exists():
        persist_residuals(
            scenarios=list(dataset.scenarios),
            pipeline_residual_scenario_ids=list(l1_report.pipeline_residual_scenario_ids),
            output_dir=data_dir,
        )
        # Re-freeze after generating residuals
        from reconciliation.evaluation.dataset_fingerprint import write_manifest as wm
        new_manifest = compute_dataset_manifest(data_dir, dataset_seed=42)
        wm(new_manifest, data_dir)
        manifest = new_manifest
        fingerprint = manifest.fingerprint()

    l1_matched = len(l1_decisions)
    l1_residual_scenarios = len(l1_report.pipeline_residual_scenario_ids)
    l1_residual_records = len(l1_residual_ids)
    print(f"  Layer 1 matched: {l1_matched} records")
    print(f"  Layer 1 residual records: {l1_residual_records}")
    print(f"  Layer 1 residual scenarios: {l1_residual_scenarios}")
    print(f"  Layer 1 time: {layer1_time_ms:.1f}ms")
    print()

    # ── Layer 2: Fresh Groq evaluation ────────────────────────────
    print("=" * 70)
    print("LAYER 2: FRESH GROQ EVALUATION")
    print("=" * 70)

    provider = GroqStructuredProvider()
    service = ProposalService(provider)
    orchestrator = ProposalOrchestrator(service)

    # Stats tracking
    stats = {
        "attempted": 0,
        "successful": 0,
        "api_error": 0,
        "quota_exhausted": 0,
        "timeout": 0,
        "transient_error": 0,
        "validation_failed": 0,
        "no_proposal": 0,
        "valid_proposal": 0,
        "malformed_output": 0,
        "unknown_error": 0,
    }
    outcomes: List[Tuple[str, ProposalOutcome, float]] = []  # (scenario_id, outcome, elapsed_ms)
    error_details: List[Dict[str, Any]] = []

    # Process each residual
    for i, residual in enumerate(residuals):
        stats["attempted"] += 1
        case = reconstruct_layer2_case(
            scenario_id=residual.scenario_id,
            member_record_ids=residual.member_record_ids,
            normalized_records=tuple(normalized),
        )
        retrieval = retrieve_candidates(case, tuple(normalized), retrieval_config)

        t0 = time.perf_counter()
        try:
            outcome = orchestrator.resolve(case, retrieval)
            elapsed = (time.perf_counter() - t0) * 1000.0
        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000.0
            # Wrap unexpected exceptions as API_ERROR
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

        outcomes.append((residual.scenario_id, outcome, elapsed))

        # Categorize outcome
        if outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID:
            stats["valid_proposal"] += 1
            stats["successful"] += 1
        elif outcome.outcome == ProposalOutcomeType.NO_PROPOSAL:
            stats["no_proposal"] += 1
            stats["successful"] += 1  # Model responded, just had no proposal
        elif outcome.outcome == ProposalOutcomeType.VALIDATION_FAILED:
            stats["validation_failed"] += 1
            stats["successful"] += 1  # Model responded, but invalid
        elif outcome.outcome == ProposalOutcomeType.API_ERROR:
            stats["api_error"] += 1
            classification = outcome.error_classification or "unknown"
            if classification == "quota_exhausted":
                stats["quota_exhausted"] += 1
            elif classification == "transient":
                stats["transient_error"] += 1
            elif classification == "timeout":
                stats["timeout"] += 1
            else:
                stats["unknown_error"] += 1
            error_details.append({
                "scenario_id": residual.scenario_id,
                "error_type": classification,
                "diagnostic": outcome.diagnostic or "",
            })
        elif outcome.outcome == ProposalOutcomeType.TIMEOUT:
            stats["timeout"] += 1
            stats["api_error"] += 1
            error_details.append({
                "scenario_id": residual.scenario_id,
                "error_type": "timeout",
                "diagnostic": outcome.diagnostic or "",
            })

        # Progress and pacing
        progress = i + 1
        if progress % 10 == 0 or progress == len(residuals):
            print(f"  [{progress}/{len(residuals)}] "
                  f"OK={stats['valid_proposal']+stats['no_proposal']} "
                  f"ERR={stats['api_error']} "
                  f"FAIL_VALID={stats['validation_failed']}")
        if i < len(residuals) - 1:
            time.sleep(3.0)  # Rate-limit pacing

    print()
    print(f"Layer 2 execution summary:")
    print(f"  Attempted:  {stats['attempted']}")
    print(f"  Successful: {stats['successful']}")
    print(f"  API errors: {stats['api_error']}")
    print(f"    - quota_exhausted: {stats['quota_exhausted']}")
    print(f"    - transient:       {stats['transient_error']}")
    print(f"    - timeout:         {stats['timeout']}")
    print(f"    - unknown:         {stats['unknown_error']}")
    print(f"  Valid proposals:     {stats['valid_proposal']}")
    print(f"  No proposals:        {stats['no_proposal']}")
    print(f"  Validation failed:   {stats['validation_failed']}")
    print()

    # ── Create canonical artifact ─────────────────────────────────
    print("Creating canonical artifact...")

    # Archive old artifact if it exists and is from the old dataset
    if artifact_path.exists():
        with artifact_path.open("r") as f:
            first_line = f.readline()
            try:
                old_rec = json.loads(first_line)
                old_fp = old_rec.get("dataset_fingerprint", "")
                if old_fp != fingerprint:
                    # Archive old artifact
                    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                    archive_path = artifact_path.with_suffix(f".old.{ts}.jsonl")
                    import shutil
                    shutil.copy2(artifact_path, archive_path)
                    print(f"  Old artifact archived to {archive_path.name}")
            except (json.JSONDecodeError, KeyError):
                pass

    # Write new artifact
    auditor = Auditor(artifact_path, archive_existing=False, atomic_write=True)
    for scenario_id, outcome, elapsed_ms in outcomes:
        member_ids = []
        for r in normalized:
            if record_to_scenario.get(r.record_id) == scenario_id:
                member_ids.append(r.record_id)

        # Build presented IDs
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

    # Compute artifact SHA-256
    import hashlib
    artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    print(f"  Artifact: {artifact_path}")
    print(f"  SHA-256:  {artifact_hash}")
    print(f"  Records:  {auditor.write_count}")
    print()

    # ── Layer 3 routing ───────────────────────────────────────────
    print("Running Layer 3 routing...")

    l2_outcomes_list = [outcome for _, outcome, _ in outcomes]
    routing_decisions = layer3_route(
        layer1_decisions=l1_decisions,
        layer2_outcomes=l2_outcomes_list,
        all_records=all_records,
    )

    record_routing = {rd.record_id: rd for rd in routing_decisions}
    bucket_counts = Counter(rd.bucket.value for rd in routing_decisions)
    print(f"  Routing composition:")
    for bucket, count in sorted(bucket_counts.items()):
        print(f"    {bucket}: {count}")
    print()

    # ── Build scenario outcomes for metrics ───────────────────────
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

        if l1_eval:
            layer1_time = layer1_time_ms
        else:
            layer1_time = None

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

        so = ScenarioOutcome(
            scenario_id=sid,
            category=scen.category,
            is_true_orphan=unit.is_true_orphan,
            has_real_match=scen.has_real_match,
            baseline_matched=False,  # will be filled below
            baseline_correct=None,
            deterministic_matched=det_matched,
            deterministic_correct=det_correct,
            routing_bucket=routing_bucket,
            ai_confidence=ai_confidence,
            ai_proposal_ids=ai_proposal_ids,
            ai_correct=ai_correct,
            layer2_outcome_type=layer2_outcome_type,
            layer1_time_ms=layer1_time,
            layer2_time_ms=layer2_ms,
        )
        scenario_outcomes.append(so)

    # ── Compute metrics ───────────────────────────────────────────
    print("Computing metrics...")

    det_metrics = compute_deterministic_metrics(scenario_outcomes)
    ai_p090 = compute_ai_precision_at_threshold(scenario_outcomes, 0.90)
    ai_p075 = compute_ai_precision_at_threshold(scenario_outcomes, 0.75)
    ai_p060 = compute_ai_precision_at_threshold(scenario_outcomes, 0.60)
    ai_recall = compute_ai_recall(scenario_outcomes)
    false_accept = compute_false_accept_metrics(
        # We need record-level outcomes for this
        # Build a simplified version
        []
    )
    throughput = compute_throughput_metrics(scenario_outcomes)

    # Build record-level outcomes for false accept metrics
    record_scenario_map = {}
    record_routing_map = {}
    record_confidence_map = {}
    for rd in routing_decisions:
        record_routing_map[rd.record_id] = rd.bucket.value
        record_confidence_map[rd.record_id] = rd.confidence
        scen_id = record_to_scenario.get(rd.record_id)
        if scen_id:
            record_scenario_map[rd.record_id] = scen_id

    # Build scenario-level record mapping
    record_outcomes = build_record_outcomes(
        scenario_outcomes=scenario_outcomes,
        record_scenario_map=record_scenario_map,
        record_routing_map=record_routing_map,
        record_confidence_map=record_confidence_map,
    )
    validate_record_outcomes(record_outcomes)

    fo = [o for o in record_outcomes if hasattr(o, "is_false_accept")]
    false_accept_final = compute_false_accept_metrics(fo)
    review_queue = compute_review_queue_composition(fo)
    exception_comp = compute_exception_composition(fo)

    # Edge-case breakdown
    cat_metrics: Dict[str, Dict[str, Any]] = {}
    for so in scenario_outcomes:
        cat = so.category.value
        if cat not in cat_metrics:
            cat_metrics[cat] = {
                "count": 0,
                "l1_matched": 0,
                "l2_attempted": 0,
                "l2_successful": 0,
                "l2_proposals_valid": 0,
                "l2_correct": 0,
                "auto_accept": 0,
                "needs_review": 0,
                "exception": 0,
                "has_real_match": 0,
            }
        cm = cat_metrics[cat]
        cm["count"] += 1
        if so.deterministic_matched:
            cm["l1_matched"] += 1
        if so.layer2_outcome_type is not None:
            cm["l2_attempted"] += 1
            if so.layer2_outcome_type in ("PROPOSAL_VALID", "NO_PROPOSAL", "VALIDATION_FAILED"):
                cm["l2_successful"] += 1
            if so.layer2_outcome_type == "PROPOSAL_VALID":
                cm["l2_proposals_valid"] += 1
                if so.ai_correct:
                    cm["l2_correct"] += 1
        if so.routing_bucket == "AI_AUTO_ACCEPTED":
            cm["auto_accept"] += 1
        elif so.routing_bucket == "HUMAN_REVIEW":
            cm["needs_review"] += 1
        elif so.routing_bucket == "EXCEPTION":
            cm["exception"] += 1
        if so.has_real_match:
            cm["has_real_match"] += 1

    # False accepts
    false_accept_scenarios = [
        so for so in scenario_outcomes
        if so.routing_bucket == "AI_AUTO_ACCEPTED" and so.ai_correct is False
    ]

    # ── Write report ──────────────────────────────────────────────
    print("Writing evaluation report...")

    report_data = {
        "evaluation_type": "FULL EVALUATION" if stats["api_error"] == 0 else "PARTIAL EVALUATION",
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "fingerprint": fingerprint,
            "schema_version": manifest.schema_version,
            "dataset_seed": manifest.dataset_seed,
            "total_records": len(all_records),
            "total_scenarios": len(dataset.scenarios),
            "settlement_rows": len(dataset.settlement_rows),
            "bank_rows": len(dataset.bank_rows),
            "ledger_rows": len(dataset.ledger_rows),
            "leakage_free": True,
        },
        "layer1_baseline": {
            "matched_records": l1_matched,
            "residual_records": l1_residual_records,
            "residual_scenarios": l1_residual_scenarios,
            "match_rate_records": l1_matched / len(all_records) if all_records else 0,
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
            "unknown_errors": stats["unknown_error"],
            "valid_proposals": stats["valid_proposal"],
            "no_proposals": stats["no_proposal"],
            "validation_failed": stats["validation_failed"],
            "error_details": error_details[:20],
        },
        "layer2_quality": {
            "ai_precision_at_090": {
                "threshold": ai_p090.threshold,
                "precision": ai_p090.precision,
                "true_positives": ai_p090.true_positives,
                "false_positives": ai_p090.false_positives,
                "total": ai_p090.total,
            },
            "ai_precision_at_075": {
                "threshold": ai_p075.threshold,
                "precision": ai_p075.precision,
                "true_positives": ai_p075.true_positives,
                "false_positives": ai_p075.false_positives,
                "total": ai_p075.total,
            },
            "ai_precision_at_060": {
                "threshold": ai_p060.threshold,
                "precision": ai_p060.precision,
                "true_positives": ai_p060.true_positives,
                "false_positives": ai_p060.false_positives,
                "total": ai_p060.total,
            },
            "ai_recall": {
                "recall_system_wide": ai_recall.recall,
                "true_positives": ai_recall.true_positives,
                "denominator": ai_recall.denominator,
                "recall_attempted_only": ai_recall.recall_attempted,
                "denominator_attempted": ai_recall.denominator_attempted,
            },
        },
        "layer3_routing": {
            "thresholds": {
                "auto_accept": AUTO_ACCEPT_THRESHOLD,
                "human_review": REVIEW_THRESHOLD,
            },
            "composition": dict(bucket_counts),
            "false_accepts": {
                "count": false_accept_final.count,
                "rate": false_accept_final.rate,
                "total_auto_accepted": false_accept_final.total_auto_accepted,
            },
            "false_accept_scenarios": [
                {
                    "scenario_id": so.scenario_id,
                    "category": so.category.value,
                    "confidence": so.ai_confidence,
                    "proposal_ids": list(so.ai_proposal_ids),
                }
                for so in false_accept_scenarios
            ],
        },
        "edge_case_breakdown": cat_metrics,
        "artifact": {
            "filename": artifact_path.name,
            "sha256": artifact_hash,
            "record_count": auditor.write_count,
        },
        "throughput": {
            "layer1_time_ms": throughput.layer1_time_ms,
            "layer2_total_time_ms": throughput.layer2_time_ms,
            "total_time_ms": throughput.total_batch_time_ms,
        },
        "safety_analysis": {
            "false_accept_count": false_accept_final.count,
            "false_accept_rate": false_accept_final.rate,
            "total_auto_accepted": false_accept_final.total_auto_accepted,
            "human_review_count": review_queue.total,
            "exception_count": exception_comp.total,
            "correctly_refused": exception_comp.correctly_refused,
            "should_have_been_caught": exception_comp.should_have_been_caught,
        },
        "provider_failures": {
            "total": stats["api_error"],
            "quota_exhausted": stats["quota_exhausted"],
            "transient": stats["transient_error"],
            "timeout": stats["timeout"],
            "unknown": stats["unknown_error"],
        },
        "limitations": {
            "api_errors": stats["api_error"] > 0,
            "partial_evaluation": stats["api_error"] > 0,
            "quota_limited": stats["quota_exhausted"] > 0,
        },
    }

    report_path = data_dir / "clean_evaluation_report.json"
    report_path.write_text(json.dumps(report_data, indent=2), encoding="utf-8")

    # Write markdown report
    md_lines = []
    md_lines.append("# Clean Layer 2 Evaluation Report")
    md_lines.append("")
    md_lines.append(f"**Evaluation type:** {report_data['evaluation_type']}")
    md_lines.append(f"**Timestamp:** {report_data['run_timestamp']}")
    md_lines.append(f"**Dataset fingerprint:** `{fingerprint}`")
    md_lines.append(f"**Artifact SHA-256:** `{artifact_hash}`")
    md_lines.append("")

    md_lines.append("## Dataset")
    md_lines.append(f"- Total records: {report_data['dataset']['total_records']}")
    md_lines.append(f"- Total scenarios: {report_data['dataset']['total_scenarios']}")
    md_lines.append(f"- Leakage-free: {report_data['dataset']['leakage_free']}")
    md_lines.append("")

    md_lines.append("## Layer 1 Baseline")
    md_lines.append(f"- Matched records: {l1_matched} / {len(all_records)} ({l1_matched/len(all_records):.1%})")
    md_lines.append(f"- Residual records: {l1_residual_records}")
    md_lines.append(f"- Residual scenarios: {l1_residual_scenarios}")
    md_lines.append(f"- Deterministic precision: {det_metrics.precision:.1%}" if det_metrics.precision else "- Deterministic precision: N/A")
    md_lines.append("")

    md_lines.append("## Layer 2 Execution")
    md_lines.append(f"- Attempted: {stats['attempted']}")
    md_lines.append(f"- Successful: {stats['successful']}")
    md_lines.append(f"- API errors: {stats['api_error']}")
    md_lines.append(f"  - quota_exhausted: {stats['quota_exhausted']}")
    md_lines.append(f"  - transient: {stats['transient_error']}")
    md_lines.append(f"  - timeout: {stats['timeout']}")
    md_lines.append(f"  - unknown: {stats['unknown_error']}")
    md_lines.append(f"- Valid proposals: {stats['valid_proposal']}")
    md_lines.append(f"- No proposals: {stats['no_proposal']}")
    md_lines.append(f"- Validation failed: {stats['validation_failed']}")
    md_lines.append("")

    md_lines.append("## Layer 2 Quality")
    md_lines.append(f"- AI precision @ 0.90: {_fmt_pct(ai_p090.precision)} ({ai_p090.true_positives}TP / {ai_p090.total} total)")
    md_lines.append(f"- AI precision @ 0.75: {_fmt_pct(ai_p075.precision)} ({ai_p075.true_positives}TP / {ai_p075.total} total)")
    md_lines.append(f"- AI precision @ 0.60: {_fmt_pct(ai_p060.precision)} ({ai_p060.true_positives}TP / {ai_p060.total} total)")
    md_lines.append(f"- AI recall (system-wide): {_fmt_pct(ai_recall.recall)} ({ai_recall.true_positives}TP / {ai_recall.denominator} residuals)")
    if ai_recall.recall_attempted is not None:
        md_lines.append(f"- AI recall (attempted only): {_fmt_pct(ai_recall.recall_attempted)} ({ai_recall.true_positives}TP / {ai_recall.denominator_attempted} residuals)")
    md_lines.append("")

    md_lines.append("## Layer 3 Routing")
    for bucket, count in sorted(bucket_counts.items()):
        md_lines.append(f"- {bucket}: {count}")
    md_lines.append("")

    md_lines.append("## Safety Analysis")
    md_lines.append(f"- False accepts: {false_accept_final.count} / {false_accept_final.total_auto_accepted} auto-accepted ({_fmt_pct(false_accept_final.rate)})")
    md_lines.append(f"- Human review: {review_queue.total}")
    md_lines.append(f"- Exceptions: {exception_comp.total}")
    md_lines.append("")

    if false_accept_scenarios:
        md_lines.append("### False Accept Details")
        for so in false_accept_scenarios:
            md_lines.append(f"- {so.scenario_id} ({so.category.value}): confidence={so.ai_confidence:.2f}, proposed={list(so.ai_proposal_ids)}")
        md_lines.append("")

    md_lines.append("## Edge-Case Breakdown")
    md_lines.append("| Category | Count | L1 Matched | L2 Attempted | L2 Correct | Auto-Accept | Review | Exception |")
    md_lines.append("|---|---|---|---|---|---|---|---|")
    for cat in sorted(cat_metrics.keys()):
        cm = cat_metrics[cat]
        md_lines.append(
            f"| {cat} | {cm['count']} | {cm['l1_matched']} | {cm['l2_attempted']} "
            f"| {cm['l2_correct']} | {cm['auto_accept']} | {cm['needs_review']} | {cm['exception']} |"
        )
    md_lines.append("")

    md_lines.append("## Limitations")
    if stats["api_error"] > 0:
        md_lines.append(f"- **PARTIAL EVALUATION**: {stats['api_error']} of {stats['attempted']} scenarios had provider errors")
        if stats["quota_exhausted"] > 0:
            md_lines.append(f"- Groq daily token quota exhausted after {stats['quota_exhausted']} scenarios")
    else:
        md_lines.append("- Full evaluation completed successfully")
    md_lines.append("")

    md_lines.append("## Artifact")
    md_lines.append(f"- File: `{artifact_path.name}`")
    md_lines.append(f"- SHA-256: `{artifact_hash}`")
    md_lines.append(f"- Records: {auditor.write_count}")
    md_lines.append(f"- Dataset fingerprint: `{fingerprint}`")

    md_path = data_dir / "clean_evaluation_report.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print(f"  Report JSON: {report_path}")
    print(f"  Report MD:   {md_path}")
    print()

    # ── Print summary ─────────────────────────────────────────────
    print("=" * 70)
    print("EVALUATION SUMMARY")
    print("=" * 70)
    print(f"  Evaluation type: {report_data['evaluation_type']}")
    print(f"  Dataset: {len(all_records)} records, {len(dataset.scenarios)} scenarios")
    print(f"  Fingerprint: {fingerprint}")
    print()
    print(f"  Layer 1: {l1_matched}/{len(all_records)} matched ({l1_matched/len(all_records):.1%})")
    print(f"  Layer 2: {stats['successful']}/{stats['attempted']} successful ({stats['successful']/stats['attempted']:.1%})")
    if ai_p090.precision is not None:
        print(f"  AI precision @0.90: {ai_p090.precision:.1%}")
    if ai_recall.recall is not None:
        print(f"  AI recall (system): {ai_recall.recall:.1%}")
    print(f"  False accepts: {false_accept_final.count}")
    print(f"  Human review: {review_queue.total}")
    print(f"  Exceptions: {exception_comp.total}")
    print()
    print(f"  Artifact: {artifact_path.name}")
    print(f"  Artifact SHA-256: {artifact_hash}")

    return 0


def _fmt_pct(val: Optional[float]) -> str:
    if val is None:
        return "N/A"
    return f"{val:.1%}"


if __name__ == "__main__":
    sys.exit(main())
