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


# ---------------------------------------------------------------------------
# Scenario data helpers — keep each scenario isolated and readable
# ---------------------------------------------------------------------------


def _scenario_1_unique_exact_id():
    records = [
        make_record(
            record_id="A",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-1001",
            order_id_hint="ORD-500",
            amount_paise=100_000,
            date=date(2026, 8, 25),
        ),
        make_record(
            record_id="B",
            source_type=SourceType.BANK,
            source_native_id="BNK-2001",
            order_id_hint="ORD-500",
            amount_paise=100_000,
            date=date(2026, 8, 26),
        ),
    ]
    config = MatcherConfig(amount_tolerance_paise=0, date_window_days=0)
    return records, config


def _scenario_2_ambiguous_exact_id():
    records = [
        make_record(
            record_id="A",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-1001",
            order_id_hint="ORD-500",
            amount_paise=100_000,
            date=date(2026, 8, 25),
        ),
        make_record(
            record_id="B",
            source_type=SourceType.BANK,
            source_native_id="BNK-2001",
            order_id_hint="ORD-500",
            amount_paise=100_000,
            date=date(2026, 8, 26),
        ),
        make_record(
            record_id="C",
            source_type=SourceType.BANK,
            source_native_id="BNK-2002",
            order_id_hint="ORD-500",
            amount_paise=100_000,
            date=date(2026, 8, 27),
        ),
    ]
    config = MatcherConfig(amount_tolerance_paise=0, date_window_days=0)
    return records, config


def _scenario_3_amount_date_window():
    records = [
        make_record(
            record_id="A",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-1002",
            order_id_hint=None,
            amount_paise=50_000,
            date=date(2026, 8, 25),
        ),
        make_record(
            record_id="B",
            source_type=SourceType.BANK,
            source_native_id="BNK-2003",
            order_id_hint=None,
            amount_paise=50_000,
            date=date(2026, 8, 26),
        ),
    ]
    config = MatcherConfig(amount_tolerance_paise=0, date_window_days=2)
    return records, config


def _scenario_4_ambiguous_amount_candidates():
    records = [
        make_record(
            record_id="A",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-1003",
            order_id_hint=None,
            amount_paise=75_000,
            date=date(2026, 8, 25),
        ),
        make_record(
            record_id="B",
            source_type=SourceType.BANK,
            source_native_id="BNK-2004",
            order_id_hint=None,
            amount_paise=75_000,
            date=date(2026, 8, 25),
        ),
        make_record(
            record_id="C",
            source_type=SourceType.BANK,
            source_native_id="BNK-2005",
            order_id_hint=None,
            amount_paise=75_000,
            date=date(2026, 8, 26),
        ),
    ]
    config = MatcherConfig(amount_tolerance_paise=0, date_window_days=2)
    return records, config


def _scenario_5_date_outside_window():
    records = [
        make_record(
            record_id="A",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-1004",
            order_id_hint=None,
            amount_paise=50_000,
            date=date(2026, 8, 25),
        ),
        make_record(
            record_id="B",
            source_type=SourceType.BANK,
            source_native_id="BNK-2006",
            order_id_hint=None,
            amount_paise=50_000,
            date=date(2026, 8, 29),
        ),
    ]
    config = MatcherConfig(amount_tolerance_paise=0, date_window_days=2)
    return records, config


def _scenario_6_amount_within_tolerance():
    records = [
        make_record(
            record_id="A",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-1005",
            order_id_hint=None,
            amount_paise=100_000,
            date=date(2026, 8, 25),
        ),
        make_record(
            record_id="B",
            source_type=SourceType.BANK,
            source_native_id="BNK-2007",
            order_id_hint=None,
            amount_paise=100_050,
            date=date(2026, 8, 26),
        ),
    ]
    config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
    return records, config


