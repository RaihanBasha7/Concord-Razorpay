"""
Evaluation accounting correctness tests.

Ensures that:
  * pair counts are never confused with record counts
  * proposal-affected records are counted exactly once
  * final buckets sum to total records
  * false accept detection is correct
  * residual + deterministic = total records
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

from reconciliation.domain.models import NormalizedRecord, SourceType
from reconciliation.evaluation.dataset_generator import (
    EdgeCaseCategory,
    generate_dataset,
    _compute_record_id,
)
from reconciliation.evaluation.full_pipeline_evaluation import (
    _build_ground_truth_units,
    _expected_match_ids,
)
from reconciliation.loader import load_normalized_records, load_residuals
from reconciliation.matcher import reconcile
from reconciliation.matcher_config import MatcherConfig
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_validation import ProposalOutcome, ProposalOutcomeType
from reconciliation.layer3 import route as layer3_route


# ── Helpers ────────────────────────────────────────────────────────


def _load_pipeline_data():
    """Load all pipeline data and return a dict of useful structures."""
    data_dir = Path("data")
    dataset = generate_dataset(seed=42)
    config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

    normalized = load_normalized_records(data_dir)
    all_records = list(normalized)

    unit_map = {
        u.scenario_id: u
        for u in _build_ground_truth_units(list(dataset.scenarios))
    }
    scenario_map = {s.scenario_id: s for s in dataset.scenarios}
    record_to_scenario: Dict[str, str] = {}
    for unit in unit_map.values():
        for rid in unit.member_record_ids:
            record_to_scenario[rid] = unit.scenario_id

    l1_result = reconcile(all_records, config)

    return {
        "dataset": dataset,
        "all_records": all_records,
        "unit_map": unit_map,
        "scenario_map": scenario_map,
        "record_to_scenario": record_to_scenario,
        "l1_result": l1_result,
        "config": config,
        "data_dir": data_dir,
    }


def _build_l2_outcomes_from_results(
    results_path: Path,
    residuals,
) -> List[ProposalOutcome]:
    """Build ProposalOutcome objects from the clean eval results JSON."""
    results_data = json.loads(results_path.read_text())
    raw_results = results_data["results"]

    outcomes = []
    results_map = {}
    for r in raw_results:
        sid = r["scenario_id"]
        residual = next((x for x in residuals if x.scenario_id == sid), None)
        if residual is None:
            continue
        member_ids = list(residual.member_record_ids)
        results_map[sid] = r

        if r["outcome"] == "PROPOSAL_VALID" and r["confidence"] is not None:
            proposal = MatchProposal(
                proposed_match_ids=member_ids,
                confidence=r["confidence"],
                rationale="",
            )
            ot = ProposalOutcomeType.PROPOSAL_VALID
        elif r["outcome"] == "NO_PROPOSAL":
            proposal = None
            ot = ProposalOutcomeType.NO_PROPOSAL
        elif r["outcome"] == "VALIDATION_FAILED":
            proposal = None
            ot = ProposalOutcomeType.VALIDATION_FAILED
        else:
            proposal = None
            ot = ProposalOutcomeType.API_ERROR

        outcomes.append(
            ProposalOutcome(
                outcome=ot,
                proposal=proposal,
                presented_record_ids=tuple(member_ids),
                reason="",
                error_classification=r.get("error_classification"),
            )
        )

    # Add unevaluated as API_ERROR
    for r in residuals:
        if r.scenario_id not in results_map:
            outcomes.append(
                ProposalOutcome(
                    outcome=ProposalOutcomeType.API_ERROR,
                    proposal=None,
                    presented_record_ids=tuple(r.member_record_ids),
                    reason="quota",
                    error_classification="quota_exhausted",
                )
            )

    return outcomes


# ── Tests ──────────────────────────────────────────────────────────


class TestPairVsRecordAccounting:
    """Issue 1: Verify that matched_pairs and matched_records are never confused."""

    def test_each_decision_has_exactly_two_records(self):
        """Every Layer 1 decision is a pair (exactly 2 member records)."""
        data = _load_pipeline_data()
        decisions = list(data["l1_result"].decisions)
        for d in decisions:
            assert len(d.member_record_ids) == 2, (
                f"Decision {d.decision_id} has {len(d.member_record_ids)} records, expected 2"
            )

    def test_matched_pairs_times_two_equals_matched_records(self):
        """matched_pairs * 2 == unique matched records."""
        data = _load_pipeline_data()
        decisions = list(data["l1_result"].decisions)
        residual_ids = set(data["l1_result"].residual_record_ids)
        all_ids = {r.record_id for r in data["all_records"]}

        matched_pairs = len(decisions)
        matched_records = set()
        for d in decisions:
            for rid in d.member_record_ids:
                matched_records.add(rid)

        assert matched_pairs * 2 == len(matched_records), (
            f"matched_pairs={matched_pairs} * 2 != matched_records={len(matched_records)}"
        )

    def test_matched_plus_residual_equals_total(self):
        """matched_records + residual_records == total_records."""
        data = _load_pipeline_data()
        decisions = list(data["l1_result"].decisions)
        residual_ids = set(data["l1_result"].residual_record_ids)
        all_ids = {r.record_id for r in data["all_records"]}

        matched_records = set()
        for d in decisions:
            for rid in d.member_record_ids:
                matched_records.add(rid)

        assert matched_records & residual_ids == set(), (
            "Overlap between matched and residual records"
        )
        assert matched_records | residual_ids == all_ids, (
            "matched_records union residual_records != all_records"
        )

    def test_no_overlap_between_decisions(self):
        """No record appears in more than one decision."""
        data = _load_pipeline_data()
        decisions = list(data["l1_result"].decisions)
        seen = set()
        for d in decisions:
            for rid in d.member_record_ids:
                assert rid not in seen, (
                    f"Record {rid} appears in multiple decisions"
                )
                seen.add(rid)


class TestProposalToRecordMapping:
    """Issue 2: Verify proposal-affected records are counted exactly once."""

    def test_proposal_records_counted_once_in_routing(self):
        """Each record appears in exactly one routing decision."""
        data = _load_pipeline_data()
        residuals = load_residuals(data["data_dir"])
        l2_outcomes = _build_l2_outcomes_from_results(
            data["data_dir"] / "clean_eval_results.json", residuals
        )

        routing = layer3_route(
            layer1_decisions=list(data["l1_result"].decisions),
            layer2_outcomes=l2_outcomes,
            all_records=data["all_records"],
        )

        # Each record should have exactly one routing decision
        routed_ids = [rd.record_id for rd in routing]
        assert len(routed_ids) == len(set(routed_ids)), (
            "Some records appear in multiple routing decisions"
        )
        assert len(routing) == len(data["all_records"]), (
            f"Routing decisions ({len(routing)}) != total records ({len(data['all_records'])})"
        )

    def test_routing_buckets_sum_to_total(self):
        """All routing buckets sum to total records."""
        data = _load_pipeline_data()
        residuals = load_residuals(data["data_dir"])
        l2_outcomes = _build_l2_outcomes_from_results(
            data["data_dir"] / "clean_eval_results.json", residuals
        )

        routing = layer3_route(
            layer1_decisions=list(data["l1_result"].decisions),
            layer2_outcomes=l2_outcomes,
            all_records=data["all_records"],
        )

        from collections import Counter
        bucket_counts = Counter(rd.bucket.value for rd in routing)
        total = sum(bucket_counts.values())
        assert total == len(data["all_records"]), (
            f"Bucket sum {total} != total records {len(data['all_records'])}"
        )

    def test_valid_proposals_affect_multiple_records(self):
        """26 valid proposals affect 56 records, not 26."""
        data = _load_pipeline_data()
        results_data = json.loads(
            (data["data_dir"] / "clean_eval_results.json").read_text()
        )
        valid_proposals = [
            r
            for r in results_data["results"]
            if r["outcome"] == "PROPOSAL_VALID"
        ]
        assert len(valid_proposals) == 26

        # Count total records affected by valid proposals
        residuals = load_residuals(data["data_dir"])
        total_affected = 0
        for r in valid_proposals:
            residual = next(
                (x for x in residuals if x.scenario_id == r["scenario_id"]), None
            )
            if residual:
                total_affected += len(residual.member_record_ids)

        assert total_affected == 56, (
            f"26 valid proposals should affect 56 records, got {total_affected}"
        )


class TestFalseAcceptDetection:
    """Issue 3: Verify false accept detection is correct."""

    def test_false_accept_count(self):
        """There are exactly 2 false accepts, not 1."""
        data = _load_pipeline_data()
        residuals = load_residuals(data["data_dir"])

        results_data = json.loads(
            (data["data_dir"] / "clean_eval_results.json").read_text()
        )
        auto_accept = [
            r
            for r in results_data["results"]
            if r["outcome"] == "PROPOSAL_VALID"
            and r["confidence"] is not None
            and r["confidence"] >= 0.90
        ]

        false_accepts = 0
        for r in auto_accept:
            sid = r["scenario_id"]
            scenario = data["scenario_map"].get(sid)
            unit = data["unit_map"].get(sid)
            residual = next(
                (x for x in residuals if x.scenario_id == sid), None
            )
            if not scenario or not unit or not residual:
                continue
            expected = tuple(sorted(_expected_match_ids(scenario, unit)))
            proposed = tuple(sorted(residual.member_record_ids))
            if proposed != expected:
                false_accepts += 1

        assert false_accepts == 2, (
            f"Expected 2 false accepts, got {false_accepts}"
        )

    def test_false_accepts_are_duplicate_scenarios(self):
        """Both false accepts are in DUPLICATE scenarios (evaluator-side check)."""
        data = _load_pipeline_data()
        residuals = load_residuals(data["data_dir"])

        results_data = json.loads(
            (data["data_dir"] / "clean_eval_results.json").read_text()
        )
        auto_accept = [
            r
            for r in results_data["results"]
            if r["outcome"] == "PROPOSAL_VALID"
            and r["confidence"] is not None
            and r["confidence"] >= 0.90
        ]

        for r in auto_accept:
            sid = r["scenario_id"]
            scenario = data["scenario_map"].get(sid)
            unit = data["unit_map"].get(sid)
            residual = next(
                (x for x in residuals if x.scenario_id == sid), None
            )
            if not scenario or not unit or not residual:
                continue
            expected = tuple(sorted(_expected_match_ids(scenario, unit)))
            proposed = tuple(sorted(residual.member_record_ids))
            if proposed != expected:
                assert scenario.category == EdgeCaseCategory.DUPLICATE, (
                    f"False accept {sid} is {scenario.category.value}, expected DUPLICATE"
                )


class TestEdgeCaseAccounting:
    """Issue 7: Verify edge-case accounting table is mathematically consistent."""

    def test_all_residual_scenarios_accounted_for(self):
        """Every residual scenario is either attempted or unevaluated."""
        data = _load_pipeline_data()
        results_data = json.loads(
            (data["data_dir"] / "clean_eval_results.json").read_text()
        )
        attempted_ids = {r["scenario_id"] for r in results_data["results"]}

        # Check all scenarios in dataset
        all_scenarios = data["dataset"].scenarios
        for scen in all_scenarios:
            # Not all scenarios are residuals — some are matched by L1
            pass

        # Check all attempted scenarios are residuals (but not vice versa)
        residuals = load_residuals(data["data_dir"])
        residual_ids = {r.scenario_id for r in residuals}
        assert attempted_ids <= residual_ids, (
            f"Some attempted scenarios are not residuals: {attempted_ids - residual_ids}"
        )
        # Unevaluated residuals are expected due to quota exhaustion
        unevaluated = residual_ids - attempted_ids
        assert len(unevaluated) == 32, (
            f"Expected 32 unevaluated, got {len(unevaluated)}"
        )


class TestGuardrailTests:
    """Issue 7: Test the same-source duplicate over-inclusion guardrail."""

    def test_false_accept_dup_002_rejected(self):
        """DUP-002 (2 settlements + 1 bank) must be rejected by the guardrail."""
        from reconciliation.domain.models import NormalizedRecord, SourceType
        from reconciliation.proposal_validation import validate_proposal, ProposalOutcomeType
        from reconciliation.proposal import MatchProposal
        from datetime import date as date_type

        # Reconstruct the exact DUP-002 structure
        s1 = NormalizedRecord(
            record_id='SETTLEMENT-ee51962fe68b', source_type=SourceType.SETTLEMENT,
            source_native_id='SET-02dadfc2-001', order_id_hint='ORD-02dadfc2',
            amount_paise=591000, date=date_type(2026, 8, 1),
            narration='ACH credit', raw_payload=None)
        s2 = NormalizedRecord(
            record_id='SETTLEMENT-a753fbdd63d7', source_type=SourceType.SETTLEMENT,
            source_native_id='SET-02dadfc2-002', order_id_hint='ORD-02dadfc2',
            amount_paise=591000, date=date_type(2026, 8, 1),
            narration='ACH credit', raw_payload=None)
        b1 = NormalizedRecord(
            record_id='BANK-f6935b68616a', source_type=SourceType.BANK,
            source_native_id='BNK-02dadfc2-001', order_id_hint='ORD-02dadfc2-B',
            amount_paise=641000, date=date_type(2026, 8, 6),
            narration='Online payment', raw_payload=None)

        records_by_id = {r.record_id: r for r in [s1, s2, b1]}
        proposal = MatchProposal(
            proposed_match_ids=[s1.record_id, s2.record_id, b1.record_id],
            confidence=0.9, rationale='test')

        outcome = validate_proposal(proposal, [s1.record_id, s2.record_id, b1.record_id],
                                    records_by_id=records_by_id)
        assert outcome.outcome == ProposalOutcomeType.VALIDATION_FAILED
        assert 'same-source duplicate' in outcome.reason.lower()

    def test_false_accept_dup_004_rejected(self):
        """DUP-004 (2 settlements + 1 bank) must be rejected by the guardrail."""
        from reconciliation.domain.models import NormalizedRecord, SourceType
        from reconciliation.proposal_validation import validate_proposal, ProposalOutcomeType
        from reconciliation.proposal import MatchProposal
        from datetime import date as date_type

        s1 = NormalizedRecord(
            record_id='SETTLEMENT-440ed69e443b', source_type=SourceType.SETTLEMENT,
            source_native_id='SET-c97f6b3e-001', order_id_hint='ORD-c97f6b3e',
            amount_paise=843000, date=date_type(2026, 8, 1),
            narration='Wallet transfer', raw_payload=None)
        s2 = NormalizedRecord(
            record_id='SETTLEMENT-5c5bb71fd4c9', source_type=SourceType.SETTLEMENT,
            source_native_id='SET-c97f6b3e-002', order_id_hint='ORD-c97f6b3e',
            amount_paise=843000, date=date_type(2026, 8, 1),
            narration='Payment gateway transfer', raw_payload=None)
        b1 = NormalizedRecord(
            record_id='BANK-fea63ed8a60a', source_type=SourceType.BANK,
            source_native_id='BNK-c97f6b3e-001', order_id_hint='ORD-c97f6b3e-B',
            amount_paise=893000, date=date_type(2026, 8, 6),
            narration='Refund processing', raw_payload=None)

        records_by_id = {r.record_id: r for r in [s1, s2, b1]}
        proposal = MatchProposal(
            proposed_match_ids=[s1.record_id, s2.record_id, b1.record_id],
            confidence=0.9, rationale='test')

        outcome = validate_proposal(proposal, [s1.record_id, s2.record_id, b1.record_id],
                                    records_by_id=records_by_id)
        assert outcome.outcome == ProposalOutcomeType.VALIDATION_FAILED

    def test_legitimate_exact_match_accepted(self):
        """Legitimate exact match (settlement + bank, same amount) must pass."""
        from reconciliation.domain.models import NormalizedRecord, SourceType
        from reconciliation.proposal_validation import validate_proposal, ProposalOutcomeType
        from reconciliation.proposal import MatchProposal
        from datetime import date as date_type

        s1 = NormalizedRecord(
            record_id='SETTLEMENT-abc', source_type=SourceType.SETTLEMENT,
            source_native_id='SET-001', order_id_hint='ORD-001',
            amount_paise=100000, date=date_type(2026, 8, 1),
            narration='Payment', raw_payload=None)
        b1 = NormalizedRecord(
            record_id='BANK-def', source_type=SourceType.BANK,
            source_native_id='BNK-001', order_id_hint='ORD-001',
            amount_paise=100000, date=date_type(2026, 8, 1),
            narration='Credit', raw_payload=None)

        records_by_id = {r.record_id: r for r in [s1, b1]}
        proposal = MatchProposal(
            proposed_match_ids=[s1.record_id, b1.record_id],
            confidence=0.95, rationale='exact match')

        outcome = validate_proposal(proposal, [s1.record_id, b1.record_id],
                                    records_by_id=records_by_id)
        assert outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID

    def test_legitimate_split_settlement_accepted(self):
        """Legitimate split settlement (1 settlement + 2 banks) must pass."""
        from reconciliation.domain.models import NormalizedRecord, SourceType
        from reconciliation.proposal_validation import validate_proposal, ProposalOutcomeType
        from reconciliation.proposal import MatchProposal
        from datetime import date as date_type

        s1 = NormalizedRecord(
            record_id='SETTLEMENT-abc', source_type=SourceType.SETTLEMENT,
            source_native_id='SET-001', order_id_hint='ORD-001',
            amount_paise=200000, date=date_type(2026, 8, 1),
            narration='Payment', raw_payload=None)
        b1 = NormalizedRecord(
            record_id='BANK-def', source_type=SourceType.BANK,
            source_native_id='BNK-001', order_id_hint='ORD-001-B',
            amount_paise=100000, date=date_type(2026, 8, 1),
            narration='Credit', raw_payload=None)
        b2 = NormalizedRecord(
            record_id='BANK-ghi', source_type=SourceType.BANK,
            source_native_id='BNK-002', order_id_hint='ORD-001-C',
            amount_paise=100000, date=date_type(2026, 8, 1),
            narration='Credit', raw_payload=None)

        records_by_id = {r.record_id: r for r in [s1, b1, b2]}
        proposal = MatchProposal(
            proposed_match_ids=[s1.record_id, b1.record_id, b2.record_id],
            confidence=0.85, rationale='split')

        outcome = validate_proposal(proposal, [s1.record_id, b1.record_id, b2.record_id],
                                    records_by_id=records_by_id)
        assert outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID

    def test_legitimate_duplicate_without_bank_accepted(self):
        """Legitimate duplicate (2 settlements, no bank) must pass."""
        from reconciliation.domain.models import NormalizedRecord, SourceType
        from reconciliation.proposal_validation import validate_proposal, ProposalOutcomeType
        from reconciliation.proposal import MatchProposal
        from datetime import date as date_type

        s1 = NormalizedRecord(
            record_id='SETTLEMENT-abc', source_type=SourceType.SETTLEMENT,
            source_native_id='SET-001', order_id_hint='ORD-001',
            amount_paise=100000, date=date_type(2026, 8, 1),
            narration='Payment', raw_payload=None)
        s2 = NormalizedRecord(
            record_id='SETTLEMENT-def', source_type=SourceType.SETTLEMENT,
            source_native_id='SET-002', order_id_hint='ORD-001',
            amount_paise=100000, date=date_type(2026, 8, 1),
            narration='Payment', raw_payload=None)

        records_by_id = {r.record_id: r for r in [s1, s2]}
        proposal = MatchProposal(
            proposed_match_ids=[s1.record_id, s2.record_id],
            confidence=0.90, rationale='duplicates')

        outcome = validate_proposal(proposal, [s1.record_id, s2.record_id],
                                    records_by_id=records_by_id)
        assert outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID

    def test_guardrail_not_applied_without_records_by_id(self):
        """Guardrail is skipped when records_by_id is not provided (backward compat)."""
        from reconciliation.domain.models import NormalizedRecord, SourceType
        from reconciliation.proposal_validation import validate_proposal, ProposalOutcomeType
        from reconciliation.proposal import MatchProposal
        from datetime import date as date_type

        s1 = NormalizedRecord(
            record_id='SETTLEMENT-abc', source_type=SourceType.SETTLEMENT,
            source_native_id='SET-001', order_id_hint='ORD-001',
            amount_paise=591000, date=date_type(2026, 8, 1),
            narration='Payment', raw_payload=None)
        s2 = NormalizedRecord(
            record_id='SETTLEMENT-def', source_type=SourceType.SETTLEMENT,
            source_native_id='SET-002', order_id_hint='ORD-001',
            amount_paise=591000, date=date_type(2026, 8, 1),
            narration='Payment', raw_payload=None)
        b1 = NormalizedRecord(
            record_id='BANK-ghi', source_type=SourceType.BANK,
            source_native_id='BNK-001', order_id_hint='ORD-001-B',
            amount_paise=641000, date=date_type(2026, 8, 6),
            narration='Credit', raw_payload=None)

        proposal = MatchProposal(
            proposed_match_ids=[s1.record_id, s2.record_id, b1.record_id],
            confidence=0.9, rationale='test')

        # Without records_by_id, guardrail is skipped
        outcome = validate_proposal(proposal, [s1.record_id, s2.record_id, b1.record_id])
        assert outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID


class TestEvaluationReadiness:
    """Issue 9: Verify the evaluation infrastructure is ready for a full run."""

    def test_results_file_exists_and_valid(self):
        """clean_eval_results.json exists and has expected structure."""
        path = Path("data/clean_eval_results.json")
        assert path.exists()
        data = json.loads(path.read_text())
        assert "results" in data
        assert "fingerprint" in data
        assert data["fingerprint"] == "b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93"

    def test_can_resume_evaluation(self):
        """The evaluation script can resume from existing results."""
        path = Path("data/clean_eval_results.json")
        data = json.loads(path.read_text())
        results = data["results"]
        done_ids = {r["scenario_id"] for r in results}
        residuals = load_residuals(Path("data"))
        remaining = [r for r in residuals if r.scenario_id not in done_ids]
        assert len(remaining) == 32, f"Expected 32 remaining, got {len(remaining)}"
