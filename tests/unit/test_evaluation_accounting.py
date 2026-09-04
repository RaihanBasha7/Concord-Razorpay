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


class TestProviderFailureAccounting:
    """Regression tests for provider-failure accounting bug fix.

    The bug: provider failures (API_ERROR/TIMEOUT) were silently counted as
    false positives in precision and as false accepts in false-accept rate.
    The fix: provider failures map to UNKNOWN, never FP. They are tracked
    separately and excluded from correctness denominators.
    """

    def test_provider_failures_map_to_unknown_not_fp(self):
        """Provider failures (API_ERROR) must be classified as UNKNOWN, never FP."""
        report_path = Path("data/current_evaluation_report.json")
        assert report_path.exists(), "Current evaluation report not found"
        report = json.loads(report_path.read_text())

        # Check outcome_states exists and has UNKNOWN count
        outcome_states = report["layer2"]["outcome_states"]
        assert outcome_states["unknown"] == 15, (
            f"Expected 15 UNKNOWN (provider failures), got {outcome_states['unknown']}"
        )
        # Corrected SPLIT_SETTLEMENT scoring: the 10 SPLT scenarios with
        # has_real_match=true and structurally-correct proposals are CORRECT.
        assert outcome_states["correct"] == 26, (
            f"Expected 26 CORRECT after SPLIT_SETTLEMENT correction, "
            f"got {outcome_states['correct']}"
        )
        assert outcome_states["incorrect"] == 36, (
            f"Expected 36 INCORRECT after SPLIT_SETTLEMENT correction, "
            f"got {outcome_states['incorrect']}"
        )

        # Check precision metrics: unknown_correctness must be 0 at all thresholds
        # (provider failures have no confidence, so not in eligible set)
        for thr in ("0_90", "0_75", "0_60"):
            m = report[f"precision_at_{thr}"]
            assert m["unknown_correctness"] == 0, (
                f"Threshold {thr}: provider failures leaked into unknown_correctness"
            )

    def test_provider_failures_excluded_from_precision_denominators(self):
        """Provider failures must not appear in TP/FP counts."""
        report_path = Path("data/current_evaluation_report.json")
        report = json.loads(report_path.read_text())

        # All precision denominators should only count known-correctness records
        for thr in ("0_90", "0_75", "0_60"):
            m = report[f"precision_at_{thr}"]
            denom = m["true_positive"] + m["false_positive"]
            assert m["known_correctness"] == denom, (
                f"Threshold {thr}: precision denominator ({denom}) != "
                f"known_correctness ({m['known_correctness']})"
            )
            assert m["known_outcome_rate"] == 1.0, (
                f"Threshold {thr}: known_outcome_rate should be 1.0 (no unknowns in eligible)"
            )

    def test_provider_failures_not_counted_as_false_accepts(self):
        """Provider failures produce no proposal → cannot be false accepts."""
        report_path = Path("data/current_evaluation_report.json")
        report = json.loads(report_path.read_text())

        # false_accept_unknown_correctness should be 0
        # (AI_AUTO_ACCEPTED are all PROPOSAL_VALID with conf >= 0.90; provider failures are API_ERROR)
        assert report.get("false_accept_unknown_correctness", 0) == 0, (
            "Provider failures leaked into false accept unknown_correctness"
        )
        assert report["false_accept_known_correctness"] == 15, (
            "Expected 15 known auto-accepted, got "
            f"{report.get('false_accept_known_correctness')}"
        )

    def test_false_accept_rate_uses_only_known_correctness(self):
        """False accept rate denominator must be known-correctness auto-accepted, not total."""
        report_path = Path("data/current_evaluation_report.json")
        report = json.loads(report_path.read_text())

        fa_count = report["false_accept_count"]
        fa_known = report["false_accept_known_correctness"]
        fa_rate = report["false_accept_rate"]

        # Corrected SPLIT_SETTLEMENT scoring: 9 of the 15 auto-accepted are
        # legitimate SPLT matches, leaving 6 false accepts (2 DUP, 3 LATE,
        # 1 REFD) over 15 known-correctness auto-accepts.
        assert fa_known == 15, (
            f"Expected 15 known auto-accepted, got {fa_known}"
        )
        assert fa_count == 6, (
            f"Expected 6 false accepts after SPLIT_SETTLEMENT correction, "
            f"got {fa_count}"
        )
        assert abs(fa_rate - 0.4) < 1e-9, (
            f"Expected false accept rate 0.4 (6/15), got {fa_rate}"
        )

        # Rate should be count / known_correctness, not count / total_auto_accepted
        if fa_known > 0:
            expected_rate = fa_count / fa_known
            assert abs(fa_rate - expected_rate) < 1e-9, (
                f"FAR {fa_rate} != count({fa_count})/known({fa_known}) = {expected_rate}"
            )

    def test_unknown_outcomes_visible_in_report(self):
        """UNKNOWN outcomes must remain visible, never silently dropped."""
        report_path = Path("data/current_evaluation_report.json")
        report = json.loads(report_path.read_text())

        # outcome_states must explicitly report UNKNOWN
        os = report["layer2"]["outcome_states"]
        assert "unknown" in os, "UNKNOWN count missing from outcome_states"
        assert os["unknown"] == 15, f"Expected 15 UNKNOWN, got {os['unknown']}"

        # provider_failure breakdown must be present
        pf = report["layer2"]["provider_failure"]
        assert pf["total"] == 15
        assert pf["quota_exhausted"] == 7
        assert pf["transient"] == 7
        assert pf["unknown"] == 1

    def test_recall_reported_computed(self):
        """Recall must be computed from the artifact via the correlation_id ->
        scenario_id -> expected_match_ids mapping (regression: this was
        previously reported NOT_COMPUTABLE even though the mapping exists)."""
        report_path = Path("data/current_evaluation_report.json")
        report = json.loads(report_path.read_text())

        recall = report["recall"]
        assert recall["status"] == "COMPUTED", (
            f"Recall status should be COMPUTED, got {recall['status']}"
        )
        # 21/27 = 77.8%: attempted real-match residual scenarios, excluding
        # the DUPLICATE category (documented duplicate-scoring ambiguity).
        assert recall["true_positives"] == 21, (
            f"Expected 21 true positives, got {recall['true_positives']}"
        )
        assert recall["denominator"] == 27, (
            f"Expected denominator 27, got {recall['denominator']}"
        )
        assert recall["value"] == pytest.approx(21 / 27)
        assert "DUPLICATE" in recall["excluded_categories"]
        assert "correlation_id" in recall["note"].lower()

    def test_precision_is_outcome_level_not_proposal_level(self):
        """Precision metrics must be labeled as outcome-level, not proposal-level."""
        report_path = Path("data/current_evaluation_report.json")
        report = json.loads(report_path.read_text())

        for thr in ("0_90", "0_75", "0_60"):
            m = report[f"precision_at_{thr}"]
            assert "outcome-level" in m["note"].lower()
            assert "NOT COMPUTABLE" in m["note"] or "not computable" in m["note"].lower()