def _scenario_7_narration_only():
    records = [
        make_record(
            record_id="A",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-1006",
            order_id_hint=None,
            amount_paise=200_000,
            date=date(2026, 8, 25),
            narration="Payment for ORD-600",
        ),
        make_record(
            record_id="B",
            source_type=SourceType.BANK,
            source_native_id="BNK-2008",
            order_id_hint=None,
            amount_paise=350_000,
            date=date(2026, 9, 15),
            narration="ORD-600 transfer",
        ),
    ]
    config = MatcherConfig(amount_tolerance_paise=0, date_window_days=0)
    return records, config


def _scenario_8_true_orphan():
    records = [
        make_record(
            record_id="A",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-1007",
            order_id_hint=None,
            amount_paise=150_000,
            date=date(2026, 8, 25),
        ),
    ]
    config = MatcherConfig(amount_tolerance_paise=0, date_window_days=0)
    return records, config


def _scenario_9_duplicate_protection():
    records = [
        make_record(
            record_id="A",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-1008",
            order_id_hint="ORD-700",
            amount_paise=80_000,
            date=date(2026, 8, 25),
        ),
        make_record(
            record_id="B",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-1009",
            order_id_hint="ORD-700",
            amount_paise=80_000,
            date=date(2026, 8, 25),
        ),
        make_record(
            record_id="C",
            source_type=SourceType.BANK,
            source_native_id="BNK-2009",
            order_id_hint="ORD-700",
            amount_paise=80_000,
            date=date(2026, 8, 26),
        ),
    ]
    config = MatcherConfig(amount_tolerance_paise=0, date_window_days=0)
    return records, config


# ---------------------------------------------------------------------------
# Fixture-construction tests — verify scenario data matches documentation
# ---------------------------------------------------------------------------


class TestScenarioFixtureConstruction:
    """Validate that scenario records are constructed exactly as documented."""

    def test_scenario_1_fixture_matches_docs(self):
        records, config = _scenario_1_unique_exact_id()
        assert len(records) == 2
        assert records[0].record_id == "A"
        assert records[0].order_id_hint == "ORD-500"
        assert records[0].amount_paise == 100_000
        assert records[0].date == date(2026, 8, 25)
        assert records[0].source_type == SourceType.SETTLEMENT
        assert records[1].record_id == "B"
        assert records[1].source_type == SourceType.BANK
        assert config.amount_tolerance_paise == 0
        assert config.date_window_days == 0

    def test_scenario_2_fixture_matches_docs(self):
        records, config = _scenario_2_ambiguous_exact_id()
        assert len(records) == 3
        assert all(r.order_id_hint == "ORD-500" for r in records)
        assert len({r.order_id_hint for r in records}) == 1

    def test_scenario_3_fixture_matches_docs(self):
        records, config = _scenario_3_amount_date_window()
        assert len(records) == 2
        assert all(r.order_id_hint is None for r in records)
        assert records[0].amount_paise == 50_000
        assert records[1].amount_paise == 50_000
        assert config.amount_tolerance_paise == 0
        assert config.date_window_days == 2

    def test_scenario_4_fixture_matches_docs(self):
        records, config = _scenario_4_ambiguous_amount_candidates()
        assert len(records) == 3
        assert all(r.amount_paise == 75_000 for r in records)
        assert config.amount_tolerance_paise == 0
        assert config.date_window_days == 2

    def test_scenario_5_fixture_matches_docs(self):
        records, config = _scenario_5_date_outside_window()
        assert len(records) == 2
        assert records[0].date == date(2026, 8, 25)
        assert records[1].date == date(2026, 8, 29)
        assert (records[1].date - records[0].date).days == 4
        assert config.date_window_days == 2

    def test_scenario_6_fixture_matches_docs(self):
        records, config = _scenario_6_amount_within_tolerance()
        assert len(records) == 2
        assert records[0].amount_paise == 100_000
        assert records[1].amount_paise == 100_050
        assert abs(records[1].amount_paise - records[0].amount_paise) == 50
        assert config.amount_tolerance_paise == 100
        assert config.date_window_days == 2

    def test_scenario_7_fixture_matches_docs(self):
        records, config = _scenario_7_narration_only()
        assert len(records) == 2
        assert records[0].narration == "Payment for ORD-600"
        assert records[1].narration == "ORD-600 transfer"
        assert records[0].amount_paise != records[1].amount_paise

    def test_scenario_8_fixture_matches_docs(self):
        records, config = _scenario_8_true_orphan()
        assert len(records) == 1
        assert records[0].record_id == "A"
        assert records[0].order_id_hint is None

    def test_scenario_9_fixture_matches_docs(self):
        records, config = _scenario_9_duplicate_protection()
        assert len(records) == 3
        assert all(r.order_id_hint == "ORD-700" for r in records)
        assert all(r.amount_paise == 80_000 for r in records)
        assert len({r.source_native_id for r in records}) == 3


