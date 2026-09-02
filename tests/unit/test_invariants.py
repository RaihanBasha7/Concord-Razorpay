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
from reconciliation.matcher_config import MatcherConfig
from tests.conftest import make_record


class TestReconciliationInvariants:
    def test_empty_input_returns_empty_result(self):
        result = reconcile([], MatcherConfig(0, 0))
        assert result.decisions == ()
        assert result.residual_record_ids == ()

    def test_duplicate_record_ids_become_residuals(self):
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
        result = reconcile(records, MatcherConfig(0, 0))
        assert len(result.decisions) == 0
        assert set(result.residual_record_ids) == {"X"}
        assert len(result.residual_record_ids) == 2

    def test_colliding_duplicates_do_not_block_independent_matches(self):
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
                order_id_hint="ORD-2",
                amount_paise=200_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="C",
                source_type=SourceType.BANK,
                source_native_id="BNK-2",
                order_id_hint="ORD-2",
                amount_paise=200_000,
                date=date(2026, 8, 26),
            ),
        ]
        result = reconcile(records, MatcherConfig(0, 0))
        assert len(result.decisions) == 1
        assert result.decisions[0].member_record_ids == ("A", "B")
        assert set(result.residual_record_ids) == {"C"}
        assert len(result.residual_record_ids) == 2

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


class TestMatcherConfigValidation:
    def test_negative_amount_tolerance_raises(self):
        with pytest.raises(ValueError, match="amount_tolerance_paise must be a non-negative integer"):
            MatcherConfig(amount_tolerance_paise=-1, date_window_days=0)

    def test_negative_date_window_raises(self):
        with pytest.raises(ValueError, match="date_window_days must be a non-negative integer"):
            MatcherConfig(amount_tolerance_paise=0, date_window_days=-1)

    def test_zero_values_are_accepted(self):
        config = MatcherConfig(amount_tolerance_paise=0, date_window_days=0)
        assert config.amount_tolerance_paise == 0
        assert config.date_window_days == 0


class TestReconciliationDecisionValidation:
    def test_fewer_than_two_member_ids_raises(self):
        with pytest.raises(ValueError, match="member_record_ids must contain at least 2 IDs"):
            ReconciliationDecision(
                decision_id="D1",
                member_record_ids=("A",),
                resolution_layer=ResolutionLayer.LAYER_1,
                rule_or_rationale=MatchRule.EXACT_ID.value,
                confidence=1.0,
            )

    def test_duplicate_member_ids_raises(self):
        with pytest.raises(ValueError, match="member_record_ids must not contain duplicates"):
            ReconciliationDecision(
                decision_id="D1",
                member_record_ids=("A", "A"),
                resolution_layer=ResolutionLayer.LAYER_1,
                rule_or_rationale=MatchRule.EXACT_ID.value,
                confidence=1.0,
            )

    def test_confidence_below_zero_raises(self):
        with pytest.raises(ValueError, match="confidence must be between 0.0 and 1.0 inclusive"):
            ReconciliationDecision(
                decision_id="D1",
                member_record_ids=("A", "B"),
                resolution_layer=ResolutionLayer.LAYER_1,
                rule_or_rationale=MatchRule.EXACT_ID.value,
                confidence=-0.1,
            )

    def test_confidence_above_one_raises(self):
        with pytest.raises(ValueError, match="confidence must be between 0.0 and 1.0 inclusive"):
            ReconciliationDecision(
                decision_id="D1",
                member_record_ids=("A", "B"),
                resolution_layer=ResolutionLayer.LAYER_1,
                rule_or_rationale=MatchRule.EXACT_ID.value,
                confidence=1.1,
            )

    def test_confidence_boundary_zero_accepted(self):
        decision = ReconciliationDecision(
            decision_id="D1",
            member_record_ids=("A", "B"),
            resolution_layer=ResolutionLayer.LAYER_1,
            rule_or_rationale=MatchRule.EXACT_ID.value,
            confidence=0.0,
        )
        assert decision.confidence == 0.0

    def test_confidence_boundary_one_accepted(self):
        decision = ReconciliationDecision(
            decision_id="D1",
            member_record_ids=("A", "B"),
            resolution_layer=ResolutionLayer.LAYER_1,
            rule_or_rationale=MatchRule.EXACT_ID.value,
            confidence=1.0,
        )
        assert decision.confidence == 1.0


class TestSharedToleranceObject:
    """Verify that Layer 1 (MatcherConfig) and Layer 3 (route) reference
    the exact same DEFAULT_TOLERANCES object — not just equal values."""

    def test_layer3_and_matcher_config_share_same_object(self):
        from reconciliation.config import DEFAULT_TOLERANCES
        from reconciliation.layer3 import route
        from reconciliation.matcher_config import MatcherConfig

        # layer3.route default arg object must be the same singleton
        import inspect

        route_sig = inspect.signature(route)
        amount_default = route_sig.parameters["amount_tolerance_paise"].default
        assert amount_default is DEFAULT_TOLERANCES.amount_tolerance_paise, (
            f"layer3.route uses {amount_default!r}, not DEFAULT_TOLERANCES.amount_tolerance_paise"
        )

        # MatcherConfig.from_tolerances builds from the same object
        mc = MatcherConfig.from_tolerances(DEFAULT_TOLERANCES)
        assert mc.amount_tolerance_paise is DEFAULT_TOLERANCES.amount_tolerance_paise
        assert mc.date_window_days is DEFAULT_TOLERANCES.date_window_days

    def test_default_tolerances_values(self):
        from reconciliation.config import DEFAULT_TOLERANCES

        assert DEFAULT_TOLERANCES.amount_tolerance_paise == 100
        assert DEFAULT_TOLERANCES.date_window_days == 2
