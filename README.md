# Concord — Intelligent Settlement Reconciliation

Concord is a precision-first settlement reconciliation engine that matches financial records across settlement, bank, and ledger sources. It prioritizes accuracy over coverage: a false match is worse than an unresolved record.

## Problem

Merchants and finance teams reconcile payments across multiple systems — settlement reports from payment gateways, bank credit statements, and internal ledger entries. These sources rarely agree perfectly on order IDs, amounts, or dates. Manual reconciliation is slow and error-prone; naive automated matching produces false positives that erode trust.

## Current MVP Capabilities

**Implemented (Day 1–2):**
- Deterministic normalization for Settlement, Bank, and Ledger records
- Canonical `NormalizedRecord` model with immutable, frozen dataclasses
- Integer paise representation for exact money arithmetic (no floats)
- Layer 1 deterministic matcher with two rules:
  - Exact identifier matching (highest priority)
  - Amount + configurable date-window matching
- Ambiguity protection — records with multiple candidates remain unresolved
- Same-source records are never automatically matched
- Explicit residual/unresolved records (nothing disappears silently)
- Duplicate `record_id` detection with fast failure
- Deterministic, input-order-independent outputs

**Intentionally not implemented:**
- Narration-based or fuzzy text matching
- Partial refunds and ambiguous fee deductions
- Complex split / aggregated settlements
- AI-assisted Layer 2 matching
- Symmetric date-window tolerance already resolves standard T+1/T+2 delay cases; directional, source-aware settlement-timing-offset reasoning is deferred.

**Implemented (Day 3): Synthetic evaluation pipeline**
- Synthetic dataset generator producing settlement, bank, and ledger CSVs from ground-truth scenarios with controlled category quotas (exact-id, amount-date, duplicate, fee, partial, split, delayed, orphan)
- Ground-truth scenario model annotated with category, true relationship flag, and exception flag
- Evaluation harness running the Layer 1 matcher against the synthetic dataset
- Metrics computation (overall and per-category coverage, precision, recall) with TP/FP/TN/FN tracking
- Leakage guard ensuring no evaluation-only fields reach the production matcher
- Residual scenario identification surfacing unmatched Layer 1 cases for downstream Layer 2 processing
- Evaluation report persisted to `data/evaluation_report.json`
- `scripts/run_day3.py` driver script orchestrating generation, evaluation, and reporting

**Implemented (Day 4): Layer 2 reconciliation pipeline**
- Residual scenario reconstruction — rebuilds unresolved cases from `residuals.csv` into typed `Layer2Case` objects; evaluation-only metadata (category, has_valid_relationship, is_true_exception, synthetic descriptions) is excluded from the production context
- Deterministic candidate retrieval — ranks plausible candidate records using transparent, explainable signals (date proximity, amount proximity, order-ID matching, cross-source compatibility) with configurable weights and ascending-score ranking
- Groq structured-output provider boundary — abstract `StructuredCompletionProvider` with secret-redacting diagnostics (`safe_diagnostic`); API key resolved from environment only, never hardcoded or logged
- Prompt boundary — builds minimal LLM payloads from production record fields only; explicitly excludes ground-truth labels, scenario category, raw payloads, and category-encoded scenario IDs
- Typed match proposal contract (`MatchProposal`) — structured JSON response with `proposed_match_ids`, `confidence`, and `rationale`, validated via pydantic and Groq JSON-schema response format
- Deterministic semantic validation — validates proposed IDs against the present record set; rejects duplicates and unknown IDs without repair; fully Groq-free and deterministic
- Safe outcome taxonomy with five explicit outcomes:
  - `PROPOSAL_VALID` — proposal passed validation against the presented record set
  - `NO_PROPOSAL` — model returned no proposed match IDs (empty list, not an error)
  - `VALIDATION_FAILED` — proposal references unknown or duplicate record IDs
  - `API_ERROR` — provider returned an API or connection error
  - `TIMEOUT` — provider request timed out
