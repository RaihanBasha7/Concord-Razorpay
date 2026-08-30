"""
Day 4.5 — Local audit trail.

Every attempted scenario produces a structured, append-only audit record. The
audit record intentionally excludes:
  * API keys / secrets,
  * ground-truth labels (category, has_valid_relationship, is_true_exception),
  * evaluation metadata (synthetic descriptions, category-encoded scenario_id).

A neutral internal correlation ID is used instead of the category-encoded
scenario_id so the LLM-facing context and the audit log never leak scenario
category. If persistence fails, the failure is reported but never mutates the
reconciliation outcome it accompanies.

Artifact lifecycle (archive + atomic write):

When ``archive_existing=True`` and ``atomic_write=True``, the previous
completed artifact remains at the canonical path while the new run writes to
a ``.tmp`` sidecar file.  ``finalize()`` validates the expected record count,
then replaces the canonical artifact with the sidecar and archives the old
artifact to ``{stem}.{timestamp}.jsonl``.  If the process is interrupted
before ``finalize()`` completes, the canonical path still references the
previous completed artifact, so downstream consumers (evaluation harness)
continue to see valid data rather than falling back to mock mode.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from reconciliation.proposal import MatchProposal


@dataclass(frozen=True)
class AuditRecord:
    correlation_id: str
    timestamp: str
    presented_record_ids: List[str]
    outcome: str
    proposal: Optional[dict]
    confidence: Optional[float]
    reason: str
    dataset_fingerprint: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))


class Auditor:
    """
    Writes audit records as JSONL. Persistence failures are caught and reported
    via the return value; they do not raise into the reconciliation flow.

    Lifecycle modes (all opt-in, fully backward-compatible):

    * ``archive_existing=True``: when combined with ``atomic_write``, the
      previous completed artifact is preserved at the canonical path until a
      new artifact is fully validated.  On successful ``finalize()``, the old
      artifact is renamed to ``{stem}.{iso-timestamp}.jsonl`` after the new
      artifact replaces it.
    * ``atomic_write=True``: records are written to a ``.tmp`` sidecar file.
      Call ``finalize()`` after all records are written to atomically promote
      the sidecar to the final artifact path.  If the process crashes before
      ``finalize()``, the canonical path still holds the previous completed
      artifact (if any) — downstream evaluation is not disrupted.
    * ``expected_record_count`` (optional): when set, ``finalize()`` validates
      that exactly this many records were written and raises ``ValueError``
      on mismatch, preventing partial artifacts from being treated as complete.
    * ``resume=True``: when combined with ``atomic_write``, an existing
      ``.tmp`` sidecar is preserved instead of truncated.  Valid records are
      counted and ``write_count`` starts from that number so that
      ``finalize()`` validates the combined total.  Corrupt or partial lines
      (from a hard kill mid-write) are silently skipped.  The caller should
      use ``resume_count`` to determine how many scenarios to skip.
    """

    def __init__(
        self,
        path: Optional[Path | str] = None,
        *,
        archive_existing: bool = False,
        atomic_write: bool = False,
        expected_record_count: Optional[int] = None,
        resume: bool = False,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._archive_existing = archive_existing
        self._atomic_write = atomic_write
        self._expected_record_count = expected_record_count
        self._write_count = 0
        self._resume_count = 0  # records recovered from prior sidecar
        self._finalized = False
        self._active_path: Optional[Path] = None
        # Record whether a previous artifact existed before this run,
        # so finalize() only archives when there was actually something
        # to displace.
        self._previous_artifact_existed = (
            self._path is not None and self._path.exists()
        )

        if self._path is not None and self._atomic_write:
            self._active_path = Path(str(self._path) + ".tmp")
            if (
                resume
                and self._active_path.exists()
                and self._active_path.stat().st_size > 0
            ):
                # Count valid records already in the sidecar so we can
                # resume without re-calling the API for scenarios that
                # already succeeded.  Corrupt/partial lines from a hard
                # kill are silently skipped.
                self._resume_count = self._count_valid_records(
                    self._active_path
                )
                self._write_count = self._resume_count
                # Do NOT truncate — open for append.
            else:
                # Start fresh for atomic write — truncate any leftover sidecar.
                try:
                    self._active_path.write_text("", encoding="utf-8")
                except OSError:
                    pass
        else:
            self._active_path = self._path

    @staticmethod
    def _count_valid_records(path: Path) -> int:
        """Count complete, parseable JSON records in a JSONL file.

        Corrupt or partial lines (e.g. from a hard kill mid-write) are
        silently skipped so that a resume only replays genuinely completed
        scenarios.
        """
        count = 0
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    count += 1
        except OSError:
            pass
        return count

    def _archive_existing_artifact(self) -> None:
        """Rename an existing artifact to a timestamped backup."""
        if self._path is None or not self._path.exists():
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
        archive_path = self._path.with_suffix(f".{timestamp}.jsonl")
        # Handle unlikely collision by appending a counter.
        counter = 1
        while archive_path.exists():
            archive_path = self._path.with_suffix(
                f".{timestamp}.{counter}.jsonl"
            )
            counter += 1
        self._path.rename(archive_path)

    def write(self, record: AuditRecord) -> bool:
        """Return True on successful write, False if persistence failed."""
        if self._active_path is None:
            self._write_count += 1
            return True
        try:
            with self._active_path.open("a", encoding="utf-8") as handle:
                handle.write(record.to_json() + "\n")
            self._write_count += 1
            return True
        except OSError:
            return False

    def finalize(self) -> None:
        """Promote the atomic sidecar to the final artifact path.

        On success: if ``archive_existing`` is set and a previous artifact
        exists at the canonical path, the old artifact is archived to a
        timestamped backup *after* the new artifact is in place.

        Raises ``ValueError`` if ``expected_record_count`` was set and the
        actual count does not match.  Idempotent: safe to call more than once.
        No-op when ``atomic_write`` is ``False`` or ``path`` is ``None``.
        """
        if self._finalized:
            return
        if self._path is None or not self._atomic_write:
            return
        if self._expected_record_count is not None:
            if self._write_count != self._expected_record_count:
                # Remove the incomplete sidecar rather than leaving it as a
                # false-complete artifact.
                try:
                    self._active_path.unlink(missing_ok=True)
                except OSError:
                    pass
                if self._resume_count:
                    raise ValueError(
                        f"Audit artifact record count mismatch: "
                        f"expected {self._expected_record_count}, "
                        f"wrote {self._write_count} "
                        f"({self._resume_count} recovered + "
                        f"{self._write_count - self._resume_count} new). "
                        f"Incomplete artifact removed."
                    )
                raise ValueError(
                    f"Audit artifact record count mismatch: "
                    f"expected {self._expected_record_count}, "
                    f"wrote {self._write_count}. "
                    f"Incomplete artifact removed."
                )
        # Archive the previous artifact first (while it still exists at
        # the canonical path), then promote the sidecar.
        if self._archive_existing and self._previous_artifact_existed:
            self._archive_existing_artifact()
        # Promote sidecar → canonical.  os.replace is cross-platform
        # atomic overwrite (unlike Path.rename which fails on Windows
        # when the target already exists).
        os.replace(self._active_path, self._path)
        self._finalized = True

    def cleanup_sidecar(self) -> None:
        """Remove the ``.tmp`` sidecar without promoting it.

        Use this when a run is aborted and the sidecar should be discarded.
        No-op if no sidecar exists or ``atomic_write`` is ``False``.
        """
        if self._active_path is None or not self._atomic_write:
            return
        try:
            self._active_path.unlink(missing_ok=True)
        except OSError:
            pass

    @property
    def path(self) -> Optional[Path]:
        return self._path

    @property
    def write_count(self) -> int:
        return self._write_count

    @property
    def resume_count(self) -> int:
        """Number of valid records recovered from an existing sidecar on resume."""
        return self._resume_count


def make_audit_record(
    *,
    correlation_id: str,
    presented_record_ids: List[str],
    outcome: str,
    proposal: Optional[MatchProposal],
    reason: str,
    dataset_fingerprint: Optional[str] = None,
) -> AuditRecord:
    """Build a safe audit record from a resolved outcome.

    ``dataset_fingerprint`` is a content hash of the frozen evaluation dataset
    the audit record was produced against. It carries no ground-truth labels or
    secrets and lets downstream evaluation verify the artifact matches the
    dataset in use.
    """
    confidence = proposal.confidence if proposal is not None else None
    proposal_payload = (
        proposal.model_dump() if proposal is not None else None
    )
    return AuditRecord(
        correlation_id=correlation_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        presented_record_ids=list(presented_record_ids),
        outcome=outcome,
        proposal=proposal_payload,
        confidence=confidence,
        reason=reason,
        dataset_fingerprint=dataset_fingerprint,
    )
