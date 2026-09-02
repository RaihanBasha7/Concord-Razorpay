"""
Full Layer 2 runner — processes ALL residual scenarios from the frozen dataset.

Creates a new provenance-verified JSONL artifact stamped with the current
dataset fingerprint.  The artifact lifecycle:
  * The active resumable partial artifact is `data/layer2_clean_audit.jsonl`.
  * If an existing artifact exists, it remains at the canonical path while
    the new run writes to a temporary sidecar file.
  * On successful completion the sidecar is atomically promoted to the final
    artifact path and the previous artifact is archived.
  * If the run is interrupted, the previous completed artifact remains
    intact at the canonical path (no mock-mode fallback in evaluation).
  * The expected record count is validated before promotion; a mismatch
    removes the incomplete sidecar and the previous artifact stays.

Usage:
    python scripts/run_layer2_full.py
    python scripts/run_layer2_full.py --delay 5.0
    python scripts/run_layer2_full.py --resume
    LAYER2_DELAY_SECONDS=4.0 python scripts/run_layer2_full.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

from reconciliation.audit import Auditor
from reconciliation.evaluation.dataset_fingerprint import (
    read_manifest,
    verify_dataset,
)
from reconciliation.evaluation.resume import (
    CompletionStatus,
    build_resume_summary,
    filter_residuals_for_resume,
    load_resume_state,
    validate_finalization,
)
from reconciliation.groq_provider import (
    GroqProviderError,
    GroqStructuredProvider,
    safe_diagnostic,
)
from reconciliation.loader import load_residuals
from reconciliation.proposal_orchestration import ProposalOrchestrator
from reconciliation.proposal_service import ProposalService
from reconciliation.proposal_validation import ProposalOutcome, ProposalOutcomeType
from reconciliation.runner import Day4Runner
from reconciliation.retrieval import RetrievalConfig


# ---------------------------------------------------------------------------
# .env loader (no external dependency)
# ---------------------------------------------------------------------------

def _load_dotenv(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Existing environment variables take precedence (same as shell export).
    Lines starting with # and blank lines are ignored. Values may be
    optionally surrounded by single or double quotes which are stripped.
    """
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# Paced + retrying orchestrator wrapper
# ---------------------------------------------------------------------------

# Outcomes that are safe to retry (rate-limit / transient infra errors)
_RETRYABLE_OUTCOMES = frozenset({
    ProposalOutcomeType.API_ERROR,
    ProposalOutcomeType.TIMEOUT,
})


class PacedRetryingOrchestrator:
    """Wraps a ProposalOrchestrator with inter-call pacing and rate-limit retry.

    * A configurable delay is inserted *before* each call (except the first)
      to avoid saturating free-tier rate limits.
    * If the outcome is API_ERROR or TIMEOUT and the error classification is
      "transient", the call is retried with exponential backoff up to
      ``max_retries`` times.
    * Classification "quota_exhausted" returns immediately (no retry).
    * If error_classification is None (unclassified), no retry is performed.
    """

    def __init__(
        self,
        orchestrator: ProposalOrchestrator,
        *,
        delay_seconds: float = 3.0,
        max_retries: int = 2,
        backoff_base: float = 6.0,
    ) -> None:
        self._orchestrator = orchestrator
        self._delay = delay_seconds
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._call_count = 0

    def resolve(self, case, retrieval_result):  # type: ignore[no-untyped-def]
        # Inter-call pacing
        if self._call_count > 0 and self._delay > 0:
            time.sleep(self._delay)
        self._call_count += 1

        outcome = self._orchestrator.resolve(case, retrieval_result)

        # Daily token quota exhaustion is a distinct failure mode from a
        # short-lived rate-limit.  When the daily quota is hit, retrying
        # within seconds or minutes is futile; return immediately.
        if (
            outcome.outcome in _RETRYABLE_OUTCOMES
            and outcome.error_classification == "quota_exhausted"
        ):
            return outcome

        retries = 0
        while (
            outcome.outcome in _RETRYABLE_OUTCOMES
            and outcome.error_classification == "transient"
            and retries < self._max_retries
        ):
            retries += 1
            backoff = self._backoff_base * (2 ** (retries - 1))
            print(
                f"  Rate limit / transient error — retrying in {backoff:.0f}s "
                f"(attempt {retries}/{self._max_retries})...",
                flush=True,
            )
            time.sleep(backoff)
            # Do NOT add extra delay on retry — the backoff IS the delay.
            outcome = self._orchestrator.resolve(case, retrieval_result)

        return outcome