- Orchestration boundary — maps provider exceptions to outcomes; no automatic retries; never invents or substitutes matches; secrets never surfaced in outcome reasons
- Append-only JSONL audit trail — per-scenario audit records with neutral internal correlation IDs replacing category-encoded scenario IDs; persistence failures reported without mutating outcomes
- End-to-end runner with configurable sample limiting to control API usage; orchestrator injected for mockable testing with no API-key dependency
- Diagnostic scripts (`scripts/diagnose_day4.py`, `scripts/diagnose_day4_all.py`) for local inspection of reconstruction and retrieval without LLM calls

## Architecture

```
Raw source rows
    ↓ normalize_record()
NormalizedRecord (canonical domain model)
    ↓ reconcile()
ReconciliationResult
    ├── decisions: Tuple[ReconciliationDecision, ...]
    └── residual_record_ids: Tuple[str, ...]
```

Evaluation and ground-truth concepts are architecturally separated from the matching engine to prevent signal leakage.

## Safety Philosophy

> Precision over coverage. A false match is worse than an unresolved record.

The engine enforces:
- No ambiguous candidate is force-matched
- No record participates in multiple decisions
- All unmatched records are explicitly surfaced as residuals
- Money amounts are handled as integer paise with strict precision rules

## Getting Started

```bash
# Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"

# Run the test suite
pytest
```

## Testing

The repository includes unit tests, invariant tests, and acceptance tests covering:
- Normalization edge cases (amounts, dates, missing fields)
- Exact identifier matching and ambiguity handling
- Amount + date window matching with tolerance
- Core reconciliation invariants (no duplicates, no silent drops, permutation invariance)
- Nine documented acceptance scenarios from `docs/reconciliation_scenarios.md`

## Day 5 — Evaluation & Guardrails

**Layer 2 evaluation status (as of 2026-09-02):** 36 of 77 Layer 2 scenarios completed successfully, 9 failed on Groq rate limits (TPM/TPD exhaustion), and 32 have not yet been attempted. The canonical artifact (`data/layer2_clean_audit.jsonl`) covers 45/77 scenarios (36 completed, 9 API_ERROR). A full 77/77 evaluation has not been completed.

Concord evaluates the full pipeline against a synthetic dataset of 120 scenarios (245 records) with controlled category quotas: exact-id matches, amount-date matches, duplicates, fee deductions, partial refunds, split settlements, rounding differences, inconsistent narrations, true orphans, and late-arriving records. The dataset is frozen on disk with a SHA-256 fingerprint; every evaluation run verifies the on-disk files match the frozen manifest before computing metrics.

### Layer 3 routing buckets

Every record is assigned exactly one of four routing decisions:

| Bucket | Meaning |
|--------|---------|
| `DETERMINISTIC_MATCH` | Layer 1 matched this record via exact ID or amount+date rules |
| `AI_AUTO_ACCEPTED` | Layer 2 proposed a match with confidence ≥ 0.90; auto-accepted |
| `HUMAN_REVIEW` | Layer 2 proposed a match with confidence 0.60–0.89; queued for human review |
| `EXCEPTION` | No valid Layer 2 outcome, low confidence, or provider failure |

### Naive baseline

A simple amount+date matcher runs against the same dataset to establish a comparison baseline. It uses the same tolerance (100 paise, 2-day window) but no ambiguity protection — it accepts the first candidate it finds. This deliberately under-specifies the matching logic so Layer 1's deterministic precision can be measured against a minimal straw man, not against a competitor.

### Layer 2 artifact replay

When the frozen evaluation dataset is uploaded through the API, Concord does **not** make a new LLM request. Instead, it loads pre-computed Layer 2 audit records from JSONL artifacts produced by prior Groq inference runs (documented in `data/layer2_clean_audit.jsonl` and historical `data/layer2_full_audit.*.jsonl` files). These stored model outputs are routed through the same Layer 3 deterministic guardrails used by the application. The `demo_mode: "artifact_replay"` field in the evaluation report makes this explicit. The `layer2_mode` field in API responses reads `"frozen_artifact"` when artifact replay is active, or `"not_executed"` for non-frozen uploads.

### Headline metrics (historical — stale artifact)

**⚠ The numbers below are HISTORICAL and no longer reflect the current dataset.** They were computed from `layer2_full_audit.20260829T181300.053055.jsonl` (fingerprint `8dca28fe7e5c5065…`), which was generated against a **previous** dataset fingerprint. The current dataset fingerprint is `b8bf3feb57ffcb23c…` (see `data/dataset_manifest.json`). These numbers are retained for historical reference only; they should not be cited as current system performance.

