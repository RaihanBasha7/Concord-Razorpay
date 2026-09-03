"""
Day 6 — Evaluation resume / checkpoint management.

Provides scenario-level checkpointing for the Layer 2 evaluation runner.

A scenario is "completed" only when it has a successful model outcome:
  - PROPOSAL_VALID
  - NO_PROPOSAL
  - VALIDATION_FAILED

API_ERROR and TIMEOUT are NOT completed and must be retried on resume.

The checkpoint state is derived from the existing JSONL audit artifact,
so no separate persistence layer is required.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class CompletionStatus(Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    MISSING = "missing"


@dataclass(frozen=True)
class ScenarioCheckpoint:
    scenario_id: str
    status: CompletionStatus
    run_id: str
    timestamp: str
    outcome: str
    error_classification: Optional[str] = None
    attempt_count: int = 1


@dataclass(frozen=True)
class ResumeSummary:
    total_scenarios: int
    completed: int
    failed: int
    missing: int
    completed_ids: Tuple[str, ...]
    failed_ids: Tuple[str, ...]
    missing_ids: Tuple[str, ...]


_COMPLETED_OUTCOMES = frozenset({
    "PROPOSAL_VALID",
    "NO_PROPOSAL",
    "VALIDATION_FAILED",
})
_FAILED_OUTCOMES = frozenset({
    "API_ERROR",
    "TIMEOUT",
})


def _extract_scenario_id(correlation_id: str) -> str:
    """Extract scenario_id from correlation_id.

    The runner produces correlation_ids like:
      {scenario_id}-{fingerprint[:8]}-{run_id}
    or
      {scenario_id}-{fingerprint[:8]}

    We strip everything after the first fingerprint-like segment.
    """
    parts = correlation_id.split("-")
    # scenario_id itself may contain hyphens (e.g. DUP-001).
    # The fingerprint segment is always exactly 8 hex chars.
    for i, part in enumerate(parts):
        if len(part) == 8 and all(c in "0123456789abcdef" for c in part.lower()):
            return "-".join(parts[:i])
    # Fallback: return everything before the first 8-char hex segment
    return correlation_id


def load_resume_state(artifact_path: Path) -> Dict[str, ScenarioCheckpoint]:
    """Read a JSONL audit artifact and build scenario-level checkpoint state.

    For each scenario_id, the most recent outcome determines its status:
      - COMPLETED if outcome is in {PROPOSAL_VALID, NO_PROPOSAL, VALIDATION_FAILED}
      - FAILED if outcome is in {API_ERROR, TIMEOUT}

    When a scenario appears multiple times (multiple attempts), the
    latest attempt is authoritative.  Selection does NOT depend on
    confidence or metric favorability.
    """
    state: Dict[str, ScenarioCheckpoint] = {}

    # Load canonical artifact first (previous completed run).
    if artifact_path.exists():
        state = _parse_artifact(artifact_path)

    # If a sidecar exists (active/incomplete run), merge it with the
    # canonical state.  Completed outcomes supersede failed ones.
    sidecar_path = Path(str(artifact_path) + ".tmp")
    if sidecar_path.exists() and sidecar_path.stat().st_size > 0:
        sidecar_state = _parse_artifact(sidecar_path)
        for sid, cp in sidecar_state.items():
            existing = state.get(sid)
            if existing is None:
                state[sid] = cp
            elif cp.status == CompletionStatus.COMPLETED and existing.status != CompletionStatus.COMPLETED:
                state[sid] = cp
            elif cp.status == existing.status and cp.timestamp >= existing.timestamp:
                state[sid] = cp

    return state


def _parse_artifact(path: Path) -> Dict[str, ScenarioCheckpoint]:
    """Parse a JSONL artifact into scenario checkpoint state.

    Within a single artifact, a completed outcome always supersedes a
    failed one for the same scenario_id, regardless of timestamp.
    Among same-status outcomes, the latest timestamp wins.
    """
    state: Dict[str, ScenarioCheckpoint] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            correlation_id = record.get("correlation_id", "")
            scenario_id = _extract_scenario_id(correlation_id)
            if not scenario_id:
                continue

            outcome = record.get("outcome", "")
            timestamp = record.get("timestamp", "")
            diagnostic = record.get("diagnostic") or ""
            error_classification = _extract_error_classification(outcome, diagnostic)

            status = (
                CompletionStatus.COMPLETED
                if outcome in _COMPLETED_OUTCOMES
                else CompletionStatus.FAILED
                if outcome in _FAILED_OUTCOMES
                else CompletionStatus.MISSING
            )

            existing = state.get(scenario_id)
            if existing is None:
                state[scenario_id] = ScenarioCheckpoint(
                    scenario_id=scenario_id,
                    status=status,
                    run_id=correlation_id,
                    timestamp=timestamp,
                    outcome=outcome,
                    error_classification=error_classification,
                    attempt_count=1,
                )
            elif status == CompletionStatus.COMPLETED and existing.status != CompletionStatus.COMPLETED:
                state[scenario_id] = ScenarioCheckpoint(
                    scenario_id=scenario_id,
                    status=status,
                    run_id=correlation_id,
                    timestamp=timestamp,
                    outcome=outcome,
                    error_classification=error_classification,
                    attempt_count=existing.attempt_count + 1,
                )
            elif status == existing.status and timestamp >= existing.timestamp:
                state[scenario_id] = ScenarioCheckpoint(
                    scenario_id=scenario_id,
                    status=status,
                    run_id=correlation_id,
                    timestamp=timestamp,
                    outcome=outcome,
                    error_classification=error_classification,
                    attempt_count=existing.attempt_count + 1,
                )
            else:
                state[scenario_id] = ScenarioCheckpoint(
                    scenario_id=scenario_id,
                    status=existing.status,
                    run_id=existing.run_id,
                    timestamp=existing.timestamp,
                    outcome=existing.outcome,
                    error_classification=existing.error_classification,
                    attempt_count=existing.attempt_count + 1,
                )

    return state


def _extract_error_classification(outcome: str, diagnostic: str) -> Optional[str]:
    """Derive error classification from outcome and diagnostic text."""
    if outcome == "TIMEOUT":
        return "timeout"
    if outcome != "API_ERROR":
        return None
    lower = diagnostic.lower()
    if "quota_exhausted" in lower or "tokens per day" in lower or "tpd" in lower:
        return "quota_exhausted"
    if "rate_limit" in lower or "429" in lower or "transient" in lower:
        return "transient"
    return "unknown"


def build_resume_summary(state: Dict[str, ScenarioCheckpoint]) -> ResumeSummary:
    """Aggregate checkpoint state into a summary."""
    completed = [sid for sid, cp in state.items() if cp.status == CompletionStatus.COMPLETED]
    failed = [sid for sid, cp in state.items() if cp.status == CompletionStatus.FAILED]
    missing = [sid for sid, cp in state.items() if cp.status == CompletionStatus.MISSING]
    return ResumeSummary(
        total_scenarios=len(state),
        completed=len(completed),
        failed=len(failed),
        missing=len(missing),
        completed_ids=tuple(sorted(completed)),
        failed_ids=tuple(sorted(failed)),
        missing_ids=tuple(sorted(missing)),
    )


def filter_residuals_for_resume(
    residuals: List[Any],
    state: Dict[str, ScenarioCheckpoint],
) -> Tuple[List[Any], List[Any], List[Any]]:
    """Partition residuals into skip / retry / run lists.

    Returns:
        (skip_list, retry_list, run_list)
    """
    skip: List[Any] = []
    retry: List[Any] = []
    run: List[Any] = []

    for residual in residuals:
        sid = residual.scenario_id
        cp = state.get(sid)
        if cp is None:
            run.append(residual)
        elif cp.status == CompletionStatus.COMPLETED:
            skip.append(residual)
        elif cp.status == CompletionStatus.FAILED:
            retry.append(residual)
        else:
            run.append(residual)

    return skip, retry, run


def validate_finalization(
    state: Dict[str, ScenarioCheckpoint],
    expected_scenario_ids: List[str],
    expected_fingerprint: str,
    artifact_path: Path,
) -> List[str]:
    """Validate that a resume state is eligible for finalization.

    Returns a list of error strings. Empty list means valid.
    """
    errors: List[str] = []

    # 1. All expected scenarios must be present
    expected_set = set(expected_scenario_ids)
    present_set = set(state.keys())
    missing = expected_set - present_set
    if missing:
        errors.append(f"Missing scenarios: {sorted(missing)}")

    # 2. No unexpected extra scenarios
    extra = present_set - expected_set
    if extra:
        errors.append(f"Unexpected extra scenarios: {sorted(extra)}")

    # 3. No failures allowed for finalization
    failed = [sid for sid, cp in state.items() if cp.status == CompletionStatus.FAILED]
    if failed:
        errors.append(f"Failed scenarios (not completed): {sorted(failed)}")

    # 4. All must be completed
    not_completed = [sid for sid, cp in state.items() if cp.status != CompletionStatus.COMPLETED]
    if not_completed:
        errors.append(f"Not-completed scenarios: {sorted(not_completed)}")

    # 5. Fingerprint check on artifact
    if artifact_path.exists():
        with artifact_path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    errors.append(f"Malformed JSON at line {line_no}")
                    continue
                fp = record.get("dataset_fingerprint")
                if fp and fp != expected_fingerprint:
                    errors.append(
                        f"Fingerprint mismatch at line {line_no}: "
                        f"{fp[:12]}... != {expected_fingerprint[:12]}..."
                    )

    # 6. No duplicate scenario IDs in artifact
    if artifact_path.exists():
        seen_ids: List[str] = []
        with artifact_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                cid = record.get("correlation_id", "")
                sid = _extract_scenario_id(cid)
                if sid in seen_ids:
                    errors.append(f"Duplicate scenario_id in artifact: {sid}")
                seen_ids.append(sid)

    return errors


def validate_resume_artifact(
    artifact_path: Path,
    residuals: List[Any],
    expected_fingerprint: str,
) -> List[str]:
    """Validate that an artifact is safe to use as resume state.

    Checks:
      * fingerprint matches expected
      * scenario_id exists in residuals
      * no duplicate scenario IDs
      * outcome is valid
      * PROPOSAL_VALID has proposed_match_ids
      * NO_PROPOSAL has empty/null proposal
      * API_ERROR has diagnostic
      * presented_record_ids are non-empty and include all scenario member IDs
      * proposed_match_ids are subsets of presented_record_ids when present

    Returns a list of error strings. Empty list means valid.
    """
    errors: List[str] = []

    if not artifact_path.exists():
        errors.append(f"Artifact not found: {artifact_path}")
        return errors

    residual_map = {r.scenario_id: r for r in residuals}
    expected_ids = set(residual_map.keys())

    seen_scenario_ids: List[str] = []
    with artifact_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                errors.append(f"Malformed JSON at line {line_no}")
                continue

            fp = record.get("dataset_fingerprint")
            if fp != expected_fingerprint:
                errors.append(
                    f"Record {line_no}: fingerprint mismatch "
                    f"{fp[:12]}... != {expected_fingerprint[:12]}..."
                )

            cid = record.get("correlation_id", "")
            scenario_id = _extract_scenario_id(cid)
            if not scenario_id:
                errors.append(f"Record {line_no}: no scenario_id in correlation_id")
                continue

            if scenario_id in seen_scenario_ids:
                errors.append(f"Record {line_no}: duplicate scenario_id {scenario_id}")
            seen_scenario_ids.append(scenario_id)

            if scenario_id not in expected_ids:
                errors.append(f"Record {line_no}: unknown scenario_id {scenario_id}")

            outcome = record.get("outcome", "")
            valid_outcomes = {
                "PROPOSAL_VALID",
                "NO_PROPOSAL",
                "VALIDATION_FAILED",
                "API_ERROR",
                "TIMEOUT",
            }
            if outcome not in valid_outcomes:
                errors.append(f"Record {line_no}: invalid outcome {outcome}")

            if outcome == "PROPOSAL_VALID":
                proposal = record.get("proposal")
                if not proposal:
                    errors.append(f"Record {line_no}: PROPOSAL_VALID missing proposal")
                else:
                    pids = proposal.get("proposed_match_ids", [])
                    if not pids:
                        errors.append(
                            f"Record {line_no}: PROPOSAL_VALID missing proposed_match_ids"
                        )
                    presented = record.get("presented_record_ids", [])
                    for pid in pids:
                        if pid not in presented:
                            errors.append(
                                f"Record {line_no}: proposed_match_id {pid} "
                                f"not in presented_record_ids"
                            )

            if outcome == "NO_PROPOSAL":
                proposal = record.get("proposal")
                if proposal is not None and proposal.get("proposed_match_ids"):
                    errors.append(
                        f"Record {line_no}: NO_PROPOSAL has non-empty proposed_match_ids"
                    )

            if outcome in ("API_ERROR", "TIMEOUT"):
                if not record.get("diagnostic"):
                    errors.append(
                        f"Record {line_no}: {outcome} missing diagnostic"
                    )

            presented = record.get("presented_record_ids", [])
            if not presented:
                errors.append(f"Record {line_no}: empty presented_record_ids")
            else:
                residual = residual_map.get(scenario_id)
                if residual:
                    member_ids = set(residual.member_record_ids)
                    presented_set = set(presented)
                    for mid in member_ids:
                        if mid not in presented_set:
                            errors.append(
                                f"Record {line_no}: scenario member {mid} "
                                f"not found in presented_record_ids for "
                                f"scenario {scenario_id}"
                            )

    return errors
