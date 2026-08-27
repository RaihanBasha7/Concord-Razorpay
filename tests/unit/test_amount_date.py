from __future__ import annotations

from datetime import date

from reconciliation.domain.models import (
    MatchRule,
    ResolutionLayer,
    SourceType,
)
from reconciliation.matcher import reconcile
from tests.conftest import MatcherConfig, make_record


class TestAmountDateRule:
    def test_mutually_unique_candidates_match(self):
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
                amount_paise=100_050,
                date=date(2026, 8, 26),
            ),
        ]
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
        result = reconcile(records, config)
        assert len(result.decisions) == 1
        decision = result.decisions[0]
        assert decision.decision_id == "AMOUNT_DATE-A-B"
        assert decision.member_record_ids == ("A", "B")
        assert decision.resolution_layer == ResolutionLayer.LAYER_1
        assert decision.rule_or_rationale == MatchRule.AMOUNT_AND_DATE.value
        assert decision.confidence == 1.0
        assert result.residual_record_ids == ()

    def test_one_record_with_multiple_candidates_no_match(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                order_id_hint=None,
                amount_paise=75_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint=None,
                amount_paise=75_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="C",
                source_type=SourceType.BANK,
                source_native_id="BNK-2",
                order_id_hint=None,
                amount_paise=75_000,
                date=date(2026, 8, 26),
            ),
        ]
        config = MatcherConfig(amount_tolerance_paise=0, date_window_days=2)
        result = reconcile(records, config)
        assert len(result.decisions) == 0
        assert len(result.residual_record_ids) == 3
        assert set(result.residual_record_ids) == {"A", "B", "C"}

    def test_amount_outside_tolerance_no_match(self):
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
                amount_paise=100_200,
                date=date(2026, 8, 26),
            ),
        ]
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
        result = reconcile(records, config)
        assert len(result.decisions) == 0
        assert len(result.residual_record_ids) == 2
        assert set(result.residual_record_ids) == {"A", "B"}

    def test_date_outside_window_no_match(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                order_id_hint=None,
                amount_paise=50_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint=None,
                amount_paise=50_000,
                date=date(2026, 8, 29),
            ),
        ]
        config = MatcherConfig(amount_tolerance_paise=0, date_window_days=2)
        result = reconcile(records, config)
        assert len(result.decisions) == 0
        assert len(result.residual_record_ids) == 2
        assert set(result.residual_record_ids) == {"A", "B"}

    def test_same_source_records_no_match(self):
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
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-2",
                order_id_hint=None,
                amount_paise=100_000,
                date=date(2026, 8, 26),
            ),
        ]
        config = MatcherConfig(amount_tolerance_paise=0, date_window_days=2)
        result = reconcile(records, config)
        assert len(result.decisions) == 0
        assert len(result.residual_record_ids) == 2
        assert set(result.residual_record_ids) == {"A", "B"}

    def test_exact_id_matched_records_not_reconsidered(self):
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
        assert result.decisions[0].rule_or_rationale == MatchRule.EXACT_ID.value
        assert result.residual_record_ids == ("C",)

    def test_amount_difference_exactly_at_tolerance_matches(self):
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
                amount_paise=100_100,
                date=date(2026, 8, 26),
            ),
        ]
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
        result = reconcile(records, config)
        assert len(result.decisions) == 1
        assert result.decisions[0].member_record_ids == ("A", "B")
        assert result.residual_record_ids == ()

    def test_date_difference_exactly_at_window_matches(self):
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
                date=date(2026, 8, 27),
            ),
        ]
        config = MatcherConfig(amount_tolerance_paise=0, date_window_days=2)
        result = reconcile(records, config)
        assert len(result.decisions) == 1
        assert result.decisions[0].member_record_ids == ("A", "B")
        assert result.residual_record_ids == ()