| Metric | Historical Value |
|--------|------------------|
| Total records | 245 (122 settlement + 81 bank + 42 ledger) |
| Layer 1 matched records | 86 of 245 (35.1%) — 43 decisions covering 120 scenarios |
| Layer 1 precision | 100.00% (43/43 correct, deterministic) |
| AI_AUTO_ACCEPTED | 11 — Layer 2 proposals with confidence ≥ 0.90, auto-accepted |
| HUMAN_REVIEW | 18 — Layer 2 proposals with confidence 0.60–0.89, queued for review |
| EXCEPTION | 130 — AI_RESPONSE_INVALID (91), NO_CANDIDATE (39) |
| AI proposals routed | 29 records from 24 PROPOSAL_VALID outcomes (out of 27 in artifact; 3 lost to scenario dedup) |
| Baseline match rate | 40.00% (48/120) |
| Baseline precision | 75.00% (36/48 correct) |
| AI precision / recall | See `data/day5_full_pipeline_report.json` (historical; not recomputed for canonical artifact) |

**Current partial numbers** (from `layer2_clean_audit.jsonl`, 45/77 scenarios, fingerprint `b8bf3feb…`): 26 PROPOSAL_VALID, 10 NO_PROPOSAL, 9 API_ERROR. The evaluation is incomplete — 32 scenarios have not been attempted and 9 failed on Groq rate limits. No headline-metrics table can be computed until the full 77/77 evaluation completes.

The Layer 1 deterministic matcher trades coverage for precision: it matches fewer records than the baseline (35.1% vs 40.00%) but never produces a false positive. The current canonical Layer 2 artifact contains 26 genuine Groq proposals (from 45 scenarios evaluated out of 77 residual scenarios). After deterministic deduplication in the loading code, these PROPOSAL_VALID outcomes route records to AI buckets via Layer 3 guardrails. The remaining residuals route to EXCEPTION via API_ERROR or NO_PROPOSAL outcomes — these represent the workload that a fully evaluated Layer 2 would address.

### AI-recall methodology

AI recall is reported as two numbers — **system-wide** and **attempted-only** — because in a financial reconciliation system "we didn't try" and "we tried and got it wrong" are different failure modes with different fixes. System-wide recall counts every residual scenario with a real match in the denominator, including provider failures (`API_ERROR`); under a total Layer 2 outage it reports 0%, not N/A, so the operational safety metric never goes silent during the worst case. Attempted-only recall excludes provider failures and measures model quality when Layer 2 actually ran. Both numbers appear in every evaluation report. A system-wide recall of 0% with an attempted-only recall of 80% would tell you the model is decent but the infrastructure is down; a system-wide recall of 80% with an attempted-only recall of 80% would tell you the model runs reliably. Neither number alone tells the full story.

### DUPLICATE scenario scoring

DUPLICATE scenarios contain two same-source settlement records (e.g. duplicate settlement reports). Layer 2 proposing that these two records match each other is currently scored as a correct outcome. This is distinct from cross-source reconciliation (e.g. settlement-to-bank matching). The expected match IDs for DUPLICATE scenarios are the settlement-only records, not all member records. This behavior is intentional for the current evaluation — it treats duplicate detection as a valid Layer 2 capability alongside cross-source matching. Whether this should remain scored as correct or be separated into its own metric is a known open question.

## Known Incidents

### LATE-008: false accept on high-confidence proposal with weak financial evidence

Scenario LATE-008 pairs a settlement record with a bank record that share the same amount (867000 paise) but have different order IDs and a 21-day date gap. A Groq proposal for this pair received confidence ≥ 0.90, which would have routed both records to `AI_AUTO_ACCEPTED` — a false accept. The root cause was that Layer 3's auto-accept gate only checked confidence threshold and same-source duplicate guardrails, without verifying that the proposal had sufficient financial evidence (matching amounts AND dates within a reasonable window). The fix added `_check_financial_evidence` (in `reconciliation/layer3.py`, line 85), which rejects auto-acceptance when date gap exceeds a configurable threshold even if the LLM assigns high confidence. The regression test `TestLate008Regression` (in `tests/unit/test_layer3_routing.py`, line 248) pins this: it constructs the exact LATE-008 record pair, feeds a confidence-0.90 proposal through `route()`, and asserts neither record lands in `AI_AUTO_ACCEPTED`.