# ---------------------------------------------------------------------------
# Acceptance tests — verify documented reconciliation contract behavior
# ---------------------------------------------------------------------------


class TestReconciliationScenarios:
    """
    Nine hand-crafted acceptance scenarios from docs/reconciliation_scenarios.md.

    Each test describes:
      - Input records
      - Configuration required
      - Expected accepted decisions or unresolved records

    The deterministic Layer 1 matcher is implemented and these tests verify
    that it behaves according to the documented reconciliation contract.
    """

    def test_scenario_1_unique_exact_identifier_match(self):
        """
        Input:
          A (SETTLEMENT, SET-1001, ORD-500, 100000 paise, 2026-08-25)
          B (BANK, BNK-2001, ORD-500, 100000 paise, 2026-08-26)
        Config: defaults (no tolerance / window needed)
        Expected: 1 accepted decision via EXACT_ID rule; 0 unresolved records
        """
        records, config = _scenario_1_unique_exact_id()
        result = reconcile(records, config)
        assert len(result.decisions) == 1
        decision = result.decisions[0]
        assert decision.decision_id == "EXACT_ID-A-B"
        assert decision.member_record_ids == ("A", "B")
        assert decision.resolution_layer == ResolutionLayer.LAYER_1
        assert decision.rule_or_rationale == MatchRule.EXACT_ID.value
        assert decision.confidence == 1.0
        assert len(result.residual_record_ids) == 0

    def test_scenario_2_ambiguous_exact_identifier(self):
        """
        Input:
          A (SETTLEMENT, SET-1001, ORD-500, 100000 paise, 2026-08-25)
          B (BANK, BNK-2001, ORD-500, 100000 paise, 2026-08-26)
          C (BANK, BNK-2002, ORD-500, 100000 paise, 2026-08-27)
        Config: defaults
        Expected: 0 accepted decisions; 3 unresolved records (ambiguous)
        """
        records, config = _scenario_2_ambiguous_exact_id()
        result = reconcile(records, config)
        assert len(result.decisions) == 0
        assert len(result.residual_record_ids) == 3
        assert set(result.residual_record_ids) == {"A", "B", "C"}

    def test_scenario_3_unique_amount_and_date_window(self):
        """
        Input:
          A (SETTLEMENT, SET-1002, no hint, 50000 paise, 2026-08-25)
          B (BANK, BNK-2003, no hint, 50000 paise, 2026-08-26)
        Config: amount_tolerance_paise=0, date_window_days=2
        Expected: 1 accepted decision via AMOUNT_AND_DATE rule; 0 unresolved
        """
        records, config = _scenario_3_amount_date_window()
        result = reconcile(records, config)
        assert len(result.decisions) == 1
        decision = result.decisions[0]
        assert decision.decision_id == "AMOUNT_DATE-A-B"
        assert decision.member_record_ids == ("A", "B")
        assert decision.resolution_layer == ResolutionLayer.LAYER_1
        assert decision.rule_or_rationale == MatchRule.AMOUNT_AND_DATE.value
        assert decision.confidence == 1.0
        assert len(result.residual_record_ids) == 0

    def test_scenario_4_ambiguous_amount_candidates(self):
        """
        Input:
          A (SETTLEMENT, SET-1003, no hint, 75000 paise, 2026-08-25)
          B (BANK, BNK-2004, no hint, 75000 paise, 2026-08-25)
          C (BANK, BNK-2005, no hint, 75000 paise, 2026-08-26)
        Config: amount_tolerance_paise=0, date_window_days=2
        Expected: 0 accepted decisions; 3 unresolved records (ambiguous)
        """
        records, config = _scenario_4_ambiguous_amount_candidates()
        result = reconcile(records, config)
        assert len(result.decisions) == 0
        assert len(result.residual_record_ids) == 3
        assert set(result.residual_record_ids) == {"A", "B", "C"}

    def test_scenario_5_date_outside_allowed_window(self):
        """
        Input:
          A (SETTLEMENT, SET-1004, no hint, 50000 paise, 2026-08-25)
          B (BANK, BNK-2006, no hint, 50000 paise, 2026-08-29)
        Config: amount_tolerance_paise=0, date_window_days=2
        Expected: 0 accepted decisions; 2 unresolved records (date gap = 4 days)
        """
        records, config = _scenario_5_date_outside_window()
        result = reconcile(records, config)
        assert len(result.decisions) == 0
        assert len(result.residual_record_ids) == 2
        assert set(result.residual_record_ids) == {"A", "B"}

    def test_scenario_6_amount_difference_within_tolerance(self):
        """
        Input:
          A (SETTLEMENT, SET-1005, no hint, 100000 paise, 2026-08-25)
          B (BANK, BNK-2007, no hint, 100050 paise, 2026-08-26)
        Config: amount_tolerance_paise=100, date_window_days=2
        Expected: 1 accepted decision via AMOUNT_AND_DATE rule; 0 unresolved
        """
        records, config = _scenario_6_amount_within_tolerance()
        result = reconcile(records, config)
        assert len(result.decisions) == 1
        decision = result.decisions[0]
        assert decision.decision_id == "AMOUNT_DATE-A-B"
        assert decision.member_record_ids == ("A", "B")
        assert decision.resolution_layer == ResolutionLayer.LAYER_1
        assert decision.rule_or_rationale == MatchRule.AMOUNT_AND_DATE.value
        assert decision.confidence == 1.0
        assert len(result.residual_record_ids) == 0

    def test_scenario_7_narration_only_similarity(self):
        """
        Input:
          A (SETTLEMENT, SET-1006, no hint, 200000 paise, 2026-08-25,
             narration="Payment for ORD-600")
          B (BANK, BNK-2008, no hint, 350000 paise, 2026-09-15,
             narration="ORD-600 transfer")
        Config: defaults
        Expected: 0 accepted decisions; 2 unresolved records (narration-only)
        """
        records, config = _scenario_7_narration_only()
        result = reconcile(records, config)
        assert len(result.decisions) == 0
        assert len(result.residual_record_ids) == 2
        assert set(result.residual_record_ids) == {"A", "B"}

    def test_scenario_8_true_orphan(self):
        """
        Input:
          A (SETTLEMENT, SET-1007, no hint, 150000 paise, 2026-08-25)
        Config: defaults
        Expected: 0 accepted decisions; 1 unresolved record (true orphan)
        """
        records, config = _scenario_8_true_orphan()
        result = reconcile(records, config)
        assert len(result.decisions) == 0
        assert len(result.residual_record_ids) == 1
        assert result.residual_record_ids == ("A",)

    def test_scenario_9_duplicate_record_protection(self):
        """
        Input:
          A (SETTLEMENT, SET-1008, ORD-700, 80000 paise, 2026-08-25)
          B (SETTLEMENT, SET-1009, ORD-700, 80000 paise, 2026-08-25)
          C (BANK, BNK-2009, ORD-700, 80000 paise, 2026-08-26)
        Config: defaults
        Expected: 0 accepted decisions; 3 unresolved records (duplicate / ambiguous)
        """
        records, config = _scenario_9_duplicate_protection()
        result = reconcile(records, config)
        assert len(result.decisions) == 0
        assert len(result.residual_record_ids) == 3
        assert set(result.residual_record_ids) == {"A", "B", "C"}