class TestSplitSettlementScoring:
    """Regression test for the SPLIT_SETTLEMENT scoring correction.

    expected_outcome: NO_MATCH in this dataset means "Layer 1's simple
    ID/amount matching cannot resolve this scenario", NOT "no correspondence
    exists". SPLIT_SETTLEMENT scenarios carry has_real_match: true; a
    PROPOSAL_VALID whose structure matches the ground-truth relationship
    (1 settlement amount == sum of 2 bank credits) is a correct,
    evidence-backed match — never a false accept.

    This pins the fix in scripts/build_current_report.py so the exception
    cannot silently regress, and confirms it is NOT extended to proposals
    whose structure does not match the scenario relationship.
    """

    @staticmethod
    def _run_summarize(records, gt_scenarios):
        """Run _summarize from scripts/build_current_report.py on synthetic data."""
        import importlib.util

        script = (
            Path(__file__).resolve().parent.parent.parent
            / "scripts"
            / "build_current_report.py"
        )
        spec = importlib.util.spec_from_file_location(
            "build_current_report", script
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        ground_truth = {s["scenario_id"]: s for s in gt_scenarios}
        return mod._summarize(records, [], ground_truth)

    @staticmethod
    def _audit_record(cid, confidence, proposed_ids):
        return {
            "correlation_id": cid,
            "outcome": "PROPOSAL_VALID",
            "confidence": confidence,
            "proposal": {"proposed_match_ids": proposed_ids},
            "reason": "test",
        }

    def test_split_settlement_valid_proposal_is_correct_not_false_accept(self):
        """A SPLT scenario with has_real_match=true and a structurally correct
        proposal is classified CORRECT and never counts as a false accept,
        while a structurally mismatched proposal stays INCORRECT."""
        gt_scenarios = [
            {
                "scenario_id": "SPLT-901",
                "category": "SPLIT_SETTLEMENT",
                "expected_outcome": "NO_MATCH",
                "has_real_match": True,
                "record_specs": [
                    {"source_type": "SETTLEMENT"},
                    {"source_type": "BANK"},
                    {"source_type": "BANK"},
                ],
            },
            {
                "scenario_id": "SPLT-902",
                "category": "SPLIT_SETTLEMENT",
                "expected_outcome": "NO_MATCH",
                "has_real_match": True,
                "record_specs": [
                    {"source_type": "SETTLEMENT"},
                    {"source_type": "BANK"},
                    {"source_type": "BANK"},
                ],
            },
        ]
        records = [
            # Correct: 1 settlement + 2 banks matches the scenario relationship.
            self._audit_record(
                "SPLT-901",
                0.95,
                ["SETTLEMENT-a", "BANK-b", "BANK-c"],
            ),
            # Incorrect: only 2 members proposed — the split settlement's
            # real relationship (1 settlement == 2 bank credits) is not met.
            self._audit_record(
                "SPLT-902",
                0.95,
                ["SETTLEMENT-a", "BANK-b"],
            ),
        ]

        summary = self._run_summarize(records, gt_scenarios)

        states = summary["outcome_states"]
        assert states["correct"] == 1, (
            f"SPLT-901 should be CORRECT, got states: {states}"
        )
        assert states["incorrect"] == 1, (
            f"SPLT-902 should be INCORRECT, got states: {states}"
        )
        assert states["unknown"] == 0

        # Both are auto-accepted (>= 0.90) with known correctness.
        fa = summary["false_accept"]
        assert fa["known_correctness"] == 2
        # Only the structurally-mismatched proposal is a false accept.
        assert fa["count"] == 1, f"Expected 1 false accept, got {fa['count']}"
        assert fa["details"][0]["scenario_id"] == "SPLT-902"
        assert fa["rate"] == 0.5

    def test_split_settlement_without_real_match_is_not_exempted(self):
        """A SPLT scenario with has_real_match=false (a true orphan variant)
        does NOT receive the exception — PROPOSAL_VALID stays INCORRECT."""
        gt_scenarios = [
            {
                "scenario_id": "SPLT-903",
                "category": "SPLIT_SETTLEMENT",
                "expected_outcome": "NO_MATCH",
                "has_real_match": False,
                "record_specs": [
                    {"source_type": "SETTLEMENT"},
                    {"source_type": "BANK"},
                    {"source_type": "BANK"},
                ],
            },
        ]
        records = [
            self._audit_record(
                "SPLT-903", 0.95, ["SETTLEMENT-a", "BANK-b", "BANK-c"]
            ),
        ]

        summary = self._run_summarize(records, gt_scenarios)
        states = summary["outcome_states"]
        assert states["correct"] == 0
        assert states["incorrect"] == 1, (
            f"SPLT with has_real_match=false must stay INCORRECT: {states}"
        )
        assert summary["false_accept"]["count"] == 1


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
