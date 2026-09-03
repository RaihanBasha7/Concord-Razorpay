# Concord — Current Canonical Evaluation Report

> **LIFECYCLE: CURRENT** (status: **FINAL**)
> Generated: 2026-09-03T16:18:24.857641+00:00
> Dataset fingerprint: `d91ead9a86a4d1dc949cf020a118957eeacb6eb4f18efb844c3c1b7afd0c6be0`
> Source artifact: `layer2_clean_audit.jsonl` (SHA-256: `6291c6f07ba98cea5d3aacca8fde7e268db9bfaded91b19aba225de50aa23c57`)

## Evaluation status: FINAL

- Status: **FINAL** — 77 of 77 residual scenarios evaluated (0 missing due to Groq quota exhaustion).
- Historical artifacts superseded: `data/clean_evaluation_report.json`, `data/clean_evaluation_report.md`, `data/clean_eval_results.json`, `data/clean_full_evaluation_report.json`, `data/clean_full_evaluation_report.md`, `data/clean_full_eval_results.json`

## Dataset provenance
- Fingerprint: `d91ead9a86a4d1dc949cf020a118957eeacb6eb4f18efb844c3c1b7afd0c6be0`
- Seed: 42
- Total records: 245
- Total scenarios: 120
- Residual scenarios: 77

## Layer 1 (deterministic)
- Matched records: 43 / 245 (17.6%)
- Residual records: 159
- Residual scenarios: 77

## Layer 2 (Groq) execution
- Attempted: 77 / 77
- Successful: 62
- Outcomes by type: {'PROPOSAL_VALID': 46, 'API_ERROR': 15, 'NO_PROPOSAL': 16}
- Provider failure total: 15 (quota_exhausted=7, transient=7, unknown=1)

## Outcome state classification
- CORRECT: 26
- INCORRECT: 36
- UNKNOWN (provider failures): 15
- Known outcome rate: 80.5%

## Layer 2 quality metrics (outcome-level precision at threshold)
| Threshold | Total Eligible | Known Correctness | Unknown | TP | FP | Precision | Known Rate |
|---|---|---|---|---|---|---|---|
| >= 0.90 | 15 | 15 | 0 | 9 | 6 | 60.0% | 100.0% |
| >= 0.75 | 27 | 27 | 0 | 10 | 17 | 37.0% | 100.0% |
| >= 0.60 | 45 | 45 | 0 | 10 | 35 | 22.2% | 100.0% |

**Recall:** NOT_COMPUTABLE — Proposal-level recall is NOT COMPUTABLE because synthetic record IDs in the artifact (e.g., 'SETTLEMENT-xxx') cannot be mapped back to ground_truth record_specs (which use synthetic_refs like 'REC-SET-001'). The normalization mapping is not present in the artifact. Minimal fix: include 'synthetic_ref' in each audit record or a 'normalization_map' in the artifact header.

## Outcome-level metrics (computable from this artifact)
- Matchable scenarios (expected_outcome in MATCH_*): 0 correct / 0 total (N/A)
- No-match scenarios (expected_outcome == NO_MATCH): 26 correct / 62 total (41.9%)

## Safety (false accept rate)
- False accepts: **6** / 15 known auto-accepted (40.0%)

## Routing buckets
| Bucket | Count |
|---|---|
| DETERMINISTIC_MATCH | 0 |
| AI_AUTO_ACCEPTED | 15 |
| HUMAN_REVIEW | 30 |
| EXCEPTION | 32 |

## Review composition
- Total: 30
  - DUP: 6
  - FEE: 9
  - LATE: 5
  - NARR: 1
  - REFD: 2
  - RND: 5
  - SPLT: 1
  - UNQ: 1

## Exception composition
- Total: 32
  - DUP: 2
  - FEE: 3
  - LATE: 2
  - NARR: 9
  - REFD: 7
  - UNQ: 9

## Per-edge-case breakdown (scenario-level)
| Category | Attempted | Missing | ValidProp | NoProp | Fail | ApiErr | AutoAcc | Review | Exception |
|---|---|---|---|---|---|---|---|---|---|
| DUP | 10 | 0 | 8 | 0 | 0 | 2 | 2 | 6 | 2 |
| FEE | 12 | 0 | 9 | 1 | 0 | 2 | 0 | 9 | 3 |
| LATE | 10 | 0 | 8 | 0 | 0 | 2 | 3 | 5 | 2 |
| NARR | 10 | 0 | 1 | 8 | 0 | 1 | 0 | 1 | 9 |
| REFD | 10 | 0 | 3 | 4 | 0 | 3 | 1 | 2 | 7 |
| RND | 5 | 0 | 5 | 0 | 0 | 0 | 0 | 5 | 0 |
| SPLT | 10 | 0 | 10 | 0 | 0 | 0 | 9 | 1 | 0 |
| UNQ | 10 | 0 | 2 | 3 | 0 | 5 | 0 | 1 | 9 |

## Throughput
- Layer 1 time: 1.03ms (deterministic).
- Layer 2 records evaluated: 77

## Baseline comparison
- Layer 1-only match rate: 17.6%
- Layer 1 deterministic precision: 100.0%

## Completeness
- 100.0% of residual scenarios evaluated.

## Limitations
- FULL EVALUATION: 77/77 residual scenarios evaluated.
- Provider failures (UNKNOWN) are explicitly tracked and excluded from correctness denominators. Proposal-level precision/recall are NOT COMPUTABLE without normalization mapping.
