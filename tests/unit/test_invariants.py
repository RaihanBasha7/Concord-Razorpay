from __future__ import annotations

from datetime import date

import pytest

from reconciliation.domain.models import (
    MatchRule,
    ReconciliationDecision,
    ResolutionLayer,
    SourceType,
)
from reconciliation.matcher import reconcile
from tests.conftest import MatcherConfig, make_record


class TestReconciliationInvariants:
    def test_empty_input_returns_empty_result(self):
        result = reconcile([], MatcherConfig(0, 0))
        assert result.decisions == ()
        assert result.residual_record_ids == ()

    def test_duplicate_record_ids_raise_value_error(self):
        records = [
            make_record(
                record_id="X",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                order_id_hint="ORD-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="X",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=100_000,
                date=date(2026, 8, 26),
            ),
        ]
        with pytest.raises(ValueError, match="record_id values must be unique"):
            reconcile(records, MatcherConfig(0, 0))

    def test_multiple_independent_exact_id_matches_all_returned(self):
        records = [
            make_record(
                record_id="A1",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                order_id_hint="ORD-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B1",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=100_000,
                date=date(2026, 8, 26),
            ),
            make_record(
                record_id="A2",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-2",
                order_id_hint="ORD-2",
                amount_paise=200_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B2",
                source_type=SourceType.BANK,
                source_native_id="BNK-2",
                order_id_hint="ORD-2",
                amount_paise=200_000,
                date=date(2026, 8, 26),
            ),
        ]
        result = reconcile(records, MatcherConfig(0, 0))
        assert len(result.decisions) == 2
        decision_ids = {d.decision_id for d in result.decisions}
        assert decision_ids == {"EXACT_ID-A1-B1", "EXACT_ID-A2-B2"}
        assert result.residual_record_ids == ()

    def test_multiple_independent_amount_date_matches_all_returned(self):
        records = [
            make_record(
                record_id="A1",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                order_id_hint=None,
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B1",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint=None,
                amount_paise=100_050,
                date=date(2026, 8, 26),
            ),
            make_record(
                record_id="A2",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-2",
                order_id_hint=None,
                amount_paise=200_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B2",
                source_type=SourceType.BANK,
                source_native_id="BNK-2",
                order_id_hint=None,
                amount_paise=200_050,
                date=date(2026, 8, 26),
            ),
        ]
        result = reconcile(records, MatcherConfig(100, 2))
        assert len(result.decisions) == 2
        decision_ids = {d.decision_id for d in result.decisions}
        assert decision_ids == {"AMOUNT_DATE-A1-B1", "AMOUNT_DATE-A2-B2"}
        assert result.residual_record_ids == ()

    def test_every_record_id_has_exactly_one_final_outcome(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                order_id_hint="ORD-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=100_000,
                date=date(2026, 8, 26),
            ),
            make_record(
                record_id="C",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-2",
                order_id_hint=None,
                amount_paise=50_000,
                date=date(2026, 8, 25),
            ),
        ]
        result = reconcile(records, MatcherConfig(0, 0))

        decision_member_ids = set()
        for decision in result.decisions:
            decision_member_ids.update(decision.member_record_ids)

        all_record_ids = {r.record_id for r in records}
        assert decision_member_ids.union(set(result.residual_record_ids)) == all_record_ids
        assert decision_member_ids.isdisjoint(set(result.residual_record_ids))

    def test_permuted_input_produces_equivalent_ordered_output(self):
        base_records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                order_id_hint="ORD-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=100_000,
                date=date(2026, 8, 26),
            ),
        ]
        permuted_records = list(reversed(base_records))

        base_result = reconcile(base_records, MatcherConfig(0, 0))
        permuted_result = reconcile(permuted_records, MatcherConfig(0, 0))

        assert base_result == permuted_result
