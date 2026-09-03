"""
Resume Layer 2 evaluation for the 32 missing scenarios only.

This script preserves every existing record in the canonical artifact
(36 completed + 9 API_ERROR failures) and evaluates ONLY the 32 scenarios
that were never reached due to Groq quota exhaustion.

Design rules enforced:
  * The 36 completed and 9 API_ERROR scenarios are NOT re-run.
  * The 9 API_ERROR records remain as genuine provider failures (no conversion
    to NO_PROPOSAL or fabricated proposals).
  * The 32 missing scenarios receive fresh evaluation through the existing
    pipeline (Day4Runner components: ProposalService, ProposalOrchestrator,
    PacedRetryingOrchestrator, Auditor).
  * All outcomes — success or API_ERROR — are recorded as genuine provider
    responses.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

from reconciliation.audit import Auditor, make_audit_record
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
    validate_resume_artifact,
)
from reconciliation.groq_provider import (
    GroqProviderError,
    GroqStructuredProvider,
    safe_diagnostic,
)
from reconciliation.layer2 import reconstruct_layer2_case
from reconciliation.loader import load_normalized_records, load_residuals
from reconciliation.proposal_orchestration import ProposalOrchestrator
from reconciliation.proposal_service import ProposalService
from reconciliation.retrieval import RetrievalConfig, retrieve_candidates
from run_layer2_full import PacedRetryingOrchestrator


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def _read_artifact_records(path: Path) -> list[dict]:
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def main() -> int:
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="Resume Layer 2 evaluation for the 32 missing scenarios only."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=float(os.environ.get("LAYER2_DELAY_SECONDS", "3.0")),
        help="Seconds to wait between Groq API calls (default: 3.0).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Max retries on rate-limit (transient) errors per scenario.",
    )
    parser.add_argument(
        "--backoff-base",
        type=float,
        default=6.0,
        help="Base seconds for exponential backoff on transient errors.",
    )
    args = parser.parse_args()

    data_dir = Path("data")
    artifact_path = data_dir / "layer2_clean_audit.jsonl"
    sidecar_path = Path(str(artifact_path) + ".tmp")

    # ── 1. Verify frozen dataset ──────────────────────────────
    print("=" * 60)
    print("Layer 2 Resume — Frozen Dataset Verification")
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

    # ── 2. Validate existing artifact ────────────────────────
    if not artifact_path.exists():
        print("ERROR: Canonical artifact not found.")
        return 1

    residuals = load_residuals(data_dir)
    expected_scenario_ids = [r.scenario_id for r in residuals]
    expected_count = len(residuals)

    validation_errors = validate_resume_artifact(
        artifact_path, residuals, fingerprint
    )
    if validation_errors:
        print("\nERROR: Existing artifact failed validation:")
        for err in validation_errors:
            print(f"  - {err}")
        return 1

    # ── 3. Load resume state and determine plan ──────────────
    resume_state = load_resume_state(artifact_path)
    summary = build_resume_summary(resume_state)

    skip_ids = set(summary.completed_ids)
    failed_ids = set(summary.failed_ids)
    recorded_ids = skip_ids | failed_ids
    missing_ids = [sid for sid in expected_scenario_ids if sid not in recorded_ids]

    print(f"\nExisting records: {len(recorded_ids)} "
          f"(completed={summary.completed}, failed={summary.failed})")
    print(f"Missing scenarios to evaluate: {len(missing_ids)}")
    print(f"Failed scenarios preserved as-is: {len(failed_ids)} "
          f"({' '.join(sorted(failed_ids))})")

    # ── 4. Validate API key ──────────────────────────────────
    provider = GroqStructuredProvider()
    print("\nValidating Groq API key...", end=" ", flush=True)
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
    except GroqProviderError as exc:
        print("FAILED")
        print(f"  {safe_diagnostic(exc)}")
        print("\nGroq API key validation failed. The 32 missing scenarios")
        print("will still be attempted — provider failures remain failures.")

    # ── 5. Pre-populate sidecar with existing 45 records ─────
    existing_records = _read_artifact_records(artifact_path)
    print(f"\nPre-populating sidecar with {len(existing_records)} existing records...")

    if sidecar_path.exists():
        sidecar_path.unlink()
    with sidecar_path.open("w", encoding="utf-8") as f:
        for record in existing_records:
            f.write(json.dumps(record) + "\n")

    # ── 6. Set up Auditor (resume mode) ──────────────────────
    auditor = Auditor(
        artifact_path,
        archive_existing=True,
        atomic_write=True,
        expected_record_count=expected_count,
        resume=True,
    )
    print(f"Auditor initialized: resume_count={auditor.resume_count}, "
          f"write_count={auditor.write_count}")

    # ── 7. Set up pipeline components ────────────────────────
    service = ProposalService(provider)
    base_orchestrator = ProposalOrchestrator(service)
    orchestrator = PacedRetryingOrchestrator(
        base_orchestrator,
        delay_seconds=args.delay,
        max_retries=args.max_retries,
        backoff_base=args.backoff_base,
    )

    normalized = load_normalized_records(data_dir)

    # Filter to only the missing scenarios
    missing_residuals = [r for r in residuals if r.scenario_id in set(missing_ids)]

    print(f"\n{'=' * 60}")
    print(f"Processing {len(missing_residuals)} missing scenarios")
    print(f"  Inter-call delay: {args.delay:.1f}s")
    print(f"  Rate-limit retries: {args.max_retries} (backoff base: {args.backoff_base:.0f}s)")
    print(f"{'=' * 60}\n")

    # ── 8. Process missing scenarios ──────────────────────────
    outcomes_counter: Counter = Counter()
    for i, residual in enumerate(missing_residuals):
        sid = residual.scenario_id
        print(f"  [{i+1}/{len(missing_residuals)}] {sid}...", flush=True)

        case = reconstruct_layer2_case(
            scenario_id=sid,
            member_record_ids=residual.member_record_ids,
            normalized_records=tuple(normalized),
        )
        retrieval = retrieve_candidates(case, tuple(normalized), RetrievalConfig())

        outcome = orchestrator.resolve(case, retrieval)
        outcomes_counter[outcome.outcome.value] += 1

        # Build presented IDs (same logic as Day4Runner._process_one)
        member_ids = [r.record_id for r in case.member_records]
        candidate_ids = [c.record.record_id for c in retrieval.candidates]
        seen: set[str] = set()
        presented_ids: list[str] = []
        for rid in member_ids + candidate_ids:
            if rid not in seen:
                seen.add(rid)
                presented_ids.append(rid)

        record = make_audit_record(
            correlation_id=sid,
            presented_record_ids=presented_ids,
            outcome=outcome.outcome.value,
            proposal=outcome.proposal,
            reason=outcome.reason,
            dataset_fingerprint=fingerprint,
            diagnostic=outcome.diagnostic or None,
        )

        if not auditor.write(record):
            print(f"  WARNING: Failed to write audit record for {sid}")

        if i < len(missing_residuals) - 1:
            print(f"    outcome: {outcome.outcome.value}")

    # ── 9. Final summary of new outcomes ─────────────────────
    print(f"\n{'=' * 60}")
    print("New outcomes (32 missing scenarios):")
    for outcome, count in sorted(outcomes_counter.items()):
        print(f"  {outcome:20s} {count:4d}")

    # ── 10. Finalize artifact (atomic promote) ──────────────
    try:
        auditor.finalize()
    except ValueError as exc:
        print(f"\nERROR: Finalization failed: {exc}")
        print("The sidecar has been cleaned up. The previous artifact is intact.")
        return 1

    print(f"\nSidecar promoted to canonical artifact: {artifact_path}")

    # ── 11. Validate finalized artifact ──────────────────────
    print(f"\n{'=' * 60}")
    print("Finalized Artifact Validation")
    print(f"{'=' * 60}\n")

    final_state = load_resume_state(artifact_path)
    final_summary = build_resume_summary(final_state)
    finalization_errors = validate_finalization(
        state=final_state,
        expected_scenario_ids=expected_scenario_ids,
        expected_fingerprint=fingerprint,
        artifact_path=artifact_path,
    )
    # Provider failures (API_ERROR/TIMEOUT) are genuine outcomes that must
    # remain as failures per task requirements. They are expected and not
    # rejected. Only structural errors (missing/duplicate scenarios, fingerprint
    # mismatches) are fatal.
    genuine_errors = [
        err
        for err in finalization_errors
        if not err.startswith("Failed scenarios (not completed)")
        and not err.startswith("Not-completed scenarios")
    ]

    if genuine_errors:
        print("ERROR: Finalization validation failed:")
        for err in genuine_errors:
            print(f"  - {err}")
        return 1
    print("Finalization validation: PASSED")
    print(f"  (Provider failures preserved as failures: {final_summary.failed} "
          f"API_ERROR/TIMEOUT scenarios: {' '.join(sorted(final_summary.failed_ids))})")

    records = _read_artifact_records(artifact_path)
    accounted = len(records)
    artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()

    print(f"\nExpected scenarios: {expected_count}")
    print(f"Records in artifact: {accounted}")
    print(f"Match: {'YES' if accounted == expected_count else 'NO'}")
    print(f"Artifact SHA-256: {artifact_hash}")

    if accounted != expected_count:
        print("ERROR: Scenario count mismatch!")
        return 1

    # Check for duplicates
    correlation_ids = [r.get("correlation_id") for r in records]
    unique_ids = set(correlation_ids)
    print(f"Unique correlation IDs: {len(unique_ids)}")
    if len(unique_ids) != accounted:
        print(f"WARNING: {accounted - len(unique_ids)} duplicate IDs found.")
    else:
        print("No duplicates.")

    # Check fingerprint on every record
    all_have_fp = all(r.get("dataset_fingerprint") == fingerprint for r in records)
    print(f"All records carry correct fingerprint: {'YES' if all_have_fp else 'NO'}")
    if not all_have_fp:
        mismatches = [i for i, r in enumerate(records) if r.get("dataset_fingerprint") != fingerprint]
        print(f"  Mismatched record indices: {mismatches[:10]}")
        return 1

    # Outcome breakdown
    outcomes = Counter(r.get("outcome", "UNKNOWN") for r in records)
    print(f"\nFull outcome breakdown (77 scenarios):")
    for outcome, count in sorted(outcomes.items()):
        print(f"  {outcome:20s} {count:4d}")

    # Verify required fields
    required_fields = ["correlation_id", "timestamp", "presented_record_ids",
                       "outcome", "proposal", "confidence", "reason",
                       "dataset_fingerprint"]
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
        print("All records have required fields.")

    # ── 12. Summary ──────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("Resume Complete")
    print(f"{'=' * 60}")
    print(f"Dataset fingerprint: {fingerprint}")
    print(f"Expected scenarios: {expected_count}")
    print(f"Accounted scenarios: {accounted}")
    print(f"Artifact path: {artifact_path}")
    print(f"Artifact SHA-256: {artifact_hash}")

    non_error = sum(outcomes.get(k, 0) for k in
                     ("PROPOSAL_VALID", "NO_PROPOSAL", "VALIDATION_FAILED"))
    provider_failures = outcomes.get("API_ERROR", 0) + outcomes.get("TIMEOUT", 0)
    print(f"\nNon-error responses: {non_error} / {accounted}")
    print(f"Provider failures: {provider_failures} / {accounted}")

    # ── 13. Update manifest with new artifact SHA-256 ────────
    print(f"\n{'=' * 60}")
    print("Updating dataset manifest")
    print(f"{'=' * 60}")

    manifest_path = data_dir / "dataset_manifest.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Update canonical artifact metadata — dataset fingerprint and file
    # hashes remain unchanged.
    manifest_data["canonical_layer2_artifact"] = {
        "filename": "layer2_clean_audit.jsonl",
        "status": "final",
        "record_count": accounted,
        "total_expected": expected_count,
        "sha256": artifact_hash,
        "note": (
            f"FINAL artifact: {accounted} of {expected_count} scenarios evaluated. "
            f"Dataset fingerprint: {fingerprint}. "
            f"Provider failures: {provider_failures} (preserved as genuine API_ERROR/TIMEOUT). "
            "Archived previous partial artifact."
        ),
    }
    manifest_data["active_resumable_partial_artifact"] = manifest_data["canonical_layer2_artifact"]

    manifest_path.write_text(
        json.dumps(manifest_data, indent=2), encoding="utf-8"
    )
    print(f"Manifest updated: {manifest_path.name}")
    print(f"  canonical_layer2_artifact.status: final")
    print(f"  canonical_layer2_artifact.sha256: {artifact_hash}")

    return 0


if __name__ == "__main__":
    # Ensure scripts/ is on the path for 'from run_layer2_full import ...'
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    sys.exit(main())
