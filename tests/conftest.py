from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from reconciliation.domain.models import NormalizedRecord, SourceType
from reconciliation.matcher_config import MatcherConfig


def make_record(
    *,
    record_id: str,
    source_type: SourceType | str,
    source_native_id: str,
    order_id_hint: str | None = None,
    amount_paise: int,
    date: date | str,
    narration: str | None = None,
    raw_payload: Any = "",
) -> NormalizedRecord:
    """
    Reusable helper for constructing NormalizedRecord instances with
    minimal repetition. Accepts strings for enums and dates to keep
    test declarations concise.
    """
    if isinstance(source_type, str):
        source_type = SourceType(source_type)
    if isinstance(date, str):
        date = date.fromisoformat(date)

    return NormalizedRecord(
        record_id=record_id,
        source_type=source_type,
        source_native_id=source_native_id,
        order_id_hint=order_id_hint,
        amount_paise=amount_paise,
        date=date,
        narration=narration,
        raw_payload=raw_payload,
    )
