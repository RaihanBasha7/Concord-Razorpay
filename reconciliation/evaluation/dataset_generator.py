"""
Extended ground-truth types and dataset generator for Concord Day 3 evaluation.

Ground truth is generated first. Source CSVs are derived from it.
"""
from __future__ import annotations

import csv
import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from reconciliation.domain.models import MatchRule, SourceType
from reconciliation.evaluation.ground_truth import EdgeCaseCategory, GroundTruthUnit


# ---------------------------------------------------------------------------
# Scenario-level ground truth
# ---------------------------------------------------------------------------

class ExpectedLayer1Outcome(str, Enum):
    MATCH_EXACT_ID = "MATCH_EXACT_ID"
    MATCH_AMOUNT_DATE = "MATCH_AMOUNT_DATE"
    NO_MATCH = "NO_MATCH"


@dataclass(frozen=True)
class ScenarioRecordSpec:
    synthetic_ref: str
    source_type: SourceType
    source_native_id: str
    order_id_hint: Optional[str]
    amount_paise: int
    record_date: date
    narration: Optional[str] = None


@dataclass(frozen=True)
class GroundTruthScenario:
    scenario_id: str
    category: EdgeCaseCategory
    record_specs: Tuple[ScenarioRecordSpec, ...]
    expected_outcome: ExpectedLayer1Outcome
    description: str


# ---------------------------------------------------------------------------
# Category quotas
# ---------------------------------------------------------------------------

CATEGORY_QUOTAS: Dict[EdgeCaseCategory, int] = {
    EdgeCaseCategory.EXACT_MATCH: 20,
    EdgeCaseCategory.T_PLUS_DELAY: 15,
    EdgeCaseCategory.FEE_DEDUCTED: 12,
    EdgeCaseCategory.PARTIAL_REFUND: 10,
    EdgeCaseCategory.SPLIT_SETTLEMENT: 10,
    EdgeCaseCategory.ROUNDING_DIFFERENCE: 13,
    EdgeCaseCategory.INCONSISTENT_NARRATION: 10,
    EdgeCaseCategory.DUPLICATE: 10,
    EdgeCaseCategory.TRUE_ORPHAN: 10,
    EdgeCaseCategory.LATE_ARRIVING: 10,
}

TOTAL_SCENARIOS = sum(CATEGORY_QUOTAS.values())


# ---------------------------------------------------------------------------
# Deterministic random helper
# ---------------------------------------------------------------------------

class _SeededRandom:
    def __init__(self, seed: int = 42) -> None:
        self._rng = random.Random(seed)

    def randint(self, a: int, b: int) -> int:
        return self._rng.randint(a, b)

    def choice(self, seq: List[Any]) -> Any:
        return self._rng.choice(seq)

    def shuffle(self, seq: List[Any]) -> None:
        self._rng.shuffle(seq)

    def sample(self, population: List[Any], k: int) -> List[Any]:
        return self._rng.sample(population, k)


# ---------------------------------------------------------------------------
# Leakage checks
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LeakageReport:
    has_leakage: bool
    issues: Tuple[str, ...]


