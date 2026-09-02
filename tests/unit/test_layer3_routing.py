"""
Day 5.1 tests: Layer 3 deterministic guardrail routing.

These tests exercise reconciliation.layer3.route only. The Day 4.6
single-outcome helper (reconciliation.routing) is intentionally untouched and
covered separately in test_routing.py.
"""
from __future__ import annotations

from datetime import date
from typing import List

import pytest

from reconciliation.domain.models import (
    NormalizedRecord,
    ReconciliationDecision,
    ResolutionLayer,
    SourceType,
)
from reconciliation.layer3 import (
    RoutingBucket,
    RoutingReason,
    RoutingDecision,
    route,
)
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_validation import ProposalOutcome, ProposalOutcomeType

from tests.conftest import make_record


def _l1_decision(*member_ids: str) -> ReconciliationDecision:
    return ReconciliationDecision(
        decision_id=f"L1-{'_'.join(member_ids)}",
        member_record_ids=tuple(member_ids),
        resolution_layer=ResolutionLayer.LAYER_1,
        rule_or_rationale="EXACT_ID",
        confidence=1.0,
    )


def _outcome(
    outcome_type: ProposalOutcomeType,
    *,
    confidence: float | None = None,
    proposed: List[str] | None = None,
    presented: List[str] | None = None,
) -> ProposalOutcome:
    proposal = None
    if outcome_type == ProposalOutcomeType.PROPOSAL_VALID and confidence is not None:
        proposal = MatchProposal(
            proposed_match_ids=proposed or [],
            confidence=confidence,
            rationale="test",
        )
    return ProposalOutcome(
        outcome=outcome_type,
        proposal=proposal,
        presented_record_ids=tuple(presented or []),
        reason="test",
    )


def _decisions_by_id(results: List[RoutingDecision]) -> dict[str, RoutingDecision]:
    return {r.record_id: r for r in results}


class TestDeterministicMatch:
    def test_layer1_record_routes_deterministic(self):
        records = [make_record(record_id="R1", source_type="SETTLEMENT",
                               source_native_id="s1", amount_paise=100, date=date(2024, 1, 1))]
        results = route([_l1_decision("R1", "RX")], [], records)
        by_id = _decisions_by_id(results)
        assert by_id["R1"].bucket == RoutingBucket.DETERMINISTIC_MATCH
        assert by_id["R1"].reason == RoutingReason.LAYER1_DETERMINISTIC
        assert by_id["R1"].source_decision_id is not None

    def test_layer1_precedence_over_layer2(self):
        records = [make_record(record_id="R1", source_type="SETTLEMENT",
                               source_native_id="s1", amount_paise=100, date=date(2024, 1, 1))]
        l2 = _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.99,
                      proposed=["R1"], presented=["R1"])
        results = route([_l1_decision("R1", "RX")], [l2], records)
        assert _decisions_by_id(results)["R1"].bucket == RoutingBucket.DETERMINISTIC_MATCH


class TestAiBuckets:
    def test_high_confidence_auto_accepted_with_evidence(self):
        """High confidence + cross-source pair with order_id match → auto-accepted."""
        records = [
            make_record(record_id="R1", source_type="SETTLEMENT",
                        source_native_id="s1", order_id_hint="ORD-1",
                        amount_paise=100, date=date(2024, 1, 1)),
            make_record(record_id="R2", source_type="BANK",
                        source_native_id="b1", order_id_hint="ORD-1",
                        amount_paise=100, date=date(2024, 1, 1)),
        ]
        l2 = _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.95,
                      proposed=["R1", "R2"], presented=["R1", "R2"])
        results = route([], [l2], records)
        by_id = _decisions_by_id(results)
        assert by_id["R1"].bucket == RoutingBucket.AI_AUTO_ACCEPTED
        assert by_id["R1"].reason == RoutingReason.AI_CONFIDENT

    def test_single_record_proposal_cannot_auto_accept(self):
        """Single-record proposal has no relational evidence → not auto-accepted."""
        records = [make_record(record_id="R1", source_type="BANK",
                               source_native_id="b1", amount_paise=100, date=date(2024, 1, 1))]
        l2 = _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.95,
                      proposed=["R1"], presented=["R1"])
        results = route([], [l2], records)
        by_id = _decisions_by_id(results)
        assert by_id["R1"].bucket == RoutingBucket.HUMAN_REVIEW
        assert by_id["R1"].reason == RoutingReason.LOW_EVIDENCE

    def test_mid_confidence_human_review(self):
        """Mid confidence, 2-record pair with evidence → HUMAN_REVIEW."""
        records = [
            make_record(record_id="R1", source_type="SETTLEMENT",
                        source_native_id="s1", order_id_hint="ORD-1",
                        amount_paise=100, date=date(2024, 1, 1)),
            make_record(record_id="R2", source_type="BANK",
                        source_native_id="b1", order_id_hint="ORD-1",
                        amount_paise=100, date=date(2024, 1, 1)),
        ]
        l2 = _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.75,
                      proposed=["R1", "R2"], presented=["R1", "R2"])
        results = route([], [l2], records)
        assert _decisions_by_id(results)["R1"].bucket == RoutingBucket.HUMAN_REVIEW
        assert _decisions_by_id(results)["R1"].reason == RoutingReason.AI_NEEDS_REVIEW

    def test_threshold_exactly_0_90_auto_accepted_with_evidence(self):
        """Exactly 0.90 confidence + evidence → auto-accepted."""
        records = [
            make_record(record_id="R1", source_type="SETTLEMENT",
                        source_native_id="s1", order_id_hint="ORD-1",
                        amount_paise=100, date=date(2024, 1, 1)),
            make_record(record_id="R2", source_type="BANK",
                        source_native_id="b1", order_id_hint="ORD-1",
                        amount_paise=100, date=date(2024, 1, 1)),
        ]
        l2 = _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.90,
                      proposed=["R1", "R2"], presented=["R1", "R2"])
        results = route([], [l2], records)
        assert _decisions_by_id(results)["R1"].bucket == RoutingBucket.AI_AUTO_ACCEPTED

    def test_threshold_exactly_0_60_human_review(self):
        records = [make_record(record_id="R1", source_type="BANK",
                               source_native_id="b1", amount_paise=100, date=date(2024, 1, 1))]
        l2 = _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.60,
                      proposed=["R1"], presented=["R1"])
        results = route([], [l2], records)
        assert _decisions_by_id(results)["R1"].bucket == RoutingBucket.HUMAN_REVIEW


