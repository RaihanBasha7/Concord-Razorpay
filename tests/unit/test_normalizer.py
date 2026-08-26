from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from reconciliation.domain.models import SourceType
from reconciliation.normalizer import NormalizationError, normalize_record


class TestSettlementNormalization:
    def test_minimal_settlement_row(self):
        row = {
            "settlement_id": "SET-1001",
            "order_id": "ORD-500",
            "gross_amount": 1000,
            "settlement_date": "2026-08-25",
            "narration": "Payment",
        }
        record = normalize_record(row, SourceType.SETTLEMENT)
        assert record.source_type == SourceType.SETTLEMENT
        assert record.source_native_id == "SET-1001"
        assert record.order_id_hint == "ORD-500"
        assert record.amount_paise == 100000
        assert record.date == date(2026, 8, 25)
        assert record.narration == "Payment"
        assert record.raw_payload == row

    def test_settlement_amount_as_string(self):
        row = {
            "settlement_id": "SET-1001",
            "order_id": "ORD-500",
            "gross_amount": "1000.50",
            "settlement_date": "2026-08-25",
        }
        record = normalize_record(row, SourceType.SETTLEMENT)
        assert record.amount_paise == 100050

    def test_settlement_amount_as_decimal(self):
        row = {
            "settlement_id": "SET-1001",
            "order_id": "ORD-500",
            "gross_amount": Decimal("1000"),
            "settlement_date": "2026-08-25",
        }
        record = normalize_record(row, SourceType.SETTLEMENT)
        assert record.amount_paise == 100000

    def test_settlement_amount_float_rejected(self):
        row = {
            "settlement_id": "SET-1001",
            "order_id": "ORD-500",
            "gross_amount": 1000.50,
            "settlement_date": "2026-08-25",
        }
        with pytest.raises(NormalizationError, match="Float amounts are not supported"):
            normalize_record(row, SourceType.SETTLEMENT)

    def test_settlement_missing_required_field_raises(self):
        row = {
            "order_id": "ORD-500",
            "gross_amount": 1000,
            "settlement_date": "2026-08-25",
        }
        with pytest.raises(NormalizationError, match="Missing required field for settlement_id"):
            normalize_record(row, SourceType.SETTLEMENT)

    def test_settlement_invalid_date_raises(self):
        row = {
            "settlement_id": "SET-1001",
            "order_id": "ORD-500",
            "gross_amount": 1000,
            "settlement_date": "not-a-date",
        }
        with pytest.raises(NormalizationError, match="Invalid date string"):
            normalize_record(row, SourceType.SETTLEMENT)

    def test_settlement_missing_date_raises(self):
        row = {
            "settlement_id": "SET-1001",
            "order_id": "ORD-500",
            "gross_amount": 1000,
        }
        with pytest.raises(NormalizationError, match="Missing date field"):
            normalize_record(row, SourceType.SETTLEMENT)


class TestBankNormalization:
    def test_minimal_bank_row(self):
        row = {
            "bank_utr": "BNK-2001",
            "credit_amount": 50000,
            "value_date": "2026-08-26",
            "narration": "Bank credit",
        }
        record = normalize_record(row, SourceType.BANK)
        assert record.source_type == SourceType.BANK
        assert record.source_native_id == "BNK-2001"
        assert record.order_id_hint is None
        assert record.amount_paise == 5000000
        assert record.date == date(2026, 8, 26)
        assert record.narration == "Bank credit"
        assert record.raw_payload == row

    def test_bank_with_order_id(self):
        row = {
            "bank_utr": "BNK-2001",
            "order_id": "ORD-500",
            "credit_amount": "1000.50",
            "value_date": "2026-08-26",
        }
        record = normalize_record(row, SourceType.BANK)
        assert record.order_id_hint == "ORD-500"
        assert record.amount_paise == 100050

    def test_bank_missing_required_field_raises(self):
        row = {
            "credit_amount": 50000,
            "value_date": "2026-08-26",
        }
        with pytest.raises(NormalizationError, match="Missing required field for bank_utr"):
            normalize_record(row, SourceType.BANK)


