from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from types import MappingProxyType
from typing import Any

from reconciliation.domain.models import NormalizedRecord, SourceType


_SUPPORTED_SETTLEMENT_AMOUNT_KEYS = ("gross_amount", "amount")
_SUPPORTED_SETTLEMENT_DATE_KEYS = ("settlement_date",)
_SUPPORTED_SETTLEMENT_ID_KEYS = ("settlement_id",)
_SUPPORTED_SETTLEMENT_ORDER_KEYS = ("order_id",)

_SUPPORTED_BANK_AMOUNT_KEYS = ("credit_amount", "amount")
_SUPPORTED_BANK_DATE_KEYS = ("value_date",)
_SUPPORTED_BANK_ID_KEYS = ("bank_utr",)

_SUPPORTED_LEDGER_AMOUNT_KEYS = ("gross_amount", "amount")
_SUPPORTED_LEDGER_DATE_KEYS = ("transaction_date",)
_SUPPORTED_LEDGER_ID_KEYS = ("order_id",)
_SUPPORTED_LEDGER_ORDER_KEYS = ("order_id",)

# SHA-256 truncated to 12 hex chars gives 48 bits of entropy.
# For an MVP dataset this collision risk is negligible.
# If the dataset grows to millions of rows, revisit this assumption.
_RECORD_ID_HASH_LENGTH = 12


class NormalizationError(ValueError):
    """Raised when a raw source row cannot be normalized."""


def normalize_record(
    raw_row: dict[str, Any],
    source_type: SourceType | str,
    record_id: str | None = None,
) -> NormalizedRecord:
    """
    Convert a raw source row dictionary into a NormalizedRecord.

    Amount handling assumptions:
      - All supported amount fields (gross_amount, credit_amount, amount)
        are interpreted as major currency units (rupees), regardless of
        whether the Python value is int, Decimal, or str.
      - The value is converted exactly to integer paise by multiplying by 100.
      - Float values are rejected because float arithmetic is unsafe for money.
      - Values with more than two decimal places are rejected to prevent
        silent precision loss.

    Supported raw field mappings are explicit per source type. Any unknown
    schema will raise NormalizationError rather than being silently guessed.
    """
    if isinstance(source_type, str):
        source_type = SourceType(source_type)

    raw_payload = MappingProxyType(dict(raw_row))

    if source_type == SourceType.SETTLEMENT:
        source_native_id = _require_field(raw_payload, _SUPPORTED_SETTLEMENT_ID_KEYS, "settlement_id")
        order_id_hint = _optional_field(raw_payload, _SUPPORTED_SETTLEMENT_ORDER_KEYS)
        amount_paise = _convert_amount(raw_payload, _SUPPORTED_SETTLEMENT_AMOUNT_KEYS)
        record_date = _convert_date(raw_payload, _SUPPORTED_SETTLEMENT_DATE_KEYS)
        narration = raw_payload.get("narration")
    elif source_type == SourceType.BANK:
        source_native_id = _require_field(raw_payload, _SUPPORTED_BANK_ID_KEYS, "bank_utr")
        order_id_hint = _optional_field(raw_payload, ("order_id",))
        amount_paise = _convert_amount(raw_payload, _SUPPORTED_BANK_AMOUNT_KEYS)
        record_date = _convert_date(raw_payload, _SUPPORTED_BANK_DATE_KEYS)
        narration = raw_payload.get("narration")
    elif source_type == SourceType.LEDGER:
        source_native_id = _require_field(raw_payload, _SUPPORTED_LEDGER_ID_KEYS, "order_id")
        order_id_hint = _optional_field(raw_payload, _SUPPORTED_LEDGER_ORDER_KEYS)
        amount_paise = _convert_amount(raw_payload, _SUPPORTED_LEDGER_AMOUNT_KEYS)
        record_date = _convert_date(raw_payload, _SUPPORTED_LEDGER_DATE_KEYS)
        narration = raw_payload.get("narration")
    else:
        raise NormalizationError(f"Unsupported source type: {source_type}")

    if record_id is None:
        record_id = _generate_record_id(source_type, source_native_id, order_id_hint, amount_paise, record_date)

    return NormalizedRecord(
        record_id=record_id,
        source_type=source_type,
        source_native_id=source_native_id,
        order_id_hint=order_id_hint,
        amount_paise=amount_paise,
        date=record_date,
        narration=narration,
        raw_payload=raw_payload,
    )


def _require_field(raw_row: Mapping[str, Any], keys: tuple[str, ...], label: str) -> str:
    for key in keys:
        value = raw_row.get(key)
        if value is not None:
            return str(value)
    raise NormalizationError(f"Missing required field for {label} in raw row.")


def _optional_field(raw_row: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = raw_row.get(key)
        if value is not None:
            return str(value)
    return None


def _convert_amount(raw_row: Mapping[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        if key not in raw_row:
            continue
        value = raw_row[key]
        if isinstance(value, bool):
            raise NormalizationError("Boolean amounts are not supported.")
        if isinstance(value, float):
            raise NormalizationError(
                "Float amounts are not supported because float arithmetic is unsafe for money."
            )
        if isinstance(value, int):
            decimal_value = Decimal(value)
        elif isinstance(value, Decimal):
            decimal_value = value
        elif isinstance(value, str):
            try:
                decimal_value = Decimal(value)
            except InvalidOperation as exc:
                raise NormalizationError(f"Invalid amount string: {value!r}") from exc
        else:
            raise NormalizationError(f"Unsupported amount type: {type(value).__name__}")

        if decimal_value != decimal_value.quantize(Decimal("0.01")):
            raise NormalizationError(
                f"Amount has more than 2 decimal places: {value!r}"
            )
        return int(decimal_value * 100)
    raise NormalizationError("Missing amount field in raw row.")


def _convert_date(raw_row: Mapping[str, Any], keys: tuple[str, ...]) -> "date":
    from datetime import date as date_type

    for key in keys:
        if key not in raw_row:
            continue
        value = raw_row[key]
        if isinstance(value, date_type):
            return value
        if isinstance(value, str):
            try:
                return date_type.fromisoformat(value)
            except ValueError as exc:
                raise NormalizationError(f"Invalid date string: {value!r}") from exc
        raise NormalizationError(f"Unsupported date type: {type(value).__name__}")
    raise NormalizationError("Missing date field in raw row.")


def _generate_record_id(
    source_type: SourceType,
    source_native_id: str,
    order_id_hint: str | None,
    amount_paise: int,
    record_date: "date",
) -> str:
    parts = [
        source_type.value,
        source_native_id,
        order_id_hint or "NA",
        str(amount_paise),
        record_date.isoformat(),
    ]
    digest = sha256("-".join(parts).encode("utf-8")).hexdigest()[:_RECORD_ID_HASH_LENGTH]
    return f"{source_type.value}-{digest}"
