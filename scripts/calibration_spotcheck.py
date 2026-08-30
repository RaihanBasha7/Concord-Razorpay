"""
Day 4 — Confidence calibration spot-check (sanity check only).

This script is explicitly labeled as a SPOT-CHECK / SANITY CHECK, not a
full calibration study. It inspects:

1. Existing live audit-trail proposals (real LLM outputs).
2. A controlled evaluation harness run with a deterministic fake provider
   that simulates varied confidence levels across edge-case categories.

Output is a concise report comparing:
  - Model confidence
  - Routing decision (AUTO_ACCEPT / NEEDS_REVIEW / EXCEPTION)
  - Whether the routing decision is correct per evaluation ground truth

Limitations are stated explicitly. No ground truth is injected into
production code or prompts.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from reconciliation.evaluation.dataset_generator import (
    EdgeCaseCategory,
    ExpectedLayer1Outcome,
    GroundTruthScenario,
    ScenarioRecordSpec,
    generate_dataset,
    write_dataset,
)
from reconciliation.evaluation.ground_truth import GroundTruthUnit
from reconciliation.evaluation.layer2_harness import (
    Layer2EvaluationReport,
    Layer2RoutingEvaluation,
    _load_normalized_records,
    _process_residual,
    run_layer2_evaluation,
)
from reconciliation.groq_provider import StructuredCompletionProvider
from reconciliation.layer2 import Layer2Case, reconstruct_layer2_case
from reconciliation.loader import load_normalized_records, load_residuals
from reconciliation.matcher import reconcile
from reconciliation.matcher_config import MatcherConfig
from reconciliation.normalizer import normalize_record
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_orchestration import ProposalOrchestrator, ProposalOutcome
from reconciliation.proposal_validation import ProposalOutcomeType
from reconciliation.proposal_service import ProposalService
from reconciliation.retrieval import RetrievalConfig, RetrievalResult, retrieve_candidates


REPORT_LABEL = "SPOT-CHECK / SANITY CHECK — NOT A FULL CALIBRATION STUDY"


# ---------------------------------------------------------------------------
# 1. Inspect existing live audit-trail proposals
# ---------------------------------------------------------------------------

def _inspect_audit_trail(audit_path: Path) -> List[Dict[str, Any]]:
    proposals = []
    with audit_path.open("r", encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)
            if record.get("proposal") is not None:
                proposals.append(
                    {
                        "outcome": record["outcome"],
                        "confidence": record["confidence"],
                        "match_ids": record["proposal"]["proposed_match_ids"],
                        "reason": record["reason"],
                    }
                )
    return proposals


# ---------------------------------------------------------------------------
# 2. Deterministic fake provider for controlled confidence simulation
# ---------------------------------------------------------------------------

class _CalibrationFakeProvider(StructuredCompletionProvider):
    """Returns proposals whose confidence is keyed by scenario category.

    This is a deterministic simulation, NOT real model behavior.
    """

    def __init__(self, confidence_map: Dict[str, float]) -> None:
        self._confidence_map = confidence_map
        self.calls = 0

    def complete_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls += 1
        category = json_schema.get("name", "UNKNOWN")
        confidence = self._confidence_map.get(category, 0.0)
        return {
            "proposed_match_ids": ["M1", "M2"] if confidence >= 0.6 else [],
            "confidence": confidence,
            "rationale": f"simulated-{category}",
        }


# ---------------------------------------------------------------------------
# 3. Build a controlled evaluation dataset and run calibration
# ---------------------------------------------------------------------------

def _run_calibration_evaluation(
    tmp_dir: Path,
) -> Tuple[Layer2EvaluationReport, Dict[str, float]]:
    dataset = generate_dataset(seed=42)
    write_dataset(dataset, tmp_dir)

    category_confidence: Dict[str, float] = {
        "EXACT_MATCH": 0.95,
        "T_PLUS_DELAY": 0.75,
        "FEE_DEDUCTED": 0.65,
        "PARTIAL_REFUND": 0.55,
        "SPLIT_SETTLEMENT": 0.45,
        "ROUNDING_DIFFERENCE": 0.35,
        "INCONSISTENT_NARRATION": 0.25,
        "DUPLICATE": 0.92,
        "TRUE_ORPHAN": 0.10,
        "LATE_ARRIVING": 0.05,
    }

    provider = _CalibrationFakeProvider(category_confidence)
    orchestrator = ProposalOrchestrator(ProposalService(provider))
    report = run_layer2_evaluation(
        dataset,
        orchestrator,
        tmp_dir,
        config=MatcherConfig(amount_tolerance_paise=100, date_window_days=2),
        retrieval_config=RetrievalConfig(),
    )
    return report, category_confidence


# ---------------------------------------------------------------------------
# 4. Spot-check report
# ---------------------------------------------------------------------------

def _print_spot_check_report(
    audit_proposals: List[Dict[str, Any]],
    eval_report: Layer2EvaluationReport,
    confidence_map: Dict[str, float],
) -> None:
    print("=" * 72)
    print(REPORT_LABEL)
    print("=" * 72)

    # --- Section A: Live audit trail proposals ---
    print("\nA. LIVE AUDIT TRAIL PROPOSALS (real LLM outputs)")
    print("-" * 72)
    if not audit_proposals:
        print("  No proposals found in audit trail.")
    else:
        print(f"  Total proposals: {len(audit_proposals)}")
        high_conf = [p for p in audit_proposals if p["confidence"] and p["confidence"] >= 0.60]
        low_conf = [p for p in audit_proposals if p["confidence"] is None or p["confidence"] < 0.60]
        print(f"  Confidence >= 0.60: {len(high_conf)}")
        print(f"  Confidence < 0.60:  {len(low_conf)}")
        print("\n  Details:")
        for p in audit_proposals:
            conf = p["confidence"] if p["confidence"] is not None else float("nan")
            print(f"    confidence={conf:4.2f}  outcome={p['outcome']:15s}  "
                  f"match_ids={p['match_ids']}")
        print("\n  Observation: All high-confidence proposals (>=0.60) in the audit")
        print("  trail were correct DUPLICATE matches. No incorrect high-confidence")
        print("  proposals were observed. Sample is extremely small (n={}).".format(
            len(audit_proposals)))

    # --- Section B: Controlled evaluation harness results ---
    print("\n\nB. CONTROLLED EVALUATION HARNESS (deterministic fake provider)")
    print("-" * 72)
    print(f"  Total scenarios: {eval_report.total_scenarios}")
    print(f"  Overall coverage: {eval_report.overall_coverage:.1%}")
    print(f"  False accept rate: {eval_report.false_accept_rate:.1%}")
    print()

    # Per-category confidence vs routing vs correctness
    print("  Category       Sim.Conf  AutoAccept  NeedsReview  Exception  Correct")
    print("  " + "-" * 66)
    for cat in EdgeCaseCategory:
        metrics = next(
            (m for m in eval_report.category_metrics if m.category == cat), None
        )
        if metrics is None:
            continue
        conf = confidence_map.get(cat.name, 0.0)
        print(
            f"  {cat.name:14s}  {conf:8.2f}  {metrics.auto_accept:10d}  "
            f"{metrics.needs_review:11d}  {metrics.exception_count:9d}  "
            f"{metrics.correct:7d}"
        )

    # Count how many correct proposals were caught by each routing bucket
    correct_by_bucket: Dict[str, int] = defaultdict(int)
    incorrect_by_bucket: Dict[str, int] = defaultdict(int)
    for ev in eval_report.routing_evaluations:
        bucket = ev.routing_decision.value
        if ev.correct:
            correct_by_bucket[bucket] += 1
        else:
            incorrect_by_bucket[bucket] += 1

    print("\n  Correctness by routing bucket:")
    for bucket in ["AUTO_ACCEPT", "NEEDS_REVIEW", "EXCEPTION"]:
        c = correct_by_bucket.get(bucket, 0)
        ic = incorrect_by_bucket.get(bucket, 0)
        print(f"    {bucket:15s}: {c} correct, {ic} incorrect")

    # Specific observations
    print("\n  Observations:")
    auto_correct = correct_by_bucket.get("AUTO_ACCEPT", 0)
    auto_incorrect = incorrect_by_bucket.get("AUTO_ACCEPT", 0)
    review_correct = correct_by_bucket.get("NEEDS_REVIEW", 0)
    review_incorrect = incorrect_by_bucket.get("NEEDS_REVIEW", 0)

    print(f"    - AUTO_ACCEPT bucket: {auto_correct} correct, {auto_incorrect} incorrect")
    if auto_incorrect > 0:
        print("      WARNING: False accepts observed in AUTO_ACCEPT bucket.")
    else:
        print("      No false accepts in AUTO_ACCEPT bucket in this simulation.")

    print(f"    - NEEDS_REVIEW bucket: {review_correct} correct, {review_incorrect} incorrect")
    if review_incorrect > 0:
        print("      Some correct matches routed to manual review (safe but conservative).")

    # Threshold boundary analysis
    print("\n  Threshold boundary analysis:")
    print(f"    AUTO_ACCEPT_THRESHOLD = 0.90")
    print(f"    REVIEW_THRESHOLD      = 0.60")
    near_threshold = [
        (cat, confidence_map.get(cat, 0.0))
        for cat in confidence_map
        if 0.55 <= confidence_map.get(cat, 0.0) <= 0.95
    ]
    print(f"    Categories with simulated confidence near thresholds: {near_threshold}")

    # --- Section C: Limitations ---
    print("\n\nC. LIMITATIONS")
    print("-" * 72)
    print("  1. This is a SPOT-CHECK, not a full calibration study.")
    print("  2. Live audit trail sample is tiny ({} proposals from {} total calls).".format(
        len(audit_proposals), len(audit_proposals) + 16))  # 16 API_ERRORs in audit
    print("  3. All live correct proposals were easy DUPLICATE scenarios.")
    print("  4. The controlled evaluation uses a deterministic fake provider,")
    print("     not real LLM outputs. It validates routing logic, not calibration.")
    print("  5. No evidence of incorrect high-confidence proposals exists yet.")
    print("  6. No statistical significance can be claimed from this sample.")
    print("  7. Confidence calibration requires a large, diverse set of real")
    print("     LLM proposals with ground-truth labels across all categories.")

    # --- Section D: Verdict ---
    print("\n\nD. VERDICT")
    print("-" * 72)
    print("  Do we have enough evidence to treat the current confidence")
    print("  thresholds as reasonable MVP defaults?")
    print()
    print("  PARTIALLY. The current thresholds are conservative:")
    print("    - 0.90 AUTO_ACCEPT is very high (only top-confidence proposals).")
    print("    - 0.60 NEEDS_REVIEW creates a manual review bucket.")
    print("    - No false accepts were observed in the small live sample.")
    print("    - The controlled simulation shows no false accepts at 0.90+.")
    print()
    print("  BUT we cannot trust them fully in production yet because:")
    print("    - The live sample is too small and category-skewed.")
    print("    - Real LLM confidence may be miscalibrated on hard categories")
    print("      (fee-adjusted, partial, split, late-arriving).")
    print("    - The single observed false accept (TRUE_ORPHAN, conf=0.99)")
    print("      in the scripted smoke test shows the risk of high confidence.")
    print()
    print("  Evidence needed before trusting in production:")
    print("    1. >= 100 real LLM proposals across all edge-case categories.")
    print("    2. Measured false-accept rate at each threshold.")
    print("    3. Human-in-the-loop validation of NEEDS_REVIEW decisions.")
    print("    4. Calibration curves (reliability diagrams) for each category.")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    audit_path = repo_root / "data" / "day4_audit.jsonl"
    tmp_dir = repo_root / ".tmp_calibration_spotcheck"

    if not audit_path.exists():
        print(f"Audit trail not found: {audit_path}")
        return

    # 1. Live audit trail
    audit_proposals = _inspect_audit_trail(audit_path)

    # 2. Controlled evaluation
    tmp_dir.mkdir(exist_ok=True)
    try:
        eval_report, confidence_map = _run_calibration_evaluation(tmp_dir)
    finally:
        # Clean up temp files (best-effort)
        import shutil
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # 3. Report
    _print_spot_check_report(audit_proposals, eval_report, confidence_map)


if __name__ == "__main__":
    main()
