"""
Day 4.1 tests: Layer 2 record reconstruction and input contract purity.
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from reconciliation.domain.models import SourceType
from reconciliation.evaluation.dataset_generator import (
    generate_dataset,
    write_dataset,
)
from reconciliation.evaluation.evaluation_harness import (
    load_normalized_records,
    run_evaluation,
)
from reconciliation.layer2 import Layer2Case, ReconstructionError, reconstruct_layer2_case
from reconciliation.matcher_config import MatcherConfig
from tests.conftest import make_record


class TestLayer2Reconstruction:
    def test_reconstructs_all_member_records(self, tmp_path: Path):
        ds = generate_dataset(seed=42)
        write_dataset(ds, tmp_path)
        config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
        harness = run_evaluation(ds, config, tmp_path)

        records = load_normalized_records(
            tmp_path / "settlements.csv",
            tmp_path / "bank.csv",
            tmp_path / "ledger.csv",
        )
        record_tuple = tuple(records)

        with harness.residual_path.open() as f:
            reader = csv.DictReader(f)
            residual_rows = list(reader)

        for row in residual_rows:
            member_ids = tuple(json.loads(row["member_record_ids"]))
            case = reconstruct_layer2_case(
                scenario_id=row["scenario_id"],
                member_record_ids=member_ids,
                normalized_records=record_tuple,
            )
            assert case.scenario_id == row["scenario_id"]
            assert case.record_count == int(row["record_count"])
            assert len(case.member_records) == len(member_ids)
            reconstructed_ids = tuple(r.record_id for r in case.member_records)
            assert reconstructed_ids == member_ids

    def test_reconstruction_fails_for_missing_record(self):
        records = (
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                order_id_hint="ORD-1",
                amount_paise=100000,
                date=date(2026, 8, 25),
            ),
        )
        with pytest.raises(ReconstructionError, match="not found"):
            reconstruct_layer2_case(
                scenario_id="MISSING-001",
                member_record_ids=("A", "B"),
                normalized_records=records,
            )

    def test_reconstruction_fails_for_empty_member_ids(self):
        records = (
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                order_id_hint="ORD-1",
                amount_paise=100000,
                date=date(2026, 8, 25),
            ),
        )
        with pytest.raises(ReconstructionError, match="must not be empty"):
            reconstruct_layer2_case(
                scenario_id="EMPTY-001",
                member_record_ids=(),
                normalized_records=records,
            )

    def test_reconstruction_is_deterministic(self):
        records = (
            make_record(
                record_id="A",
                source_type=SourceType.SETTLEMENT,
                source_native_id="SET-1",
                order_id_hint="ORD-1",
                amount_paise=100000,
                date=date(2026, 8, 25),
            ),
            make_record(
                record_id="B",
                source_type=SourceType.BANK,
                source_native_id="BNK-1",
                order_id_hint="ORD-1",
                amount_paise=100000,
                date=date(2026, 8, 26),
            ),
        )
        case1 = reconstruct_layer2_case(
            scenario_id="DET-001",
            member_record_ids=("A", "B"),
            normalized_records=records,
        )
        case2 = reconstruct_layer2_case(
            scenario_id="DET-001",
            member_record_ids=("A", "B"),
            normalized_records=records,
        )
        assert case1 == case2
        assert case1.member_records[0].record_id == "A"
        assert case1.member_records[1].record_id == "B"


class TestLayer2ContractPurity:
    def test_evaluation_fields_not_in_layer2_case(self):
        fields = {f.name for f in Layer2Case.__dataclass_fields__.values()}
        assert "category" not in fields
        assert "has_valid_relationship" not in fields
        assert "is_true_exception" not in fields
        assert "description" not in fields

    def test_layer2_contract_contains_only_production_fields(self):
        fields = {f.name for f in Layer2Case.__dataclass_fields__.values()}
        assert fields == {"scenario_id", "member_records", "record_count"}
