from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from reconciliation.domain.models import ReconciliationDecision


@dataclass(frozen=True)
class ReconciliationResult:
    """
    The public result of a reconciliation run.

    Records that are not part of any accepted decision are explicitly
    surfaced as residual_record_ids so nothing disappears silently.
    """

    decisions: Tuple[ReconciliationDecision, ...]
    residual_record_ids: Tuple[str, ...]
