# Clean Layer 2 Evaluation Report

> **LIFECYCLE: HISTORICAL** — This artifact was generated against dataset fingerprint `b8bf3feb` which is no longer the canonical fingerprint.
>
> Current canonical dataset fingerprint: `d91ead9a86a4d1dc949cf020a118957eeacb6eb4f18efb844c3c1b7afd0c6be0`.
>
> Superseded by `data/layer2_clean_audit.jsonl`. Retained for historical reference only; must never be interpreted as a current evaluation result.

**Evaluation type:** PARTIAL EVALUATION
**Timestamp:** 2026-09-01T09:50:18.167347+00:00
**Dataset fingerprint:** `b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93`
**Artifact SHA-256:** `3c313d9892f6f8c6cec44fdc517868be78bdcdfdf001449bd32b047976edaff2`

## Dataset
- Total records: 245
- Total scenarios: 120
- Leakage-free: True

## Layer 1 Baseline
- Matched records: 43 / 245 (17.6%)
- Residual records: 159
- Residual scenarios: 77
- Deterministic precision: 100.0%

## Layer 2 Execution
- Attempted: 77
- Successful: 64
- API errors: 13
  - quota_exhausted: 0
  - transient: 13
  - timeout: 0
  - unknown: 0
- Valid proposals: 48
- No proposals: 16
- Validation failed: 0

## Layer 2 Quality
- AI precision @ 0.90: 100.0% (13TP / 13 total)
- AI precision @ 0.75: 100.0% (22TP / 22 total)
- AI precision @ 0.60: 89.6% (43TP / 48 total)
- AI recall (system-wide): 71.4% (30TP / 42 residuals)
- AI recall (attempted only): 88.2% (30TP / 34 residuals)

## Layer 3 Routing
- AI_AUTO_ACCEPTED: 22
- DETERMINISTIC_MATCH: 86
- EXCEPTION: 97
- HUMAN_REVIEW: 40

## Safety Analysis
- False accepts: 0 / 22 auto-accepted (0.0%)
- Human review: 40
- Exceptions: 97

## Edge-Case Breakdown
| Category | Count | L1 Matched | L2 Attempted | L2 Correct | Auto-Accept | Review | Exception |
|---|---|---|---|---|---|---|---|
| DUPLICATE | 10 | 0 | 10 | 10 | 4 | 6 | 0 |
| EXACT_MATCH | 20 | 20 | 0 | 0 | 0 | 0 | 0 |
| FEE_DEDUCTED | 12 | 0 | 12 | 11 | 0 | 12 | 0 |
| INCONSISTENT_NARRATION | 10 | 0 | 10 | 1 | 0 | 1 | 9 |
| LATE_ARRIVING | 10 | 0 | 10 | 9 | 5 | 4 | 1 |
| PARTIAL_REFUND | 10 | 0 | 10 | 5 | 0 | 5 | 5 |
| ROUNDING_DIFFERENCE | 13 | 8 | 5 | 3 | 0 | 3 | 2 |
| SPLIT_SETTLEMENT | 10 | 0 | 10 | 4 | 4 | 0 | 6 |
| TRUE_ORPHAN | 10 | 0 | 10 | 0 | 0 | 4 | 6 |
| T_PLUS_DELAY | 15 | 15 | 0 | 0 | 0 | 0 | 0 |

## Limitations
- **PARTIAL EVALUATION**: 13 of 77 scenarios had provider errors

## Artifact
- File: `layer2_clean_audit.jsonl`
- SHA-256: `3c313d9892f6f8c6cec44fdc517868be78bdcdfdf001449bd32b047976edaff2`
- Records: 77
- Dataset fingerprint: `b8bf3feb57ffcb23c44b1058742be8113caf0f538ea709e9b64893d5ee5dde93`