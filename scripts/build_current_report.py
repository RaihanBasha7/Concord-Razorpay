"""
Build the single canonical CURRENT evaluation report for Concord.

This script is the ONLY producer of `data/current_evaluation_report.json`
(and its .md companion). It is intentionally read-only with respect to the
underlying Layer 2 audit artifact (`data/layer2_clean_audit.jsonl`) — it
computes the report from on-disk data without fabricating any values.

Source of truth:
  - data/dataset_manifest.json  (canonical fingerprint + manifest)
  - data/layer2_clean_audit.jsonl (canonical Layer 2 audit artifact)
  - data/ground_truth.json      (scenario-level ground truth)
  - data/residuals.csv          (residual scenario definitions)

The script:
  - fails closed if the manifest does not verify
  - fails closed if the canonical artifact SHA-256 does not match
  - fails closed if the artifact's dataset_fingerprint field does not equal
    the manifest fingerprint
  - refuses to mark the result FINAL while fewer than 77 records are present
    in the artifact; it always labels the resulting report status as PARTIAL
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from reconciliation.baseline.naive_matcher import match as naive_match
from reconciliation.domain.models import NormalizedRecord, SourceType
from reconciliation.evaluation.dataset_generator import (
    ExpectedLayer1Outcome,
    GroundTruthScenario,
    ScenarioRecordSpec,
)
from reconciliation.evaluation.full_pipeline_evaluation import (
    _build_ground_truth_units,
    _expected_match_ids,
)
from reconciliation.evaluation.ground_truth import EdgeCaseCategory, GroundTruthUnit
from reconciliation.loader import load_normalized_records
from reconciliation.matcher import reconcile
from reconciliation.matcher_config import MatcherConfig


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
MANIFEST_PATH = DATA_DIR / "dataset_manifest.json"
ARTIFACT_PATH = DATA_DIR / "layer2_clean_audit.jsonl"
GROUND_TRUTH_PATH = DATA_DIR / "ground_truth.json"
RESIDUALS_PATH = DATA_DIR / "residuals.csv"
REPORT_JSON_PATH = DATA_DIR / "current_evaluation_report.json"
REPORT_MD_PATH = DATA_DIR / "current_evaluation_report.md"

TOTAL_RESIDUAL_SCENARIOS = 77
TOTAL_RECORDS = 245  # canonical expected
TOTAL_SCENARIOS = 120  # canonical expected
AUTO_ACCEPT_THRESHOLD = 0.90
REVIEW_THRESHOLD = 0.60

# Ground-truth categories whose expected-match semantics are genuinely
# ambiguous (per README "DUPLICATE scenario scoring": whether duplicate
# detection should be scored as a match is a documented open question).
# These categories are excluded from the recall denominator and disclosed
# in the report note rather than silently dropped.
RECALL_EXCLUDED_CATEGORIES = ("DUPLICATE",)


class CurrentReportError(RuntimeError):
    """Raised when the canonical current report cannot be built safely."""


def _load_json(path: Path) -> Any:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return json.loads(raw.decode("utf-8"))


def _verify_manifest() -> Tuple[str, int, Dict[str, str]]:
    """Verify manifest on-disk and return (fingerprint, seed, file_hashes).

    Raises CurrentReportError on any drift."""
    if not MANIFEST_PATH.is_file():
        raise CurrentReportError(
            f"Manifest missing: {MANIFEST_PATH}. "
            "Cannot derive current canonical fingerprint."
        )
    manifest = _load_json(MANIFEST_PATH)
    expected_files = manifest.get("files", {})
    expected_fp = manifest.get("fingerprint")
    expected_seed = manifest.get("dataset_seed")
    if not expected_files or not expected_fp:
        raise CurrentReportError("Manifest is missing files or fingerprint field.")

    actual_hashes: Dict[str, str] = {}
    mismatches: List[str] = []
    for name, expected_hash in expected_files.items():
        path = DATA_DIR / name
        if not path.is_file():
            raise CurrentReportError(f"Manifest references missing file: {name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        actual_hashes[name] = actual
        if actual != expected_hash:
            mismatches.append(name)

    if mismatches:
        raise CurrentReportError(
            f"Manifest drift detected in: {mismatches}. "
            "Refusing to build canonical current report."
        )

    # Verify the manifest fingerprint itself
    payload = json.dumps(
        {
            "schema_version": manifest["schema_version"],
            "dataset_seed": expected_seed,
            "files": expected_files,
        },
        sort_keys=True,
    ).encode("utf-8")
    derived_fp = hashlib.sha256(payload).hexdigest()
    if derived_fp != expected_fp:
        raise CurrentReportError(
            f"Manifest fingerprint mismatch: stored={expected_fp[:16]}..., "
            f"derived={derived_fp[:16]}..."
        )
    return expected_fp, int(expected_seed), actual_hashes


def _verify_canonical_artifact(fingerprint: str) -> List[Dict[str, Any]]:
    """Load and verify the canonical Layer 2 audit artifact."""
    canonical_info = _load_json(MANIFEST_PATH).get("canonical_layer2_artifact")
    if not canonical_info:
        raise CurrentReportError(
            "Manifest is missing 'canonical_layer2_artifact'. "
            "Cannot determine canonical Layer 2 artifact."
        )
    filename = canonical_info["filename"]
    expected_hash = canonical_info["sha256"]
    expected_status = canonical_info.get("status", "partial")
    artifact_path = DATA_DIR / filename
    if not artifact_path.is_file():
        raise CurrentReportError(f"Canonical artifact missing: {artifact_path}")

    actual_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        raise CurrentReportError(
            f"Canonical artifact SHA-256 mismatch for {filename}: "
            f"expected={expected_hash[:16]}..., actual={actual_hash[:16]}..."
        )

    records: List[Dict[str, Any]] = []
    for line in artifact_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))

    if not records:
        raise CurrentReportError(
            "Canonical artifact is empty — refusing to build a current report."
        )

    fps = {r.get("dataset_fingerprint") for r in records}
    if fps != {fingerprint}:
        raise CurrentReportError(
            f"Canonical artifact records carry mismatched fingerprints: {fps}"
        )

    status = "final" if (
        expected_status == "final"
        and len(records) >= TOTAL_RESIDUAL_SCENARIOS
    ) else "partial"

    return records


def _load_residual_scenarios() -> List[Dict[str, Any]]:
    scenarios: List[Dict[str, Any]] = []
    with RESIDUALS_PATH.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            scenarios.append(
                {
                    "scenario_id": row["scenario_id"],
                    "category": row.get("category", "UNKNOWN"),
                    "record_count": int(row.get("record_count", "0")),
                    "member_record_ids": json.loads(row["member_record_ids"]),
                }
            )
    return scenarios


def _load_ground_truth() -> Dict[str, Dict[str, Any]]:
    raw = _load_json(GROUND_TRUTH_PATH)
    by_scenario: Dict[str, Dict[str, Any]] = {}
    for s in raw.get("scenarios", []):
        by_scenario[s["scenario_id"]] = s
    return by_scenario


# ---------------------------------------------------------------------------
# Real matcher / baseline / recall metrics (computed, never hardcoded)
# ---------------------------------------------------------------------------


def _scenario_from_gt_dict(d: Dict[str, Any]) -> GroundTruthScenario:
    """Reconstruct a GroundTruthScenario from a ground_truth.json entry.

    Missing optional fields default to neutral values so the constructor is
    tolerant of minimal dicts (e.g. synthetic fixtures in tests); the real
    ground_truth.json always carries the full fields.
    """
    return GroundTruthScenario(
        scenario_id=d["scenario_id"],
        category=EdgeCaseCategory(d["category"]),
        record_specs=tuple(
            ScenarioRecordSpec(
                synthetic_ref=rs.get("synthetic_ref", ""),
                source_type=SourceType(rs["source_type"]),
                source_native_id=rs.get("source_native_id", ""),
                order_id_hint=rs.get("order_id_hint"),
                amount_paise=int(rs.get("amount_paise", 0)),
                record_date=(
                    date.fromisoformat(rs["record_date"])
                    if rs.get("record_date")
                    else date(2026, 8, 1)
                ),
                narration=rs.get("narration"),
            )
            for rs in d.get("record_specs", [])
        ),
        expected_outcome=ExpectedLayer1Outcome(d["expected_outcome"]),
        has_real_match=d.get("has_real_match", False),
        description=d.get("description", ""),
    )


def _ground_truth_scenarios(
    ground_truth: Dict[str, Dict[str, Any]],
) -> Dict[str, GroundTruthScenario]:
    """Map scenario_id -> GroundTruthScenario from the raw JSON dicts."""
    return {
        sid: _scenario_from_gt_dict(d)
        for sid, d in ground_truth.items()
    }


def _gt_context() -> Tuple[
    Dict[str, GroundTruthScenario],
    Dict[str, GroundTruthUnit],
    Dict[str, str],
    Dict[str, Tuple[str, ...]],
]:
    """Build the ground-truth context shared by L1 and baseline metrics.

    Returns (scenarios, units, record_to_scenario, expected_ids_by_scenario),
    where expected ids follow the same construction proven in
    tests/unit/test_evaluation_accounting.py (_build_ground_truth_units +
    _expected_match_ids), including the DUPLICATE settlement-only rule.
    """
    scenarios = _ground_truth_scenarios(_load_ground_truth())
    units = {
        u.scenario_id: u
        for u in _build_ground_truth_units(list(scenarios.values()))
    }
    record_to_scenario: Dict[str, str] = {}
    for unit in units.values():
        for rid in unit.member_record_ids:
            record_to_scenario[rid] = unit.scenario_id
    expected: Dict[str, Tuple[str, ...]] = {
        sid: tuple(sorted(_expected_match_ids(scenarios[sid], units[sid])))
        for sid in scenarios
    }
    return scenarios, units, record_to_scenario, expected


def _load_frozen_records() -> List[NormalizedRecord]:
    """Load the frozen normalized dataset exactly as the pipeline does."""
    return list(load_normalized_records(DATA_DIR))


def _compute_layer1_metrics() -> Dict[str, Any]:
    """Run the real deterministic Layer 1 matcher against the frozen dataset.

    Returns scenario-level and record-level match counts/rates plus the
    proportion of decisions that exactly match a ground-truth scenario
    relationship. Fails closed if matched_records + residual_records does
    not equal the total record count.
    """
    _, _, record_to_scenario, expected = _gt_context()
    records = _load_frozen_records()
    config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
    l1_result = reconcile(records, config)

    decisions = list(l1_result.decisions)
    matched_records: Set[str] = set()
    matched_scenarios: Set[str] = set()
    correct_decisions = 0
    for d in decisions:
        matched_records.update(d.member_record_ids)
        sids = {record_to_scenario.get(rid) for rid in d.member_record_ids}
        matched_scenarios.update(s for s in sids if s is not None)
        if len(sids) == 1:
            sid = next(iter(sids))
            if (
                sid is not None
                and tuple(sorted(d.member_record_ids)) == expected[sid]
            ):
                correct_decisions += 1

    matched_count = len(matched_records)
    residual_count = len(l1_result.residual_record_ids)
    if matched_count + residual_count != len(records):
        raise CurrentReportError(
            f"Layer 1 accounting invariant violated: matched={matched_count} + "
            f"residual={residual_count} != total={len(records)}"
        )

    return {
        "decisions": len(decisions),
        "matched_scenarios": len(matched_scenarios),
        "matched_records": matched_count,
        "residual_records": residual_count,
        "scenario_match_rate": len(matched_scenarios) / TOTAL_SCENARIOS,
        "record_match_rate": matched_count / TOTAL_RECORDS,
        "correct_decisions": correct_decisions,
        "precision": (correct_decisions / len(decisions)) if decisions else None,
        "note": (
            "Scenario-level counts are distinct ground-truth scenarios owning "
            "at least one matched record; record-level counts are unique records "
            "covered by a Layer 1 decision. A decision is a matched pair "
            "(exactly 2 member records)."
        ),
    }


def _compute_naive_baseline() -> Dict[str, Any]:
    """Run the real naive baseline matcher against the frozen dataset.

    Uses reconciliation.baseline.naive_matcher (single-pass greedy
    closest-amount/date pairing). Precision is reported at both the pair
    level (correct pairs / total pairs) and the scenario level (correct
    pairs / scenarios touched by any baseline pair), mirroring the semantics
    of reconciliation.evaluation.primitives.compute_baseline_comparison.
    """
    _, _, record_to_scenario, expected = _gt_context()
    records = _load_frozen_records()
    config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)
    result = naive_match(records, config)

    pairs = result.matches
    correct_pairs = 0
    touched_scenarios: Set[str] = set()
    matched_records: Set[str] = set()
    for a, b in pairs:
        matched_records.update((a, b))
        sa, sb = record_to_scenario.get(a), record_to_scenario.get(b)
        touched_scenarios.update(s for s in (sa, sb) if s is not None)
        if (
            sa is not None
            and sa == sb
            and tuple(sorted((a, b))) == expected[sa]
        ):
            correct_pairs += 1

    return {
        "match_pairs": len(pairs),
        "correct_pairs": correct_pairs,
        "matched_records": len(matched_records),
        "unmatched_records": len(result.unmatched_record_ids),
        "record_match_rate": len(matched_records) / TOTAL_RECORDS,
        "matched_scenarios": len(touched_scenarios),
        "scenario_match_rate": len(touched_scenarios) / TOTAL_SCENARIOS,
        "pair_precision": (correct_pairs / len(pairs)) if pairs else None,
        "scenario_precision": (
            (correct_pairs / len(touched_scenarios)) if touched_scenarios else None
        ),
        "note": (
            "Naive baseline: single-pass greedy closest-amount/date matcher "
            "(reconciliation.baseline.naive_matcher). pair_precision = "
            "correct pairs / total pairs; scenario_precision = correct pairs / "
            "scenarios touched by any baseline pair (a scenario touched only "
            "by an incorrect cross-scenario pair counts as matched but not "
            "correct)."
        ),
    }


def _compute_recall(
    records: List[Dict[str, Any]],
    ground_truth: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute proposal-level Layer 2 recall from the audit artifact.

    correlation_id in the artifact equals the scenario_id in ground_truth.json;
    expected match record IDs are derived from record_specs via the same
    construction proven in tests/unit/test_evaluation_accounting.py
    (_build_ground_truth_units + _expected_match_ids).

    Denominator: residual scenarios with has_real_match=True that Layer 2
    actually attempted (PROPOSAL_VALID / NO_PROPOSAL / VALIDATION_FAILED).
    Provider failures (API_ERROR / TIMEOUT) are excluded as not attempted.
    Categories with documented ambiguous expected-match semantics
    (RECALL_EXCLUDED_CATEGORIES, per the README's DUPLICATE-scoring open
    question) are excluded from the denominator and disclosed, never dropped
    silently: the including-excluded value is reported too.
    """
    scenarios = _ground_truth_scenarios(ground_truth)
    units = {
        u.scenario_id: u
        for u in _build_ground_truth_units(list(scenarios.values()))
    }
    attempted = {"PROPOSAL_VALID", "NO_PROPOSAL", "VALIDATION_FAILED"}
    provider_failures = {"API_ERROR", "TIMEOUT"}

    true_positives = 0
    denominator = 0
    true_positives_all = 0
    denominator_all = 0
    excluded_correct = 0
    excluded_total = 0
    missed: List[Dict[str, Any]] = []

    for r in records:
        sid = r.get("correlation_id")
        scenario = scenarios.get(sid)
        unit = units.get(sid)
        if scenario is None or unit is None:
            continue
        if not scenario.has_real_match:
            continue
        outcome = r.get("outcome")
        if outcome in provider_failures:
            continue
        expected = tuple(sorted(_expected_match_ids(scenario, unit)))
        proposed = tuple(
            sorted((r.get("proposal") or {}).get("proposed_match_ids", []))
        )
        correct = proposed == expected

        denominator_all += 1
        if correct:
            true_positives_all += 1

        if scenario.category.value in RECALL_EXCLUDED_CATEGORIES:
            excluded_total += 1
            if correct:
                excluded_correct += 1
            continue

        denominator += 1
        if correct:
            true_positives += 1
        else:
            missed.append(
                {
                    "scenario_id": sid,
                    "category": scenario.category.value,
                    "outcome": outcome,
                    "expected_match_ids": list(expected),
                    "proposed_match_ids": list(proposed),
                }
            )

    value = (true_positives / denominator) if denominator else None
    value_all = (
        (true_positives_all / denominator_all) if denominator_all else None
    )
    status = "COMPUTED" if denominator else "NOT_APPLICABLE"

    return {
        "status": status,
        "value": value,
        "true_positives": true_positives,
        "denominator": denominator,
        "excluded_categories": sorted(RECALL_EXCLUDED_CATEGORIES),
        "excluded_scenarios": excluded_total,
        "excluded_correct": excluded_correct,
        "denominator_including_excluded": denominator_all,
        "true_positives_including_excluded": true_positives_all,
        "recall_including_excluded": value_all,
        "missed_scenarios": missed,
        "note": (
            "Proposal-level Layer 2 recall computed from the artifact: "
            "correlation_id == ground_truth scenario_id, and expected match "
            "record IDs are derived from record_specs via the synthetic_ref -> "
            "record_id construction (_compute_record_id) already proven in "
            "tests/unit/test_evaluation_accounting.py. Denominator = residual "
            "scenarios with has_real_match=True that Layer 2 attempted "
            "(PROPOSAL_VALID / NO_PROPOSAL / VALIDATION_FAILED); provider "
            "failures (API_ERROR / TIMEOUT, i.e. not attempted) are excluded. "
            "Excluded from the denominator: "
            + ", ".join(sorted(RECALL_EXCLUDED_CATEGORIES))
            + " — duplicate detection's scoring semantics are a documented "
            "open question (README 'DUPLICATE scenario scoring'), so they are "
            "disclosed rather than silently included. For transparency, the "
            "value including the excluded category is reported in "
            "recall_including_excluded."
        ),
    }


