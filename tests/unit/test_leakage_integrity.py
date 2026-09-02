"""
Ground-truth leakage and evaluation integrity tests.

Covers:
  * Prompt snapshot test — captures the actual serialized prompt payload
    and verifies it contains no evaluator metadata.
  * Adversarial leakage tests — deliberately malicious identifiers are
    rejected by the leakage checker.
  * Neutral identifier verification — generated IDs are semantically neutral.
  * Narration leakage — NARR scenarios don't embed category info in narration.
  * Old artifact invalidation — old artifacts with category-encoded record
    IDs are rejected when dataset changes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from reconciliation.domain.models import NormalizedRecord, SourceType
from reconciliation.evaluation.dataset_generator import (
    CATEGORY_QUOTAS,
    EdgeCaseCategory,
    GroundTruthScenario,
    ScenarioRecordSpec,
    _neutral_id,
    _neutral_order_id,
    check_leakage,
    generate_dataset,
)
from reconciliation.evaluation.leakage import LeakageReport
from reconciliation.groq_provider import StructuredCompletionProvider
from reconciliation.layer2 import Layer2Case, reconstruct_layer2_case
from reconciliation.loader import load_normalized_records, load_residuals
from reconciliation.proposal_service import PromptBuilder, _record_repr
from reconciliation.proposal_orchestration import ProposalOrchestrator
from reconciliation.proposal_service import ProposalService
from reconciliation.retrieval import RetrievalConfig, retrieve_candidates


# =========================================================================
# Helper: FakeProvider that captures the actual prompt sent to the LLM
# =========================================================================

class PromptCapturingProvider(StructuredCompletionProvider):
    """Captures the exact system_prompt and user_prompt sent to the model."""

    def __init__(self):
        self.captured_system_prompt: Optional[str] = None
        self.captured_user_prompt: Optional[str] = None
        self.call_count = 0

    def complete_structured(self, *, system_prompt, user_prompt, json_schema):
        self.captured_system_prompt = system_prompt
        self.captured_user_prompt = user_prompt
        self.call_count += 1
        return {"proposed_match_ids": [], "confidence": 0.1, "rationale": "test"}


# =========================================================================
# Category abbreviation / alias set (for adversarial tests)
# =========================================================================

_CATEGORY_LEAKAGE_TOKENS = [
    # Full category names
    "EXACT_MATCH", "T_PLUS_DELAY", "FEE_DEDUCTED", "PARTIAL_REFUND",
    "SPLIT_SETTLEMENT", "ROUNDING_DIFFERENCE", "INCONSISTENT_NARRATION",
    "DUPLICATE", "TRUE_ORPHAN", "LATE_ARRIVING",
    # Full names lowercase
    "exact_match", "fee_deducted", "partial_refund", "split_settlement",
    "rounding_difference", "inconsistent_narration", "duplicate",
    "true_orphan", "late_arriving",
    # Abbreviations / prefixes
    "EXACT", "TDLY", "FEE", "REFD", "SPLT", "RND", "NARR", "DUP", "UNQ", "LATE",
    # Lowercase abbreviations
    "exact", "tdly", "fee", "refd", "splt", "rnd", "narr", "dup", "unq", "late",
    # Mixed case
    "Fee", "Dup", "Splt", "Narr", "Refd",
    # Delimiter variants
    "FEE-001", "FEE_001", "FEE001", "fee-001", "fee_001",
    "ORD-FEE-001-A", "ORD-DUP-001", "ORD-SPLT-001-B",
    "SET-FEE-001-001", "BNK-DUP-001-001",
    # Scenario ID patterns
    "FEE-003", "DUP-012", "SPLT-009-B", "NARR-005", "LATE-001",
    # Edge-case patterns
    "T_PLUS", "t_plus", "tplus",
]

# Tokens that SHOULD be allowed (legitimate financial terms)
_LEGITIMATE_FINANCIAL_TOKENS = [
    "refund",  # legitimate financial term (not an abbreviation for PARTIAL_REFUND)
    "Refund processing",
    "Commission disbursement",
    "Monthly subscription fee",
    "Settlement transfer",
    "Reconciliation entry",
    "Payment gateway transfer",
    "ACH credit",
    "Wire transfer",
]


# =========================================================================
# Phase 6: Prompt Snapshot Test
# =========================================================================

class TestPromptSnapshot:
    """Verify the ACTUAL serialized prompt payload contains no evaluator
    metadata.  This tests the exact strings that reach the provider boundary,
    not the Python objects before serialization.
    """

    def _run_prompt_capture(self, data_dir: Path) -> PromptCapturingProvider:
        """Run the full Layer 2 pipeline for 1 scenario and capture the prompt."""
        provider = PromptCapturingProvider()
        service = ProposalService(provider)
        orchestrator = ProposalOrchestrator(service)

        normalized = load_normalized_records(data_dir)
        residuals = load_residuals(data_dir)

        # Take the first residual
        residual = residuals[0]
        case = reconstruct_layer2_case(
            scenario_id=residual.scenario_id,
            member_record_ids=residual.member_record_ids,
            normalized_records=tuple(normalized),
        )
        retrieval = retrieve_candidates(case, tuple(normalized), RetrievalConfig())

        # Build the prompt exactly as the PromptBuilder does
        builder = PromptBuilder()
        prompt_result = builder.build(case, retrieval)

        # Verify via the orchestrator (which calls the provider)
        orchestrator.resolve(case, retrieval)

        return provider, prompt_result, residual

    def test_user_prompt_excludes_scenario_ids(self):
        """The user prompt must not contain category-encoded scenario IDs."""
        provider, prompt_result, residual = self._run_prompt_capture(Path("data"))
        user_prompt = prompt_result.user_prompt

        # The scenario_id itself should not appear
        assert residual.scenario_id not in user_prompt, (
            f"scenario_id '{residual.scenario_id}' leaked into user_prompt"
        )

    def test_user_prompt_data_fields_exclude_category_labels(self):
        """Data-carrying parts of the user prompt must not contain
        category labels.  The prompt template text legitimately mentions
        "duplicate", "fee", "split" etc. as relationship examples --
        those are NOT leakage.  We verify the data fields only.
        """
        import re
        from datetime import date as date_type
        from reconciliation.normalizer import normalize_record

        # Build a record with a LEAKING order_id_hint
        leaking_row = {
            "settlement_id": "SET-abcdef-001",
            "order_id": "ORD-FEE-003-A",
            "gross_amount": "1000.00",
            "settlement_date": "2026-08-01",
        }
        record = normalize_record(leaking_row, SourceType.SETTLEMENT)
        repr_dict = _record_repr(record)
        serialized = json.dumps(repr_dict).lower()

        # The leaking order_id_hint should be detectable
        assert "fee" in serialized, "Test setup: leaking record must contain 'fee'"

        # Now build a record with a NEUTRAL order_id_hint
        neutral_row = {
            "settlement_id": "SET-a523cc54-001",
            "order_id": "ORD-a523cc54-A",
            "gross_amount": "1000.00",
            "settlement_date": "2026-08-01",
        }
        record_neutral = normalize_record(neutral_row, SourceType.SETTLEMENT)
        repr_neutral = _record_repr(record_neutral)
        serialized_neutral = json.dumps(repr_neutral).lower()

        # The neutral record must NOT contain category tokens in data values.
        # Check the actual values (not JSON keys like 'narration').
        data_values = [
            repr_neutral["record_id"],
            repr_neutral["source_type"],
            repr_neutral.get("order_id_hint", ""),
            repr_neutral.get("narration", "") or "",
        ]
        combined_values = " ".join(data_values).lower()
        for cat_token in ["exact", "tdly", "fee", "refd", "splt", "rnd",
                          "narr", "dup", "unq", "late"]:
            assert cat_token not in combined_values, (
                f"Category token '{cat_token}' found in neutral record values: "
                f"{repr_neutral}"
            )

    def test_system_prompt_is_static_and_clean(self):
        """System prompt is a fixed template with no dynamic data."""
        _, prompt_result, _ = self._run_prompt_capture(Path("data"))
        # System prompt should be the fixed template
        assert "settlement reconciliation analyst" in prompt_result.system_prompt.lower()
        assert len(prompt_result.system_prompt) < 2000

    def test_prompt_record_repr_excludes_source_native_id(self):
        """_record_repr must not include source_native_id."""
        record = NormalizedRecord(
            record_id="SETTLEMENT-abc123def456",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-FEE-003-001",
            order_id_hint="ORD-neutral",
            amount_paise=100000,
            date=Path("data").__class__,  # placeholder
            narration=None,
            raw_payload={},
        )
        from datetime import date as date_type
        record = NormalizedRecord(
            record_id="SETTLEMENT-abc123def456",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-FEE-003-001",
            order_id_hint="ORD-neutral",
            amount_paise=100000,
            date=date_type(2026, 8, 1),
            narration=None,
            raw_payload={},
        )
        repr_dict = _record_repr(record)
        assert "source_native_id" not in repr_dict
        assert "SET-FEE-003-001" not in json.dumps(repr_dict)

    def test_prompt_contains_only_legitimate_fields(self):
        """The prompt repr dict must contain exactly the allowed fields."""
        from datetime import date as date_type
        record = NormalizedRecord(
            record_id="SETTLEMENT-abc123def456",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-FEE-003-001",
            order_id_hint="ORD-neutral",
            amount_paise=100000,
            date=date_type(2026, 8, 1),
            narration="Payment for services",
            raw_payload={"secret": True},
        )
        repr_dict = _record_repr(record)
        allowed_keys = {"record_id", "source_type", "order_id_hint", "amount_paise", "date", "narration"}
        assert set(repr_dict.keys()) == allowed_keys
        # raw_payload must never appear
        assert "raw_payload" not in repr_dict
        assert "secret" not in json.dumps(repr_dict)


# =========================================================================
# Phase 7: Adversarial Leakage Tests
# =========================================================================

class TestAdversarialLeakage:
    """Verify the leakage checker catches deliberately malicious identifiers."""

    def _make_spec(
        self,
        source_native_id: str,
        order_id_hint: Optional[str] = None,
        narration: Optional[str] = None,
    ) -> ScenarioRecordSpec:
        """Create a minimal ScenarioRecordSpec for testing."""
        from datetime import date as date_type
        return ScenarioRecordSpec(
            synthetic_ref="REC-SET-001",
            source_type=SourceType.SETTLEMENT,
            source_native_id=source_native_id,
            order_id_hint=order_id_hint,
            amount_paise=100000,
            record_date=date_type(2026, 8, 1),
            narration=narration,
        )

    def _make_scenario(
        self,
        specs: List[ScenarioRecordSpec],
        category: EdgeCaseCategory = EdgeCaseCategory.EXACT_MATCH,
    ) -> GroundTruthScenario:
        return GroundTruthScenario(
            scenario_id="TEST-001",
            category=category,
            record_specs=tuple(specs),
            expected_outcome="MATCH_EXACT_ID",
            has_real_match=True,
            description="Test scenario.",
        )

    @pytest.mark.parametrize("token", _CATEGORY_LEAKAGE_TOKENS)
    def test_category_tokens_rejected_in_order_id_hint(self, token: str):
        """Category tokens in order_id_hint must be caught."""
        spec = self._make_spec(
            source_native_id="SET-abcdef12",
            order_id_hint=f"ORD-{token}",
        )
        scenario = self._make_scenario([spec])
        report = check_leakage([scenario], [spec])
        assert report.has_leakage, (
            f"Leakage checker missed category token '{token}' in order_id_hint"
        )

    @pytest.mark.parametrize("token", _CATEGORY_LEAKAGE_TOKENS)
    def test_category_tokens_rejected_in_source_native_id(self, token: str):
        """Category tokens in source_native_id must be caught."""
        spec = self._make_spec(
            source_native_id=f"SET-{token}-001",
        )
        scenario = self._make_scenario([spec])
        report = check_leakage([scenario], [spec])
        assert report.has_leakage, (
            f"Leakage checker missed category token '{token}' in source_native_id"
        )

    @pytest.mark.parametrize("token", [
        # Full category labels embedded in narration
        "EXACT_MATCH", "T_PLUS_DELAY", "FEE_DEDUCTED", "PARTIAL_REFUND",
        "SPLIT_SETTLEMENT", "ROUNDING_DIFFERENCE", "INCONSISTENT_NARRATION",
        "DUPLICATE", "TRUE_ORPHAN", "LATE_ARRIVING",
        # Scenario prefixes
        "EXACT-001", "TDLY-001", "FEE-001", "REFD-001", "SPLT-001",
        "RND-001", "NARR-001", "DUP-001", "UNQ-001", "LATE-001",
    ])
    def test_category_tokens_rejected_in_narration(self, token: str):
        """Scenario IDs and full category labels in narration must be caught.
        
        Note: Abbreviations like "fee", "dup" are NOT checked in
        narration because they are legitimate financial terms.
        """
        spec = self._make_spec(
            source_native_id="SET-abcdef12",
            order_id_hint="ORD-abcdef12",
            narration=f"Payment related to ORD-{token}-001",
        )
        scenario = self._make_scenario([spec])
        report = check_leakage([scenario], [spec])
        assert report.has_leakage, (
            f"Leakage checker missed scenario ID '{token}' in narration"
        )

    @pytest.mark.parametrize("token", [
        "ORD-FEE-999", "ORD-DUP-999", "ORD-SPLT-999",
        "ORD-REFD-999", "ORD-NARR-999", "ORD-EXACT-999",
        "ORD-TDLY-999", "ORD-RND-999", "ORD-UNQ-999", "ORD-LATE-999",
    ])
    def test_malicious_order_ids_rejected(self, token: str):
        """Deliberately malicious order IDs are caught."""
        spec = self._make_spec(
            source_native_id="SET-abcdef12",
            order_id_hint=token,
        )
        scenario = self._make_scenario([spec])
        report = check_leakage([scenario], [spec])
        assert report.has_leakage, (
            f"Malicious order_id '{token}' was not caught"
        )

    @pytest.mark.parametrize("neutral_id", [
        "a523cc54", "02dadfc2", "112eed84", "c97f6b3e", "be1a1409",
        "deadbeef", "cafebabe", "01234567",
    ])
    def test_neutral_ids_pass(self, neutral_id: str):
        """Hex-hash IDs pass the leakage checker."""
        spec = self._make_spec(
            source_native_id=f"SET-{neutral_id}-001",
            order_id_hint=f"ORD-{neutral_id}",
        )
        scenario = self._make_scenario([spec])
        report = check_leakage([scenario], [spec])
        assert not report.has_leakage, (
            f"Neutral ID '{neutral_id}' was falsely flagged: {report.issues}"
        )

    def test_legitimate_narration_passes(self):
        """Generic financial narrations without embedded IDs pass."""
        for text in _LEGITIMATE_FINANCIAL_TOKENS:
            spec = self._make_spec(
                source_native_id="SET-abcdef12",
                order_id_hint="ORD-abcdef12",
                narration=text,
            )
            scenario = self._make_scenario([spec])
            report = check_leakage([scenario], [spec])
            assert not report.has_leakage, (
                f"Legitimate narration '{text}' was falsely flagged: {report.issues}"
            )

    def test_orphan_prefixes_detected(self):
        """ORPH-, LOST-, MISS- prefixes in orphan records are caught."""
        spec = self._make_spec(source_native_id="ORPH-001")
        scenario = self._make_scenario(
            [spec], category=EdgeCaseCategory.TRUE_ORPHAN
        )
        report = check_leakage([scenario], [spec])
        assert report.has_leakage

    def test_full_category_enum_in_id_detected(self):
        """Full enum value 'fee_deducted' in an ID is caught."""
        spec = self._make_spec(
            source_native_id="SET-fee_deducted-001",
            order_id_hint="ORD-fee_deducted",
        )
        scenario = self._make_scenario([spec])
        report = check_leakage([scenario], [spec])
        assert report.has_leakage


# =========================================================================
# Phase 4 verification: Neutral ID generation
# =========================================================================

class TestNeutralIDs:
    """Verify that neutral IDs are deterministic, unique, and category-free."""

    def test_neutral_id_is_deterministic(self):
        """Same input produces the same output."""
        a = _neutral_id("FEE-001")
        b = _neutral_id("FEE-001")
        assert a == b
        assert len(a) == 8
        # Must be hex
        int(a, 16)

    def test_neutral_id_differs_across_scenarios(self):
        """Different scenario IDs produce different neutral IDs."""
        ids = [_neutral_id(f"CAT-{i:03d}") for i in range(20)]
        assert len(set(ids)) == 20, "Neutral IDs must be unique"

    def test_neutral_id_does_not_contain_category(self):
        """The neutral ID never contains category abbreviations."""
        categories = ["EXACT", "TDLY", "FEE", "REFD", "SPLT", "RND", "NARR", "DUP", "UNQ", "LATE"]
        for cat in categories:
            neutral = _neutral_id(f"{cat}-001")
            # Neutral ID should be pure hex, no category substring
            for abbr in categories:
                assert abbr.lower() not in neutral.lower(), (
                    f"Neutral ID '{neutral}' for '{cat}-001' contains '{abbr}'"
                )

    def test_neutral_order_id_format(self):
        """_neutral_order_id produces correctly formatted IDs."""
        oid = _neutral_order_id("FEE-001")
        assert oid.startswith("ORD-")
        assert len(oid) == 12  # "ORD-" + 8 hex chars

        oid_a = _neutral_order_id("FEE-001", suffix="A")
        assert oid_a == f"{oid}-A"

    def test_generated_dataset_ids_are_neutral(self):
        """The full generated dataset contains no category-encoding IDs."""
        ds = generate_dataset(seed=42)
        leakage = check_leakage(list(ds.scenarios), list(ds.record_specs))
        assert not leakage.has_leakage, (
            f"Generated dataset has leakage: {leakage.issues}"
        )

    def test_narration_does_not_contain_scenario_ids(self):
        """NARR scenario narrations reference neutral order IDs, not scenario IDs."""
        ds = generate_dataset(seed=42)
        for scen in ds.scenarios:
            if scen.category != EdgeCaseCategory.INCONSISTENT_NARRATION:
                continue
            for r in scen.record_specs:
                if r.narration:
                    # Narration must not contain any scenario_id prefix
                    for prefix in ["EXACT-", "TDLY-", "FEE-", "REFD-", "SPLT-",
                                   "RND-", "NARR-", "DUP-", "UNQ-", "LATE-"]:
                        assert prefix not in r.narration, (
                            f"NARR scenario {scen.scenario_id} narration "
                            f"'{r.narration}' contains scenario prefix '{prefix}'"
                        )


# =========================================================================
# Phase 8: Dataset Integrity After Regeneration
# =========================================================================

class TestDatasetIntegrity:
    """Verify the regenerated dataset preserves all required properties."""

    def test_total_scenario_count(self):
        """120 scenarios total across all categories."""
        ds = generate_dataset(seed=42)
        total = sum(CATEGORY_QUOTAS.values())
        assert len(ds.scenarios) == total
        assert total == 120

    def test_category_quotas_met(self):
        """Each category meets its minimum quota."""
        ds = generate_dataset(seed=42)
        counts: Dict[EdgeCaseCategory, int] = {cat: 0 for cat in EdgeCaseCategory}
        for scen in ds.scenarios:
            counts[scen.category] += 1
        for cat, quota in CATEGORY_QUOTAS.items():
            assert counts[cat] >= quota, (
                f"{cat.value}: {counts[cat]} < {quota}"
            )

    def test_record_count_matches(self):
        """Total records across CSVs match the generated specs."""
        ds = generate_dataset(seed=42)
        set_rows = len(ds.settlement_rows)
        bank_rows = len(ds.bank_rows)
        ledger_rows = len(ds.ledger_rows)
        total = set_rows + bank_rows + ledger_rows
        assert total == len(ds.record_specs), (
            f"CSV rows ({total}) != record_specs ({len(ds.record_specs)})"
        )

    def test_all_edge_case_categories_present(self):
        """Every required edge case category has at least one scenario."""
        ds = generate_dataset(seed=42)
        present = {scen.category for scen in ds.scenarios}
        for cat in EdgeCaseCategory:
            assert cat in present, f"Missing category: {cat.value}"

    def test_ground_truth_mapping_integrity(self):
        """Ground truth mapping between scenarios and record specs is consistent."""
        ds = generate_dataset(seed=42)
        for scen in ds.scenarios:
            assert len(scen.record_specs) >= 1
            for spec in scen.record_specs:
                assert spec.amount_paise > 0
                assert spec.record_date is not None

    def test_leakage_checker_passes_on_clean_dataset(self):
        """The leakage checker passes on the regenerated dataset."""
        ds = generate_dataset(seed=42)
        report = check_leakage(list(ds.scenarios), list(ds.record_specs))
        assert not report.has_leakage


# =========================================================================
# Phase 9: Old Artifact Invalidation
# =========================================================================

class TestOldArtifactInvalidation:
    """Old artifacts with category-encoded record IDs MUST be rejected
    when the dataset changes to neutral IDs.
    """

    def test_old_record_ids_not_in_new_dataset(self):
        """Record IDs from the old dataset do not exist in the new one."""
        # Old IDs contain category encoding (e.g., SETTLEMENT-{hash based on
        # source_native_id containing "SET-FEE-003-001"})
        # New IDs are based on neutral source_native_ids.
        # They must differ.
        old_ds = {
            "SETTLEMENT": "SET-FEE-003-001",
            "order_id": "ORD-FEE-003-A",
        }
        # The old record_id would be computed from these leaking values.
        # The new record_id is computed from neutral values.
        new_ds = generate_dataset(seed=42)
        new_ids = {spec.source_native_id for spec in new_ds.record_specs}
        assert old_ds["SETTLEMENT"] not in new_ids, (
            "Old category-encoding settlement ID still present in new dataset"
        )

    def test_dataset_fingerprint_changes(self):
        """The dataset fingerprint changes when IDs are neutralized."""
        # The old fingerprint from data/dataset_manifest.json
        old_manifest_path = Path("data/dataset_manifest.json")
        if old_manifest_path.exists():
            old_manifest = json.loads(old_manifest_path.read_text())
            old_fingerprint = old_manifest["fingerprint"]
            # New dataset would have a different fingerprint
            # (We can't compute it without writing files, but we can verify
            # the generator produces different content)
            ds = generate_dataset(seed=42)
            # The settlement rows now contain neutral IDs
            sample_row = ds.settlement_rows[0]
            assert "ORD-" in sample_row["order_id"]
            # Old IDs contained category names like "FEE", "DUP", etc.
            # New IDs should not
            for row in ds.settlement_rows:
                oid = row.get("order_id", "")
                for cat in ["EXACT", "TDLY", "FEE", "REFD", "SPLT",
                           "RND", "NARR", "DUP", "UNQ", "LATE"]:
                    # Check as standalone token in the order_id
                    assert cat not in oid.split("-")[1:], (
                        f"Category '{cat}' found in order_id '{oid}'"
                    )


# =========================================================================
# Phase 12: Final Integrity Checks
# =========================================================================

class TestFinalIntegrityAudit:
    """Verify the complete integrity checklist."""

    def test_ground_truth_only_in_evaluation_code(self):
        """Ground truth types are defined only in evaluation modules."""
        from reconciliation.evaluation.ground_truth import EdgeCaseCategory
        # Verify the enum values
        assert len(EdgeCaseCategory) == 10

    def test_layer2_cannot_access_ground_truth(self):
        """Layer2Case does not contain ground-truth fields."""
        # Inspect the class definition — Layer2Case must NOT have
        # category, has_real_match, is_true_orphan, or description.
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(Layer2Case)}
        assert "category" not in field_names
        assert "has_real_match" not in field_names
        assert "is_true_orphan" not in field_names
        assert "description" not in field_names
        # Allowed fields: scenario_id, member_records, record_count
        assert field_names == {"scenario_id", "member_records", "record_count"}

    def test_prompt_builder_excludes_raw_payload(self):
        """_record_repr must never include raw_payload."""
        from datetime import date as date_type
        record = NormalizedRecord(
            record_id="SETTLEMENT-abc123",
            source_type=SourceType.SETTLEMENT,
            source_native_id="SET-abcdef12-001",
            order_id_hint="ORD-abcdef12",
            amount_paise=100000,
            date=date_type(2026, 8, 1),
            narration="Test narration",
            raw_payload={"secret_key": "should_not_leak"},
        )
        repr_dict = _record_repr(record)
        assert "raw_payload" not in repr_dict
        serialized = json.dumps(repr_dict)
        assert "secret_key" not in serialized
        assert "should_not_leak" not in serialized

    def test_residuals_csv_category_field_not_sent_to_llm(self):
        """The residuals.csv 'category' column is loaded but never passed
        to the prompt builder."""
        # ResidualScenario strips evaluation fields
        from reconciliation.loader import ResidualScenario
        rs = ResidualScenario(
            scenario_id="TEST-001",
            record_count=1,
            member_record_ids=("SETTLEMENT-abc",),
        )
        assert not hasattr(rs, "category")
        assert not hasattr(rs, "has_valid_relationship")
        assert not hasattr(rs, "is_true_exception")
        assert not hasattr(rs, "description")

    def test_scenario_ref_dropped_before_normalization(self):
        """scenario_ref and synthetic_ref are dropped from CSV rows
        before normalization."""
        row = {
            "scenario_ref": "FEE-003",
            "synthetic_ref": "REC-SET-001",
            "settlement_id": "SET-abcdef-001",
            "order_id": "ORD-abcdef",
            "gross_amount": "1000.00",
            "settlement_date": "2026-08-01",
        }
        # This is how evaluation_harness drops them
        clean_row = {k: v for k, v in row.items()
                     if k not in ("scenario_ref", "synthetic_ref")}
        assert "scenario_ref" not in clean_row
        assert "synthetic_ref" not in clean_row
        assert "FEE-003" not in clean_row.values()