class TestExceptions:
    def test_low_confidence_exception(self):
        records = [make_record(record_id="R1", source_type="BANK",
                               source_native_id="b1", amount_paise=100, date=date(2024, 1, 1))]
        l2 = _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.50,
                      proposed=["R1"], presented=["R1"])
        results = route([], [l2], records)
        by_id = _decisions_by_id(results)
        assert by_id["R1"].bucket == RoutingBucket.EXCEPTION
        assert by_id["R1"].reason == RoutingReason.LOW_CONFIDENCE

    def test_no_candidate_exception(self):
        records = [make_record(record_id="R1", source_type="BANK",
                               source_native_id="b1", amount_paise=100, date=date(2024, 1, 1))]
        l2 = _outcome(ProposalOutcomeType.NO_PROPOSAL, presented=["R1"])
        results = route([], [l2], records)
        by_id = _decisions_by_id(results)
        assert by_id["R1"].bucket == RoutingBucket.EXCEPTION
        assert by_id["R1"].reason == RoutingReason.NO_CANDIDATE

    @pytest.mark.parametrize(
        "outcome_type",
        [
            ProposalOutcomeType.API_ERROR,
            ProposalOutcomeType.TIMEOUT,
            ProposalOutcomeType.VALIDATION_FAILED,
        ],
    )
    def test_ai_response_invalid_exception(self, outcome_type):
        records = [make_record(record_id="R1", source_type="BANK",
                               source_native_id="b1", amount_paise=100, date=date(2024, 1, 1))]
        l2 = _outcome(outcome_type, presented=["R1"])
        results = route([], [l2], records)
        by_id = _decisions_by_id(results)
        assert by_id["R1"].bucket == RoutingBucket.EXCEPTION
        assert by_id["R1"].reason == RoutingReason.AI_RESPONSE_INVALID
        assert by_id["R1"].source_outcome == outcome_type

    def test_true_residual_with_no_layer2_attempt_not_dropped(self):
        records = [make_record(record_id="R1", source_type="LEDGER",
                               source_native_id="l1", amount_paise=100, date=date(2024, 1, 1))]
        results = route([], [], records)
        by_id = _decisions_by_id(results)
        assert by_id["R1"].bucket == RoutingBucket.EXCEPTION
        assert by_id["R1"].reason == RoutingReason.NO_CANDIDATE


class TestCoverageAndInvariants:
    def test_exactly_one_decision_per_record(self):
        records = [
            make_record(record_id=f"R{i}", source_type="BANK",
                        source_native_id=f"b{i}", amount_paise=100, date=date(2024, 1, 1))
            for i in range(5)
        ]
        l1 = [_l1_decision("R0", "RX")]
        l2 = [
            _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.95,
                     proposed=["R1"], presented=["R1"]),
            _outcome(ProposalOutcomeType.NO_PROPOSAL, presented=["R2"]),
            _outcome(ProposalOutcomeType.API_ERROR, presented=["R3"]),
        ]
        results = route(l1, l2, records)
        assert len(results) == len(records)
        assert {r.record_id for r in results} == {f"R{i}" for i in range(5)}
        assert len({r.record_id for r in results}) == len(results)

    def test_inputs_not_mutated(self):
        records = [make_record(record_id="R1", source_type="BANK",
                               source_native_id="b1", amount_paise=100, date=date(2024, 1, 1))]
        l1 = [_l1_decision("R1", "RX")]
        l2_in = [
            _outcome(ProposalOutcomeType.PROPOSAL_VALID, confidence=0.99,
                     proposed=["R1"], presented=["R1"]),
        ]
        l2_snapshot = [
            (o.outcome, o.proposal, tuple(o.presented_record_ids)) for o in l2_in
        ]
        records_snapshot = [(r.record_id, r.amount_paise) for r in records]

        route(l1, l2_in, records)

        assert [(o.outcome, o.proposal, tuple(o.presented_record_ids)) for o in l2_in] == l2_snapshot
        assert [(r.record_id, r.amount_paise) for r in records] == records_snapshot
        assert l1[0].member_record_ids == ("R1", "RX")


# ---------------------------------------------------------------------------
# LATE-008 Regression Test
# ---------------------------------------------------------------------------