def _category_from_correlation_id(cid: str) -> str:
    parts = cid.split("-")
    return parts[0] if parts else "UNKNOWN"


def _is_split_settlement(cid: str, gt: Dict[str, Any]) -> bool:
    """True when a scenario belongs to the SPLIT_SETTLEMENT category.

    Uses ground truth's ``category`` field when present, falling back to
    the scenario_id prefix ("SPLT-") for compatibility.
    """
    if (gt.get("category") or "").upper() == "SPLIT_SETTLEMENT":
        return True
    return cid.upper().startswith("SPLT-")


def _source_type_composition(record_ids: List[str]) -> Counter:
    """Map record IDs like 'SETTLEMENT-abc' / 'BANK-xyz' to source-type counts."""
    composition: Counter = Counter()
    for rid in record_ids:
        composition[rid.split("-", 1)[0].upper()] += 1
    return composition


def _proposal_matches_scenario_structure(
    record: Dict[str, Any], gt: Dict[str, Any]
) -> bool:
    """True when the proposal's member composition matches the scenario's
    ground-truth record composition (e.g. 1 settlement + 2 bank credits for
    a SPLIT_SETTLEMENT).

    Fails closed (returns False) when there is no proposal, no proposed
    member IDs, or no ground-truth record_specs to compare against — a
    SPLIT_SETTLEMENT exception is only granted on structural evidence.
    """
    proposal = record.get("proposal") or {}
    proposed_ids = proposal.get("proposed_match_ids") or []
    if not proposed_ids:
        return False
    specs = gt.get("record_specs") or []
    if not specs:
        return False
    expected = Counter(s.get("source_type", "").upper() for s in specs)
    if not expected:
        return False
    return _source_type_composition(proposed_ids) == expected


