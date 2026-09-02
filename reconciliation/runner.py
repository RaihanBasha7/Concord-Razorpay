"""
Day 4.5 — End-to-end Layer 2 runner.

Connects the existing Day 4 components:

  Residual scenario
    -> reconstruction (Layer2Case)
    -> candidate retrieval (RetrievalResult)
    -> proposal (ProposalService via ProposalOrchestrator)
    -> validation (deterministic, Groq-free)
    -> audit record (local JSONL)

The runner processes only a configurable, small number of residual scenarios by
default to control API usage. It does NOT perform routing/threshold decisions,
evaluation against ground truth, or bulk analytics.

The LLM-facing context is the sanitized production record context only; the
category-encoded scenario_id is never sent to the model. A neutral internal
correlation ID is used for the audit trail.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from reconciliation.audit import Auditor, make_audit_record
from reconciliation.domain.models import NormalizedRecord
from reconciliation.evaluation.dataset_fingerprint import (
    compute_dataset_manifest,
    read_manifest,
)
from reconciliation.evaluation.resume import (
    CompletionStatus,
    filter_residuals_for_resume,
    load_resume_state,
)
from reconciliation.layer2 import Layer2Case, reconstruct_layer2_case
from reconciliation.loader import (
    ResidualScenario,
    load_normalized_records,
    load_residuals,
)
from reconciliation.proposal_orchestration import ProposalOrchestrator
from reconciliation.proposal_validation import ProposalOutcomeType
from reconciliation.retrieval import (
    RetrievalConfig,
    RetrievalResult,
    retrieve_candidates,
)

DEFAULT_SAMPLE_LIMIT = 5


@dataclass
class RunSummary:
    """Concise per-outcome tally across processed scenarios."""

    attempted: int
    by_outcome: Dict[str, int] = field(default_factory=dict)
    audit_path: Optional[str] = None
    diagnostics: List[str] = field(default_factory=list)
    dataset_fingerprint: Optional[str] = None


class Day4Runner:
    """
    Drives the Day 4.5 Layer 2 flow for a limited set of residual scenarios.

    The orchestrator is injected so tests can supply a fake provider boundary
    with zero network/API-key dependency.
    """

    def __init__(
        self,
        *,
        data_dir: Path | str,
        orchestrator: ProposalOrchestrator,
        limit: int = DEFAULT_SAMPLE_LIMIT,
        auditor: Optional[Auditor] = None,
        retrieval_config: Optional[RetrievalConfig] = None,
        start_offset: int = 0,
        resume_state_path: Optional[Path | str] = None,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1.")
        if start_offset < 0:
            raise ValueError("start_offset must be non-negative.")
        self._data_dir = Path(data_dir)
        self._orchestrator = orchestrator
        self._limit = limit
        self._auditor = auditor
        self._retrieval_config = retrieval_config or RetrievalConfig()
        self._start_offset = start_offset
        self._resume_state_path = (
            Path(resume_state_path) if resume_state_path else None
        )
        self._resume_state: Dict[str, Any] = {}
        if self._resume_state_path:
            self._resume_state = load_resume_state(self._resume_state_path)
        self._audit_failures = 0
        self._dataset_fingerprint, self._fingerprint_diagnostics = (
            self._resolve_dataset_fingerprint()
        )

    def _resolve_dataset_fingerprint(self) -> Tuple[Optional[str], List[str]]:
        """Compute the fingerprint of the dataset this runner is consuming.

        Stamps every audit record with this fingerprint so that downstream
        evaluation can prove the Layer 2 artifact was produced from the same
        dataset. If a frozen manifest is present, the on-disk dataset is
        verified against it and a warning is emitted (as a diagnostic) on drift
        rather than failing the run, since the runner is a manual tool.
        """
        diagnostics: List[str] = []
        try:
            manifest = compute_dataset_manifest(self._data_dir)
        except FileNotFoundError as exc:
            diagnostics.append(
                f"Could not compute dataset fingerprint: {exc}"
            )
            return None, diagnostics

        fingerprint = manifest.fingerprint()

        frozen = read_manifest(self._data_dir)
        if frozen is not None and frozen.fingerprint() != fingerprint:
            diagnostics.append(
                "Dataset drift detected: the dataset on disk no longer matches "
                "the frozen manifest. Layer 2 audit records produced now will "
                "carry a different fingerprint and will be rejected by "
                "provenance verification in the evaluation harness."
            )
        return fingerprint, diagnostics

    def run(self) -> RunSummary:
        normalized = load_normalized_records(self._data_dir)
        all_residuals = load_residuals(self._data_dir)

        if self._resume_state:
            skip, retry, run = filter_residuals_for_resume(
                all_residuals, self._resume_state
            )
            active_residuals = retry + run
            residuals = active_residuals[
                self._start_offset : self._start_offset + self._limit
            ]
        else:
            residuals = all_residuals[
                self._start_offset : self._start_offset + self._limit
            ]

        by_outcome: Dict[str, int] = {}
        diagnostics: List[str] = []
        diagnostics.extend(self._fingerprint_diagnostics)
        for residual in residuals:
            outcome = self._process_one(residual, normalized)
            by_outcome[outcome.outcome.value] = (
                by_outcome.get(outcome.outcome.value, 0) + 1
            )
            if outcome.diagnostic:
                diagnostics.append(outcome.diagnostic)

        return RunSummary(
            attempted=len(residuals),
            by_outcome=by_outcome,
            audit_path=(
                str(self._auditor.path)
                if self._auditor and self._auditor.path
                else None
            ),
            diagnostics=diagnostics,
            dataset_fingerprint=self._dataset_fingerprint,
        )

    def _process_one(
        self,
        residual: ResidualScenario,
        normalized: Tuple[NormalizedRecord, ...],
    ) -> "object":
        normalized_records = tuple(normalized)
        correlation_id = uuid.uuid4().hex

        case = reconstruct_layer2_case(
            scenario_id=residual.scenario_id,
            member_record_ids=residual.member_record_ids,
            normalized_records=normalized_records,
        )
        retrieval: RetrievalResult = retrieve_candidates(
            case, normalized_records, self._retrieval_config
        )

        outcome = self._orchestrator.resolve(case, retrieval)

        member_ids = [r.record_id for r in case.member_records]
        candidate_ids = [c.record.record_id for c in retrieval.candidates]
        seen: set[str] = set()
        presented_ids: List[str] = []
        for rid in member_ids + candidate_ids:
            if rid not in seen:
                seen.add(rid)
                presented_ids.append(rid)

        record = make_audit_record(
            correlation_id=correlation_id,
            presented_record_ids=presented_ids,
            outcome=outcome.outcome.value,
            proposal=outcome.proposal,
            reason=outcome.reason,
            dataset_fingerprint=self._dataset_fingerprint,
            diagnostic=outcome.diagnostic or None,
        )

        if self._auditor is not None:
            if not self._auditor.write(record):
                self._audit_failures += 1

        return outcome

    def print_summary(self, summary: RunSummary) -> None:
        print(f"Day 4.5 Layer 2 run — scenarios attempted: {summary.attempted}")
        if summary.audit_path:
            print(f"Audit trail: {summary.audit_path}")
        if summary.dataset_fingerprint:
            print(f"Dataset fingerprint: {summary.dataset_fingerprint}")
        for outcome, count in sorted(summary.by_outcome.items()):
            print(f"  {outcome:18s} {count}")
        if summary.diagnostics:
            print("\nDiagnostics:")
            for diagnostic in summary.diagnostics:
                print(f"  - {diagnostic}")
