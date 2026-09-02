# Complete Clean Layer 2 Evaluation Report

**Evaluation type:** PARTIAL EVALUATION
**Verdict:** YELLOW — Evaluation is clean but incomplete due to provider quota limits.
**Timestamp:** 2026-09-01T11:12:33.152568+00:00
**Dataset fingerprint:** `b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93`

## 1. Dataset Provenance
- Total records: 245
- Total scenarios: 120
- Residual scenarios: 77
- Leakage-free: Verified
- Fingerprint: `b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93`

## 2. Layer 1 Baseline
- Matched records: 43 / 245 (17.6%)
- Residual records: 159
- Residual scenarios: 77
- Deterministic precision: 100.0%

## 3. Layer 2 Execution
- Total attempted: 77
- Fresh inference: 6 (with proposed_match_ids)
- Prior continuation: 31 (outcome+confidence only)
- Provider failures: 40 (quota exhausted)
- Unevaluated: 0

## 4. Layer 2 Quality
### Proposal-level
- Proposals with known correctness: 6
- Correct: 5
- Incorrect: 1
- Precision: 83.3%
- Note: 21 prior proposals lack proposed_match_ids.

### Precision at thresholds
- >=0.90: 60.0% (3TP / 5 total)
- >=0.75: 28.6% (4TP / 14 total)
- >=0.60: 18.5% (5TP / 27 total)

### Recall
- System-wide: 9.5% (4TP / 42 residuals)

## 5. Layer 3 Routing
### Record-level
- DETERMINISTIC_MATCH: 86 (35.1%)
- AI_AUTO_ACCEPTED: 6 (2.4%)
- HUMAN_REVIEW: 4 (1.6%)
- EXCEPTION: 149 (60.8%)
- Accounting check: 86+6+4+149 = 245

### Scenario-level
- DETERMINISTIC_MATCH: 43
- AI_AUTO_ACCEPTED: 5
- HUMAN_REVIEW: 21
- EXCEPTION: 51

## 6. False Accepts
- Count: 0
- Rate: 0.0%
- Total auto-accepted: 6

## 7. Guardrail Impact
- Proposals affected: 1
  - DUP-005 (DUPLICATE)

## 8. Edge-Case Breakdown (SCENARIO-LEVEL)
| Category | Scn | L1 | Res | L2att | L2ok | Err | NoP | Prop | A-acc | Rev | Exc | FA | Correct? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DUPLICATE | 10 | 0 | 10 | 10 | 9 | 1 | 0 | 8 | 2 | 6 | 2 | 0 | 4K/5U |
| EXACT_MATCH | 20 | 20 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0K/0U |
| FEE_DEDUCTED | 12 | 0 | 12 | 12 | 10 | 2 | 1 | 9 | 0 | 9 | 3 | 0 | 0K/11U |
| INCONSISTENT_NARRATION | 10 | 0 | 10 | 10 | 9 | 1 | 8 | 1 | 0 | 1 | 9 | 0 | 8K/2U |
| LATE_ARRIVING | 10 | 0 | 10 | 10 | 8 | 2 | 0 | 8 | 3 | 5 | 2 | 0 | 1K/9U |
| PARTIAL_REFUND | 10 | 0 | 10 | 10 | 1 | 9 | 1 | 0 | 0 | 0 | 10 | 0 | 0K/9U |
| ROUNDING_DIFFERENCE | 13 | 8 | 5 | 5 | 0 | 5 | 0 | 0 | 0 | 0 | 5 | 0 | 0K/5U |
| SPLIT_SETTLEMENT | 10 | 0 | 10 | 10 | 0 | 10 | 0 | 0 | 0 | 0 | 10 | 0 | 0K/10U |
| TRUE_ORPHAN | 10 | 0 | 10 | 10 | 0 | 10 | 0 | 0 | 0 | 0 | 10 | 0 | 0K/10U |
| T_PLUS_DELAY | 15 | 15 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0K/0U |

*K = known correctness, U = unknown (prior results without proposed_match_ids)*

## 9. Provider Reliability
- API failures: 40 / 77 (51.9%)
- Quota exhausted: 40
- Transient errors: 0

## 10. Limitations
1. **PARTIAL EVALUATION**: Only 6 of 77 residual scenarios have fresh Groq outputs with proposed_match_ids.
2. **31 prior results lack proposed_match_ids**: Correctness is UNKNOWN for these scenarios. They contribute to routing but not to precision/recall metrics.
3. **40 provider failures**: Groq daily quota (200K tokens) exhausted after 6 fresh requests.
4. **Proposal-level precision** is only computable for 6 scenarios with known correctness.

## 11. Artifact Provenance
- Dataset fingerprint: `b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93`
- Fresh inference results: 6 scenarios
- Prior continuation results: 31 scenarios
- Provider failures: 40 scenarios
- No canonical artifact created (partial evaluation)