def check_leakage(
    scenarios: List[GroundTruthScenario],
    records: List[ScenarioRecordSpec],
) -> LeakageReport:
    issues: List[str] = []

    orphan_ids = [
        r.source_native_id
        for scen in scenarios
        if scen.category == EdgeCaseCategory.TRUE_ORPHAN
        for r in scen.record_specs
    ]
    for oid in orphan_ids:
        if any(prefix in oid for prefix in ["ORPH-", "LOST-", "MISS-"]):
            issues.append(f"Orphan record uses special prefix: {oid}")

    for scen in scenarios:
        cat_label = scen.category.value.lower()
        for r in scen.record_specs:
            if cat_label in r.source_native_id.lower():
                issues.append(
                    f"Category encoded in source_native_id: {r.source_native_id}"
                )
            if r.order_id_hint and cat_label in r.order_id_hint.lower():
                issues.append(
                    f"Category encoded in order_id_hint: {r.order_id_hint}"
                )

    narration_by_category: Dict[EdgeCaseCategory, List[str]] = {}
    for scen in scenarios:
        narration_by_category.setdefault(scen.category, [])
        for r in scen.record_specs:
            if r.narration:
                narration_by_category[scen.category].append(r.narration.lower())

    for cat, narrs in narration_by_category.items():
        if len(narrs) < 2:
            continue
        common = set(narrs[0].split())
        for n in narrs[1:]:
            common &= set(n.split())
        if len(common) > 3:
            issues.append(
                f"Category {cat.value} has highly overlapping narration words: {common}"
            )

    return LeakageReport(has_leakage=len(issues) > 0, issues=tuple(issues))


# ---------------------------------------------------------------------------
# Record ID generation (mirrors normalizer logic for consistency)
# ---------------------------------------------------------------------------

_RECORD_ID_HASH_LENGTH = 12


def _compute_record_id(spec: ScenarioRecordSpec) -> str:
    parts = [
        spec.source_type.value,
        spec.source_native_id,
        spec.order_id_hint or "NA",
        str(spec.amount_paise),
        spec.record_date.isoformat(),
    ]
    digest = hashlib.sha256("-".join(parts).encode("utf-8")).hexdigest()[:_RECORD_ID_HASH_LENGTH]
    return f"{spec.source_type.value}-{digest}"


# ---------------------------------------------------------------------------
# Shared randomizers and unique generators
# ---------------------------------------------------------------------------

_BASE_DATE = date(2026, 8, 1)

_AMOUNTS = [
    50000, 75000, 100000, 125000, 150000, 200000,
    250000, 500000, 750000, 1000000, 1500000, 2000000,
]

_NARRATIONS = [
    "Payment for services rendered",
    "Invoice settlement",
    "Monthly subscription fee",
    "Order fulfillment payment",
    "Client remittance",
    "Vendor payout",
    "Commission disbursement",
    "Refund processing",
    "Adjustment entry",
    "Reconciliation entry",
    "Payment gateway transfer",
    "Bank credit",
    "Settlement transfer",
    "Wire transfer",
    "ACH credit",
    "Direct deposit",
    "Online payment",
    "Card settlement",
    "UPI transaction",
    "Wallet transfer",
]


def _random_amount(rng: _SeededRandom) -> int:
    return rng.choice(_AMOUNTS)


def _random_date(rng: _SeededRandom, base: date = _BASE_DATE) -> date:
    return base + timedelta(days=rng.randint(0, 27))


def _random_narration(rng: _SeededRandom) -> str:
    return rng.choice(_NARRATIONS)


def _make_settlement_spec(
    rng: _SeededRandom,
    scenario_id: str,
    seq: int,
    amount: int,
    date_offset: int = 0,
    order_id: Optional[str] = None,
    narration: Optional[str] = None,
) -> ScenarioRecordSpec:
    settlement_id = f"SET-{scenario_id}-{seq:03d}"
    oid = order_id or f"ORD-{scenario_id}-{seq:03d}"
    d = _BASE_DATE + timedelta(days=date_offset)
    n = narration or _random_narration(rng)
    return ScenarioRecordSpec(
        synthetic_ref=f"REC-SET-{seq:03d}",
        source_type=SourceType.SETTLEMENT,
        source_native_id=settlement_id,
        order_id_hint=oid,
        amount_paise=amount,
        record_date=d,
        narration=n,
    )