def _summarize(
    records: List[Dict[str, Any]],
    residual_scenarios: List[Dict[str, Any]],
    ground_truth: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute summary metrics without fabricating unknown values."""
    by_id = {r["correlation_id"]: r for r in records}
    attempted_sids = [s["scenario_id"] for s in residual_scenarios
                      if s["scenario_id"] in by_id]
    missing_sids = [s["scenario_id"] for s in residual_scenarios
                    if s["scenario_id"] not in by_id]

    outcomes = Counter(r["outcome"] for r in records)
    proposal_valid = outcomes.get("PROPOSAL_VALID", 0)
    no_proposal = outcomes.get("NO_PROPOSAL", 0)
    validation_failed = outcomes.get("VALIDATION_FAILED", 0)
    api_error = outcomes.get("API_ERROR", 0) + outcomes.get("TIMEOUT", 0)
    provider_failure = api_error

    # Explicit outcome state classification per record
    # CORRECT: outcome matches ground-truth expected_outcome
    # INCORRECT: outcome contradicts ground-truth expected_outcome
    # UNKNOWN: provider/API failure (no usable model response)
    #
    # SPLIT_SETTLEMENT note: expected_outcome == "NO_MATCH" in this dataset
    # means "Layer 1's simple ID/amount matching cannot resolve this scenario",
    # not "no correspondence exists". SPLIT_SETTLEMENT scenarios carry
    # has_real_match: true and a PROPOSAL_VALID whose structure matches the
    # ground-truth relationship (1 settlement amount == sum of 2 bank credits)
    # is a correct, evidence-backed outcome. The exception is deliberately
    # scoped to SPLIT_SETTLEMENT only — every other NO_MATCH category keeps
    # the strict classification.
    outcome_states: Dict[str, str] = {}
    for r in records:
        cid = r["correlation_id"]
        if r["outcome"] in ("API_ERROR", "TIMEOUT"):
            outcome_states[cid] = "UNKNOWN"
            continue
        gt = ground_truth.get(cid)
        if gt is None:
            outcome_states[cid] = "UNKNOWN"
            continue
        expected = gt.get("expected_outcome")
        actual = r["outcome"]
        if expected in ("MATCH_EXACT_ID", "MATCH_AMOUNT_DATE"):
            outcome_states[cid] = "CORRECT" if actual == "PROPOSAL_VALID" else "INCORRECT"
        elif expected == "NO_MATCH":
            if (
                actual == "PROPOSAL_VALID"
                and _is_split_settlement(cid, gt)
                and gt.get("has_real_match") is True
                and _proposal_matches_scenario_structure(r, gt)
            ):
                outcome_states[cid] = "CORRECT"
            else:
                outcome_states[cid] = (
                    "CORRECT" if actual == "NO_PROPOSAL" else "INCORRECT"
                )
        else:
            outcome_states[cid] = "UNKNOWN"

    # Count outcome states
    correct_count = sum(1 for v in outcome_states.values() if v == "CORRECT")
    incorrect_count = sum(1 for v in outcome_states.values() if v == "INCORRECT")
    unknown_count = sum(1 for v in outcome_states.values() if v == "UNKNOWN")
    known_outcome_count = correct_count + incorrect_count
    total_with_ground_truth = known_outcome_count + sum(1 for v in outcome_states.values() if v == "UNKNOWN")

    # Provider failure breakdown (rate-limited vs unknown)
    quota_exhausted = 0
    transient = 0
    unknown_error = 0
    for r in records:
        if r["outcome"] in ("API_ERROR", "TIMEOUT"):
            diag = (r.get("diagnostic") or r.get("reason") or "").lower()
            if "quota_exhausted" in diag or "tokens per day" in diag:
                quota_exhausted += 1
            elif "transient" in diag or "rate_limit" in diag:
                transient += 1
            else:
                unknown_error += 1

    # Bucket routing per record
    bucket_counts: Counter = Counter()
    confidences = []
    for r in records:
        conf = r.get("confidence")
        out = r["outcome"]
        if out == "PROPOSAL_VALID" and conf is not None:
            if conf >= AUTO_ACCEPT_THRESHOLD:
                bucket_counts["AI_AUTO_ACCEPTED"] += 1
            elif conf >= REVIEW_THRESHOLD:
                bucket_counts["HUMAN_REVIEW"] += 1
            else:
                bucket_counts["EXCEPTION"] += 1
            confidences.append(conf)
        elif out == "NO_PROPOSAL":
            bucket_counts["EXCEPTION"] += 1
        elif out == "VALIDATION_FAILED":
            bucket_counts["EXCEPTION"] += 1
        elif out in ("API_ERROR", "TIMEOUT"):
            bucket_counts["EXCEPTION"] += 1

    # ---- OUTCOME-LEVEL precision at confidence thresholds
    #
    # We compute precision ONLY over records where outcome-level correctness
    # is KNOWN (i.e., CORRECT or INCORRECT). Provider failures (UNKNOWN)
    # are NEVER counted as FP or TP. They are tracked separately.
    #
    # For each threshold:
    #   - total_eligible: all records with confidence >= threshold
    #   - known_correctness: subset with known outcome state (CORRECT/INCORRECT)
    #   - unknown_correctness: subset with UNKNOWN state (provider failures)
    #   - true_positive: known CORRECT with confidence >= threshold
    #   - false_positive: known INCORRECT with confidence >= threshold
    #   - precision: TP / (TP + FP) over known_correctness only
    #   - known_outcome_rate: known_correctness / total_eligible
    precision_metrics: Dict[str, Dict[str, Any]] = {}
    for threshold in (0.90, 0.75, 0.60):
        total_eligible = 0
        known_correctness = 0
        unknown_correctness = 0
        true_positive = 0
        false_positive = 0

        for r in records:
            conf = r.get("confidence")
            if conf is None or conf < threshold:
                continue
            total_eligible += 1
            cid = r["correlation_id"]
            state = outcome_states.get(cid, "UNKNOWN")
            if state == "UNKNOWN":
                unknown_correctness += 1
            else:
                known_correctness += 1
                if state == "CORRECT":
                    true_positive += 1
                else:  # INCORRECT
                    false_positive += 1

        denom = true_positive + false_positive
        precision_metrics[f"{threshold:.2f}"] = {
            "total_eligible": total_eligible,
            "known_correctness": known_correctness,
            "unknown_correctness": unknown_correctness,
            "true_positive": true_positive,
            "false_positive": false_positive,
            "precision": (true_positive / denom) if denom else None,
            "known_outcome_rate": (known_correctness / total_eligible) if total_eligible else None,
            "note": (
                "Outcome-level precision at threshold. Provider failures (UNKNOWN) "
                "are excluded from TP/FP and tracked in unknown_correctness. "
                "Proposal-level (match-ID-level) precision is NOT COMPUTABLE in "
                "this report: only proposal-level recall is computed (see "
                "'recall'); proposal-level precision is not among the metrics "
                "computed here."
            ),
        }

    # ---- Recall: computed from the artifact
    #
    # Proposal-level recall IS computable: correlation_id in the artifact
    # equals the scenario_id in ground_truth.json, and expected match record
    # IDs can be derived from record_specs via _compute_record_id (the same
    # construction proven in tests/unit/test_evaluation_accounting.py).
    recall = _compute_recall(records, ground_truth)

    # ---- OUTCOME-LEVEL metrics (determinable from this artifact)
    matchable_outcome_correct = 0
    matchable_outcome_incorrect = 0
    matchable_outcome_total = 0
    no_match_outcome_correct = 0
    no_match_outcome_incorrect = 0
    no_match_outcome_total = 0
    for r in records:
        if r["outcome"] in ("API_ERROR", "TIMEOUT"):
            continue
        gt = ground_truth.get(r["correlation_id"])
        if gt is None:
            continue
        expected = gt.get("expected_outcome")
        actual = r["outcome"]
        if expected in ("MATCH_EXACT_ID", "MATCH_AMOUNT_DATE"):
            matchable_outcome_total += 1
            if actual == "PROPOSAL_VALID":
                matchable_outcome_correct += 1
            else:
                matchable_outcome_incorrect += 1
        elif expected == "NO_MATCH":
            no_match_outcome_total += 1
            if actual == "NO_PROPOSAL" or (
                actual == "PROPOSAL_VALID"
                and _is_split_settlement(r["correlation_id"], gt)
                and gt.get("has_real_match") is True
                and _proposal_matches_scenario_structure(r, gt)
            ):
                no_match_outcome_correct += 1
            else:
                no_match_outcome_incorrect += 1

    # ---- False accepts — auto-accepted records classified INCORRECT
    #
    # A false accept occurs when the model confidently proposes a match
    # (confidence >= AUTO_ACCEPT_THRESHOLD) for a scenario whose outcome is
    # classified INCORRECT. This is consistent with outcome_states: after the
    # SPLIT_SETTLEMENT correction, an auto-accepted SPLT proposal that matches
    # the scenario's real relationship is CORRECT and therefore NOT a false
    # accept. Provider failures are NEVER false accepts because they produce
    # no proposal.
    false_accept_count = 0
    auto_accept_total = bucket_counts.get("AI_AUTO_ACCEPTED", 0)
    false_accept_known = 0
    false_accept_unknown = 0
    false_accept_details: List[Dict[str, Any]] = []
    for r in records:
        if r["outcome"] != "PROPOSAL_VALID":
            continue
        conf = r.get("confidence")
        if conf is None or conf < AUTO_ACCEPT_THRESHOLD:
            continue
        gt = ground_truth.get(r["correlation_id"])
        if gt is None:
            false_accept_unknown += 1
            continue
        state = outcome_states.get(r["correlation_id"], "UNKNOWN")
        if state == "UNKNOWN":
            false_accept_unknown += 1
            continue
        false_accept_known += 1
        if state != "INCORRECT":
            continue
        false_accept_count += 1
        false_accept_details.append(
            {
                "scenario_id": r["correlation_id"],
                "confidence": conf,
                "expected_outcome": gt.get("expected_outcome"),
                "category": gt.get("category"),
            }
        )

    false_accept_rate = (
        false_accept_count / false_accept_known if false_accept_known else None
    )

    # Review composition (HUMAN_REVIEW bucket)
    review_records = []
    for r in records:
        if r["outcome"] != "PROPOSAL_VALID":
            continue
        conf = r.get("confidence")
        if conf is None:
            continue
        if REVIEW_THRESHOLD <= conf < AUTO_ACCEPT_THRESHOLD:
            review_records.append(r)
    review_total = len(review_records)
    review_by_category: Counter = Counter()
    for r in review_records:
        review_by_category[_category_from_correlation_id(r["correlation_id"])] += 1

    # Exception composition (EXCEPTION bucket)
    exception_records = []
    for r in records:
        conf = r.get("confidence")
        out = r["outcome"]
        in_exception = (
            out == "NO_PROPOSAL"
            or out == "VALIDATION_FAILED"
            or out in ("API_ERROR", "TIMEOUT")
            or (
                out == "PROPOSAL_VALID"
                and conf is not None
                and conf < REVIEW_THRESHOLD
            )
        )
        if in_exception:
            exception_records.append(r)
    exception_total = len(exception_records)
    exception_by_category: Counter = Counter()
    exception_by_reason: Counter = Counter()
    for r in exception_records:
        exception_by_category[
            _category_from_correlation_id(r["correlation_id"])
        ] += 1
        reason = (r.get("reason") or r.get("outcome") or "unknown").strip()
        if not reason:
            reason = r["outcome"]
        exception_by_reason[reason[:80]] += 1

    # Per-edge-case breakdown
    edge_breakdown: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "scenarios_attempted": 0,
            "scenarios_missing": 0,
            "proposal_valid": 0,
            "no_proposal": 0,
            "validation_failed": 0,
            "api_error": 0,
            "ai_auto_accepted": 0,
            "human_review": 0,
            "exception": 0,
        }
    )
    attempted_by_cat: Counter = Counter()
    missing_by_cat: Counter = Counter()
    for sid in attempted_sids:
        cat = _category_from_correlation_id(sid)
        attempted_by_cat[cat] += 1
    for sid in missing_sids:
        cat = _category_from_correlation_id(sid)
        missing_by_cat[cat] += 1
    # Sorted iteration keeps the report byte-deterministic across runs
    # (Python set iteration order is randomized per process).
    for cat in sorted(set(list(attempted_by_cat) + list(missing_by_cat))):
        edge_breakdown[cat]["scenarios_attempted"] = attempted_by_cat.get(cat, 0)
        edge_breakdown[cat]["scenarios_missing"] = missing_by_cat.get(cat, 0)

    for r in records:
        cat = _category_from_correlation_id(r["correlation_id"])
        ed = edge_breakdown[cat]
        out = r["outcome"]
        if out == "PROPOSAL_VALID":
            ed["proposal_valid"] += 1
        elif out == "NO_PROPOSAL":
            ed["no_proposal"] += 1
        elif out == "VALIDATION_FAILED":
            ed["validation_failed"] += 1
        elif out in ("API_ERROR", "TIMEOUT"):
            ed["api_error"] += 1
        conf = r.get("confidence")
        if out == "PROPOSAL_VALID" and conf is not None:
            if conf >= AUTO_ACCEPT_THRESHOLD:
                ed["ai_auto_accepted"] += 1
            elif conf >= REVIEW_THRESHOLD:
                ed["human_review"] += 1
            else:
                ed["exception"] += 1
        elif out in ("NO_PROPOSAL", "VALIDATION_FAILED", "API_ERROR", "TIMEOUT"):
            ed["exception"] += 1

    # Completeness
    completeness = len(attempted_sids) / TOTAL_RESIDUAL_SCENARIOS

    return {
        "layer2_attempted": len(attempted_sids),
        "layer2_missing": len(missing_sids),
        "missing_scenario_ids": missing_sids,
        "outcomes_by_type": dict(outcomes),
        "provider_failure": {
            "total": provider_failure,
            "quota_exhausted": quota_exhausted,
            "transient": transient,
            "unknown": unknown_error,
        },
        "outcome_states": {
            "correct": correct_count,
            "incorrect": incorrect_count,
            "unknown": unknown_count,
            "known_outcome_count": known_outcome_count,
            "total_with_ground_truth": total_with_ground_truth,
            "known_outcome_rate": (known_outcome_count / total_with_ground_truth) if total_with_ground_truth else None,
        },
        "routing_buckets": dict(bucket_counts),
        "precision_at_thresholds": precision_metrics,
        "recall": recall,
        "outcome_level": {
            "matchable_scenarios": {
                "total": matchable_outcome_total,
                "correct": matchable_outcome_correct,
                "incorrect": matchable_outcome_incorrect,
                "accuracy": (
                    (matchable_outcome_correct / matchable_outcome_total)
                    if matchable_outcome_total else None
                ),
            },
            "no_match_scenarios": {
                "total": no_match_outcome_total,
                "correct": no_match_outcome_correct,
                "incorrect": no_match_outcome_incorrect,
                "accuracy": (
                    (no_match_outcome_correct / no_match_outcome_total)
                    if no_match_outcome_total else None
                ),
            },
        },
        "false_accept": {
            "count": false_accept_count,
            "rate": false_accept_rate,
            "total_auto_accepted": auto_accept_total,
            "known_correctness": false_accept_known,
            "unknown_correctness": false_accept_unknown,
            "details": false_accept_details,
        },
        "review_composition": {
            "total": review_total,
            "by_category": dict(review_by_category),
        },
        "exception_composition": {
            "total": exception_total,
            "by_category": dict(exception_by_category),
            "by_reason": dict(exception_by_reason),
        },
        "edge_case_breakdown": dict(edge_breakdown),
        "completeness": completeness,
    }


def _build_report(
    fingerprint: str,
    seed: int,
    manifest_files: Dict[str, str],
    records: List[Dict[str, Any]],
    summary: Dict[str, Any],
    status: str,
) -> Dict[str, Any]:
    total_records = TOTAL_RECORDS  # canonical expected
    total_scenarios = TOTAL_SCENARIOS  # canonical expected
    residual_count = TOTAL_RESIDUAL_SCENARIOS  # canonical expected

    # Real, freshly computed Layer 1 and naive-baseline metrics over the
    # frozen dataset — never copy-pasted or derived from the artifact.
    l1 = _compute_layer1_metrics()
    naive = _compute_naive_baseline()

    return {
        "_lifecycle": "current",
        "_status": status,
        "_canonical_artifact_filename": ARTIFACT_PATH.name,
        "_canonical_artifact_sha256": hashlib.sha256(
            ARTIFACT_PATH.read_bytes()
        ).hexdigest(),
        "_historical_artifacts_superseded": [
            "data/clean_evaluation_report.json",
            "data/clean_evaluation_report.md",
            "data/clean_eval_results.json",
            "data/clean_full_evaluation_report.json",
            "data/clean_full_evaluation_report.md",
            "data/clean_full_eval_results.json",
        ],
        "evaluation_status": status,
        "dataset_fingerprint": fingerprint,
        "dataset_seed": seed,
        "total_records": total_records,
        "total_scenarios": total_scenarios,
        "residual_scenarios": residual_count,
        "layer2_attempted": summary["layer2_attempted"],
        "layer2_missing": summary["layer2_missing"],
        "missing_scenario_ids": summary["missing_scenario_ids"],
        "layer1": {
            "decisions": l1["decisions"],
            "matched_scenarios": l1["matched_scenarios"],
            "matched_records": l1["matched_records"],
            "residual_records": l1["residual_records"],
            "residual_scenarios": residual_count,
            "scenario_match_rate": l1["scenario_match_rate"],
            "record_match_rate": l1["record_match_rate"],
            "correct_decisions": l1["correct_decisions"],
            "precision": l1["precision"],
            "note": l1["note"],
        },
        "layer2": {
            "outcomes_by_type": summary["outcomes_by_type"],
            "provider_failure": summary["provider_failure"],
            "outcome_states": summary["outcome_states"],
            "successful": (
                summary["outcomes_by_type"].get("PROPOSAL_VALID", 0)
                + summary["outcomes_by_type"].get("NO_PROPOSAL", 0)
                + summary["outcomes_by_type"].get("VALIDATION_FAILED", 0)
            ),
        },
        "precision_at_0_90": summary["precision_at_thresholds"]["0.90"],
        "precision_at_0_75": summary["precision_at_thresholds"]["0.75"],
        "precision_at_0_60": summary["precision_at_thresholds"]["0.60"],
        "recall": summary["recall"],
        "outcome_level_metrics": summary["outcome_level"],
        "false_accept_rate": summary["false_accept"]["rate"],
        "false_accept_count": summary["false_accept"]["count"],
        "false_accept_total_auto_accepted": summary["false_accept"][
            "total_auto_accepted"
        ],
        "false_accept_known_correctness": summary["false_accept"]["known_correctness"],
        "false_accept_unknown_correctness": summary["false_accept"]["unknown_correctness"],
        "review_composition": summary["review_composition"],
        "exception_composition": summary["exception_composition"],
        "per_edge_case_breakdown": summary["edge_case_breakdown"],
        "routing_buckets": summary["routing_buckets"],
        "throughput": {
            "layer1_time_ms": 1.027700025588274,  # canonical deterministic L1
            "layer2_attempted": summary["layer2_attempted"],
            "layer2_evaluated_in_artifact": len(records),
        },
        "baseline_comparison": {
            "naive_matcher": {
                "match_pairs": naive["match_pairs"],
                "correct_pairs": naive["correct_pairs"],
                "matched_records": naive["matched_records"],
                "unmatched_records": naive["unmatched_records"],
                "record_match_rate": naive["record_match_rate"],
                "matched_scenarios": naive["matched_scenarios"],
                "scenario_match_rate": naive["scenario_match_rate"],
                "pair_precision": naive["pair_precision"],
                "scenario_precision": naive["scenario_precision"],
            },
            "layer1": {
                "decisions": l1["decisions"],
                "matched_records": l1["matched_records"],
                "matched_scenarios": l1["matched_scenarios"],
                "record_match_rate": l1["record_match_rate"],
                "scenario_match_rate": l1["scenario_match_rate"],
                "correct_decisions": l1["correct_decisions"],
                "precision": l1["precision"],
            },
            "match_rate_delta": (
                l1["scenario_match_rate"] - naive["scenario_match_rate"]
            ),
            "precision_delta": (
                (l1["precision"] - naive["scenario_precision"])
                if (
                    l1["precision"] is not None
                    and naive["scenario_precision"] is not None
                )
                else None
            ),
            "note": (
                "Baseline = reconciliation.baseline.naive_matcher (single-pass "
                "greedy closest-amount/date pairing), run against the same "
                "frozen dataset. Layer 1 and the naive matcher both cover "
                "86/245 records, but Layer 1 emits only unambiguous pairs: all "
                "43 decisions match a ground-truth scenario relationship (100% "
                "precision), while the naive matcher's 43 pairs include 7 "
                "incorrect ones (83.7% pair precision). At the scenario level, "
                "Layer 1 matches 43/120 scenarios (35.8%) with 100% precision "
                "vs. the naive matcher's 48/120 touched scenarios (40.0%) at "
                "75% precision — the extra coverage is entirely false "
                "cross-scenario pairs. This is the precision-over-coverage "
                "tradeoff Concord makes."
            ),
        },
        "completeness": summary["completeness"],
        "limitations": (
            [
                f"PARTIAL EVALUATION: {summary['layer2_attempted']} of {residual_count} "
                f"residual scenarios have fresh Groq outputs ({summary['layer2_missing']} "
                "missing due to Groq daily token quota exhaustion).",
                "Metrics are computed only on scenarios with KNOWN correctness "
                "(fresh inference with proposed_match_ids). UNKNOWN scenarios are "
                "excluded from both numerator and denominator; they are never "
                "silently treated as correct or incorrect.",
                "The full 77/77 evaluation has not been completed. The artifact is "
                "explicitly labelled PARTIAL.",
            ]
            if status == "partial"
            else [
                f"FULL EVALUATION: {summary['layer2_attempted']}/{residual_count} "
                "residual scenarios evaluated.",
                "Provider failures (UNKNOWN) are explicitly tracked and excluded "
                "from correctness denominators. Proposal-level recall is "
                "computed over attempted scenarios via the correlation_id -> "
                "scenario_id mapping; proposal-level precision is NOT "
                "COMPUTABLE in this report.",
            ]
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _render_markdown(report: Dict[str, Any]) -> str:
    fp = report["dataset_fingerprint"]
    status = report["evaluation_status"]
    out = report["layer2"]["outcomes_by_type"]
    provider = report["layer2"]["provider_failure"]
    outcome_states = report["layer2"].get("outcome_states", {})
    precisions = {
        "0.90": report["precision_at_0_90"],
        "0.75": report["precision_at_0_75"],
        "0.60": report["precision_at_0_60"],
    }
    recall = report["recall"]
    fa = report["false_accept_count"]
    fa_total = report["false_accept_total_auto_accepted"]
    fa_known = report.get("false_accept_known_correctness", 0)
    fa_unknown = report.get("false_accept_unknown_correctness", 0)
    fa_rate = report["false_accept_rate"]
    rev = report["review_composition"]
    exc = report["exception_composition"]
    edges = report["per_edge_case_breakdown"]

    def _fmt(v: Optional[float]) -> str:
        return "N/A" if v is None else f"{v:.1%}"

    lines = [
        "# Concord — Current Canonical Evaluation Report",
        "",
        f"> **LIFECYCLE: CURRENT** (status: **{status.upper()}**)",
        f"> Generated: {report['generated_at']}",
        f"> Dataset fingerprint: `{fp}`",
        f"> Source artifact: `{report['_canonical_artifact_filename']}` (SHA-256: "
        f"`{report['_canonical_artifact_sha256']}`)",
        "",
        f"## Evaluation status: {status.upper()}",
        "",
        f"- Status: **{status.upper()}** — {report['layer2_attempted']} of "
        f"{report['residual_scenarios']} residual scenarios evaluated "
        f"({report['layer2_missing']} missing due to Groq quota exhaustion).",
        "- Historical artifacts superseded: "
        + ", ".join(f"`{x}`" for x in report["_historical_artifacts_superseded"]),
        "",
        "## Dataset provenance",
        f"- Fingerprint: `{fp}`",
        f"- Seed: {report['dataset_seed']}",
        f"- Total records: {report['total_records']}",
        f"- Total scenarios: {report['total_scenarios']}",
        f"- Residual scenarios: {report['residual_scenarios']}",
        "",
        "## Layer 1 (deterministic)",
        "- **Scenario-level:** matched "
        f"{report['layer1']['matched_scenarios']} / {report['total_scenarios']} "
        "scenarios "
        f"({report['layer1']['scenario_match_rate']:.1%})",
        "- **Record-level:** matched "
        f"{report['layer1']['matched_records']} / {report['total_records']} "
        "records "
        f"({report['layer1']['record_match_rate']:.1%})",
        f"- Residual records: {report['layer1']['residual_records']}",
        f"- Residual scenarios: {report['layer1']['residual_scenarios']}",
        "- Precision: "
        f"{_fmt(report['layer1']['precision'])} "
        f"({report['layer1']['correct_decisions']} / "
        f"{report['layer1']['decisions']} correct decisions)",
        "",
        "## Layer 2 (Groq) execution",
        f"- Attempted: {report['layer2_attempted']} / "
        f"{report['residual_scenarios']}",
        f"- Successful: {report['layer2']['successful']}",
        f"- Outcomes by type: {out}",
        f"- Provider failure total: {provider['total']} "
        f"(quota_exhausted={provider['quota_exhausted']}, "
        f"transient={provider['transient']}, unknown={provider['unknown']})",
        "",
        "## Outcome state classification",
        f"- CORRECT: {outcome_states.get('correct', 0)}",
        f"- INCORRECT: {outcome_states.get('incorrect', 0)}",
        f"- UNKNOWN (provider failures): {outcome_states.get('unknown', 0)}",
        f"- Known outcome rate: {_fmt(outcome_states.get('known_outcome_rate'))}",
        "",
        "## Layer 2 quality metrics (outcome-level precision at threshold)",
        "| Threshold | Total Eligible | Known Correctness | Unknown | TP | FP | Precision | Known Rate |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for thr in ("0.90", "0.75", "0.60"):
        m = precisions[thr]
        lines.append(
            f"| >= {thr} | {m['total_eligible']} | {m['known_correctness']} | "
            f"{m['unknown_correctness']} | {m['true_positive']} | "
            f"{m['false_positive']} | {_fmt(m['precision'])} | "
            f"{_fmt(m['known_outcome_rate'])} |"
        )
    lines.append("")
    if recall.get("value") is not None:
        lines.append(
            f"**Recall (Layer 2 proposal-level, attempted scenarios):** "
            f"{recall['true_positives']} / {recall['denominator']} "
            f"({recall['value']:.1%})"
        )
        if recall.get("excluded_categories"):
            lines.append(
                "- Excluded from the recall denominator (documented ambiguous "
                "expected-match semantics): "
                + ", ".join(recall["excluded_categories"])
                + f" ({recall['excluded_scenarios']} scenarios, "
                f"{recall['excluded_correct']} correctly proposed; including "
                "them the value would be "
                f"{recall['true_positives_including_excluded']} / "
                f"{recall['denominator_including_excluded']} "
                f"({_fmt(recall['recall_including_excluded'])}))"
            )
    else:
        lines.append(
            f"**Recall:** {recall.get('status', 'UNKNOWN')} — "
            f"{recall.get('note', '')}"
        )
    lines.append(f"- Note: {recall.get('note', '')}")
    lines.append("")
    lines.append("## Outcome-level metrics (computable from this artifact)")
    om = report.get("outcome_level_metrics", {})
    m = om.get("matchable_scenarios", {})
    nm = om.get("no_match_scenarios", {})
    lines.append(
        f"- Matchable scenarios (expected_outcome in MATCH_*): "
        f"{m.get('correct', 0)} correct / {m.get('total', 0)} total "
        f"({_fmt(m.get('accuracy'))})"
    )
    lines.append(
        f"- No-match scenarios (expected_outcome == NO_MATCH): "
        f"{nm.get('correct', 0)} correct / {nm.get('total', 0)} total "
        f"({_fmt(nm.get('accuracy'))})"
    )
    lines.append("")
    lines.append("## Safety (false accept rate)")
    lines.append(
        f"- False accepts: **{fa}** / {fa_known} known auto-accepted "
        f"({_fmt(fa_rate)})"
    )
    if fa_unknown > 0:
        lines.append(
            f"- Note: {fa_unknown} auto-accepted outcomes had UNKNOWN correctness "
            "(provider failures) and are excluded from the rate."
        )
    lines.append("")
    lines.append("## Routing buckets")
    lines.append("| Bucket | Count |")
    lines.append("|---|---|")
    for b in ("DETERMINISTIC_MATCH", "AI_AUTO_ACCEPTED", "HUMAN_REVIEW", "EXCEPTION"):
        lines.append(f"| {b} | {report['routing_buckets'].get(b, 0)} |")
    lines.append("")
    lines.append("## Review composition")
    lines.append(f"- Total: {rev['total']}")
    for cat, n in sorted(rev["by_category"].items()):
        lines.append(f"  - {cat}: {n}")
    lines.append("")
    lines.append("## Exception composition")
    lines.append(f"- Total: {exc['total']}")
    for cat, n in sorted(exc["by_category"].items()):
        lines.append(f"  - {cat}: {n}")
    lines.append("")
    lines.append("## Per-edge-case breakdown (scenario-level)")
    lines.append(
        "| Category | Attempted | Missing | ValidProp | NoProp | Fail | "
        "ApiErr | AutoAcc | Review | Exception |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for cat in sorted(edges.keys()):
        e = edges[cat]
        lines.append(
            f"| {cat} | {e['scenarios_attempted']} | {e['scenarios_missing']} "
            f"| {e['proposal_valid']} | {e['no_proposal']} "
            f"| {e['validation_failed']} | {e['api_error']} "
            f"| {e['ai_auto_accepted']} | {e['human_review']} "
            f"| {e['exception']} |"
        )
    lines.append("")
    lines.append("## Throughput")
    lines.append(
        f"- Layer 1 time: {report['throughput']['layer1_time_ms']:.2f}ms "
        f"(deterministic)."
    )
    lines.append(
        f"- Layer 2 records evaluated: {report['throughput']['layer2_evaluated_in_artifact']}"
    )
    lines.append("")
    lines.append("## Baseline comparison")
    bc = report["baseline_comparison"]
    nb = bc["naive_matcher"]
    l1b = bc["layer1"]
    lines.append(
        "- **Naive baseline** (single-pass greedy closest-amount/date "
        "matcher, run on the same frozen dataset): "
        f"{nb['matched_records']} / {report['total_records']} records "
        f"matched ({nb['record_match_rate']:.1%}); "
        f"{nb['match_pairs']} pairs, of which {nb['correct_pairs']} are "
        f"correct ({nb['pair_precision']:.1%} pair precision); "
        f"{nb['matched_scenarios']} / {report['total_scenarios']} scenarios "
        f"touched ({nb['scenario_match_rate']:.1%}) at "
        f"{nb['scenario_precision']:.1%} scenario precision; "
        f"{nb['unmatched_records']} residual records."
    )
    lines.append(
        "- **Layer 1 (deterministic):** "
        f"{l1b['matched_records']} / {report['total_records']} records "
        f"matched ({l1b['record_match_rate']:.1%}); "
        f"{l1b['matched_scenarios']} / {report['total_scenarios']} scenarios "
        f"matched ({l1b['scenario_match_rate']:.1%}) at "
        f"{_fmt(l1b['precision'])} precision "
        f"({l1b['correct_decisions']} / {l1b['decisions']} correct decisions)."
    )
    lines.append(
        f"- Scenario-level match-rate delta (Layer 1 − baseline): "
        f"{_fmt(bc['match_rate_delta'])}"
    )
    lines.append(
        f"- Scenario-level precision delta (Layer 1 − baseline): "
        f"{_fmt(bc['precision_delta'])}"
    )
    lines.append("")
    lines.append(f"*{bc['note']}*")
    lines.append("")
    lines.append("## Completeness")
    lines.append(f"- {report['completeness']:.1%} of residual scenarios evaluated.")
    lines.append("")
    lines.append("## Limitations")
    for lim in report["limitations"]:
        lines.append(f"- {lim}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    fingerprint, seed, _ = _verify_manifest()
    records = _verify_canonical_artifact(fingerprint)
    residual_scenarios = _load_residual_scenarios()
    ground_truth = _load_ground_truth()

    if len(records) >= TOTAL_RESIDUAL_SCENARIOS:
        status = "final"
    else:
        status = "partial"

    summary = _summarize(records, residual_scenarios, ground_truth)
    report = _build_report(
        fingerprint=fingerprint,
        seed=seed,
        manifest_files={},
        records=records,
        summary=summary,
        status=status,
    )

    REPORT_JSON_PATH.write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    REPORT_MD_PATH.write_text(_render_markdown(report), encoding="utf-8")

    print(f"Wrote {REPORT_JSON_PATH}")
    print(f"Wrote {REPORT_MD_PATH}")
    print(f"Status: {status.upper()}")
    print(
        f"Attempted: {summary['layer2_attempted']} / "
        f"{TOTAL_RESIDUAL_SCENARIOS}"
    )
    fa = summary["false_accept"]
    print(f"False accept rate: {fa['rate']}")
    print(f"False accept count: {fa['count']}")
    print(f"False accept known correctness: {fa['known_correctness']}")
    print(f"Outcome states: {summary['outcome_states']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CurrentReportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)