class TestLate008Regression:
    """LATE-008: same amount, different order IDs, 21-day date gap.

    Must NOT produce AI_AUTO_ACCEPTED regardless of confidence.
    Expected: HUMAN_REVIEW / LOW_EVIDENCE.
    """

    def test_late_008_cannot_auto_accept(self):
        settlement = make_record(
            record_id="SETTLEMENT-00dd16aa6c51",
            source_type="SETTLEMENT",
            source_native_id="SET-b5f7b21a-001",
            order_id_hint="ORD-b5f7b21a-A",
            amount_paise=867000,
            date=date(2026, 8, 1),
        )
        bank = make_record(
            record_id="BANK-9b42ebd53333",
            source_type="BANK",
            source_native_id="BNK-b5f7b21a-001",
            order_id_hint="ORD-b5f7b21a-B",
            amount_paise=867000,
            date=date(2026, 8, 22),
        )
        records = [settlement, bank]

        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID,
            confidence=0.90,
            proposed=["SETTLEMENT-00dd16aa6c51", "BANK-9b42ebd53333"],
            presented=["SETTLEMENT-00dd16aa6c51", "BANK-9b42ebd53333"],
        )
        results = route([], [l2], records)
        by_id = _decisions_by_id(results)

        # Both records must NOT be AI_AUTO_ACCEPTED
        for rid in ["SETTLEMENT-00dd16aa6c51", "BANK-9b42ebd53333"]:
            assert by_id[rid].bucket != RoutingBucket.AI_AUTO_ACCEPTED, (
                f"LATE-008 record {rid} must not be AI_AUTO_ACCEPTED"
            )
            assert by_id[rid].bucket == RoutingBucket.HUMAN_REVIEW
            assert by_id[rid].reason == RoutingReason.LOW_EVIDENCE

    def test_late_008_high_confidence_still_blocked(self):
        """Even with confidence=0.99, the 21-day gap blocks auto-accept."""
        settlement = make_record(
            record_id="SET-LATE008",
            source_type="SETTLEMENT",
            source_native_id="SET-x",
            order_id_hint="ORD-A",
            amount_paise=500000,
            date=date(2026, 8, 1),
        )
        bank = make_record(
            record_id="BNK-LATE008",
            source_type="BANK",
            source_native_id="BNK-x",
            order_id_hint="ORD-B",
            amount_paise=500000,
            date=date(2026, 8, 22),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID,
            confidence=0.99,
            proposed=["SET-LATE008", "BNK-LATE008"],
            presented=["SET-LATE008", "BNK-LATE008"],
        )
        results = route([], [l2], [settlement, bank])
        by_id = _decisions_by_id(results)
        assert by_id["SET-LATE008"].bucket == RoutingBucket.HUMAN_REVIEW
        assert by_id["BNK-LATE008"].bucket == RoutingBucket.HUMAN_REVIEW


# ---------------------------------------------------------------------------
# 3+ Record Bypass Regression Tests
# ---------------------------------------------------------------------------


class TestMultiRecordBypass:
    """Proposals with 3+ records must NOT bypass the evidence gate."""

    def test_three_record_insufficient_evidence_blocks_auto_accept(self):
        """3 records with no valid split structure → NOT AI_AUTO_ACCEPTED."""
        settlement = make_record(
            record_id="SET-SPLIT",
            source_type="SETTLEMENT",
            source_native_id="SET-s1",
            order_id_hint="ORD-A",
            amount_paise=1000000,
            date=date(2026, 8, 1),
        )
        bank1 = make_record(
            record_id="BNK-SPLIT1",
            source_type="BANK",
            source_native_id="BNK-s1",
            order_id_hint="ORD-B",
            amount_paise=600000,
            date=date(2026, 8, 1),
        )
        bank2 = make_record(
            record_id="BNK-SPLIT2",
            source_type="BANK",
            source_native_id="BNK-s2",
            order_id_hint="ORD-C",
            amount_paise=500000,
            date=date(2026, 8, 1),
        )
        records = [settlement, bank1, bank2]
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID,
            confidence=0.95,
            proposed=["SET-SPLIT", "BNK-SPLIT1", "BNK-SPLIT2"],
            presented=["SET-SPLIT", "BNK-SPLIT1", "BNK-SPLIT2"],
        )
        results = route([], [l2], records)
        by_id = _decisions_by_id(results)

        # Sum of parts (1100000) != whole (1000000), exceeds tolerance
        for rid in ["SET-SPLIT", "BNK-SPLIT1", "BNK-SPLIT2"]:
            assert by_id[rid].bucket != RoutingBucket.AI_AUTO_ACCEPTED

    def test_three_record_same_source_blocks_auto_accept(self):
        """3 same-source records → NOT AI_AUTO_ACCEPTED (not a valid split)."""
        s1 = make_record(
            record_id="S1", source_type="SETTLEMENT",
            source_native_id="SET-1", amount_paise=100000, date=date(2026, 8, 1),
        )
        s2 = make_record(
            record_id="S2", source_type="SETTLEMENT",
            source_native_id="SET-2", amount_paise=100000, date=date(2026, 8, 1),
        )
        s3 = make_record(
            record_id="S3", source_type="SETTLEMENT",
            source_native_id="SET-3", amount_paise=100000, date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID,
            confidence=0.95,
            proposed=["S1", "S2", "S3"],
            presented=["S1", "S2", "S3"],
        )
        results = route([], [l2], [s1, s2, s3])
        by_id = _decisions_by_id(results)
        for rid in ["S1", "S2", "S3"]:
            assert by_id[rid].bucket != RoutingBucket.AI_AUTO_ACCEPTED

    def test_three_record_all_different_sources_blocks_auto_accept(self):
        """3 records from 3 different sources → NOT AI_AUTO_ACCEPTED."""
        set_rec = make_record(
            record_id="SET-3", source_type="SETTLEMENT",
            source_native_id="SET-1", amount_paise=100000, date=date(2026, 8, 1),
        )
        bnk_rec = make_record(
            record_id="BNK-3", source_type="BANK",
            source_native_id="BNK-1", amount_paise=50000, date=date(2026, 8, 1),
        )
        led_rec = make_record(
            record_id="LED-3", source_type="LEDGER",
            source_native_id="LED-1", amount_paise=50000, date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID,
            confidence=0.95,
            proposed=["SET-3", "BNK-3", "LED-3"],
            presented=["SET-3", "BNK-3", "LED-3"],
        )
        results = route([], [l2], [set_rec, bnk_rec, led_rec])
        by_id = _decisions_by_id(results)
        for rid in ["SET-3", "BNK-3", "LED-3"]:
            assert by_id[rid].bucket != RoutingBucket.AI_AUTO_ACCEPTED