def _make_bank_spec(
    rng: _SeededRandom,
    scenario_id: str,
    seq: int,
    amount: int,
    date_offset: int = 0,
    order_id: Optional[str] = None,
    narration: Optional[str] = None,
) -> ScenarioRecordSpec:
    bank_utr = f"BNK-{scenario_id}-{seq:03d}"
    oid = order_id or f"ORD-{scenario_id}-{seq:03d}"
    d = _BASE_DATE + timedelta(days=date_offset)
    n = narration or _random_narration(rng)
    return ScenarioRecordSpec(
        synthetic_ref=f"REC-BNK-{seq:03d}",
        source_type=SourceType.BANK,
        source_native_id=bank_utr,
        order_id_hint=oid,
        amount_paise=amount,
        record_date=d,
        narration=n,
    )


def _make_ledger_spec(
    rng: _SeededRandom,
    scenario_id: str,
    seq: int,
    amount: int,
    date_offset: int = 0,
    order_id: Optional[str] = None,
    narration: Optional[str] = None,
) -> ScenarioRecordSpec:
    oid = order_id or f"ORD-{scenario_id}-{seq:03d}"
    d = _BASE_DATE + timedelta(days=date_offset)
    n = narration or _random_narration(rng)
    return ScenarioRecordSpec(
        synthetic_ref=f"REC-LED-{seq:03d}",
        source_type=SourceType.LEDGER,
        source_native_id=oid,
        order_id_hint=oid,
        amount_paise=amount,
        record_date=d,
        narration=n,
    )


# ---------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------

def _build_exact_match(rng: _SeededRandom, cat: EdgeCaseCategory, idx: int, global_idx: int) -> GroundTruthScenario:
    scen_id = f"EXACT-{idx:03d}"
    base = _random_amount(rng)
    amount = base + global_idx * 1000
    base_date = _BASE_DATE + timedelta(days=global_idx)
    order_id = f"ORD-{scen_id}"
    set_spec = _make_settlement_spec(rng, scen_id, 1, amount, date_offset=0, order_id=order_id)
    if rng.choice([True, False]):
        bn_spec = _make_bank_spec(rng, scen_id, 1, amount, date_offset=rng.choice([0, 1]), order_id=order_id)
    else:
        bn_spec = _make_ledger_spec(rng, scen_id, 1, amount, date_offset=rng.choice([0, 1]), order_id=order_id)
    return GroundTruthScenario(
        scenario_id=scen_id,
        category=cat,
        record_specs=(set_spec, bn_spec),
        expected_outcome=ExpectedLayer1Outcome.MATCH_EXACT_ID,
        description="Two records share the same order identifier across different sources with identical amounts.",
    )


def _build_t_plus_delay(rng: _SeededRandom, cat: EdgeCaseCategory, idx: int, global_idx: int) -> GroundTruthScenario:
    scen_id = f"TDLY-{idx:03d}"
    base = _random_amount(rng)
    amount = base + global_idx * 1000
    base_date = _BASE_DATE + timedelta(days=global_idx)
    order_id = f"ORD-{scen_id}"
    set_spec = _make_settlement_spec(rng, scen_id, 1, amount, date_offset=0, order_id=order_id)
    date_diff = rng.choice([1, 2])
    if rng.choice([True, False]):
        bn_spec = _make_bank_spec(rng, scen_id, 1, amount, date_offset=date_diff, order_id=order_id)
    else:
        bn_spec = _make_ledger_spec(rng, scen_id, 1, amount, date_offset=date_diff, order_id=order_id)
    return GroundTruthScenario(
        scenario_id=scen_id,
        category=cat,
        record_specs=(set_spec, bn_spec),
        expected_outcome=ExpectedLayer1Outcome.MATCH_EXACT_ID,
        description="Records share an order identifier but settlement and bank dates differ by 1-2 days.",
    )


