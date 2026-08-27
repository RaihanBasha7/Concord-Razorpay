from __future__ import annotations

from datetime import date

from reconciliation.domain.models import (
    MatchRule,
    ReconciliationDecision,
    ResolutionLayer,
    SourceType,
)
from reconciliation.matcher import reconcile
from reconciliation.matcher_config import MatcherConfig
from reconciliation.normalizer import normalize_record


class TestNormalizationToReconciliationIntegration:
    def test_settlement_and_bank_exact_id_end_to_end(self):
        settlement_row = {
            "settlement_id": "SET-1001",
            "order_id": "ORD-500",
            "gross_amount": 1000,
            "settlement_date": "2026-08-25",
        }
        bank_row = {
            "bank_utr": "BNK-2001",
            "order_id": "ORD-500",
            "credit_amount": 100000,
            "value_date": "2026-08-26",
        }

        settlement = normalize_record(settlement_row, SourceType.SETTLEMENT)
        bank = normalize_record(bank_row, SourceType.BANK)

        result = reconcile([settlement, bank], MatcherConfig(0, 0))

        assert len(result.decisions) == 1
        decision = result.decisions[0]
        assert set(decision.member_record_ids) == {settlement.record_id, bank.record_id}
        assert decision.resolution_layer == ResolutionLayer.LAYER_1
        assert decision.rule_or_rationale == MatchRule.EXACT_ID.value
        assert decision.confidence == 1.0
        assert result.residual_record_ids == ()

    def test_settlement_and_bank_amount_date_end_to_end(self):
        settlement_row = {
            "settlement_id": "SET-1002",
            "gross_amount": "5000.00",
            "settlement_date": "2026-08-25",
        }
        bank_row = {
            "bank_utr": "BNK-2003",
            "credit_amount": 5000,
            "value_date": "2026-08-26",
        }

        settlement = normalize_record(settlement_row, SourceType.SETTLEMENT)
        bank = normalize_record(bank_row, SourceType.BANK)

        result = reconcile([settlement, bank], MatcherConfig(0, 2))

        assert len(result.decisions) == 1
        decision = result.decisions[0]
        assert set(decision.member_record_ids) == {settlement.record_id, bank.record_id}
        assert decision.rule_or_rationale == MatchRule.AMOUNT_AND_DATE.value
        assert result.residual_record_ids == ()

    def test_unmatched_record_surfaces_as_residual_end_to_end(self):
        settlement_row = {
            "settlement_id": "SET-1003",
            "order_id": "ORD-600",
            "gross_amount": 1500,
            "settlement_date": "2026-08-25",
        }

        settlement = normalize_record(settlement_row, SourceType.SETTLEMENT)

        result = reconcile([settlement], MatcherConfig(0, 0))

        assert len(result.decisions) == 0
        assert result.residual_record_ids == (settlement.record_id,)
