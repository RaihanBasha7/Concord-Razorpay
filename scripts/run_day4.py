"""
Day 4.5 manual runner script.

Run a SMALL sample of residual scenarios through the Layer 2 pipeline. This
script is for local manual verification only and is NOT invoked by tests.

Usage:
    python scripts/run_day4.py --limit 5
    python scripts/run_day4.py --limit 5 --audit data/day4_audit.jsonl

Requires GROQ_API_KEY in the environment for live inference. It never sends the
category-encoded scenario_id or evaluation metadata to the LLM.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from reconciliation.audit import Auditor
from reconciliation.groq_provider import GroqStructuredProvider
from reconciliation.proposal_orchestration import ProposalOrchestrator
from reconciliation.proposal_service import ProposalService
from reconciliation.runner import DEFAULT_SAMPLE_LIMIT, Day4Runner


def main() -> None:
    parser = argparse.ArgumentParser(description="Concord Day 4.5 Layer 2 runner")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_SAMPLE_LIMIT,
        help="Max number of residual scenarios to process (small to control API use).",
    )
    parser.add_argument(
        "--audit",
        type=str,
        default="day4_audit.jsonl",
        help="Path to append JSONL audit records.",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default="data",
        help="Directory containing the source CSVs and residuals.csv.",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    auditor = Auditor(data_dir / args.audit)
    service = ProposalService(GroqStructuredProvider())
    orchestrator = ProposalOrchestrator(service)

    runner = Day4Runner(
        data_dir=data_dir,
        orchestrator=orchestrator,
        limit=args.limit,
        auditor=auditor,
    )
    summary = runner.run()
    runner.print_summary(summary)

    if summary.diagnostics:
        print("\nLocal diagnostics (NOT persisted to the audit trail):")
        for diagnostic in summary.diagnostics:
            print(f"  - {diagnostic}")


if __name__ == "__main__":
    main()
