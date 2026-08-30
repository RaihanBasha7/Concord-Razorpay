"""
Day 4.6 script: scripted smoke test for the Layer 2 routing/scoring pipeline.

Uses a hand-scripted fake provider so the full pipeline can be exercised without
a real API key. The output is NOT a measurement of the real system's
false-accept rate — the fake orchestrator deliberately injects a ~10% false-
accept quota to prove the scoring layer detects them correctly. Re-run this
whenever the evaluation harness or routing logic changes to verify the pipeline
still works end-to-end.
"""
from __future__ import annotations

import json
from pathlib import Path

from reconciliation.evaluation.dataset_generator import (
    EdgeCaseCategory,
    ExpectedLayer1Outcome,
    _compute_record_id,
    generate_dataset,
)
from reconciliation.domain.models import SourceType
from reconciliation.evaluation.layer2_harness import (
    Layer2EvaluationReport,
    run_layer2_evaluation,
)
from reconciliation.layer2 import Layer2Case
from reconciliation.loader import load_normalized_records
from reconciliation.matcher import reconcile
from reconciliation.matcher_config import MatcherConfig
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_orchestration import ProposalOutcome
from reconciliation.proposal_validation import ProposalOutcomeType
from reconciliation.retrieval import RetrievalResult


class _FakeLayer2Orchestrator:
    def __init__(self, outcomes: dict[str, ProposalOutcome]) -> None:
        self._outcomes = outcomes

    def resolve(self, case: Layer2Case, retrieval_result: RetrievalResult) -> ProposalOutcome:
        return self._outcomes.get(
            case.scenario_id,
            ProposalOutcome(
                outcome=ProposalOutcomeType.NO_PROPOSAL,
                proposal=None,
                presented_record_ids=tuple(r.record_id for r in case.member_records),
                reason="default",
            ),
        )


def main() -> None:
    output_dir = Path(__file__).parent.parent / "data"
    output_dir.mkdir(parents=True, exist_ok=True)

    dataset = generate_dataset(seed=42)
    records = load_normalized_records(output_dir)
    reconcile(records, MatcherConfig(amount_tolerance_paise=100, date_window_days=2))

    from reconciliation.loader import load_residuals
    residual_scenarios = load_residuals(output_dir)
    residual_ids = {rs.scenario_id for rs in residual_scenarios}

    outcomes: dict[str, ProposalOutcome] = {}
    false_accept_count = 0
    false_accept_target = max(1, int(len(residual_ids) * 0.1))
    needs_review_count = 0
    needs_review_target = 3

    for scen in dataset.scenarios:
        member_ids = tuple(_compute_record_id(spec) for spec in scen.record_specs)
        is_residual = scen.scenario_id in residual_ids

        if not is_residual:
            continue

        is_orphan = scen.category == EdgeCaseCategory.TRUE_ORPHAN
        is_no_match = scen.expected_outcome == ExpectedLayer1Outcome.NO_MATCH

        if scen.category == EdgeCaseCategory.DUPLICATE:
            duplicate_ids = tuple(
                _compute_record_id(spec)
                for spec in scen.record_specs
                if spec.source_type == SourceType.SETTLEMENT
            )
            proposal_ids = list(duplicate_ids)
        else:
            proposal_ids = list(member_ids)

        if is_orphan or is_no_match:
            if false_accept_count < false_accept_target:
                false_accept_count += 1
                outcomes[scen.scenario_id] = ProposalOutcome(
                    outcome=ProposalOutcomeType.PROPOSAL_VALID,
                    proposal=MatchProposal(
                        proposed_match_ids=proposal_ids,
                        confidence=0.95,
                        rationale="overconfident",
                    ),
                    presented_record_ids=member_ids,
                    reason="Valid proposal.",
                )
            elif needs_review_count < needs_review_target and not is_orphan:
                needs_review_count += 1
                outcomes[scen.scenario_id] = ProposalOutcome(
                    outcome=ProposalOutcomeType.PROPOSAL_VALID,
                    proposal=MatchProposal(
                        proposed_match_ids=proposal_ids,
                        confidence=0.75,
                        rationale="low confidence",
                    ),
                    presented_record_ids=member_ids,
                    reason="Valid proposal.",
                )
            else:
                outcomes[scen.scenario_id] = ProposalOutcome(
                    outcome=ProposalOutcomeType.NO_PROPOSAL,
                    proposal=None,
                    presented_record_ids=member_ids,
                    reason="No proposal.",
                )
        else:
            needs_review_ids = {"EXACT-001", "TDLY-001", "RND-001"}
            confidence = 0.75 if scen.scenario_id in needs_review_ids else 0.95
            outcomes[scen.scenario_id] = ProposalOutcome(
                outcome=ProposalOutcomeType.PROPOSAL_VALID,
                proposal=MatchProposal(
                    proposed_match_ids=proposal_ids,
                    confidence=confidence,
                    rationale="correct",
                ),
                presented_record_ids=member_ids,
                reason="Valid proposal.",
            )

    orchestrator = _FakeLayer2Orchestrator(outcomes)
    report = run_layer2_evaluation(
        dataset,
        orchestrator,
        output_dir,
        config=MatcherConfig(amount_tolerance_paise=100, date_window_days=2),
    )

    _print_report(report)
    _write_report(report, output_dir / "layer2_evaluation_report.json")


def _print_report(report: Layer2EvaluationReport) -> None:
    print("Day 4.6 Layer 2 routing/scoring smoke test")
    print(f"Residual scenarios evaluated: {report.total_scenarios}")
    print(f"Overall coverage: {report.overall_coverage:.2%}")
    prec = f"{report.overall_precision:.2%}" if report.overall_precision is not None else "N/A"
    print(f"Overall precision: {prec}")
    print(f"Aggregate false-accept rate: {report.false_accept_rate:.2%}")
    print()
    print("Per-category results:")
    for m in report.category_metrics:
        prec = f"{m.precision:.2%}" if m.precision is not None else "N/A"
        print(
            f"  {m.category.value:20s} | total={m.total:3d} | correct={m.correct:3d} | "
            f"coverage={m.coverage:6.2%} | precision={prec:6s} | "
            f"auto_accept={m.auto_accept:2d} needs_review={m.needs_review:2d} exception={m.exception_count:2d} | "
            f"false_accepts={m.false_accepts:2d}"
        )
    print()
    print(f"False accepts: {len(report.false_accepts)}")
    for fa in report.false_accepts:
        print(
            f"  scenario={fa.scenario_id} category={fa.category.value} "
            f"reason={fa.incorrect_reason}"
        )


def _write_report(report: Layer2EvaluationReport, path: Path) -> None:
    payload = {
        "run_type": "scripted_smoke_test",
        "total_scenarios": report.total_scenarios,
        "overall_coverage": report.overall_coverage,
        "overall_precision": report.overall_precision,
        "false_accept_rate": report.false_accept_rate,
        "category_metrics": [
            {
                "category": m.category.value,
                "total": m.total,
                "correct": m.correct,
                "coverage": m.coverage,
                "precision": m.precision,
                "auto_accept": m.auto_accept,
                "needs_review": m.needs_review,
                "exception_count": m.exception_count,
                "false_accepts": m.false_accepts,
            }
            for m in report.category_metrics
        ],
        "false_accepts": [
            {
                "scenario_id": fa.scenario_id,
                "category": fa.category.value,
                "reason": fa.incorrect_reason,
            }
            for fa in report.false_accepts
        ],
    }
    path.write_text(json.dumps(payload, indent=2))
    print(f"\nReport written to {path}")


if __name__ == "__main__":
    main()
