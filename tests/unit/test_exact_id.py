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


class TestExactIdentifierRule:
    def test_two_records_different_sources_same_hint_match(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                order_id_hint="ORD-X",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-X",
                amount_paise=100_000,
                date=date(2026, 8, 26),
            ),
        ]
        config = MatcherConfig(amount_tolerance_paise=0, date_window_days=0)
        result = reconcile(records, config)
        assert len(result.decisions) == 1
        decision = result.decisions[0]
        assert decision.decision_id == "EXACT_ID-A-B"
        assert decision.member_record_ids == ("A", "B")
        assert decision.resolution_layer == ResolutionLayer.LAYER_1
        assert decision.rule_or_rationale == MatchRule.EXACT_ID.value
        assert decision.confidence == 1.0
        assert result.residual_record_ids == ()

    def test_two_records_same_source_same_hint_remain_unresolved(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                order_id_hint="ORD-X",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-2",
                order_id_hint="ORD-X",
                amount_paise=100_000,
                date=date(2026, 8, 26),
            ),
        ]
        config = MatcherConfig(amount_tolerance_paise=0, date_window_days=0)
        result = reconcile(records, config)
        assert len(result.decisions) == 0
        assert len(result.residual_record_ids) == 2
        assert set(result.residual_record_ids) == {"A", "B"}

    def test_three_records_same_hint_remain_unresolved(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                order_id_hint="ORD-X",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-X",
                amount_paise=100_000,
                date=date(2026, 8, 26),
            ),
            make_record(
                record_id="C",
                source_type=SourceType.BANK,
                source_native_id="BNK-2",
                order_id_hint="ORD-X",
                amount_paise=100_000,
                date=date(2026, 8, 27),
            ),
        ]
        config = MatcherConfig(amount_tolerance_paise=0, date_window_days=0)
        result = reconcile(records, config)
        assert len(result.decisions) == 0
        assert len(result.residual_record_ids) == 3
        assert set(result.residual_record_ids) == {"A", "B", "C"}

    def test_records_without_hint_remain_unresolved(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                order_id_hint=None,
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint=None,
                amount_paise=100_000,
                date=date(2026, 8, 26),
            ),
        ]
        config = MatcherConfig(amount_tolerance_paise=0, date_window_days=0)
        result = reconcile(records, config)
        assert len(result.decisions) == 0
        assert len(result.residual_record_ids) == 2
        assert set(result.residual_record_ids) == {"A", "B"}

    def test_mixed_matched_and_unmatched_records(self):
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
        config = MatcherConfig(amount_tolerance_paise=0, date_window_days=0)
        result = reconcile(records, config)
        assert len(result.decisions) == 1
        assert result.decisions[0].member_record_ids == ("A", "B")
        assert result.residual_record_ids == ("C",)