class TestLedgerNormalization:
    def test_minimal_ledger_row(self):
        row = {
            "order_id": "ORD-500",
            "gross_amount": 100000,
            "transaction_date": "2026-08-25",
            "narration": "Ledger entry",
        }
        record = normalize_record(row, SourceType.LEDGER)
        assert record.source_type == SourceType.LEDGER
        assert record.source_native_id == "ORD-500"
        assert record.order_id_hint == "ORD-500"
        assert record.amount_paise == 10000000
        assert record.date == date(2026, 8, 25)
        assert record.narration == "Ledger entry"
        assert record.raw_payload == row

    def test_ledger_amount_as_string(self):
        row = {
            "order_id": "ORD-500",
            "gross_amount": "2000",
            "transaction_date": "2026-08-25",
        }
        record = normalize_record(row, SourceType.LEDGER)
        assert record.amount_paise == 200000

    def test_ledger_missing_required_field_raises(self):
        row = {
            "gross_amount": 100000,
            "transaction_date": "2026-08-25",
        }
        with pytest.raises(NormalizationError, match="Missing required field for order_id"):
            normalize_record(row, SourceType.LEDGER)


class TestAmountConsistency:
    def test_equivalent_int_str_decimal_normalize_consistently(self):
        base_row = {
            "settlement_id": "SET-1",
            "order_id": "ORD-1",
            "settlement_date": "2026-08-25",
        }
        int_row = {**base_row, "gross_amount": 1000}
        str_row = {**base_row, "gross_amount": "1000"}
        dec_row = {**base_row, "gross_amount": Decimal("1000")}
        assert normalize_record(int_row, SourceType.SETTLEMENT).amount_paise == 100000
        assert normalize_record(str_row, SourceType.SETTLEMENT).amount_paise == 100000
        assert normalize_record(dec_row, SourceType.SETTLEMENT).amount_paise == 100000

    def test_amount_with_three_decimal_places_rejected(self):
        row = {
            "settlement_id": "SET-1",
            "order_id": "ORD-1",
            "gross_amount": "1000.123",
            "settlement_date": "2026-08-25",
        }
        with pytest.raises(NormalizationError, match="more than 2 decimal places"):
            normalize_record(row, SourceType.SETTLEMENT)

    def test_amount_with_two_decimal_places_accepted(self):
        row = {
            "settlement_id": "SET-1",
            "order_id": "ORD-1",
            "gross_amount": "1000.50",
            "settlement_date": "2026-08-25",
        }
        record = normalize_record(row, SourceType.SETTLEMENT)
        assert record.amount_paise == 100050


class TestDeterministicRecordIdGeneration:
    def test_explicit_record_id_is_used(self):
        row = {
            "settlement_id": "SET-1001",
            "order_id": "ORD-500",
            "gross_amount": 1000,
            "settlement_date": "2026-08-25",
        }
        record = normalize_record(row, SourceType.SETTLEMENT, record_id="custom-id")
        assert record.record_id == "custom-id"

    def test_generated_record_id_is_stable(self):
        row = {
            "settlement_id": "SET-1001",
            "order_id": "ORD-500",
            "gross_amount": 1000,
            "settlement_date": "2026-08-25",
        }
        first = normalize_record(row, SourceType.SETTLEMENT)
        second = normalize_record(row, SourceType.SETTLEMENT)
        assert first.record_id == second.record_id
        assert first.record_id.startswith("SETTLEMENT-")

    def test_different_rows_produce_different_record_ids(self):
        row1 = {
            "settlement_id": "SET-1001",
            "order_id": "ORD-500",
            "gross_amount": 1000,
            "settlement_date": "2026-08-25",
        }
        row2 = {
            "settlement_id": "SET-1002",
            "order_id": "ORD-500",
            "gross_amount": 1000,
            "settlement_date": "2026-08-25",
        }
        assert normalize_record(row1, SourceType.SETTLEMENT).record_id != normalize_record(row2, SourceType.SETTLEMENT).record_id


class TestRawPayloadImmutability:
    def test_raw_payload_is_read_only(self):
        row = {
            "settlement_id": "SET-1001",
            "order_id": "ORD-500",
            "gross_amount": 1000,
            "settlement_date": "2026-08-25",
        }
        record = normalize_record(row, SourceType.SETTLEMENT)
        with pytest.raises(TypeError):
            record.raw_payload["settlement_id"] = "MUTATED"

    def test_raw_payload_preserves_original_values(self):
        row = {
            "settlement_id": "SET-1001",
            "order_id": "ORD-500",
            "gross_amount": 1000,
            "settlement_date": "2026-08-25",
            "extra_field": "preserved",
        }
        record = normalize_record(row, SourceType.SETTLEMENT)
        assert record.raw_payload["extra_field"] == "preserved"
        assert record.raw_payload["gross_amount"] == 1000