### Same-source duplicate overinclusion in Layer 2 proposals

Layer 2 proposals sometimes included extra records beyond the minimal matching pair — for example, proposing that three same-source settlement records match each other when only two are warranted. This overinclusion could cause correct two-record matches to fail validation or produce false positives. The fix added `_check_same_source_duplicate_overinclusion` (in `reconciliation/proposal_validation.py`, line 64), which rejects proposals that propose matching more than two same-source records when a tighter two-record match exists. This guardrail is exercised by the existing test suite and by the guardrail-impact comparison in `scripts/run_full_clean_eval.py` (line 741), which quantifies how many proposals the guardrail diverts from `AI_AUTO_ACCEPTED` to `EXCEPTION`.

### TPM vs TPD rate-limit misclassification

During the Layer 2 evaluation, Groq returned 429 errors for both tokens-per-minute (TPM) and tokens-per-day (TPD) rate limits. The original error classifier in `reconciliation/groq_provider.py` only checked the `type` field of the error body (e.g. `"tokens"`), which is the same for both TPM and TPD errors. This caused TPD daily-quota exhaustion to be classified as `"transient"` instead of `"quota_exhausted"`, triggering futile retries that wasted API budget. The fix (`_classify_api_error` at `reconciliation/groq_provider.py`, line 65) now checks both the `type` and `code` fields, and inspects the error message for daily-quota markers ("per day", "TPD", "tokens per day") via `_has_daily_quota_marker`. When a `rate_limit_exceeded` code is paired with a daily-quota marker, the error is classified as `"quota_exhausted"` and retries are suppressed. The tests `test_tpd_rate_limit_exceeded_classified_as_quota_exhausted` and `test_tpd_rate_limit_exceeded_not_unknown` (in `tests/unit/test_groq_provider.py`, lines 428 and 448) verify this behavior.

## Day 6 — API

