"""
Day 3 script: generate synthetic dataset, run Layer 1 evaluation, and print report.
"""
from __future__ import annotations

import json
from pathlib import Path

from reconciliation.evaluation.dataset_generator import generate_dataset, write_dataset
from reconciliation.evaluation.evaluation_harness import HarnessResult, run_evaluation
from reconciliation.evaluation.metrics import EvaluationReport
from reconciliation.matcher_config import MatcherConfig


def main() -> None:
    output_dir = Path(__file__).parent.parent / "data"
    output_dir.mkdir(parents=True, exist_ok=True)

    dataset = generate_dataset(seed=42)
    write_dataset(dataset, output_dir)

    config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
    harness = run_evaluation(dataset, config, output_dir)
    report = harness.report

    print(f"Total scenarios: {report.total_scenarios}")
    print(f"Overall coverage: {report.overall_coverage:.2%}")
    print(f"Overall precision: {report.overall_precision:.2%}" if report.overall_precision is not None else "Overall precision: N/A (no Layer 1 resolutions)")
    print()
    print("Per-category results:")
    for m in report.category_metrics:
        prec = f"{m.precision:.2%}" if m.precision is not None else "N/A"
        print(
            f"  {m.category.value:20s} | total={m.total:3d} | correct={m.correctly_resolved:3d} | "
            f"coverage={m.coverage:6.2%} | precision={prec:6s} | "
            f"tp={m.true_positives:2d} fp={m.false_positives:2d} tn={m.true_negatives:2d} fn={m.false_negatives:2d}"
        )
    print()
    print(f"Incorrect matches: {len(report.incorrect_matches)}")
    for ev in report.incorrect_matches:
        print(
            f"  scenario={ev.scenario_id} category={ev.category.value} reason={ev.incorrect_reason}"
        )
    print()
    print(f"Layer 1 false negatives: {len(report.layer1_false_negative_ids)}")
    for sid in report.layer1_false_negative_ids:
        print(f"  {sid}")
    print()
    print(f"Pipeline residual scenarios: {len(report.pipeline_residual_scenario_ids)}")
    for sid in report.pipeline_residual_scenario_ids:
        print(f"  {sid}")
    print()
    print(f"True exception scenarios: {len(report.true_exception_scenario_ids)}")
    for sid in report.true_exception_scenario_ids:
        print(f"  {sid}")
    print()
    print(f"Residual file: {harness.residual_path}")

    # Persist report
    report_path = output_dir / "evaluation_report.json"
    report_payload = {
        "total_scenarios": report.total_scenarios,
        "overall_coverage": report.overall_coverage,
        "overall_precision": report.overall_precision,
        "category_metrics": [
            {
                "category": m.category.value,
                "total": m.total,
                "correctly_resolved": m.correctly_resolved,
                "coverage": m.coverage,
                "precision": m.precision,
                "recall": m.recall,
                "true_positives": m.true_positives,
                "false_positives": m.false_positives,
                "true_negatives": m.true_negatives,
                "false_negatives": m.false_negatives,
            }
            for m in report.category_metrics
        ],
        "incorrect_matches": [
            {
                "scenario_id": ev.scenario_id,
                "category": ev.category.value,
                "reason": ev.incorrect_reason,
            }
            for ev in report.incorrect_matches
        ],
        "layer1_false_negative_ids": report.layer1_false_negative_ids,
        "pipeline_residual_scenario_ids": report.pipeline_residual_scenario_ids,
        "true_exception_scenario_ids": report.true_exception_scenario_ids,
    }
    report_path.write_text(json.dumps(report_payload, indent=2))
    print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    main()
