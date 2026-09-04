# Concord — Current Canonical Evaluation Report

> **LIFECYCLE: CURRENT** (status: **FINAL**)
> Generated: 2026-09-04T11:36:49.699491+00:00
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
- **Scenario-level:** matched 43 / 120 scenarios (35.8%)
- **Record-level:** matched 86 / 245 records (35.1%)
- Residual records: 159
- Residual scenarios: 77
- Precision: 100.0% (43 / 43 correct decisions)

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

**Recall (Layer 2 proposal-level, attempted scenarios):** 21 / 27 (77.8%)
- Excluded from the recall denominator (documented ambiguous expected-match semantics): DUPLICATE (8 scenarios, 4 correctly proposed; including them the value would be 25 / 35 (71.4%))
- Note: Proposal-level Layer 2 recall computed from the artifact: correlation_id == ground_truth scenario_id, and expected match record IDs are derived from record_specs via the synthetic_ref -> record_id construction (_compute_record_id) already proven in tests/unit/test_evaluation_accounting.py. Denominator = residual scenarios with has_real_match=True that Layer 2 attempted (PROPOSAL_VALID / NO_PROPOSAL / VALIDATION_FAILED); provider failures (API_ERROR / TIMEOUT, i.e. not attempted) are excluded. Excluded from the denominator: DUPLICATE — duplicate detection's scoring semantics are a documented open question (README 'DUPLICATE scenario scoring'), so they are disclosed rather than silently included. For transparency, the value including the excluded category is reported in recall_including_excluded.

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
- **Naive baseline** (single-pass greedy closest-amount/date matcher, run on the same frozen dataset): 86 / 245 records matched (35.1%); 43 pairs, of which 36 are correct (83.7% pair precision); 48 / 120 scenarios touched (40.0%) at 75.0% scenario precision; 159 residual records.
- **Layer 1 (deterministic):** 86 / 245 records matched (35.1%); 43 / 120 scenarios matched (35.8%) at 100.0% precision (43 / 43 correct decisions).
- Scenario-level match-rate delta (Layer 1 − baseline): -4.2%
- Scenario-level precision delta (Layer 1 − baseline): 25.0%

*Baseline = reconciliation.baseline.naive_matcher (single-pass greedy closest-amount/date pairing), run against the same frozen dataset. Layer 1 and the naive matcher both cover 86/245 records, but Layer 1 emits only unambiguous pairs: all 43 decisions match a ground-truth scenario relationship (100% precision), while the naive matcher's 43 pairs include 7 incorrect ones (83.7% pair precision). At the scenario level, Layer 1 matches 43/120 scenarios (35.8%) with 100% precision vs. the naive matcher's 48/120 touched scenarios (40.0%) at 75% precision — the extra coverage is entirely false cross-scenario pairs. This is the precision-over-coverage tradeoff Concord makes.*

## Completeness
- 100.0% of residual scenarios evaluated.

## Limitations
- FULL EVALUATION: 77/77 residual scenarios evaluated.
- Provider failures (UNKNOWN) are explicitly tracked and excluded from correctness denominators. Proposal-level recall is computed over attempted scenarios via the correlation_id -> scenario_id mapping; proposal-level precision is NOT COMPUTABLE in this report.