The HTTP API exposes five batch endpoints and a liveness probe:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/batches` | Accept three CSV files (settlement, bank, ledger), run the full pipeline, return a batch ID |
| `GET` | `/batches/{batch_id}/status` | Processing state, record count, routing composition, Layer 2 mode |
| `GET` | `/batches/{batch_id}/results` | Routing decisions, optionally filtered by bucket (`?bucket=EXCEPTION`) |
| `GET` | `/batches/{batch_id}/eval` | Evaluation report computed for this batch |
| `GET` | `/batches/{batch_id}/records/{record_id}` | Full decision context for one record (source data, Layer 1/Layer 2 info, audit trail) |
| `GET` | `/health` | Liveness probe (returns 200 when the process is alive) |

### Layer 2 / API gap

The API does not execute Layer 2 from arbitrary uploaded batches. Layer 2 is evaluated against the synthetic residual set using a frozen dataset with provenance-verified artifacts, but it is not safely wired to arbitrary uploaded CSVs. The reason is a scenario-disjointness issue found during development: the evaluation harness assumes each residual scenario is independent and processes them in isolation, but arbitrary uploaded batches may contain inter-record relationships that the current retrieval and proposal logic does not model. Wiring Layer 2 to arbitrary uploads without addressing this would produce proposals against an incomplete understanding of the record universe. The API records `layer2_mode: not_executed` in all batch responses so consumers never mistake it for real AI processing.

## Persistence & Deployment

### Why SQLite

Concord uses SQLite for batch persistence. This is a deliberate engineering choice for the current project stage:

- **Zero operational overhead.** No database server to install, configure, or monitor. The persistence layer is a single file (`data/concord.db`, gitignored).
- **Right-sized for single-instance use.** Local development, hackathon demos, and evaluation pipelines run one API process. SQLite handles this workload without unnecessary infrastructure.
- **Clean interface.** `BatchStore` encapsulates all persistence behind a narrow API. Swapping the backing database later is a localized change, not a rewrite.

### Current Limitations

These are real constraints of the current implementation, not hypothetical concerns:

**Single-writer concurrency.** SQLite serializes writes at the file level. `BatchStore` holds one connection and commits after every write operation. Concurrent batch submissions are safe but serialize on the write lock — they will not run in parallel.

**Single-instance only.** The database is a local file. The current implementation is not designed or validated for multi-instance deployment — concurrent access from multiple processes is not tested, and the lack of WAL mode or retry logic means write contention under concurrent load is unhandled.

**No WAL mode.** The default journal mode is used. WAL would improve concurrent read throughput and is a straightforward future optimization, but is not configured today.

**One connection, no pooling.** `BatchStore` creates a single `sqlite3.connect()` at startup with `check_same_thread=False`. There is no connection pool, no automatic reconnection, and no retry logic. If the connection drops, requests fail until the process restarts.

**No automated backups.** The `.db` file is the only durability mechanism. `BatchStore` commits every write immediately, so committed data persists reliably under normal operation — but there is no backup or recovery strategy. If the file is deleted, corrupted, or the disk fails, data is unrecoverable. For hackathon and demo use this is acceptable; for workflows with durability requirements, periodic snapshots or a migration to a server-backed database is needed.

**No migration framework.** Schema is created via `CREATE TABLE IF NOT EXISTS` at startup. Adding columns or tables in future versions will require manual migration scripts or a tool like Alembic.

### Production Scaling Path

When Concord needs concurrent users, multi-instance deployment, or stronger durability, the following changes would close the gap — none require architectural redesign:

1. **Swap SQLite for a client-server database** (e.g., PostgreSQL). This resolves single-instance, concurrency, and durability limitations in one step.
2. **Add connection pooling.** Replace the single-connection `BatchStore` with a pooled connection manager.
3. **Introduce a migration framework** (e.g., Alembic) to manage schema changes across environments.
4. **Add a `/ready` endpoint** that verifies database connectivity, complementing the existing `/health` liveness probe.
5. **Containerize.** A Dockerfile and orchestration config for consistent deployment.

The `BatchStore` interface is intentionally narrow — these changes are engineering work, not a rethink.

### Deployment Status

| Capability | Status |
|-----------|--------|
| Local development | Fully supported |
| Single-instance deployment | Fully supported |
| Concurrent batch submissions | Safe, serialized |
| Multi-instance deployment | Not supported |
| Database backups | Manual |
| Schema migrations | Not supported |

## Data Retention

Audit artifacts follow a clear lifecycle:

- **Active resumable partial artifact** (`data/layer2_clean_audit.jsonl`): committed to git. The current in-progress evaluation state produced by a partial Groq run (45 of 77 scenarios evaluated as of 2026-09-02). It is the authoritative source for `--resume` operations and is wired as the `canonical_layer2_artifact` in the dataset manifest. Once all 77 scenarios are evaluated and the artifact is finalized, it becomes the **FINAL CANONICAL ARTIFACT**.
- **Final canonical artifact** (`data/layer2_full_audit.jsonl`): committed to git as a 1-record stub (old fingerprint `8dca28fe…`). Does not currently exist on disk. When `scripts/run_layer2_full.py` completes a full 77/77 run, the output is promoted atomically from the `.tmp` sidecar to this path. Its SHA-256 hash is verified at load time.
- **Historical artifacts** (`data/layer2_full_audit.20260829T181300.053055.jsonl`, `data/layer2_full_audit.legacy.jsonl`, and 9 other timestamped files): gitignored (pattern: `data/layer2_full_audit.*.jsonl`). These are the only record of intermediate or failed runs. Policy: keep all of them. They are small (47-78K each) and infrequent (one per run). If disk usage becomes a concern, delete the oldest archives manually — there is no automated cleanup.
- **Historical evaluation report** (`data/day5_full_pipeline_report.json`, `.md`): committed to git. Generated on 2026-08-30 by the evaluation harness from a previous artifact version. Retained for historical reference only.
- **Dataset manifest** (`data/dataset_manifest.json`): committed to git. Frozen fingerprint of the evaluation dataset.
- **SQLite database** (`data/concord.db`): gitignored. Created by the API's `BatchStore`. Ephemeral — not backed up.

To inspect run history: `ls -lt data/layer2_full_audit.*.jsonl` shows archived runs in reverse chronological order. Each filename contains the ISO-8601 timestamp of when the run completed.