def _build_fee_deducted(rng: _SeededRandom, cat: EdgeCaseCategory, idx: int, global_idx: int) -> GroundTruthScenario:
    scen_id = f"FEE-{idx:03d}"
    base = _random_amount(rng)
    amount = base + global_idx * 1000
    fee = rng.choice([1500, 2500, 3500, 5000])
    base_date = _BASE_DATE + timedelta(days=global_idx)
    set_spec = _make_settlement_spec(rng, scen_id, 1, amount, date_offset=0, order_id=f"ORD-{scen_id}-A")
    bn_spec = _make_bank_spec(
        rng, scen_id, 1, amount - fee, date_offset=rng.choice([0, 1]), order_id=f"ORD-{scen_id}-B"
    )
    return GroundTruthScenario(
        scenario_id=scen_id,
        category=cat,
        record_specs=(set_spec, bn_spec),
        expected_outcome=ExpectedLayer1Outcome.NO_MATCH,
        description="Settlement amount exceeds bank amount by a fixed fee; Layer 1 amount tolerance is insufficient.",
    )


def _build_partial_refund(rng: _SeededRandom, cat: EdgeCaseCategory, idx: int, global_idx: int) -> GroundTruthScenario:
    scen_id = f"REFD-{idx:03d}"
    base = _random_amount(rng)
    amount = base + global_idx * 1000
    refund = rng.choice([20000, 50000, 75000])
    base_date = _BASE_DATE + timedelta(days=global_idx)
    set_spec = _make_settlement_spec(rng, scen_id, 1, amount, date_offset=0, order_id=f"ORD-{scen_id}-A")
    bn_spec = _make_bank_spec(
        rng, scen_id, 1, amount - refund, date_offset=rng.choice([0, 1]), order_id=f"ORD-{scen_id}-B"
    )
    return GroundTruthScenario(
        scenario_id=scen_id,
        category=cat,
        record_specs=(set_spec, bn_spec),
        expected_outcome=ExpectedLayer1Outcome.NO_MATCH,
        description="Partial refund reduces bank amount below settlement amount; not a full reconciliation.",
    )


def _build_split_settlement(rng: _SeededRandom, cat: EdgeCaseCategory, idx: int, global_idx: int) -> GroundTruthScenario:
    scen_id = f"SPLT-{idx:03d}"
    base = _random_amount(rng)
    total = base + global_idx * 1000
    part1 = total // 2
    part2 = total - part1
    if part1 == part2:
        part1 -= rng.choice([1000, 2000, 5000])
        part2 = total - part1
    base_date = _BASE_DATE + timedelta(days=global_idx)
    set_spec = _make_settlement_spec(rng, scen_id, 1, total, date_offset=0, order_id=f"ORD-{scen_id}-A")
    bn1 = _make_bank_spec(rng, scen_id, 1, part1, date_offset=rng.choice([0, 1]), order_id=f"ORD-{scen_id}-B")
    bn2 = _make_bank_spec(rng, scen_id, 2, part2, date_offset=rng.choice([0, 1]), order_id=f"ORD-{scen_id}-C")
    return GroundTruthScenario(
        scenario_id=scen_id,
        category=cat,
        record_specs=(set_spec, bn1, bn2),
        expected_outcome=ExpectedLayer1Outcome.NO_MATCH,
        description="One settlement is split across two bank credits; no individual pair matches exactly.",
    )


def _build_rounding_difference(
    rng: _SeededRandom, cat: EdgeCaseCategory, idx: int, global_idx: int
) -> GroundTruthScenario:
    scen_id = f"RND-{idx:03d}"
    base = _random_amount(rng)
    amount = base + global_idx * 1000
    base_date = _BASE_DATE + timedelta(days=global_idx)
    if idx <= 8:
        diff = rng.choice([1, 2, 3, 5, 7])
        expected = ExpectedLayer1Outcome.MATCH_AMOUNT_DATE
        desc = "Amounts differ by a few paise within default tolerance; should match on amount+date."
    else:
        diff = rng.choice([150, 250, 500, 750, 1000])
        expected = ExpectedLayer1Outcome.NO_MATCH
        desc = "Amounts differ by more than tolerance; should not match."
    set_spec = _make_settlement_spec(rng, scen_id, 1, amount, date_offset=0, order_id=f"ORD-{scen_id}-A")
    if rng.choice([True, False]):
        bn_spec = _make_bank_spec(
            rng, scen_id, 1, amount + diff, date_offset=rng.choice([0, 1]), order_id=f"ORD-{scen_id}-B"
        )
    else:
        bn_spec = _make_ledger_spec(
            rng, scen_id, 1, amount + diff, date_offset=rng.choice([0, 1]), order_id=f"ORD-{scen_id}-B"
        )
    return GroundTruthScenario(
        scenario_id=scen_id,
        category=cat,
        record_specs=(set_spec, bn_spec),
        expected_outcome=expected,
        description=desc,
    )


