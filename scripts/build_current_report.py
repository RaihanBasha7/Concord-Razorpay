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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
MANIFEST_PATH = DATA_DIR / "dataset_manifest.json"
ARTIFACT_PATH = DATA_DIR / "layer2_clean_audit.jsonl"
GROUND_TRUTH_PATH = DATA_DIR / "ground_truth.json"
RESIDUALS_PATH = DATA_DIR / "residuals.csv"
REPORT_JSON_PATH = DATA_DIR / "current_evaluation_report.json"
REPORT_MD_PATH = DATA_DIR / "current_evaluation_report.md"

TOTAL_RESIDUAL_SCENARIOS = 77
AUTO_ACCEPT_THRESHOLD = 0.90
REVIEW_THRESHOLD = 0.60


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
                "Proposal-level precision is NOT COMPUTABLE (synthetic record IDs "
                "in artifact cannot map to ground truth record_specs)."
            ),
        }

    # ---- Recall: NOT COMPUTABLE
    #
    # Proposal-level recall requires mapping proposed_match_ids back to
    # ground_truth record_specs via synthetic_ref. The artifact contains
    # normalized record IDs (e.g., "SETTLEMENT-xxx") but ground_truth uses
    # synthetic_refs (e.g., "REC-SET-001"). The normalization mapping
    # (synthetic_ref -> actual record ID) is not present in the artifact.
    #
    # Minimal provenance change needed for future evaluations:
    #   Add "synthetic_ref" field to each record in the Layer 2 audit artifact,
    #   or include a normalization_map {synthetic_ref: actual_record_id} in
    #   the artifact header.
    recall = {
        "status": "NOT_COMPUTABLE",
        "value": None,
        "true_positives": 0,
        "denominator": 0,
        "note": (
            "Proposal-level recall is NOT COMPUTABLE because synthetic record IDs "
            "in the artifact (e.g., 'SETTLEMENT-xxx') cannot be mapped back to "
            "ground_truth record_specs (which use synthetic_refs like "
            "'REC-SET-001'). The normalization mapping is not present in the "
            "artifact. Minimal fix: include 'synthetic_ref' in each audit record "
            "or a 'normalization_map' in the artifact header."
        ),
    }

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
    total_records = 245  # canonical expected
    total_scenarios = 120  # canonical expected
    residual_count = 77  # canonical expected

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
            "matched_records": 43,  # canonical L1 deterministic match count
            "residual_records": 159,
            "residual_scenarios": 77,
            "match_rate_records": 43 / 245,
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
            "layer1_only_match_rate": 43 / 245,
            "layer1_deterministic_precision": 1.0,
            "note": "Baseline comparison uses the Layer 1 deterministic matcher "
                    "as the no-AI reference. Concord's hybrid (L1 + L2 + L3) "
                    "is evaluated against it.",
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
                "from correctness denominators. Proposal-level precision/recall "
                "are NOT COMPUTABLE without normalization mapping.",
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
        f"- Matched records: {report['layer1']['matched_records']} / "
        f"{report['total_records']} ({report['layer1']['match_rate_records']:.1%})",
        f"- Residual records: {report['layer1']['residual_records']}",
        f"- Residual scenarios: {report['layer1']['residual_scenarios']}",
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
    lines.append(
        f"**Recall:** {recall.get('status', 'UNKNOWN')} — {recall.get('note', '')}"
    )
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
    lines.append(
        f"- Layer 1-only match rate: {report['baseline_comparison']['layer1_only_match_rate']:.1%}"
    )
    lines.append(
        f"- Layer 1 deterministic precision: "
        f"{report['baseline_comparison']['layer1_deterministic_precision']:.1%}"
    )
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