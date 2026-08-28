"""
Day 4.2 tests: Deterministic candidate retrieval.
"""
from __future__ import annotations

from datetime import date

import pytest

from reconciliation.domain.models import SourceType
from reconciliation.layer2 import Layer2Case
from reconciliation.retrieval import (
    CandidateRecord,
    RetrievalConfig,
    RetrievalError,
    RetrievalResult,
    retrieve_candidates,
)
from tests.conftest import make_record


class TestRetrievalDeterminism:
    def test_retrieval_is_deterministic(self):
        case = Layer2Case(
            scenario_id="DET-001",
            member_records=(
                make_record(
                    record_id="M1",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="SET-1",
                    order_id_hint="ORD-1",
                    amount_paise=100000,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        records = (
            make_record(
                record_id="C1",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=100000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="C2",
                source_type=SourceType.LEDGER,
                source_native_id="LED-1",
                order_id_hint="ORD-1",
                amount_paise=100000,
                date=date(2026, 8, 25),
            ),
        )
        config = RetrievalConfig(max_candidates=10)
        result1 = retrieve_candidates(case, records, config)
        result2 = retrieve_candidates(case, records, config)
        assert result1 == result2
        ids1 = [c.record.record_id for c in result1.candidates]
        ids2 = [c.record.record_id for c in result2.candidates]
        assert ids1 == ids2

    def test_retrieval_order_stable_for_equal_scores(self):
        case = Layer2Case(
            scenario_id="STABLE-001",
            member_records=(
                make_record(
                    record_id="M1",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="SET-1",
                    order_id_hint="ORD-1",
                    amount_paise=100000,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        records = tuple(
            make_record(
                record_id=f"C{i}",
                source_type=SourceType.BANK,
                source_native_id=f"BNK-{i}",
                order_id_hint=f"ORD-{i}",
                amount_paise=100000,
                date=date(2026, 8, 25),
            )
            for i in range(1, 6)
        )
        result = retrieve_candidates(case, records, RetrievalConfig(max_candidates=10))
        ids = [c.record.record_id for c in result.candidates]
        assert ids == sorted(ids)


class TestRetrievalCandidateCount:
    def test_candidate_count_never_exceeds_max(self):
        case = Layer2Case(
            scenario_id="COUNT-001",
            member_records=(
                make_record(
                    record_id="M1",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="SET-1",
                    order_id_hint="ORD-1",
                    amount_paise=100000,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        many_records = tuple(
            make_record(
                record_id=f"C{i}",
                source_type=SourceType.BANK,
                source_native_id=f"BNK-{i}",
                order_id_hint=f"ORD-{i}",
                amount_paise=100000 + i * 1000,
                date=date(2026, 8, 25),
            )
            for i in range(1, 51)
        )
        for max_c in (1, 5, 10, 20):
            result = retrieve_candidates(
                case, many_records, RetrievalConfig(max_candidates=max_c)
            )
            assert result.candidate_count <= max_c
            assert len(result.candidates) <= max_c
            assert result.max_candidates == max_c

    def test_default_max_candidates_is_ten(self):
        assert RetrievalConfig().max_candidates == 10

    def test_empty_pool_returns_zero_candidates(self):
        case = Layer2Case(
            scenario_id="EMPTY-001",
            member_records=(
                make_record(
                    record_id="M1",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="SET-1",
                    order_id_hint="ORD-1",
                    amount_paise=100000,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        result = retrieve_candidates(case, (), RetrievalConfig(max_candidates=10))
        assert result.candidate_count == 0
        assert result.candidates == ()


class TestRetrievalCrossSource:
    def test_same_source_poor_signals_ranked_below_cross_source(self):
        case = Layer2Case(
            scenario_id="SAME-001",
            member_records=(
                make_record(
                    record_id="M1",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="SET-1",
                    order_id_hint="ORD-1",
                    amount_paise=100000,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        records = (
            make_record(
                record_id="SAME",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-99",
                order_id_hint="ORD-99",
                amount_paise=500000,
                date=date(2026, 1, 1),
            ),
            make_record(
                record_id="CROSS",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=100000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="FAR",
                source_type=SourceType.LEDGER,
                source_native_id="LED-1",
                order_id_hint="ORD-99",
                amount_paise=500000,
                date=date(2026, 1, 1),
            ),
        )
        result = retrieve_candidates(
            case, records, RetrievalConfig(max_candidates=2)
        )
        ids = [c.record.record_id for c in result.candidates]
        assert ids[0] == "CROSS"
        assert "SAME" not in ids

    def test_plausible_cross_source_candidate_retrieved(self):
        case = Layer2Case(
            scenario_id="CROSS-001",
            member_records=(
                make_record(
                    record_id="M1",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="SET-1",
                    order_id_hint="ORD-1",
                    amount_paise=100000,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        records = (
            make_record(
                record_id="BANK-CAND",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=100000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="LEDGER-CAND",
                source_type=SourceType.LEDGER,
                source_native_id="LED-1",
                order_id_hint="ORD-1",
                amount_paise=100000,
                date=date(2026, 8, 25),
            ),
        )
        result = retrieve_candidates(
            case, records, RetrievalConfig(max_candidates=10)
        )
        assert result.candidate_count >= 1
        cross_source_ids = [
            c.record.record_id
            for c in result.candidates
            if c.source_type != SourceType.SETTLEMENT
        ]
        assert len(cross_source_ids) >= 1


class TestRetrievalNonExactAmount:
    def test_fee_deducted_amount_retrieved(self):
        case = Layer2Case(
            scenario_id="FEE-001",
            member_records=(
                make_record(
                    record_id="M1",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="SET-1",
                    order_id_hint="ORD-1",
                    amount_paise=100000,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        records = (
            make_record(
                record_id="BANK-FEE",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=99500,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="FAR",
                source_type=SourceType.LEDGER,
                source_native_id="LED-1",
                order_id_hint="ORD-99",
                amount_paise=500000,
                date=date(2026, 1, 1),
            ),
        )
        result = retrieve_candidates(
            case, records, RetrievalConfig(max_candidates=2)
        )
        ids = [c.record.record_id for c in result.candidates]
        assert "BANK-FEE" in ids

    def test_partial_refund_amount_retrieved(self):
        case = Layer2Case(
            scenario_id="REFUND-001",
            member_records=(
                make_record(
                    record_id="M1",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="SET-1",
                    order_id_hint="ORD-1",
                    amount_paise=200000,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        records = (
            make_record(
                record_id="BANK-REFUND",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=150000,
                date=date(2026, 8, 25),
            ),
        )
        result = retrieve_candidates(
            case, records, RetrievalConfig(max_candidates=1)
        )
        assert result.candidate_count == 1
        assert result.candidates[0].record.record_id == "BANK-REFUND"
        assert "amount_proximity" in result.candidates[0].match_signals


class TestRetrievalEvaluationBoundary:
    def test_raw_payload_evaluation_metadata_does_not_influence(self):
        case = Layer2Case(
            scenario_id="EVAL-001",
            member_records=(
                make_record(
                    record_id="M1",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="SET-1",
                    order_id_hint="ORD-1",
                    amount_paise=100000,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        base_payload: dict = {"source_ref": "SRC-1"}
        records_a = (
            make_record(
                record_id="C1",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=100000,
                date=date(2026, 8, 25),
                raw_payload={**base_payload, "category": "FEE_DEDUCTED"},
            ),
        )
        records_b = (
            make_record(
                record_id="C1",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=100000,
                date=date(2026, 8, 25),
                raw_payload={**base_payload, "category": "PARTIAL_REFUND"},
            ),
        )
        config = RetrievalConfig(max_candidates=10)
        result_a = retrieve_candidates(case, records_a, config)
        result_b = retrieve_candidates(case, records_b, config)
        assert result_a.scenario_id == result_b.scenario_id
        assert result_a.candidate_count == result_b.candidate_count
        assert result_a.max_candidates == result_b.max_candidates
        assert result_a.retrieval_signals_used == result_b.retrieval_signals_used
        for ca, cb in zip(result_a.candidates, result_b.candidates):
            assert ca.score == cb.score
            assert ca.rank == cb.rank
            assert ca.match_signals == cb.match_signals
            assert ca.record.record_id == cb.record.record_id


class TestRetrievalConfig:
    def test_invalid_max_candidates_raises(self):
        with pytest.raises(ValueError, match="max_candidates must be at least 1"):
            RetrievalConfig(max_candidates=0)

    def test_invalid_negative_weight_raises(self):
        with pytest.raises(ValueError, match="date_weight must be non-negative"):
            RetrievalConfig(date_weight=-1.0)


class TestAmountProximityScaling:
    def test_same_proportional_different_scales_equivalent_score(self):
        case_small = Layer2Case(
            scenario_id="SCALE-SMALL",
            member_records=(
                make_record(
                    record_id="M1",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="SET-1",
                    order_id_hint="ORD-1",
                    amount_paise=100000,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        case_large = Layer2Case(
            scenario_id="SCALE-LARGE",
            member_records=(
                make_record(
                    record_id="M2",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="SET-2",
                    order_id_hint="ORD-2",
                    amount_paise=10000000,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        records_small = (
            make_record(
                record_id="SMALL-5PCT",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=95000,
                date=date(2026, 8, 25),
            ),
        )
        records_large = (
            make_record(
                record_id="LARGE-5PCT",
                source_type=SourceType.BANK,
                source_native_id="BNK-2",
                order_id_hint="ORD-2",
                amount_paise=9500000,
                date=date(2026, 8, 25),
            ),
        )
        result_small = retrieve_candidates(
            case_small, records_small, RetrievalConfig(max_candidates=10)
        )
        result_large = retrieve_candidates(
            case_large, records_large, RetrievalConfig(max_candidates=10)
        )
        score_small = result_small.candidates[0].score
        score_large = result_large.candidates[0].score
        assert score_small == score_large
        assert "amount_proximity" in result_small.candidates[0].match_signals
        assert "amount_proximity" in result_large.candidates[0].match_signals

    def test_exact_amount_scores_zero(self):
        case = Layer2Case(
            scenario_id="EXACT-001",
            member_records=(
                make_record(
                    record_id="M1",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="SET-1",
                    order_id_hint="ORD-1",
                    amount_paise=100000,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        records = (
            make_record(
                record_id="EXACT",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=100000,
                date=date(2026, 8, 25),
            ),
        )
        result = retrieve_candidates(
            case, records, RetrievalConfig(max_candidates=10)
        )
        assert result.candidates[0].score == -3.0
        assert "amount_proximity" not in result.candidates[0].match_signals

    def test_zero_and_nonzero_amounts_scored_safely(self):
        case = Layer2Case(
            scenario_id="ZERO-001",
            member_records=(
                make_record(
                    record_id="M1",
                    source_type=SourceType.SETTLEMENT,
                    source_native_id="SET-1",
                    order_id_hint="ORD-1",
                    amount_paise=0,
                    date=date(2026, 8, 25),
                ),
            ),
            record_count=1,
        )
        records = (
            make_record(
                record_id="ZERO-EXACT",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=0,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="ZERO-NONZERO",
                source_type=SourceType.BANK,
                source_native_id="BNK-2",
                order_id_hint="ORD-2",
                amount_paise=100000,
                date=date(2026, 8, 25),
            ),
        )
        result = retrieve_candidates(
            case, records, RetrievalConfig(max_candidates=10)
        )
        scores = {c.record.record_id: c.score for c in result.candidates}
        assert scores["ZERO-EXACT"] < scores["ZERO-NONZERO"]
        assert "amount_proximity" in result.candidates[1].match_signals