# ---------------------------------------------------------------------------
# API key validation
# ---------------------------------------------------------------------------

def _validate_api_key(provider: GroqStructuredProvider) -> bool:
    """Make a single cheap test call to confirm the API key is valid.

    Returns True on success, False (after printing the error) on failure.
    """
    print("Validating Groq API key...", end=" ", flush=True)
    try:
        provider.complete_structured(
            system_prompt="Reply only with the JSON provided.",
            user_prompt='Return: {"ok": true}',
            json_schema={
                "name": "health_check",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        )
        print("OK")
        return True
    except GroqProviderError as exc:
        print("FAILED")
        print(f"  {safe_diagnostic(exc)}")
        print(
            "\nCannot proceed without a valid GROQ_API_KEY.\n"
            "Set it in your environment or in the .env file and re-run."
        )
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="Concord Layer 2 full runner — all residual scenarios",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=float(os.environ.get("LAYER2_DELAY_SECONDS", "3.0")),
        help=(
            "Seconds to wait between Groq API calls (default: 3.0, "
            "or LAYER2_DELAY_SECONDS env var). Set higher if hitting rate limits."
        ),
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Max retries on rate-limit errors per scenario (default: 2).",
    )
    parser.add_argument(
        "--backoff-base",
        type=float,
        default=6.0,
        help="Base seconds for exponential backoff on rate-limit retries (default: 6.0).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help=(
            "Resume from the active resumable partial artifact "
            "(`data/layer2_clean_audit.jsonl`).  Skips completed scenarios, "
            "retries failed ones, and runs missing scenarios.  Without this "
            "flag, any existing sidecar is discarded and the run starts from "
            "scratch."
        ),
    )
    args = parser.parse_args()

    data_dir = Path("data")
    artifact_path = data_dir / "layer2_clean_audit.jsonl"
    sidecar_path = Path(str(artifact_path) + ".tmp")

    # ── 1. Verify frozen dataset ──────────────────────────────────────
    print("=" * 60)
    print("Layer 2 Full Run — Frozen Dataset Verification")
    print("=" * 60)

    manifest = read_manifest(data_dir)
    if manifest is None:
        print("ERROR: No frozen dataset manifest found.")
        return 1

    verification = verify_dataset(data_dir, manifest)
    if not verification.ok:
        print(f"DRIFT DETECTED: {verification.details}")
        print(f"  Mismatches: {verification.mismatches}")
        print(f"  Missing: {verification.missing}")
        return 1

    fingerprint = manifest.fingerprint()
    print(f"Frozen dataset verified.")
    print(f"  Fingerprint: {fingerprint}")
    print(f"  Schema version: {manifest.schema_version}")
    print(f"  Dataset seed: {manifest.dataset_seed}")

    # ── 1b. Validate active resumable artifact ────────────────────────
    if artifact_path.exists():
        from reconciliation.evaluation.resume import validate_resume_artifact
        residuals_for_validation = load_residuals(data_dir)
        validation_errors = validate_resume_artifact(
            artifact_path,
            residuals_for_validation,
            fingerprint,
        )
        if validation_errors:
            print("\nERROR: Active resumable artifact failed validation:")
            for err in validation_errors:
                print(f"  - {err}")
            return 1
        print(f"\nActive resumable artifact validated: {artifact_path.name}")
    else:
        print(f"\nNo active resumable artifact found. Fresh run will start from scratch.")

    # ── 2. Validate API key ───────────────────────────────────────────
    provider = GroqStructuredProvider()
    if not _validate_api_key(provider):
        return 1

    # ── 3. Load residuals and count expected scenarios ─────────────────
    residuals = load_residuals(data_dir)
    expected_count = len(residuals)
    expected_scenario_ids = [r.scenario_id for r in residuals]
    print(f"\nExpected residual scenarios: {expected_count}")

    # ── 4. Detect incomplete sidecar from prior run ──────────────────
    do_resume = args.resume
    resume_state = load_resume_state(artifact_path)
    summary = build_resume_summary(resume_state) if resume_state else None

    if summary and summary.total_scenarios > 0:
        completed_count = summary.completed
        failed_count = summary.failed
        missing_count = summary.missing
        print(
            f"\nResume state loaded: {completed_count} completed, "
            f"{failed_count} failed, {missing_count} missing "
            f"(total tracked: {summary.total_scenarios})"
        )
        if not do_resume:
            print(
                "\nWARNING: Found incomplete sidecar with prior run state."
            )
            print("  Use --resume to continue, or the sidecar will be discarded.")
    else:
        completed_count = 0
        failed_count = 0
        missing_count = 0
        if do_resume:
            print(
                "\nWARNING: --resume was passed but no prior run state found."
            )
            do_resume = False

    remaining_count = expected_count - completed_count

    if do_resume and remaining_count == 0 and failed_count == 0:
        print("\nAll scenarios already completed. Nothing to do.")
        print("Re-running validation against the existing sidecar...")
        auditor = Auditor(
            artifact_path,
            archive_existing=True,
            atomic_write=True,
            expected_record_count=expected_count,
            resume=True,
        )
        try:
            auditor.finalize()
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1
        print("Sidecar validated and promoted successfully.")
        return 0

    if do_resume:
        skip, retry, run = filter_residuals_for_resume(residuals, resume_state)
        active_residuals = retry + run
        remaining_count = len(active_residuals)
        print(
            f"\nResume plan: skip {len(skip)}, retry {len(retry)}, "
            f"run {len(run)} (total active: {remaining_count})"
        )
    else:
        active_residuals = residuals

    # ── 5. Run Layer 2 against remaining residuals ────────────────────
    print(f"\n{'=' * 60}")
    print(
        f"Processing {remaining_count} scenarios"
        + (f" (resume mode)" if do_resume else "")
        + "..."
    )
    print(f"  Inter-call delay: {args.delay:.1f}s")
    print(f"  Rate-limit retries: {args.max_retries} (backoff base: {args.backoff_base:.0f}s)")
    print(f"{'=' * 60}\n")

    # On resume, pre-populate the sidecar with existing artifact records
    # so that finalize() sees the full expected record count.
    if do_resume and artifact_path.exists():
        if not sidecar_path.exists():
            with artifact_path.open("r", encoding="utf-8") as src:
                with sidecar_path.open("w", encoding="utf-8") as dst:
                    for line in src:
                        dst.write(line)
            print(
                f"\nPre-populated sidecar with {completed_count} existing records "
                f"from {artifact_path.name}."
            )

    auditor = Auditor(
        artifact_path,
        archive_existing=True,
        atomic_write=True,
        expected_record_count=expected_count,
        resume=do_resume,
    )
    if do_resume and auditor.resume_count != completed_count:
        print(
            f"WARNING: Sidecar record count changed between detection"
            f" ({completed_count}) and Auditor init"
            f" ({auditor.resume_count})."
        )

    service = ProposalService(provider)
    base_orchestrator = ProposalOrchestrator(service)

    orchestrator = PacedRetryingOrchestrator(
        base_orchestrator,
        delay_seconds=args.delay,
        max_retries=args.max_retries,
        backoff_base=args.backoff_base,
    )

    runner = Day4Runner(
        data_dir=data_dir,
        orchestrator=orchestrator,
        limit=remaining_count if do_resume else expected_count,
        auditor=auditor,
        retrieval_config=RetrievalConfig(),
        start_offset=0 if do_resume else 0,
        resume_state_path=str(artifact_path) if do_resume else None,
    )

    summary = runner.run()
    runner.print_summary(summary)

    # ── 5b. Finalize artifact (atomic promote) ──────────────────────
    try:
        auditor.finalize()
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1

    # ── 6. Validate the artifact ─────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("Artifact Validation")
    print(f"{'=' * 60}\n")

    if not artifact_path.exists():
        print("ERROR: Artifact file not created.")
        return 1

    # Finalization validation
    finalization_errors = validate_finalization(
        state=resume_state if resume_state else load_resume_state(artifact_path),
        expected_scenario_ids=expected_scenario_ids,
        expected_fingerprint=fingerprint,
        artifact_path=artifact_path,
    )
    if finalization_errors:
        print("ERROR: Finalization validation failed:")
        for err in finalization_errors:
            print(f"  - {err}")
        return 1
    print("Finalization validation: PASSED")

    records = []
    with artifact_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    accounted = len(records)
    print(f"Expected scenarios: {expected_count}")
    print(f"Records in artifact: {accounted}")
    print(f"Match: {'YES' if accounted == expected_count else 'NO'}")

    if accounted != expected_count:
        print(f"ERROR: Scenario count mismatch!")
        return 1

    # Check for duplicates
    correlation_ids = [r.get("correlation_id") for r in records]
    unique_ids = set(correlation_ids)
    print(f"Unique correlation IDs: {len(unique_ids)}")
    if len(unique_ids) != accounted:
        dup_count = accounted - len(unique_ids)
        print(f"WARNING: {dup_count} duplicate correlation IDs found.")
    else:
        print("No duplicates.")

    # Check fingerprint on every record
    fingerprints = [r.get("dataset_fingerprint") for r in records]
    all_have_fp = all(fp == fingerprint for fp in fingerprints)
    print(f"\nAll records carry correct fingerprint: {'YES' if all_have_fp else 'NO'}")
    if not all_have_fp:
        mismatches = [i for i, fp in enumerate(fingerprints) if fp != fingerprint]
        print(f"  Mismatched record indices: {mismatches[:10]}")

    # Outcome breakdown
    outcomes = Counter(r.get("outcome", "UNKNOWN") for r in records)
    print(f"\nOutcome breakdown:")
    for outcome, count in sorted(outcomes.items()):
        print(f"  {outcome:20s} {count:4d}")

    # Verify every record has required fields
    required_fields = ["correlation_id", "timestamp", "presented_record_ids",
                       "outcome", "proposal", "confidence", "reason", "dataset_fingerprint"]
    missing_fields = []
    for i, r in enumerate(records):
        for field in required_fields:
            if field not in r:
                missing_fields.append((i, field))
    if missing_fields:
        print(f"\nWARNING: Missing fields in {len(missing_fields)} records.")
        for idx, field in missing_fields[:5]:
            print(f"  Record {idx}: missing '{field}'")
    else:
        print("\nAll records have required fields.")

    # ── 7. Summary ──────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("Final Summary")
    print(f"{'=' * 60}")
    print(f"Dataset fingerprint: {fingerprint}")
    print(f"Expected scenarios: {expected_count}")
    print(f"Accounted scenarios: {accounted}")
    print(f"Artifact path: {artifact_path}")
    print(f"Artifact records written: {auditor.write_count}")
    print(f"Artifact provenance-verified: {'YES' if all_have_fp and accounted == expected_count else 'NO'}")

    non_error = sum(
        outcomes.get(k, 0)
        for k in ("PROPOSAL_VALID", "NO_PROPOSAL", "VALIDATION_FAILED")
    )
    print(f"\nNon-error responses: {non_error} / {accounted}")

    if non_error == 0:
        print(f"\nNOTE: All {accounted} scenarios resulted in API_ERROR/TIMEOUT.")
        print(f"This means the Groq API was not available during this run.")
        print(f"To get real AI proposals, set GROQ_API_KEY and re-run:")
        print(f"  python scripts/run_layer2_full.py")

    return 0


if __name__ == "__main__":
    sys.exit(main())