# ---------------------------------------------------------------------------
# Legitimate Multi-record (Split Settlement) Tests
# ---------------------------------------------------------------------------


class TestLegitimateSplitSettlement:
    """Legitimate split settlement: 1 settlement + 2 banks, sum(ports) ≈ whole."""

    def test_valid_split_settlement_passes_evidence_gate(self):
        """Sum of bank amounts == settlement amount, dates compatible."""
        settlement = make_record(
            record_id="SET-SPLT",
            source_type="SETTLEMENT",
            source_native_id="SET-s1",
            order_id_hint="ORD-A",
            amount_paise=1000000,
            date=date(2026, 8, 1),
        )
        bank1 = make_record(
            record_id="BNK-SPLT1",
            source_type="BANK",
            source_native_id="BNK-s1",
            order_id_hint="ORD-B",
            amount_paise=490000,
            date=date(2026, 8, 1),
        )
        bank2 = make_record(
            record_id="BNK-SPLT2",
            source_type="BANK",
            source_native_id="BNK-s2",
            order_id_hint="ORD-C",
            amount_paise=510000,
            date=date(2026, 8, 2),
        )
        records = [settlement, bank1, bank2]
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID,
            confidence=0.90,
            proposed=["SET-SPLT", "BNK-SPLT1", "BNK-SPLT2"],
            presented=["SET-SPLT", "BNK-SPLT1", "BNK-SPLT2"],
        )
        results = route([], [l2], records)
        by_id = _decisions_by_id(results)
        # Sum == whole, dates within window → evidence OK
        assert by_id["SET-SPLT"].bucket == RoutingBucket.AI_AUTO_ACCEPTED

    def test_split_settlement_with_tiny_amount_diff_passes(self):
        """Sum of parts differs from whole by 50 paise (within tolerance)."""
        settlement = make_record(
            record_id="SET-SPLT2",
            source_type="SETTLEMENT",
            source_native_id="SET-s2",
            order_id_hint="ORD-A",
            amount_paise=1000000,
            date=date(2026, 8, 1),
        )
        bank1 = make_record(
            record_id="BNK-SPLT2a",
            source_type="BANK",
            source_native_id="BNK-s2a",
            order_id_hint="ORD-B",
            amount_paise=500000,
            date=date(2026, 8, 1),
        )
        bank2 = make_record(
            record_id="BNK-SPLT2b",
            source_type="BANK",
            source_native_id="BNK-s2b",
            order_id_hint="ORD-C",
            amount_paise=499950,
            date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID,
            confidence=0.92,
            proposed=["SET-SPLT2", "BNK-SPLT2a", "BNK-SPLT2b"],
            presented=["SET-SPLT2", "BNK-SPLT2a", "BNK-SPLT2b"],
        )
        results = route([], [l2], [settlement, bank1, bank2])
        by_id = _decisions_by_id(results)
        assert by_id["SET-SPLT2"].bucket == RoutingBucket.AI_AUTO_ACCEPTED

    def test_split_settlement_date_exceeds_window_blocks(self):
        """Parts have dates too far from whole → NOT AI_AUTO_ACCEPTED."""
        settlement = make_record(
            record_id="SET-SPLT3",
            source_type="SETTLEMENT",
            source_native_id="SET-s3",
            order_id_hint="ORD-A",
            amount_paise=1000000,
            date=date(2026, 8, 1),
        )
        bank1 = make_record(
            record_id="BNK-SPLT3a",
            source_type="BANK",
            source_native_id="BNK-s3a",
            order_id_hint="ORD-B",
            amount_paise=500000,
            date=date(2026, 8, 1),
        )
        bank2 = make_record(
            record_id="BNK-SPLT3b",
            source_type="BANK",
            source_native_id="BNK-s3b",
            order_id_hint="ORD-C",
            amount_paise=500000,
            date=date(2026, 8, 15),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID,
            confidence=0.95,
            proposed=["SET-SPLT3", "BNK-SPLT3a", "BNK-SPLT3b"],
            presented=["SET-SPLT3", "BNK-SPLT3a", "BNK-SPLT3b"],
        )
        results = route([], [l2], [settlement, bank1, bank2])
        by_id = _decisions_by_id(results)
        assert by_id["SET-SPLT3"].bucket != RoutingBucket.AI_AUTO_ACCEPTED

    def test_split_settlement_amount_mismatch_blocks(self):
        """Sum of parts exceeds whole by more than tolerance → blocked."""
        settlement = make_record(
            record_id="SET-SPLT4",
            source_type="SETTLEMENT",
            source_native_id="SET-s4",
            order_id_hint="ORD-A",
            amount_paise=1000000,
            date=date(2026, 8, 1),
        )
        bank1 = make_record(
            record_id="BNK-SPLT4a",
            source_type="BANK",
            source_native_id="BNK-s4a",
            order_id_hint="ORD-B",
            amount_paise=500000,
            date=date(2026, 8, 1),
        )
        bank2 = make_record(
            record_id="BNK-SPLT4b",
            source_type="BANK",
            source_native_id="BNK-s4b",
            order_id_hint="ORD-C",
            amount_paise=600000,
            date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID,
            confidence=0.95,
            proposed=["SET-SPLT4", "BNK-SPLT4a", "BNK-SPLT4b"],
            presented=["SET-SPLT4", "BNK-SPLT4a", "BNK-SPLT4b"],
        )
        results = route([], [l2], [settlement, bank1, bank2])
        by_id = _decisions_by_id(results)
        assert by_id["SET-SPLT4"].bucket != RoutingBucket.AI_AUTO_ACCEPTED


