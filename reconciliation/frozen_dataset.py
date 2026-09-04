"""
Frozen dataset fingerprint verification and Layer 2 artifact loading.

When a batch upload matches the frozen evaluation dataset (verified by
content-hash fingerprinting of the uploaded CSV bytes against the on-disk
manifest), this module loads pre-computed Layer 2 audit records and
reconstructs ProposalOutcome objects that can be fed to Layer 3 routing.

Safety guarantees:
  * The fingerprint is computed from the RAW CSV bytes (SHA-256), not
    filenames or metadata.  Any modification to the CSV content changes
    the fingerprint.
  * Only the three source CSVs (settlements, bank, ledger) are uploaded;
    ground_truth.json and residuals.csv are verified implicitly via the
    manifest's stored hashes (the manifest was created from the same
    generation seed).
  * If ANY hash mismatches, the dataset is rejected and Layer 2 outcomes
    remain empty (fail-closed).
  * Audit records are matched to residual scenarios by verifying that
    the residual's member_record_ids are a subset of the audit record's
    presented_record_ids.  No fuzzy matching is used.
  * Records from the Layer 2 artifact are never applied to datasets
    that don't match the frozen fingerprint.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from reconciliation.domain.models import NormalizedRecord
from reconciliation.evaluation.dataset_fingerprint import (
    DatasetManifest,
    FROZEN_DATASET_FILES,
    compute_file_hash,
    manifest_path,
    read_manifest,
)
from reconciliation.loader import load_residuals
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_validation import ProposalOutcome, ProposalOutcomeType

logger = logging.getLogger("concord.frozen_dataset")

# File names for the three uploaded source CSVs, matching the manifest keys.
_SOURCE_CSV_MANIFEST_KEYS: Tuple[str, str, str] = (
    "settlements.csv",
    "bank.csv",
    "ledger.csv",
)


# ---------------------------------------------------------------------------
# Fingerprint computation
# ---------------------------------------------------------------------------

def compute_upload_fingerprint(
    settlement_bytes: bytes,
    bank_bytes: bytes,
    ledger_bytes: bytes,
) -> Dict[str, str]:
    """Compute SHA-256 hashes for each uploaded CSV's raw bytes.

    Returns a dict mapping manifest file names to hex digest strings,
    suitable for comparison against ``DatasetManifest.files``.
    """
    return {
        "settlements.csv": hashlib.sha256(settlement_bytes).hexdigest(),
        "bank.csv": hashlib.sha256(bank_bytes).hexdigest(),
        "ledger.csv": hashlib.sha256(ledger_bytes).hexdigest(),
    }


def verify_upload_against_manifest(
    upload_hashes: Dict[str, str],
    data_dir: Path | str,
) -> Tuple[bool, Optional[DatasetManifest], List[str]]:
    """Verify uploaded CSV hashes against the frozen dataset manifest.

    Returns ``(matches, manifest, mismatch_details)``:
      * ``matches`` is True only if ALL three source CSVs match.
      * ``manifest`` is the loaded manifest (or None if absent).
      * ``mismatch_details`` lists human-readable mismatch reasons.
    """
    data_dir = Path(data_dir)
    manifest = read_manifest(data_dir)
    if manifest is None:
        return False, None, ["No frozen dataset manifest found."]

    mismatches: List[str] = []
    for key in _SOURCE_CSV_MANIFEST_KEYS:
        expected = manifest.files.get(key)
        actual = upload_hashes.get(key)
        if expected is None:
            mismatches.append(f"Manifest has no hash for {key}.")
        elif actual is None:
            mismatches.append(f"Upload has no hash for {key}.")
        elif expected != actual:
            mismatches.append(
                f"{key}: expected {expected[:16]}…, got {actual[:16]}…"
            )

    return len(mismatches) == 0, manifest, mismatches


# ---------------------------------------------------------------------------
# Layer 2 audit record loading and mapping
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _AuditRecord:
    """Lightweight parsed audit record from the JSONL artifact."""

    correlation_id: str
    presented_record_ids: Tuple[str, ...]
    outcome: str
    proposal: Optional[Dict[str, Any]]
    confidence: Optional[float]
    reason: str


def _load_canonical_audit_records(data_dir: Path) -> List[_AuditRecord]:
    """Load Layer 2 audit records from the single canonical artifact.

    The canonical artifact filename is specified in the dataset manifest
    under ``canonical_layer2_artifact.filename``.  Its SHA-256 hash is
    verified against ``canonical_layer2_artifact.sha256`` in the manifest.

    If the manifest is absent, the canonical file is missing, or the hash
    does not match, a ValueError is raised (fail-closed).  Historical
    artifact files are never loaded.

    NOTE: We read the raw JSON manifest rather than using ``read_manifest()``
    because ``DatasetManifest.from_dict()`` only deserialises the core fields
    (schema_version, dataset_seed, files) and silently drops the
    ``canonical_layer2_artifact`` extension.
    """
    manifest_file = manifest_path(data_dir)
    if not manifest_file.exists():
        raise ValueError("No dataset manifest found; cannot locate canonical artifact.")

    raw_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    canonical_info = raw_manifest.get("canonical_layer2_artifact")
    if canonical_info is None:
        raise ValueError(
            "Dataset manifest has no 'canonical_layer2_artifact' entry. "
            "Cannot determine which Layer 2 artifact to load."
        )

    filename = canonical_info["filename"]
    expected_hash = canonical_info["sha256"]
    artifact_path = data_dir / filename

    if not artifact_path.exists():
        raise FileNotFoundError(
            f"Canonical Layer 2 artifact not found: {artifact_path}"
        )

    # Verify artifact hash.
    actual_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError(
            f"Canonical Layer 2 artifact hash mismatch for {filename}: "
            f"expected {expected_hash[:16]}..., got {actual_hash[:16]}..."
        )

    # Load records from the single canonical file.
    records: List[_AuditRecord] = []
    try:
        with artifact_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                records.append(
                    _AuditRecord(
                        correlation_id=raw.get("correlation_id", ""),
                        presented_record_ids=tuple(
                            raw.get("presented_record_ids", [])
                        ),
                        outcome=raw.get("outcome", "API_ERROR"),
                        proposal=raw.get("proposal"),
                        confidence=raw.get("confidence"),
                        reason=raw.get("reason", ""),
                    )
                )
    except OSError as exc:
        raise IOError(f"Could not read canonical artifact {artifact_path}: {exc}") from exc

    logger.info(
        "Loaded %d records from canonical artifact %s (hash %s)",
        len(records), filename, actual_hash[:16],
    )
    return records


def _build_record_to_audit_index(
    audit_records: List[_AuditRecord],
) -> Dict[str, Set[int]]:
    """Build an inverted index: record_id → set of audit record indices."""
    index: Dict[str, Set[int]] = defaultdict(set)
    for i, rec in enumerate(audit_records):
        for rid in rec.presented_record_ids:
            index[rid].add(i)
    return index


def _find_audit_for_residual(
    member_ids: Tuple[str, ...],
    record_index: Dict[str, Set[int]],
    audit_records: List[_AuditRecord],
) -> Optional[int]:
    """Find the best audit record index for a residual scenario.

    Matching criterion: ALL member_record_ids must be present in the
    audit record's presented_record_ids.

    When multiple audit records match, preference order:
      1. PROPOSAL_VALID (has actual proposal)
      2. NO_PROPOSAL (model responded but proposed nothing)
      3. API_ERROR / TIMEOUT / VALIDATION_FAILED

    Among same-outcome records, prefer the one with the smallest
    presented_record_ids (most specific / tightest match).
    """
    if not member_ids:
        return None

    # Find all audit records containing every member ID.
    first = member_ids[0]
    candidate_indices = record_index.get(first, set()).copy()
    for mid in member_ids[1:]:
        candidate_indices &= record_index.get(mid, set())

    if not candidate_indices:
        return None

    # Rank candidates by outcome quality, then by specificity.
    _OUTCOME_RANK = {
        "PROPOSAL_VALID": 0,
        "NO_PROPOSAL": 1,
        "API_ERROR": 2,
        "TIMEOUT": 2,
        "VALIDATION_FAILED": 2,
    }

    best_idx = min(
        candidate_indices,
        key=lambda idx: (
            _OUTCOME_RANK.get(audit_records[idx].outcome, 3),
            len(audit_records[idx].presented_record_ids),
        ),
    )
    return best_idx


def _audit_to_proposal_outcome(
    audit: _AuditRecord,
) -> ProposalOutcome:
    """Convert a frozen audit record to a ProposalOutcome for Layer 3."""
    outcome_str = audit.outcome

    # Map string outcome to enum.
    try:
        outcome_type = ProposalOutcomeType(outcome_str)
    except ValueError:
        outcome_type = ProposalOutcomeType.API_ERROR

    proposal = None
    if audit.proposal is not None and outcome_type == ProposalOutcomeType.PROPOSAL_VALID:
        try:
            proposal = MatchProposal(
                proposed_match_ids=list(audit.proposal.get("proposed_match_ids", [])),
                confidence=audit.proposal.get("confidence", 0.0),
                rationale=audit.proposal.get("rationale", ""),
            )
        except Exception:
            # Malformed proposal → treat as validation failure.
            outcome_type = ProposalOutcomeType.VALIDATION_FAILED

    return ProposalOutcome(
        outcome=outcome_type,
        proposal=proposal,
        presented_record_ids=audit.presented_record_ids,
        reason=audit.reason or f"Frozen artifact: {outcome_str}",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_frozen_l2_outcomes(
    data_dir: Path | str,
    all_records: List[NormalizedRecord],
    residual_record_ids: Tuple[str, ...],
) -> List[ProposalOutcome]:
    """Load Layer 2 outcomes from frozen audit artifacts for residual records.

    This function is ONLY called after the upload fingerprint has been
    verified against the frozen dataset manifest.  It must never be
    called for arbitrary uploads.

    Parameters
    ----------
    data_dir : Path
        Directory containing the Layer 2 audit JSONL files and residuals.csv.
    all_records : list
        All normalized records (needed for reconstruction context).
    residual_record_ids : tuple
        Record IDs identified as residuals by Layer 1.

    Returns
    -------
    list[ProposalOutcome]
        One ProposalOutcome per residual record, in the same order as
        ``residual_record_ids``.  Records that cannot be matched to an
        audit record receive an API_ERROR outcome (fail-closed).
    """
    data_dir = Path(data_dir)

    # Load audit records from the single canonical artifact.
    try:
        audit_records = _load_canonical_audit_records(data_dir)
    except (ValueError, FileNotFoundError, IOError) as exc:
        logger.warning("Cannot load canonical Layer 2 artifact: %s", exc)
        return [
            ProposalOutcome(
                outcome=ProposalOutcomeType.API_ERROR,
                proposal=None,
                presented_record_ids=(rid,),
                reason=f"Canonical Layer 2 artifact unavailable: {exc}",
            )
            for rid in residual_record_ids
        ]

    # Load residual scenarios to get member_record_ids for each residual.
    try:
        residual_scenarios = load_residuals(data_dir)
    except Exception as exc:
        logger.warning("Could not load residuals.csv: %s", exc)
        return [
            ProposalOutcome(
                outcome=ProposalOutcomeType.API_ERROR,
                proposal=None,
                presented_record_ids=(rid,),
                reason="Could not load residual scenarios.",
            )
            for rid in residual_record_ids
        ]

    # Build a mapping from record_id to the residual scenario that owns it.
    record_to_residual: Dict[str, Any] = {}
    for scenario in residual_scenarios:
        for mid in scenario.member_record_ids:
            record_to_residual[mid] = scenario

    # Canonical artifact records carry the ground-truth scenario id in
    # ``correlation_id`` (proven in the README's AI-recall methodology and
    # exercised against the real artifact by the test suite), so the record's
    # OWN scenario outcome can always be resolved when needed.
    audit_by_correlation: Dict[str, List[_AuditRecord]] = defaultdict(list)
    for audit in audit_records:
        audit_by_correlation[audit.correlation_id].append(audit)
    record_index = _build_record_to_audit_index(audit_records)

    # Routing-relevant outcomes keep the deterministic claim order: when a
    # residual scenario's best subset match has not yet been claimed, its
    # audit outcome is emitted once with the FULL presented record set and
    # claims those records (identical to Layer 3's documented first-wins
    # semantics, which produce the record-level routing composition in the
    # README).  Every LATER residual record of an already-claimed scenario
    # must still carry a truthful outcome object (one per residual record),
    # but presenting the full candidate set again would re-claim records
    # and change routing, so those occurrences receive their own scenario's
    # real outcome with the presented set trimmed to the scenario's own
    # members (which were already claimed).  No API_ERROR is ever invented
    # for a scenario whose audit record exists — provider failures in the
    # tallies are always genuine artifact outcomes.
    claimed_audits: Set[int] = set()

    outcomes: List[ProposalOutcome] = []
    matched_count = 0
    unmatched_count = 0

    for rid in residual_record_ids:
        scenario = record_to_residual.get(rid)
        if scenario is None:
            # This residual record is not part of any known residual scenario.
            # This shouldn't happen if the dataset matches, but fail closed.
            outcomes.append(
                ProposalOutcome(
                    outcome=ProposalOutcomeType.API_ERROR,
                    proposal=None,
                    presented_record_ids=(rid,),
                    reason="Record not found in residual scenarios.",
                )
            )
            unmatched_count += 1
            continue

        member_ids = scenario.member_record_ids
        best_idx = _find_audit_for_residual(
            member_ids, record_index, audit_records
        )

        if best_idx is not None and best_idx not in claimed_audits:
            claimed_audits.add(best_idx)
            outcomes.append(_audit_to_proposal_outcome(audit_records[best_idx]))
            matched_count += 1
            continue

        # This record's scenario audit was already claimed (either by this
        # scenario's first member, or by an earlier overlapping scenario's
        # outcome).  Emit the record's OWN scenario outcome — never an
        # invented provider failure — but restrict the presented set to the
        # scenario members, which are already claimed, so routing is
        # unaffected.
        own_audits = audit_by_correlation.get(scenario.scenario_id)
        if own_audits:
            own_outcome = _audit_to_proposal_outcome(own_audits[0])
            outcomes.append(
                ProposalOutcome(
                    outcome=own_outcome.outcome,
                    proposal=own_outcome.proposal,
                    presented_record_ids=member_ids,
                    reason=own_outcome.reason,
                    invalid_ids=own_outcome.invalid_ids,
                )
            )
            matched_count += 1
        else:
            # No audit record at all for this scenario → fail closed.
            outcomes.append(
                ProposalOutcome(
                    outcome=ProposalOutcomeType.API_ERROR,
                    proposal=None,
                    presented_record_ids=member_ids,
                    reason="No matching frozen audit record for residual scenario.",
                )
            )
            unmatched_count += 1

    logger.info(
        "Frozen Layer 2 mapping: %d matched, %d unmatched out of %d residuals",
        matched_count,
        unmatched_count,
        len(residual_record_ids),
    )

    return outcomes


def frozen_artifact_summary(data_dir: Path | str) -> Dict[str, Any]:
    """Scenario-level provenance summary of the pinned canonical Layer 2 artifact.

    Counts come from the canonical artifact itself (one audit record per
    residual scenario), so ``provider_failures`` here are GENUINE provider
    failures (API_ERROR / TIMEOUT) — never per-record replays.  This is the
    number the README reports (15 of 77) and the number the evaluation
    report must expose.

    Returns an error-keyed dict when the artifact cannot be loaded
    (fail-closed), mirroring ``load_frozen_l2_outcomes``.
    """
    data_dir = Path(data_dir)
    try:
        audit_records = _load_canonical_audit_records(data_dir)
    except (ValueError, FileNotFoundError, IOError) as exc:
        return {"error": f"Canonical Layer 2 artifact unavailable: {exc}"}

    outcome_counts: Dict[str, int] = {}
    for audit in audit_records:
        outcome_counts[audit.outcome] = outcome_counts.get(audit.outcome, 0) + 1

    return {
        "residual_scenarios": len(audit_records),
        "scenario_outcomes": outcome_counts,
        "successful_proposals": outcome_counts.get(
            ProposalOutcomeType.PROPOSAL_VALID.value, 0
        ),
        "no_proposal": outcome_counts.get(
            ProposalOutcomeType.NO_PROPOSAL.value, 0
        ),
        "provider_failures": (
            outcome_counts.get(ProposalOutcomeType.API_ERROR.value, 0)
            + outcome_counts.get(ProposalOutcomeType.TIMEOUT.value, 0)
        ),
        "granularity_note": (
            "Scenario-level tallies from the pinned canonical artifact (one "
            "audit record per residual scenario). Record-level routing "
            "composition for this batch is reported under "
            "layer3_routing_composition."
        ),
    }
