"""Replay clean Layer 2 proposals through the updated Layer 3 guardrail.

Reports before/after routing buckets for each scenario.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from reconciliation.domain.models import NormalizedRecord, SourceType
from reconciliation.evaluation.dataset_generator import (
    generate_dataset,
    _compute_record_id,
)
from reconciliation.layer3 import (
    RoutingBucket,
    RoutingDecision,
    route,
)
from reconciliation.normalizer import normalize_record
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_validation import ProposalOutcome, ProposalOutcomeType


def load_clean_records(data_dir: Path) -> Dict[str, NormalizedRecord]:
    """Load and normalize all records from the frozen CSV files."""
    records: Dict[str, NormalizedRecord] = {}
    for csv_name, source_type in [
        ("settlements.csv", SourceType.SETTLEMENT),
        ("bank.csv", SourceType.BANK),
        ("ledger.csv", SourceType.LEDGER),
    ]:
        import csv
        with (data_dir / csv_name).open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                clean_row = {k: v for k, v in row.items() if k not in ("scenario_ref", "synthetic_ref")}
                record = normalize_record(clean_row, source_type)
                records[record.record_id] = record
    return records


def load_clean_eval_results(path: Path) -> Dict[str, Dict[str, Any]]:
    """Load clean eval results keyed by scenario_id."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {r["scenario_id"]: r for r in data["results"]}


def load_ground_truth_map(data_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Load ground truth, keyed by scenario_id."""
    with (data_dir / "ground_truth.json").open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {s["scenario_id"]: s for s in data["scenarios"]}


def load_residuals(data_dir: Path) -> Dict[str, List[str]]:
    """Load residuals, keyed by scenario_id."""
    import csv
    residuals = {}
    with (data_dir / "residuals.csv").open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sid = row["scenario_id"]
            member_ids = json.loads(row["member_record_ids"])
            residuals[sid] = member_ids
    return residuals


def simulate_routing(
    records: Dict[str, NormalizedRecord],
    clean_results: Dict[str, Dict[str, Any]],
    residuals: Dict[str, List[str]],
    gt_map: Dict[str, Dict[str, Any]],
) -> None:
    """Simulate Layer 3 routing for each clean eval scenario."""

    # Classify scenarios
    interesting_scenarios = [
        "LATE-008", "DUP-002", "DUP-004", "DUP-005",
        "LATE-001", "LATE-002", "LATE-003", "LATE-004", "LATE-005",
        "LATE-006", "LATE-007", "LATE-009", "LATE-010",
        "DUP-001", "DUP-003",
        "SPLT-001", "SPLT-002", "SPLT-003", "SPLT-004", "SPLT-005",
    ]

    # Track all results
    all_before: Dict[str, str] = {}
    all_after: Dict[str, str] = {}

    for scenario_id, result in clean_results.items():
        member_ids = residuals.get(scenario_id, [])
        gt = gt_map.get(scenario_id, {})
        category = gt.get("category", "UNKNOWN")

        if result["outcome"] == "PROPOSAL_VALID":
            proposed_ids = result.get("proposed_match_ids", [])
            confidence = result.get("confidence", 0.0)

            # Build proposal
            proposal = MatchProposal(
                proposed_match_ids=proposed_ids,
                confidence=confidence,
                rationale="replay",
            )

            # Build outcome
            outcome = ProposalOutcome(
                outcome=ProposalOutcomeType.PROPOSAL_VALID,
                proposal=proposal,
                presented_record_ids=tuple(member_ids),
                reason="replay",
            )

            # Route through updated Layer 3
            routing = route(
                layer1_decisions=[],
                layer2_outcomes=[outcome],
                all_records=list(records.values()),
            )

            for rd in routing:
                if rd.record_id in proposed_ids:
                    bucket = rd.bucket.value
                    all_after[scenario_id] = bucket

                    # Simulate old behavior
                    if confidence >= 0.90:
                        old_bucket = "AI_AUTO_ACCEPTED"
                    elif confidence >= 0.60:
                        old_bucket = "HUMAN_REVIEW"
                    else:
                        old_bucket = "EXCEPTION"
                    all_before[scenario_id] = old_bucket

        elif result["outcome"] == "VALIDATION_FAILED":
            all_before[scenario_id] = "EXCEPTION"
            all_after[scenario_id] = "EXCEPTION"

        elif result["outcome"] in ("API_ERROR", "NO_PROPOSAL"):
            all_before[scenario_id] = "EXCEPTION"
            all_after[scenario_id] = "EXCEPTION"

    # Print interesting scenarios
    print("\n" + "=" * 80)
    print("REPLAY: SCENARIO-LEVEL ROUTING CHANGES")
    print("=" * 80)
    print(f"{'Scenario':<15} {'Category':<25} {'Confidence':<12} {'Before':<20} {'After':<20} {'Changed?'}")
    print("-" * 100)

    changes = []
    for scenario_id in interesting_scenarios:
        if scenario_id in clean_results:
            r = clean_results[scenario_id]
            conf = r.get("confidence", "N/A")
            gt = gt_map.get(scenario_id, {})
            cat = gt.get("category", "?")
            before = all_before.get(scenario_id, "N/A")
            after = all_after.get(scenario_id, "N/A")
            changed = "YES" if before != after else ""
            print(f"{scenario_id:<15} {cat:<25} {str(conf):<12} {before:<20} {after:<20} {changed}")
            if before != after:
                changes.append((scenario_id, cat, conf, before, after))

    # Print summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    before_counts = {}
    after_counts = {}
    for sid in all_before:
        b = all_before[sid]
        a = all_after[sid]
        before_counts[b] = before_counts.get(b, 0) + 1
        after_counts[a] = after_counts.get(a, 0) + 1

    print(f"\nBefore (old guardrail):")
    for bucket in sorted(before_counts.keys()):
        print(f"  {bucket:<25} {before_counts[bucket]}")

    print(f"\nAfter (new guardrail):")
    for bucket in sorted(after_counts.keys()):
        print(f"  {bucket:<25} {after_counts[bucket]}")

    if changes:
        print(f"\n{len(changes)} scenario(s) changed routing:")
        for sid, cat, conf, before, after in changes:                print(f"  {sid} ({cat}, conf={conf}): {before} -> {after}")
    else:
        print("\nNo scenarios changed routing.")


def main():
    data_dir = Path("data")

    print("Loading records...")
    records = load_clean_records(data_dir)
    print(f"  Loaded {len(records)} records")

    print("Loading clean eval results (full)...")
    clean_results = load_clean_eval_results(data_dir / "clean_full_eval_results.json")
    print(f"  Loaded {len(clean_results)} scenarios")

    print("Loading ground truth...")
    gt_map = load_ground_truth_map(data_dir)
    print(f"  Loaded {len(gt_map)} scenarios")

    print("Loading residuals...")
    residuals = load_residuals(data_dir)
    print(f"  Loaded {len(residuals)} residual scenarios")

    simulate_routing(records, clean_results, residuals, gt_map)


if __name__ == "__main__":
    main()