# ---------------------------------------------------------------------------
# Same-source Duplicate Evidence Tests
# ---------------------------------------------------------------------------


class TestSameSourceEvidence:
    """Same-source 2-record proposals must have sufficient evidence."""

    def test_legitimate_duplicate_with_order_id_passes(self):
        """Same source, same order_id, same amount, same date → evidence OK."""
        s1 = make_record(
            record_id="SET-DUP1", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-X",
            amount_paise=500000, date=date(2026, 8, 1),
        )
        s2 = make_record(
            record_id="SET-DUP2", source_type="SETTLEMENT",
            source_native_id="SET-2", order_id_hint="ORD-X",
            amount_paise=500000, date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID,
            confidence=0.90,
            proposed=["SET-DUP1", "SET-DUP2"],
            presented=["SET-DUP1", "SET-DUP2"],
        )
        results = route([], [l2], [s1, s2])
        by_id = _decisions_by_id(results)
        assert by_id["SET-DUP1"].bucket == RoutingBucket.AI_AUTO_ACCEPTED

    def test_same_source_different_order_id_different_date_blocked(self):
        """Same source, same amount, but different order_id and date → blocked."""
        s1 = make_record(
            record_id="SET-WEAK1", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-A",
            amount_paise=500000, date=date(2026, 8, 1),
        )
        s2 = make_record(
            record_id="SET-WEAK2", source_type="SETTLEMENT",
            source_native_id="SET-2", order_id_hint="ORD-B",
            amount_paise=500000, date=date(2026, 8, 15),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID,
            confidence=0.95,
            proposed=["SET-WEAK1", "SET-WEAK2"],
            presented=["SET-WEAK1", "SET-WEAK2"],
        )
        results = route([], [l2], [s1, s2])
        by_id = _decisions_by_id(results)
        assert by_id["SET-WEAK1"].bucket != RoutingBucket.AI_AUTO_ACCEPTED
        assert by_id["SET-WEAK1"].bucket == RoutingBucket.HUMAN_REVIEW
        assert by_id["SET-WEAK1"].reason == RoutingReason.LOW_EVIDENCE

    def test_same_source_different_order_id_same_date_passes(self):
        """Same source, same amount, no order_id match but same date → passes."""
        s1 = make_record(
            record_id="SET-WEAK3", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-A",
            amount_paise=500000, date=date(2026, 8, 1),
        )
        s2 = make_record(
            record_id="SET-WEAK4", source_type="SETTLEMENT",
            source_native_id="SET-2", order_id_hint="ORD-B",
            amount_paise=500000, date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID,
            confidence=0.90,
            proposed=["SET-WEAK3", "SET-WEAK4"],
            presented=["SET-WEAK3", "SET-WEAK4"],
        )
        results = route([], [l2], [s1, s2])
        by_id = _decisions_by_id(results)
        assert by_id["SET-WEAK3"].bucket == RoutingBucket.AI_AUTO_ACCEPTED

    def test_same_source_no_order_id_different_dates_blocked(self):
        """Same source, no order_id on either, different dates → blocked."""
        s1 = make_record(
            record_id="SET-NOID1", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint=None,
            amount_paise=500000, date=date(2026, 8, 1),
        )
        s2 = make_record(
            record_id="SET-NOID2", source_type="SETTLEMENT",
            source_native_id="SET-2", order_id_hint=None,
            amount_paise=500000, date=date(2026, 8, 15),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID,
            confidence=0.95,
            proposed=["SET-NOID1", "SET-NOID2"],
            presented=["SET-NOID1", "SET-NOID2"],
        )
        results = route([], [l2], [s1, s2])
        by_id = _decisions_by_id(results)
        assert by_id["SET-NOID1"].bucket != RoutingBucket.AI_AUTO_ACCEPTED

    def test_same_source_exact_amount_differs_beyond_tolerance_blocked(self):
        """Same source, amounts differ by more than tolerance → blocked."""
        s1 = make_record(
            record_id="SET-AMT1", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-X",
            amount_paise=500000, date=date(2026, 8, 1),
        )
        s2 = make_record(
            record_id="SET-AMT2", source_type="SETTLEMENT",
            source_native_id="SET-2", order_id_hint="ORD-X",
            amount_paise=500200, date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID,
            confidence=0.95,
            proposed=["SET-AMT1", "SET-AMT2"],
            presented=["SET-AMT1", "SET-AMT2"],
        )
        results = route([], [l2], [s1, s2])
        by_id = _decisions_by_id(results)
        assert by_id["SET-AMT1"].bucket != RoutingBucket.AI_AUTO_ACCEPTED


# ---------------------------------------------------------------------------
# Two-record cross-source evidence tests
# ---------------------------------------------------------------------------