def _build_inconsistent_narration(
    rng: _SeededRandom, cat: EdgeCaseCategory, idx: int, global_idx: int
) -> GroundTruthScenario:
    scen_id = f"NARR-{idx:03d}"
    base = _random_amount(rng)
    amount = base + global_idx * 1000
    base_date = _BASE_DATE + timedelta(days=global_idx)
    set_spec = _make_settlement_spec(
        rng, scen_id, 1, amount, date_offset=0, order_id=f"ORD-{scen_id}-A",
        narration=f"Payment related to ORD-{scen_id}",
    )
    diff_amount = amount + rng.choice([50000, 100000, 200000])
    far_date = base_date + timedelta(days=rng.randint(10, 20))
    if rng.choice([True, False]):
        bn_spec = _make_bank_spec(
            rng, scen_id, 1, diff_amount, date_offset=(far_date - base_date).days, order_id=f"ORD-{scen_id}-B",
            narration=f"Transfer mentioning ORD-{scen_id}",
        )
    else:
        bn_spec = _make_ledger_spec(
            rng, scen_id, 1, diff_amount, date_offset=(far_date - base_date).days, order_id=f"ORD-{scen_id}-B",
            narration=f"Ledger entry for ORD-{scen_id}",
        )
    return GroundTruthScenario(
        scenario_id=scen_id,
        category=cat,
        record_specs=(set_spec, bn_spec),
        expected_outcome=ExpectedLayer1Outcome.NO_MATCH,
        description="Narrations reference a common order but amounts and dates are too dissimilar for deterministic matching.",
    )


def _build_duplicate(rng: _SeededRandom, cat: EdgeCaseCategory, idx: int, global_idx: int) -> GroundTruthScenario:
    scen_id = f"DUP-{idx:03d}"
    base = _random_amount(rng)
    amount = base + global_idx * 1000
    base_date = _BASE_DATE + timedelta(days=global_idx)
    set1 = _make_settlement_spec(rng, scen_id, 1, amount, date_offset=0, order_id=f"ORD-{scen_id}")
    set2 = _make_settlement_spec(rng, scen_id, 2, amount, date_offset=0, order_id=f"ORD-{scen_id}")
    if rng.choice([True, False]):
        bn = _make_bank_spec(rng, scen_id, 1, amount + 50000, date_offset=5, order_id=f"ORD-{scen_id}-B")
        return GroundTruthScenario(
            scenario_id=scen_id,
            category=cat,
            record_specs=(set1, set2, bn),
            expected_outcome=ExpectedLayer1Outcome.NO_MATCH,
            description="Duplicate settlement records share native ID; ambiguous exact identifier prevents automatic match.",
        )
    return GroundTruthScenario(
        scenario_id=scen_id,
        category=cat,
        record_specs=(set1, set2),
        expected_outcome=ExpectedLayer1Outcome.NO_MATCH,
        description="Duplicate settlement records share native ID; no counterpart to match.",
    )


