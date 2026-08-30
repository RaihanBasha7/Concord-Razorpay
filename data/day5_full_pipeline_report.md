# Concord Day 5 — Full Pipeline Evaluation Report

**Run timestamp:** 2026-08-29T23:43:16.768220
**Dataset seed:** 42
**Total scenarios:** 120
**Total records:** 245
**Layer 2 mode:** artifact-backed
**Layer 2 provenance:** Real Layer 2 artifacts from layer2_full_audit.jsonl. Dataset fingerprint 8dca28fe7e5c… verified; 77 of 77 residual scenarios have Layer 2 artifacts (77 API_ERROR); record-id coverage 100%; 77 artifact(s) provenance-verified (fingerprint-matched), 0 legacy artifact(s) provenance-unverified (record-id compatibility checked only).
**Auto-accept threshold:** 0.9
**Review threshold:** 0.6
**Baseline config:** {'amount_tolerance_paise': 100, 'date_window_days': 2}

---

## 1. Deterministic Match Rate

- **Total scenarios:** 120
- **Matched:** 43
- **Match rate:** 35.83%

## 2. Deterministic Match Precision

- **Correct:** 43
- **Precision:** 100.00%

---

## 3. AI-Proposal Precision at Three Analysis Cuts

### confidence >= 0.90

- **Included:** 0
- **True positives:** 0
- **False positives:** 0
- **Precision:** N/A

### confidence >= 0.75

- **Included:** 0
- **True positives:** 0
- **False positives:** 0
- **Precision:** N/A

### confidence >= 0.60

- **Included:** 0
- **True positives:** 0
- **False positives:** 0
- **Precision:** N/A


---

## 4. AI-Proposal Recall

- **True positives:** 0
- **Denominator (residuals with real match):** 0
- **Recall:** N/A

---

## 5. False-Accept Rate

> **This is the headline safety metric.**

- **Total auto-accepted:** 0
- **False accepts:** 0
- **False-accept rate:** N/A

---

## 6. Review-Queue Composition (HUMAN_REVIEW)

- **Total count:** 0
- **Percentage of batch:** 0.00%

### By category

| Category | Count | % of review queue |
|----------|-------|-------------------|
| EXACT_MATCH | 0 | N/A |
| T_PLUS_DELAY | 0 | N/A |
| FEE_DEDUCTED | 0 | N/A |
| PARTIAL_REFUND | 0 | N/A |
| SPLIT_SETTLEMENT | 0 | N/A |
| ROUNDING_DIFFERENCE | 0 | N/A |
| INCONSISTENT_NARRATION | 0 | N/A |
| DUPLICATE | 0 | N/A |
| TRUE_ORPHAN | 0 | N/A |
| LATE_ARRIVING | 0 | N/A |

*Note: Percentages are over total HUMAN_REVIEW records (denominator = review queue total), not total batch.*

---

## 7. Exception Composition

- **Total count:** 159
- **Percentage of batch:** 64.90%
- **Correctly refused (no real match exists):** 60 (37.74%)
- **Should have been caught (real match existed):** 99 (62.26%)

---

## 8. Throughput

- **Total batch time:** 116.96 ms
- **Layer 1 time:** 1.13 ms
- **Layer 2 time:** 115.83 ms

*Layer 2 time is summed across all residual scenarios. Layer 1 is batch-level.*

---

## 9. Baseline Comparison (Layer 1 only)

| Metric | Baseline | Layered System (Layer 1) | Delta |
|--------|----------|--------------------------|-------|
| Match rate | 40.00% | 35.83% | -4.17pp |
| Precision | 75.00% | 100.00% | +25.00pp |

*Note: This table compares baseline vs Layer 1 only. Baseline does not implement Layer 2/AI metrics, so a single match rate delta for the full system would be misleading.*

---

## 10. Final System Routing Composition (all layers)

- **Total records:** 245

| Routing bucket | Count | Rate |
|----------------|-------|------|
| DETERMINISTIC_MATCH | 86 | 35.10% |
| AI_AUTO_ACCEPTED | 0 | 0.00% |
| HUMAN_REVIEW | 0 | 0.00% |
| EXCEPTION | 159 | 64.90% |

Final system routing composition at the record level. DETERMINISTIC_MATCH = Layer 1 exact/amount-date match. AI_AUTO_ACCEPTED = Layer 2 proposal accepted automatically. HUMAN_REVIEW = Layer 2 proposal needs human review. EXCEPTION = no valid Layer 2 outcome or low confidence.

---

## Metadata

- **Dataset used:** Synthetic dataset with seed 42
- **Dataset fingerprint:** 8dca28fe7e5c50655b8d0cfbf3bfe858c17f52af82d44dc70a6520eec6fa5e6d
- **Evaluation run identity/time:** 2026-08-29T23:43:16.768220
- **Layer 2 provenance:** Real Layer 2 artifacts from layer2_full_audit.jsonl. Dataset fingerprint 8dca28fe7e5c… verified; 77 of 77 residual scenarios have Layer 2 artifacts (77 API_ERROR); record-id coverage 100%; 77 artifact(s) provenance-verified (fingerprint-matched), 0 legacy artifact(s) provenance-unverified (record-id compatibility checked only).
- **Configured routing thresholds:** auto_accept=0.9, review=0.6
- **Baseline configuration:** {'amount_tolerance_paise': 100, 'date_window_days': 2}
- **Layer 2 execution mode:** artifact-backed
- **Dataset fingerprint:** 8dca28fe7e5c50655b8d0cfbf3bfe858c17f52af82d44dc70a6520eec6fa5e6d