class TestTwoRecordCrossSourceEvidence:
    """Cross-source 2-record proposals with various evidence patterns."""

    def test_exact_match_passes(self):
        """Same order_id, same amount, close date → passes."""
        set_rec = make_record(
            record_id="SET-EX", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-1",
            amount_paise=100000, date=date(2026, 8, 1),
        )
        bnk_rec = make_record(
            record_id="BNK-EX", source_type="BANK",
            source_native_id="BNK-1", order_id_hint="ORD-1",
            amount_paise=100000, date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID, confidence=0.95,
            proposed=["SET-EX", "BNK-EX"], presented=["SET-EX", "BNK-EX"],
        )
        results = route([], [l2], [set_rec, bnk_rec])
        assert _decisions_by_id(results)["SET-EX"].bucket == RoutingBucket.AI_AUTO_ACCEPTED

    def test_t_plus_delay_passes(self):
        """Same order_id, same amount, 2-day gap → passes."""
        set_rec = make_record(
            record_id="SET-TP", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-1",
            amount_paise=100000, date=date(2026, 8, 1),
        )
        bnk_rec = make_record(
            record_id="BNK-TP", source_type="BANK",
            source_native_id="BNK-1", order_id_hint="ORD-1",
            amount_paise=100000, date=date(2026, 8, 3),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID, confidence=0.92,
            proposed=["SET-TP", "BNK-TP"], presented=["SET-TP", "BNK-TP"],
        )
        results = route([], [l2], [set_rec, bnk_rec])
        assert _decisions_by_id(results)["SET-TP"].bucket == RoutingBucket.AI_AUTO_ACCEPTED

    def test_rounding_difference_passes(self):
        """Different order_ids, amounts within tolerance, dates within window → passes."""
        set_rec = make_record(
            record_id="SET-RND", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-A",
            amount_paise=100000, date=date(2026, 8, 1),
        )
        bnk_rec = make_record(
            record_id="BNK-RND", source_type="BANK",
            source_native_id="BNK-1", order_id_hint="ORD-B",
            amount_paise=100003, date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID, confidence=0.91,
            proposed=["SET-RND", "BNK-RND"], presented=["SET-RND", "BNK-RND"],
        )
        results = route([], [l2], [set_rec, bnk_rec])
        assert _decisions_by_id(results)["SET-RND"].bucket == RoutingBucket.AI_AUTO_ACCEPTED

    def test_fee_deducted_no_order_id_no_date_blocks(self):
        """Different order_ids, amounts within tolerance, dates within window → passes.
        (This is the FEE_DEDUCTED pattern: different order IDs but same date.)"""
        set_rec = make_record(
            record_id="SET-FEE", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-A",
            amount_paise=100000, date=date(2026, 8, 1),
        )
        bnk_rec = make_record(
            record_id="BNK-FEE", source_type="BANK",
            source_native_id="BNK-1", order_id_hint="ORD-B",
            amount_paise=95000, date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID, confidence=0.90,
            proposed=["SET-FEE", "BNK-FEE"], presented=["SET-FEE", "BNK-FEE"],
        )
        results = route([], [l2], [set_rec, bnk_rec])
        # Amount diff = 5000 > tolerance=100 → evidence fails
        assert _decisions_by_id(results)["SET-FEE"].bucket != RoutingBucket.AI_AUTO_ACCEPTED

    def test_fee_deducted_within_tolerance_passes(self):
        """Fee deduction where the diff is within tolerance → passes."""
        set_rec = make_record(
            record_id="SET-FEE2", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-A",
            amount_paise=100000, date=date(2026, 8, 1),
        )
        bnk_rec = make_record(
            record_id="BNK-FEE2", source_type="BANK",
            source_native_id="BNK-1", order_id_hint="ORD-B",
            amount_paise=99950, date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID, confidence=0.90,
            proposed=["SET-FEE2", "BNK-FEE2"], presented=["SET-FEE2", "BNK-FEE2"],
        )
        results = route([], [l2], [set_rec, bnk_rec])
        assert _decisions_by_id(results)["SET-FEE2"].bucket == RoutingBucket.AI_AUTO_ACCEPTED

    def test_partial_refund_blocks(self):
        """Amount diff > tolerance → blocked."""
        set_rec = make_record(
            record_id="SET-REFD", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-A",
            amount_paise=200000, date=date(2026, 8, 1),
        )
        bnk_rec = make_record(
            record_id="BNK-REFD", source_type="BANK",
            source_native_id="BNK-1", order_id_hint="ORD-B",
            amount_paise=150000, date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID, confidence=0.95,
            proposed=["SET-REFD", "BNK-REFD"], presented=["SET-REFD", "BNK-REFD"],
        )
        results = route([], [l2], [set_rec, bnk_rec])
        assert _decisions_by_id(results)["SET-REFD"].bucket != RoutingBucket.AI_AUTO_ACCEPTED

    def test_late_arriving_blocks(self):
        """Different order_ids, same amount, but 10-day gap → blocked."""
        set_rec = make_record(
            record_id="SET-LATE", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-A",
            amount_paise=100000, date=date(2026, 8, 1),
        )
        bnk_rec = make_record(
            record_id="BNK-LATE", source_type="BANK",
            source_native_id="BNK-1", order_id_hint="ORD-B",
            amount_paise=100000, date=date(2026, 8, 11),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID, confidence=0.95,
            proposed=["SET-LATE", "BNK-LATE"], presented=["SET-LATE", "BNK-LATE"],
        )
        results = route([], [l2], [set_rec, bnk_rec])
        assert _decisions_by_id(results)["SET-LATE"].bucket != RoutingBucket.AI_AUTO_ACCEPTED

    def test_inconsistent_narration_blocks(self):
        """Different order_ids, different amounts, different dates → blocked."""
        set_rec = make_record(
            record_id="SET-NARR", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-A",
            amount_paise=1000000, date=date(2026, 8, 1),
        )
        bnk_rec = make_record(
            record_id="BNK-NARR", source_type="BANK",
            source_native_id="BNK-1", order_id_hint="ORD-B",
            amount_paise=1100000, date=date(2026, 8, 15),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID, confidence=0.95,
            proposed=["SET-NARR", "BNK-NARR"], presented=["SET-NARR", "BNK-NARR"],
        )
        results = route([], [l2], [set_rec, bnk_rec])
        assert _decisions_by_id(results)["SET-NARR"].bucket != RoutingBucket.AI_AUTO_ACCEPTED

    def test_true_orphan_single_record_blocks(self):
        """Single record proposal → blocked (< 2 records)."""
        rec = make_record(
            record_id="SET-ORPH", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-1",
            amount_paise=100000, date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID, confidence=0.95,
            proposed=["SET-ORPH"], presented=["SET-ORPH"],
        )
        results = route([], [l2], [rec])
        assert _decisions_by_id(results)["SET-ORPH"].bucket != RoutingBucket.AI_AUTO_ACCEPTED


