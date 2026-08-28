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
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

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

    def to_json(self) -> str:
        return json.dumps(asdict(self))


class Auditor:
    """
    Writes audit records as JSONL. Persistence failures are caught and reported
    via the return value; they do not raise into the reconciliation flow.
    """

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self._path = Path(path) if path is not None else None

    def write(self, record: AuditRecord) -> bool:
        """Return True on successful write, False if persistence failed."""
        if self._path is None:
            return True
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(record.to_json() + "\n")
            return True
        except OSError:
            return False

    @property
    def path(self) -> Optional[Path]:
        return self._path


def make_audit_record(
    *,
    correlation_id: str,
    presented_record_ids: List[str],
    outcome: str,
    proposal: Optional[MatchProposal],
    reason: str,
) -> AuditRecord:
    """Build a safe audit record from a resolved outcome."""
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
    )
