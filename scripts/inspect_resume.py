"""
Local resume inspection utility for Layer 2 evaluation.

Loads the active resumable partial artifact and the residual scenarios,
then computes the resume plan (skip / retry / run) without making any
network calls.

Usage:
    python scripts/inspect_resume.py
    python scripts/inspect_resume.py --artifact data/layer2_clean_audit.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from reconciliation.evaluation.resume import (
    CompletionStatus,
    build_resume_summary,
    filter_residuals_for_resume,
    load_resume_state,
    validate_resume_artifact,
)
from reconciliation.loader import load_residuals


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect Layer 2 resume state — zero network calls."
    )
    parser.add_argument(
        "--artifact",
        default="data/layer2_clean_audit.jsonl",
        help="Path to the active resumable partial artifact (default: data/layer2_clean_audit.jsonl)",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Path to the data directory containing residuals.csv and dataset_manifest.json",
    )
    args = parser.parse_args()

    artifact_path = Path(args.artifact)
    data_dir = Path(args.data_dir)

    if not artifact_path.exists():
        print(f"ERROR: Artifact not found: {artifact_path}")
        return 1

    # Load residuals
    try:
        residuals = load_residuals(data_dir)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 1

    # Load manifest for fingerprint
    from reconciliation.evaluation.dataset_fingerprint import read_manifest
    manifest = read_manifest(data_dir)
    expected_fingerprint = manifest.fingerprint() if manifest else None

    # Validate artifact
    print("=" * 60)
    print("Artifact Validation")
    print("=" * 60)
    validation_errors = validate_resume_artifact(
        artifact_path,
        residuals,
        expected_fingerprint or "",
    )
    if validation_errors:
        print("ERROR: Artifact validation failed:")
        for err in validation_errors:
            print(f"  - {err}")
        return 1
    print("PASSED: Artifact is safe to use as resume state.\n")

    # Load resume state
    resume_state = load_resume_state(artifact_path)
    summary = build_resume_summary(resume_state)

    # Compute plan
    skip, retry, run = filter_residuals_for_resume(residuals, resume_state)

    # Outcome breakdown
    outcomes = Counter()
    with artifact_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            outcomes[record.get("outcome", "UNKNOWN")] += 1

    print("=" * 60)
    print("Resume State")
    print("=" * 60)
    print(f"Artifact: {artifact_path}")
    print(f"Total residual scenarios: {len(residuals)}")
    print(f"Tracked in artifact: {summary.total_scenarios}")
    print(f"Completed: {summary.completed}")
    print(f"Failed: {summary.failed}")
    print(f"Missing: {summary.missing}")
    print()
    print("Outcome breakdown:")
    for outcome, count in sorted(outcomes.items()):
        print(f"  {outcome:20s} {count:4d}")

    print()
    print("=" * 60)
    print("Resume Plan")
    print("=" * 60)
    print(f"SKIP  (completed): {len(skip)}")
    print(f"RETRY (failed):    {len(retry)}")
    print(f"RUN   (missing):   {len(run)}")
    print(f"TOTAL:             {len(skip) + len(retry) + len(run)}")

    if expected_fingerprint:
        print()
        print(f"Dataset fingerprint: {expected_fingerprint}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