# ---------------------------------------------------------------------------
# Duplicate Over-inclusion Regression
# ---------------------------------------------------------------------------


class TestDuplicateOverInclusionRegression:
    """Invalid duplicate group + unrelated cross-source record → NOT AI_AUTO_ACCEPTED."""

    def test_duplicate_overinclusion_caught_by_validation(self):
        """3-record proposal: 2 same-source dups + 1 unrelated cross-source.
        Should be caught by proposal validation (VALIDATION_FAILED),
        which routes to EXCEPTION/AI_RESPONSE_INVALID.
        """
        from reconciliation.proposal_validation import validate_proposal

        s1 = make_record(
            record_id="SET-DUP-A", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-X",
            amount_paise=500000, date=date(2026, 8, 1),
        )
        s2 = make_record(
            record_id="SET-DUP-B", source_type="SETTLEMENT",
            source_native_id="SET-2", order_id_hint="ORD-X",
            amount_paise=500000, date=date(2026, 8, 1),
        )
        bnk = make_record(
            record_id="BNK-UNRELATED", source_type="BANK",
            source_native_id="BNK-1", order_id_hint="ORD-Y",
            amount_paise=600000, date=date(2026, 8, 6),
        )

        proposal = MatchProposal(
            proposed_match_ids=["SET-DUP-A", "SET-DUP-B", "BNK-UNRELATED"],
            confidence=0.95,
            rationale="test",
        )
        outcome = validate_proposal(
            proposal, ["SET-DUP-A", "SET-DUP-B", "BNK-UNRELATED"],
            records_by_id={
                "SET-DUP-A": s1, "SET-DUP-B": s2, "BNK-UNRELATED": bnk,
            },
        )
        assert outcome.outcome == ProposalOutcomeType.VALIDATION_FAILED

    def test_valid_split_not_caught_by_overinclusion(self):
        """Legitimate split settlement should pass validation."""
        from reconciliation.proposal_validation import validate_proposal

        set_rec = make_record(
            record_id="SET-SPLT-V", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-A",
            amount_paise=1000000, date=date(2026, 8, 1),
        )
        bnk1 = make_record(
            record_id="BNK-SPLT-V1", source_type="BANK",
            source_native_id="BNK-1", order_id_hint="ORD-B",
            amount_paise=500000, date=date(2026, 8, 1),
        )
        bnk2 = make_record(
            record_id="BNK-SPLT-V2", source_type="BANK",
            source_native_id="BNK-2", order_id_hint="ORD-C",
            amount_paise=500000, date=date(2026, 8, 1),
        )

        proposal = MatchProposal(
            proposed_match_ids=["SET-SPLT-V", "BNK-SPLT-V1", "BNK-SPLT-V2"],
            confidence=0.90,
            rationale="test",
        )
        outcome = validate_proposal(
            proposal, ["SET-SPLT-V", "BNK-SPLT-V1", "BNK-SPLT-V2"],
            records_by_id={
                "SET-SPLT-V": set_rec,
                "BNK-SPLT-V1": bnk1,
                "BNK-SPLT-V2": bnk2,
            },
        )
        assert outcome.outcome == ProposalOutcomeType.PROPOSAL_VALID


# ---------------------------------------------------------------------------
# High-Confidence Adversarial Tests
# ---------------------------------------------------------------------------