def _build_true_orphan(rng: _SeededRandom, cat: EdgeCaseCategory, idx: int, global_idx: int) -> GroundTruthScenario:
    scen_id = f"UNQ-{idx:03d}"
    base = _random_amount(rng)
    amount = base + global_idx * 1000
    base_date = _BASE_DATE + timedelta(days=global_idx)
    source_choice = rng.choice(["settlement", "bank", "ledger"])
    order_id = f"ORD-{scen_id}"
    if source_choice == "settlement":
        rec = _make_settlement_spec(rng, scen_id, 1, amount, date_offset=0, order_id=order_id)
    elif source_choice == "bank":
        rec = _make_bank_spec(rng, scen_id, 1, amount, date_offset=0, order_id=order_id)
    else:
        rec = _make_ledger_spec(rng, scen_id, 1, amount, date_offset=0, order_id=order_id)
    return GroundTruthScenario(
        scenario_id=scen_id,
        category=cat,
        record_specs=(rec,),
        expected_outcome=ExpectedLayer1Outcome.NO_MATCH,
        description="Single record with no counterpart in any other source.",
    )


def _build_late_arriving(rng: _SeededRandom, cat: EdgeCaseCategory, idx: int, global_idx: int) -> GroundTruthScenario:
    scen_id = f"LATE-{idx:03d}"
    base = _random_amount(rng)
    amount = base + global_idx * 1000
    base_date = _BASE_DATE + timedelta(days=global_idx)
    date_gap = rng.choice([5, 7, 10, 14, 21])
    set_spec = _make_settlement_spec(rng, scen_id, 1, amount, date_offset=0, order_id=f"ORD-{scen_id}-A")
    if rng.choice([True, False]):
        bn_spec = _make_bank_spec(rng, scen_id, 1, amount, date_offset=date_gap, order_id=f"ORD-{scen_id}-B")
    else:
        bn_spec = _make_ledger_spec(rng, scen_id, 1, amount, date_offset=date_gap, order_id=f"ORD-{scen_id}-B")
    return GroundTruthScenario(
        scenario_id=scen_id,
        category=cat,
        record_specs=(set_spec, bn_spec),
        expected_outcome=ExpectedLayer1Outcome.NO_MATCH,
        description="Counterpart record arrives later than the configured date window; no deterministic match.",
    )


_BUILDERS = {
    EdgeCaseCategory.EXACT_MATCH: _build_exact_match,
    EdgeCaseCategory.T_PLUS_DELAY: _build_t_plus_delay,
    EdgeCaseCategory.FEE_DEDUCTED: _build_fee_deducted,
    EdgeCaseCategory.PARTIAL_REFUND: _build_partial_refund,
    EdgeCaseCategory.SPLIT_SETTLEMENT: _build_split_settlement,
    EdgeCaseCategory.ROUNDING_DIFFERENCE: _build_rounding_difference,
    EdgeCaseCategory.INCONSISTENT_NARRATION: _build_inconsistent_narration,
    EdgeCaseCategory.DUPLICATE: _build_duplicate,
    EdgeCaseCategory.TRUE_ORPHAN: _build_true_orphan,
    EdgeCaseCategory.LATE_ARRIVING: _build_late_arriving,
}


# ---------------------------------------------------------------------------
# Public generator API
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GeneratedDataset:
    scenarios: Tuple[GroundTruthScenario, ...]
    settlement_rows: Tuple[Dict[str, Any], ...]
    bank_rows: Tuple[Dict[str, Any], ...]
    ledger_rows: Tuple[Dict[str, Any], ...]
    record_specs: Tuple[ScenarioRecordSpec, ...]


