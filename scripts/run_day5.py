"""
Day 5 driver: generate dataset, run full pipeline evaluation, and persist
both JSON and Markdown reports.
"""
from __future__ import annotations

import json
from pathlib import Path

from reconciliation.evaluation.dataset_generator import generate_dataset
from reconciliation.evaluation.full_pipeline_evaluation import (
    FullPipelineReport,
    run_full_pipeline,
    serialize_report,
    write_human_readable_report,
)
from reconciliation.matcher_config import MatcherConfig


def main() -> None:
    output_dir = Path(__file__).parent.parent / "data"
    output_dir.mkdir(parents=True, exist_ok=True)

    dataset = generate_dataset(seed=42)
    config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

    report = run_full_pipeline(
        dataset=dataset,
        config=config,
        output_dir=output_dir,
        baseline_config=config,
        layer2_artifact_path=output_dir / "layer2_full_audit.jsonl",
    )

    json_path = output_dir / "day5_full_pipeline_report.json"
    md_path = output_dir / "day5_full_pipeline_report.md"

    json_path.write_text(json.dumps(serialize_report(report), indent=2), encoding="utf-8")
    write_human_readable_report(report, md_path)

    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")
    print()
    print(f"Total scenarios: {report.total_scenarios}")
    print(f"Total records: {report.total_records}")
    print(f"Layer 2 mode: {report.layer2_mode}")
    print()
    print(f"Deterministic match rate: {report.deterministic_metrics.match_rate:.2%}")
    print(f"Deterministic precision: {report.deterministic_metrics.precision:.2%}")
    print()
    print("AI precision at thresholds:")
    print(f"  >= 0.90: {_fmt_pct(report.ai_precision_090.precision)} (n={report.ai_precision_090.total})")
    print(f"  >= 0.75: {_fmt_pct(report.ai_precision_075.precision)} (n={report.ai_precision_075.total})")
    print(f"  >= 0.60: {_fmt_pct(report.ai_precision_060.precision)} (n={report.ai_precision_060.total})")
    print()
    print(f"AI recall: {_fmt_pct(report.ai_recall.recall)} (tp={report.ai_recall.true_positives}, denom={report.ai_recall.denominator})")
    print()
    print(f"False-accept rate: {_fmt_pct(report.false_accept.rate)} (count={report.false_accept.count}, total_auto_accepted={report.false_accept.total_auto_accepted})")
    print()
    print(f"Review queue: {report.review_queue.total} records")
    print(f"Exception composition: {report.exception_composition.total} records")
    print()
    print(f"Throughput: total={report.throughput.total_batch_time_ms:.2f}ms, L1={report.throughput.layer1_time_ms:.2f}ms, L2={report.throughput.layer2_time_ms:.2f}ms")
    print()
    print("Baseline comparison:")
    print(f"  Baseline match rate: {_fmt_pct(report.baseline_comparison.baseline_match_rate)}")
    print(f"  Layered match rate: {_fmt_pct(report.baseline_comparison.deterministic_match_rate)}")
    print(f"  Delta: {_fmt_delta(report.baseline_comparison.match_rate_delta)}")
    print(f"  Baseline precision: {_fmt_pct(report.baseline_comparison.baseline_precision)}")
    print(f"  Layered precision: {_fmt_pct(report.baseline_comparison.deterministic_precision)}")
    print(f"  Delta: {_fmt_delta(report.baseline_comparison.precision_delta)}")


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.2f}%"


def _fmt_delta(value: float | None) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value * 100:.2f}pp"


if __name__ == "__main__":
    main()