class TestAdversarialHighConfidence:
    """Prove that confidence alone cannot override deterministic evidence."""

    def test_wrong_pair_high_confidence_blocks(self):
        """2 records, wrong relationship, confidence=0.99 → NOT auto-accepted."""
        r1 = make_record(
            record_id="ADV-WRONG1", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-A",
            amount_paise=500000, date=date(2026, 8, 1),
        )
        r2 = make_record(
            record_id="ADV-WRONG2", source_type="BANK",
            source_native_id="BNK-1", order_id_hint="ORD-B",
            amount_paise=500000, date=date(2026, 8, 15),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID, confidence=0.99,
            proposed=["ADV-WRONG1", "ADV-WRONG2"],
            presented=["ADV-WRONG1", "ADV-WRONG2"],
        )
        results = route([], [l2], [r1, r2])
        by_id = _decisions_by_id(results)
        # Different order_ids, 14-day gap → no evidence
        assert by_id["ADV-WRONG1"].bucket == RoutingBucket.HUMAN_REVIEW
        assert by_id["ADV-WRONG1"].reason == RoutingReason.LOW_EVIDENCE

    def test_arbitrary_three_record_high_confidence_blocks(self):
        """3 records, arbitrary structure, confidence=0.99 → NOT auto-accepted."""
        r1 = make_record(
            record_id="ADV-3R1", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-A",
            amount_paise=100000, date=date(2026, 8, 1),
        )
        r2 = make_record(
            record_id="ADV-3R2", source_type="BANK",
            source_native_id="BNK-1", order_id_hint="ORD-B",
            amount_paise=50000, date=date(2026, 8, 1),
        )
        r3 = make_record(
            record_id="ADV-3R3", source_type="BANK",
            source_native_id="BNK-2", order_id_hint="ORD-C",
            amount_paise=50000, date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID, confidence=0.99,
            proposed=["ADV-3R1", "ADV-3R2", "ADV-3R3"],
            presented=["ADV-3R1", "ADV-3R2", "ADV-3R3"],
        )
        results = route([], [l2], [r1, r2, r3])
        by_id = _decisions_by_id(results)
        # 1+2 source types, but sum(50000+50000)=100000 == whole=100000 → evidence passes
        # Actually this IS a valid split. Let me make it invalid.
        # Change r3 amount so sum != whole.
        r3_tweaked = make_record(
            record_id="ADV-3R3", source_type="BANK",
            source_native_id="BNK-2", order_id_hint="ORD-C",
            amount_paise=60000, date=date(2026, 8, 1),
        )
        results = route([], [l2], [r1, r2, r3_tweaked])
        by_id = _decisions_by_id(results)
        # sum(50000+60000)=110000 != whole=100000 → blocks
        assert by_id["ADV-3R1"].bucket != RoutingBucket.AI_AUTO_ACCEPTED
        assert by_id["ADV-3R1"].bucket == RoutingBucket.HUMAN_REVIEW
        assert by_id["ADV-3R1"].reason == RoutingReason.LOW_EVIDENCE

    def test_same_source_unrelated_high_confidence_blocks(self):
        """Same-source unrelated pair, confidence=0.99 → NOT auto-accepted."""
        r1 = make_record(
            record_id="ADV-SRC1", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-A",
            amount_paise=500000, date=date(2026, 8, 1),
        )
        r2 = make_record(
            record_id="ADV-SRC2", source_type="SETTLEMENT",
            source_native_id="SET-2", order_id_hint="ORD-B",
            amount_paise=500000, date=date(2026, 8, 10),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID, confidence=0.99,
            proposed=["ADV-SRC1", "ADV-SRC2"],
            presented=["ADV-SRC1", "ADV-SRC2"],
        )
        results = route([], [l2], [r1, r2])
        by_id = _decisions_by_id(results)
        # Same amount, but different order_ids and 9-day gap → no evidence
        assert by_id["ADV-SRC1"].bucket == RoutingBucket.HUMAN_REVIEW
        assert by_id["ADV-SRC1"].reason == RoutingReason.LOW_EVIDENCE

    def test_true_orphan_single_record_high_confidence_blocks(self):
        """Single record, confidence=0.99 → NOT auto-accepted (no relationship)."""
        rec = make_record(
            record_id="ADV-ORPH", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-1",
            amount_paise=100000, date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID, confidence=0.99,
            proposed=["ADV-ORPH"], presented=["ADV-ORPH"],
        )
        results = route([], [l2], [rec])
        by_id = _decisions_by_id(results)
        assert by_id["ADV-ORPH"].bucket != RoutingBucket.AI_AUTO_ACCEPTED
        assert by_id["ADV-ORPH"].bucket == RoutingBucket.HUMAN_REVIEW
        assert by_id["ADV-ORPH"].reason == RoutingReason.LOW_EVIDENCE

    def test_three_same_source_high_confidence_blocks(self):
        """3 same-source records, confidence=0.99 → NOT auto-accepted."""
        r1 = make_record(
            record_id="ADV-3S1", source_type="SETTLEMENT",
            source_native_id="SET-1", order_id_hint="ORD-A",
            amount_paise=50000, date=date(2026, 8, 1),
        )
        r2 = make_record(
            record_id="ADV-3S2", source_type="SETTLEMENT",
            source_native_id="SET-2", order_id_hint="ORD-B",
            amount_paise=50000, date=date(2026, 8, 1),
        )
        r3 = make_record(
            record_id="ADV-3S3", source_type="SETTLEMENT",
            source_native_id="SET-3", order_id_hint="ORD-C",
            amount_paise=50000, date=date(2026, 8, 1),
        )
        l2 = _outcome(
            ProposalOutcomeType.PROPOSAL_VALID, confidence=0.99,
            proposed=["ADV-3S1", "ADV-3S2", "ADV-3S3"],
            presented=["ADV-3S1", "ADV-3S2", "ADV-3S3"],
        )
        results = route([], [l2], [r1, r2, r3])
        by_id = _decisions_by_id(results)
        # 1 source type, 3 records → not a valid split
        assert by_id["ADV-3S1"].bucket != RoutingBucket.AI_AUTO_ACCEPTED
        assert by_id["ADV-3S1"].bucket == RoutingBucket.HUMAN_REVIEW
        assert by_id["ADV-3S1"].reason == RoutingReason.LOW_EVIDENCE
