# Concord Day 5 — Full Pipeline Evaluation Report

**Run timestamp:** 2026-08-30T16:56:59.965778
**Dataset seed:** 42
**Total scenarios:** 120
**Total records:** 245
**Layer 2 mode:** artifact-backed
**Layer 2 provenance:** Real Layer 2 artifacts from layer2_full_audit.jsonl. Dataset fingerprint 8dca28fe7e5c… verified; 77 of 77 residual scenarios have Layer 2 artifacts (74 API_ERROR); record-id coverage 100%; 77 artifact(s) provenance-verified (fingerprint-matched), 0 legacy artifact(s) provenance-unverified (record-id compatibility checked only).
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

- **Included:** 1
- **True positives:** 1
- **False positives:** 0
- **Precision:** 100.00%

### confidence >= 0.75

- **Included:** 2
- **True positives:** 2
- **False positives:** 0
- **Precision:** 100.00%

### confidence >= 0.60

- **Included:** 2
- **True positives:** 2
- **False positives:** 0
- **Precision:** 100.00%


---

## 4. AI-Proposal Recall

### AI recall (system-wide)

*Denominator: all residual scenarios with a real match, regardless of Layer 2 outcome.*

- **True positives:** 2
- **Denominator:** 42
- **Recall:** 4.76%

### AI recall (of attempted scenarios)

*Denominator: only residuals where Layer 2 actually ran (excludes API_ERROR provider failures).*

- **True positives:** 2
- **Denominator:** 3
- **Recall:** 66.67%

---

## 5. False-Accept Rate

> **This is the headline safety metric.**

- **Total auto-accepted:** 2
- **False accepts:** 0
- **False-accept rate:** 0.00%

---

## 6. Review-Queue Composition (HUMAN_REVIEW)

- **Total count:** 1
- **Percentage of batch:** 0.41%

### By category

| Category | Count | % of review queue |
|----------|-------|-------------------|
| EXACT_MATCH | 0 | 0.00% |
| T_PLUS_DELAY | 0 | 0.00% |
| FEE_DEDUCTED | 1 | 100.00% |
| PARTIAL_REFUND | 0 | 0.00% |
| SPLIT_SETTLEMENT | 0 | 0.00% |
| ROUNDING_DIFFERENCE | 0 | 0.00% |
| INCONSISTENT_NARRATION | 0 | 0.00% |
| DUPLICATE | 0 | 0.00% |
| TRUE_ORPHAN | 0 | 0.00% |
| LATE_ARRIVING | 0 | 0.00% |

*Note: Percentages are over total HUMAN_REVIEW records (denominator = review queue total), not total batch.*

---

## 7. Exception Composition

- **Total count:** 156
- **Percentage of batch:** 63.67%
- **Correctly refused (no real match exists):** 60 (38.46%)
- **Should have been caught (real match existed):** 96 (61.54%)

---

## 8. Throughput

- **Total batch time:** 31.67 ms
- **Layer 1 time:** 1.09 ms
- **Layer 2 time:** 30.58 ms

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
| AI_AUTO_ACCEPTED | 2 | 0.82% |
| HUMAN_REVIEW | 1 | 0.41% |
| EXCEPTION | 156 | 63.67% |

Final system routing composition at the record level. DETERMINISTIC_MATCH = Layer 1 exact/amount-date match. AI_AUTO_ACCEPTED = Layer 2 proposal accepted automatically. HUMAN_REVIEW = Layer 2 proposal needs human review. EXCEPTION = no valid Layer 2 outcome or low confidence.

---

## Metadata

- **Dataset used:** Synthetic dataset with seed 42
- **Dataset fingerprint:** 8dca28fe7e5c50655b8d0cfbf3bfe858c17f52af82d44dc70a6520eec6fa5e6d
- **Evaluation run identity/time:** 2026-08-30T16:56:59.965778
- **Layer 2 provenance:** Real Layer 2 artifacts from layer2_full_audit.jsonl. Dataset fingerprint 8dca28fe7e5c… verified; 77 of 77 residual scenarios have Layer 2 artifacts (74 API_ERROR); record-id coverage 100%; 77 artifact(s) provenance-verified (fingerprint-matched), 0 legacy artifact(s) provenance-unverified (record-id compatibility checked only).
- **Configured routing thresholds:** auto_accept=0.9, review=0.6
- **Baseline configuration:** {'amount_tolerance_paise': 100, 'date_window_days': 2}
- **Layer 2 execution mode:** artifact-backed
- **Dataset fingerprint:** 8dca28fe7e5c50655b8d0cfbf3bfe858c17f52af82d44dc70a6520eec6fa5e6d