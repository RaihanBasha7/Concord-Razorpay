"""
Day 5.2 tests: naive baseline matcher.
"""
from __future__ import annotations

from datetime import date

import pytest

from reconciliation.baseline.naive_matcher import NaiveMatchResult, match
from reconciliation.domain.models import SourceType
from reconciliation.matcher_config import MatcherConfig
from tests.conftest import make_record


class TestNaiveMatchResult:
    def test_empty_input(self):
        result = match([], MatcherConfig(0, 0))
        assert result.matches == ()
        assert result.unmatched_record_ids == ()

    def test_obvious_match(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
        ]
        result = match(records, MatcherConfig(0, 0))
        assert result.matches == (("A", "B"),)
        assert result.unmatched_record_ids == ()

    def test_amount_outside_tolerance(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                amount_paise=100_200,
                date=date(2026, 8, 25),
            ),
        ]
        result = match(records, MatcherConfig(100, 0))
        assert result.matches == ()
        assert result.unmatched_record_ids == ("A", "B")

    def test_date_outside_window(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                amount_paise=100_000,
                date=date(2026, 8, 28),
            ),
        ]
        result = match(records, MatcherConfig(0, 2))
        assert result.matches == ()
        assert result.unmatched_record_ids == ("A", "B")

    def test_closest_candidate_selection(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                amount_paise=100_050,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="C",
                source_type=SourceType.BANK,
                source_native_id="BNK-2",
                amount_paise=100_100,
                date=date(2026, 8, 25),
            ),
        ]
        result = match(records, MatcherConfig(200, 0))
        assert result.matches == (("A", "B"),)
        assert result.unmatched_record_ids == ("C",)

    def test_closest_candidate_selection_by_date_tiebreak(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                amount_paise=100_000,
                date=date(2026, 8, 27),
            ),
            make_record(
                record_id="C",
                source_type=SourceType.BANK,
                source_native_id="BNK-2",
                amount_paise=100_000,
                date=date(2026, 8, 26),
            ),
        ]
        result = match(records, MatcherConfig(0, 2))
        assert result.matches == (("A", "C"),)
        assert result.unmatched_record_ids == ("B",)

    def test_same_source_no_match(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-2",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
        ]
        result = match(records, MatcherConfig(0, 0))
        assert result.matches == ()
        assert result.unmatched_record_ids == ("A", "B")

    def test_unmatched_record(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
        ]
        result = match(records, MatcherConfig(0, 0))
        assert result.matches == ()
        assert result.unmatched_record_ids == ("A",)

    def test_three_way_match_only_one_pair(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="C",
                source_type=SourceType.LEDGER,
                source_native_id="LED-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
        ]
        result = match(records, MatcherConfig(0, 0))
        assert len(result.matches) == 1
        assert len(result.unmatched_record_ids) == 1
        matched_ids = {rid for pair in result.matches for rid in pair}
        assert matched_ids.issubset({"A", "B", "C"})

    def test_input_not_mutated(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
        ]
        ids_before = [r.record_id for r in records]
        match(records, MatcherConfig(0, 0))
        ids_after = [r.record_id for r in records]
        assert ids_before == ids_after

    def test_zero_tolerance_zero_window(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
        ]
        result = match(records, MatcherConfig(0, 0))
        assert result.matches == (("A", "B"),)

    def test_exact_tolerance_boundary_match(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                amount_paise=100_050,
                date=date(2026, 8, 25),
            ),
        ]
        result = match(records, MatcherConfig(50, 0))
        assert result.matches == (("A", "B"),)

    def test_exact_window_boundary_match(self):
        records = [
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                amount_paise=100_000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                amount_paise=100_000,
                date=date(2026, 8, 27),
            ),
        ]
        result = match(records, MatcherConfig(0, 2))
        assert result.matches == (("A", "B"),)
