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

    residual_record_ids is a multiset, not a set: a record_id may appear
    more than once if multiple physical records share a colliding id
    (e.g., true duplicates), and this repetition is intentional and must
    be preserved by any code that consumes this field, not deduplicated.
    """

    decisions: Tuple[ReconciliationDecision, ...]
    residual_record_ids: Tuple[str, ...]