def generate_dataset(seed: int = 42) -> GeneratedDataset:
    rng = _SeededRandom(seed)
    scenarios: List[GroundTruthScenario] = []
    all_specs: List[ScenarioRecordSpec] = []

    global_idx = 0
    for category, quota in CATEGORY_QUOTAS.items():
        builder = _BUILDERS[category]
        for i in range(1, quota + 1):
            scen = builder(rng, category, i, global_idx)
            scenarios.append(scen)
            all_specs.extend(scen.record_specs)
            global_idx += 1

    rng.shuffle(scenarios)

    settlement_rows: List[Dict[str, Any]] = []
    bank_rows: List[Dict[str, Any]] = []
    ledger_rows: List[Dict[str, Any]] = []

    for scen in scenarios:
        for spec in scen.record_specs:
            row: Dict[str, Any] = {
                "scenario_ref": scen.scenario_id,
                "synthetic_ref": spec.synthetic_ref,
            }
            if spec.source_type == SourceType.SETTLEMENT:
                row.update({
                    "settlement_id": spec.source_native_id,
                    "order_id": spec.order_id_hint,
                    "gross_amount": f"{spec.amount_paise / 100:.2f}",
                    "settlement_date": spec.record_date.isoformat(),
                    "narration": spec.narration or "",
                })
                settlement_rows.append(row)
            elif spec.source_type == SourceType.BANK:
                row.update({
                    "bank_utr": spec.source_native_id,
                    "order_id": spec.order_id_hint or "",
                    "credit_amount": f"{spec.amount_paise / 100:.2f}",
                    "value_date": spec.record_date.isoformat(),
                    "narration": spec.narration or "",
                })
                bank_rows.append(row)
            elif spec.source_type == SourceType.LEDGER:
                row.update({
                    "order_id": spec.source_native_id,
                    "gross_amount": f"{spec.amount_paise / 100:.2f}",
                    "transaction_date": spec.record_date.isoformat(),
                    "narration": spec.narration or "",
                })
                ledger_rows.append(row)

    rng.shuffle(settlement_rows)
    rng.shuffle(bank_rows)
    rng.shuffle(ledger_rows)

    return GeneratedDataset(
        scenarios=tuple(scenarios),
        settlement_rows=tuple(settlement_rows),
        bank_rows=tuple(bank_rows),
        ledger_rows=tuple(ledger_rows),
        record_specs=tuple(all_specs),
    )


def validate_quotas(scenarios: List[GroundTruthScenario]) -> None:
    counts: Dict[EdgeCaseCategory, int] = {cat: 0 for cat in EdgeCaseCategory}
    for scen in scenarios:
        counts[scen.category] += 1
    for cat, quota in CATEGORY_QUOTAS.items():
        if counts.get(cat, 0) < quota:
            raise ValueError(
                f"Category {cat.value} is underrepresented: {counts.get(cat, 0)} < {quota}"
            )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _scenario_to_dict(scen: GroundTruthScenario) -> Dict[str, Any]:
    return {
        "scenario_id": scen.scenario_id,
        "category": scen.category.value,
        "record_specs": [
            {
                "synthetic_ref": rs.synthetic_ref,
                "source_type": rs.source_type.value,
                "source_native_id": rs.source_native_id,
                "order_id_hint": rs.order_id_hint,
                "amount_paise": rs.amount_paise,
                "record_date": rs.record_date.isoformat(),
                "narration": rs.narration,
            }
            for rs in scen.record_specs
        ],
        "expected_outcome": scen.expected_outcome.value,
        "description": scen.description,
    }


def write_ground_truth(dataset: GeneratedDataset, path: Path) -> None:
    payload = {
        "total_scenarios": len(dataset.scenarios),
        "categories": {cat.value: quota for cat, quota in CATEGORY_QUOTAS.items()},
        "scenarios": [_scenario_to_dict(s) for s in dataset.scenarios],
    }
    path.write_text(json.dumps(payload, indent=2))


def write_csv(rows: Tuple[Dict[str, Any], ...], path: Path) -> None:
    if not rows:
        path.write_text("")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_dataset(dataset: GeneratedDataset, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_ground_truth(dataset, output_dir / "ground_truth.json")
    write_csv(dataset.settlement_rows, output_dir / "settlements.csv")
    write_csv(dataset.bank_rows, output_dir / "bank.csv")
    write_csv(dataset.ledger_rows, output_dir / "ledger.csv")
