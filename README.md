# Concord — Intelligent Settlement Reconciliation

Concord is a precision-first settlement reconciliation engine that matches financial records across settlement, bank, and ledger sources. It prioritizes accuracy over coverage: a false match is worse than an unresolved record.

## Problem

Merchants and finance teams reconcile payments across multiple systems — settlement reports from payment gateways, bank credit statements, and internal ledger entries. These sources rarely agree perfectly on order IDs, amounts, or dates. Manual reconciliation is slow and error-prone; naive automated matching produces false positives that erode trust.

## Who Is This For?

Concord is designed for merchants, finance operations teams, and payment businesses that need to reconcile records across payment settlement reports, bank credits, and internal ledgers.

It is particularly useful when records cannot be reliably matched using a single identifier because transaction IDs, amounts, settlement structures, or dates differ across systems.

## Project Context

Concord was originally built for the Razorpay Buildathon. I am also using it as a capstone project for the AI Fluency program because it represents an end-to-end system where I applied AI-assisted development, evaluation, engineering judgment, and transparent communication about system limitations.

## Evaluation Summary

Concord was evaluated on a frozen synthetic dataset containing 120 scenarios and 245 records.

Key results from the current canonical evaluation:

- Layer 1 matched 86 of 245 records (35.1%) with 100% deterministic precision and zero false positives.
- All 77 residual Layer 2 scenarios were evaluated.
- Layer 2 produced 46 valid proposals, 16 no-proposal outcomes, and 15 genuine provider failures.
- At the scenario level, 15 proposals initially met the auto-accept confidence threshold, with 9 correct and 6 false accepts before production guardrails.
- Production Layer 3 guardrails reject unsafe cases using deterministic financial-evidence and duplicate protections.

See the detailed evaluation section below for methodology, metric definitions, known limitations, and incident analysis.

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

**Not implemented in the initial Day 1–2 deterministic MVP:**
- Narration-based or fuzzy text matching
- Partial refunds and ambiguous fee deductions
- Complex split / aggregated settlements
- AI-assisted Layer 2 matching (implemented in the subsequent Layer 2 pipeline)
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

## How It Works

1. Upload settlement, bank, and ledger CSV files.
2. Concord normalizes records into a canonical format.
3. Layer 1 attempts deterministic reconciliation using:
   - Exact identifier matching
   - Amount and configurable date-window matching
4. Ambiguous or unresolved records remain explicit residuals rather than being force-matched.
5. For the frozen evaluation dataset, pre-computed Layer 2 AI proposals are replayed through deterministic Layer 3 guardrails.
6. Records are routed into deterministic matches, AI auto-accepted matches, human review, or exceptions.
7. Users can inspect individual reconciliation decisions and their supporting context.

## Architecture

```
┌──────────────────────────────────────────────┐
│               Input Sources                  │
│                                              │
│  Settlement CSV   Bank CSV   Ledger CSV      │
└───────────────────────┬──────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────┐
│           Normalization Layer                │
│                                              │
│       Canonical NormalizedRecord             │
└───────────────────────┬──────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────┐
│        Layer 1: Deterministic Matching       │
│                                              │
│  • Exact ID                                  │
│  • Amount + Date Window                      │
│  • Ambiguity Protection                      │
└───────────────────────┬──────────────────────┘
                        │
             Matched / Residual
                        │
                        ▼
┌──────────────────────────────────────────────┐
│         Layer 2: AI Match Proposals          │
│                                              │
│  Candidate Retrieval → Structured Proposal   │
└───────────────────────┬──────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────┐
│        Layer 3: Deterministic Guardrails     │
│                                              │
│  • Confidence thresholds                     │
│  • Financial evidence                        │
│  • Duplicate protection                      │
└───────────────────────┬──────────────────────┘
                        │
                        ▼
       ┌────────────────────────────────┐
       │        Final Routing           │
       │                                │
       │  Deterministic Match           │
       │  AI Auto-Accepted              │
       │  Human Review                  │
       │  Exception                     │
       └────────────────────────────────┘
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

**Layer 2 evaluation status (final — 77/77):** all 77 residual Layer 2 scenarios have been evaluated against the frozen dataset (fingerprint `d91ead9a86a4d1dc949cf020a118957eeacb6eb4f18efb844c3c1b7afd0c6be0`). The canonical artifact (`data/layer2_clean_audit.jsonl`) contains 77/77 records: 46 `PROPOSAL_VALID`, 16 `NO_PROPOSAL`, and 15 `API_ERROR` (genuine provider failures preserved from Groq rate limiting, never converted into fabricated proposals). The run was completed with `scripts/resume_layer2.py`, which evaluated only the 32 previously-unattempted scenarios and preserved every existing record, then promoted the artifact to FINAL in `data/dataset_manifest.json`.

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

A simple amount+date matcher runs against the same frozen dataset to establish a comparison baseline. It uses the same tolerance (100 paise, 2-day window) but no ambiguity protection — single-pass greedy closest-amount/date pairing (smallest amount difference, then date difference, never reconsidering a matched record). This deliberately under-specifies the matching logic so Layer 1's deterministic precision can be measured against a minimal straw man, not against a competitor. The computed comparison (pairs, precision, and scenario coverage for both matchers) is in the headline table below.

### Layer 2 artifact replay

When the frozen evaluation dataset is uploaded through the API, Concord does **not** make a new LLM request. Instead, it loads pre-computed Layer 2 audit records from JSONL artifacts produced by prior Groq inference runs (documented in `data/layer2_clean_audit.jsonl` and historical `data/layer2_full_audit.*.jsonl` files). These stored model outputs are routed through the same Layer 3 deterministic guardrails used by the application. The `demo_mode: "artifact_replay"` field in the evaluation report makes this explicit. The `layer2_mode` field in API responses reads `"frozen_artifact"` when artifact replay is active, or `"not_executed"` for non-frozen uploads.

### Headline metrics (final — 77/77)

**These are the current, canonical numbers**, computed from `data/layer2_clean_audit.jsonl` (77/77 records, dataset fingerprint `d91ead9a86a4d1dc949cf020a118957eeacb6eb4f18efb844c3c1b7afd0c6be0`) by `scripts/build_current_report.py` → `data/current_evaluation_report.json`. Earlier artifacts (fingerprint `b8bf3feb…`) are superseded and retained for provenance only.

| Metric | Value |
|--------|-------|
| Total records | 245 (122 settlement + 81 bank + 42 ledger) |
| Layer 1 matched records | 86 of 245 (35.1%) — deterministic, 100% precision (43/43 correct decisions), zero false positives |
| Residual scenarios (Layer 2) | 77 of 77 evaluated (100% completeness) |
| Layer 2 outcomes | 46 PROPOSAL_VALID, 16 NO_PROPOSAL, 15 API_ERROR |
| Outcome-state classification | 26 CORRECT, 36 INCORRECT, 15 UNKNOWN (provider failures) |
| Known outcome rate | 62/77 (80.5%) — provider failures never counted as TP/FP |
| AI_AUTO_ACCEPTED | 15 — scenario-level; Layer 2 proposals with confidence ≥ 0.90, auto-accepted (canonical artifact) |
| HUMAN_REVIEW | 30 — scenario-level; Layer 2 proposals with confidence 0.60–0.89, queued for review (canonical artifact) |
| EXCEPTION | 32 — scenario-level; NO_PROPOSAL / API_ERROR / low-confidence outcomes (canonical artifact) |
| False accept rate | 6 / 15 known auto-accepted (40%) — see SPLIT_SETTLEMENT scoring below |
| Outcome-level precision ≥ 0.90 | 60.0% (9 TP / 6 FP) |
| Outcome-level precision ≥ 0.75 | 37.0% (10 TP / 17 FP) |
| Outcome-level precision ≥ 0.60 | 22.2% (10 TP / 35 FP) |
| Recall | 77.8% (21/27) — proposal-level over attempted scenarios; DUPLICATE excluded and disclosed (including them: 71.4% = 25/35) — see AI-recall methodology below |
| Layer 1-only baseline | 35.1% match rate (86/245) at 100% deterministic precision (43/43 correct decisions) |
| Naive baseline | 35.1% record match rate (86/245); 43 pairs, 36 correct (83.7% pair precision); touches 48/120 scenarios (40.0%) at 75.0% scenario precision |

**Granularity note.** The `AI_AUTO_ACCEPTED` / `HUMAN_REVIEW` / `EXCEPTION` rows above are **scenario-level** counts from the canonical artifact (one bucket per evaluated scenario; `DETERMINISTIC_MATCH` is 0 at this level because Layer 1 is an earlier, separate stage). At the **record level**, a live upload of the frozen dataset through the API routes all 245 records (`/batches/{id}/eval` → `layer3_routing_composition`): DETERMINISTIC_MATCH 86, AI_AUTO_ACCEPTED 24, HUMAN_REVIEW 48, EXCEPTION 87. Both sets are current — scenario-level from `data/current_evaluation_report.json` (`routing_buckets`), record-level from the live pipeline — and they are not interchangeable.

**False-accept accounting (corrected).** Of the 15 auto-accepted Layer 2 outcomes, 9 are SPLIT_SETTLEMENT scenarios whose proposals were verified against ground truth and are scored CORRECT (see [SPLIT_SETTLEMENT scenario scoring](#split_settlement-scenario-scoring)); the 6 false accepts are 2 DUPLICATE (`DUP-002`, `DUP-004`), 3 LATE_ARRIVING (`LATE-005`, `LATE-008`, `LATE-010`), and 1 PARTIAL_REFUND (`REFD-010`). At the record level, the production pipeline additionally runs every proposal through deterministic Layer 3 guardrails — the same-source-duplicate guardrail rejects the DUP proposals and the financial-evidence guardrail rejects the LATE/REFD proposals — so no false accept reaches `AI_AUTO_ACCEPTED` in production — a live upload of the frozen dataset auto-accepts exactly 24 records, all SPLIT_SETTLEMENT (record-level; see the granularity note above).

The Layer 1 deterministic matcher trades coverage for precision: it never produces a false positive. The canonical Layer 2 artifact contains 46 genuine Groq proposals from all 77 residual scenarios. After deterministic deduplication in the loading code, these PROPOSAL_VALID outcomes route records to AI buckets via Layer 3 guardrails. The 15 API_ERROR and 16 NO_PROPOSAL residuals route to EXCEPTION — these represent genuine provider failures and correctly-refused matches, not a workload backlog.

### AI-recall methodology

Recall is reported as **proposal-level Layer 2 recall over attempted scenarios**, computed from the canonical artifact (`data/layer2_clean_audit.jsonl`). The artifact's `correlation_id` maps directly to the ground-truth `scenario_id`, and expected match record IDs are derived from each scenario's `record_specs` via the `synthetic_ref → record_id` construction (`_compute_record_id`) proven in `tests/unit/test_evaluation_accounting.py`. The denominator is the set of residual scenarios with `has_real_match: true` that Layer 2 actually attempted (outcomes `PROPOSAL_VALID`, `NO_PROPOSAL`, `VALIDATION_FAILED`).

We distinguish what we could and couldn't measure the same way the pipeline does operationally: provider failures (`API_ERROR` / `TIMEOUT`) are excluded because Layer 2 never ran on them — "we tried and got it wrong" is counted against recall, while "we couldn't try" is tracked separately (`layer2.provider_failure`) and never counted as correct or incorrect. `DUPLICATE` scenarios are excluded from the denominator because duplicate detection's scoring semantics are a documented open question (see [DUPLICATE scenario scoring](#duplicate-scenario-scoring)); they are disclosed rather than silently included, and the value including them is reported for transparency. The current result is **21 / 27 = 77.8%**; including the 8 excluded DUPLICATE scenarios (4 of which were correctly proposed) it would be **25 / 35 = 71.4%**. Proposal-level precision is NOT COMPUTABLE in this report — only proposal-level recall is computed.

### DUPLICATE scenario scoring

DUPLICATE scenarios contain two same-source settlement records (e.g. duplicate settlement reports). Layer 2 proposing that these two records match each other is currently scored as a correct outcome. This is distinct from cross-source reconciliation (e.g. settlement-to-bank matching). The expected match IDs for DUPLICATE scenarios are the settlement-only records, not all member records. This behavior is intentional for the current evaluation — it treats duplicate detection as a valid Layer 2 capability alongside cross-source matching. Whether this should remain scored as correct or be separated into its own metric is a known open question.

### SPLIT_SETTLEMENT scenario scoring

`expected_outcome: NO_MATCH` in this dataset means **"Layer 1's simple ID/amount matching cannot resolve this scenario"** — it does not mean "no correspondence exists". SPLIT_SETTLEMENT scenarios (`SPLT-001`…`SPLT-010`) describe one settlement whose amount exactly equals the sum of two bank credits on matching dates; every one carries `has_real_match: true` in `data/ground_truth.json`. A Layer 2 proposal of the correct three-record structure (1 settlement + 2 bank credits, composition-verified against the ground-truth `record_specs`) is a correct, evidence-backed match — precisely the multi-record financial-evidence case `_check_financial_evidence` in `reconciliation/layer3.py` exists to validate.

The report generator (`scripts/build_current_report.py`) initially applied a blanket rule: any `expected_outcome: NO_MATCH` scenario receiving `PROPOSAL_VALID` was scored INCORRECT and counted as a false accept. That misclassified all 9 auto-accepted SPLIT_SETTLEMENT proposals as false accepts (a reported false accept rate of 15/15 = 100%). This was caught during pre-submission self-verification and corrected: SPLIT_SETTLEMENT proposals whose structure matches the scenario's real relationship, with `has_real_match: true`, are now scored CORRECT. The corrected report shows 26 CORRECT / 36 INCORRECT / 15 UNKNOWN and a false accept rate of 6/15 (40%). The exception is deliberately scoped to SPLIT_SETTLEMENT only — no other category receives it (see the DUPLICATE open question above), and no confidence threshold, tolerance, or guardrail was changed. The correction is pinned by regression tests in `tests/unit/test_evaluation_accounting.py` (`TestSplitSettlementScoring`).

## Known Incidents

### LATE-008: false accept on high-confidence proposal with weak financial evidence

Scenario LATE-008 pairs a settlement record with a bank record that share the same amount (867000 paise) but have different order IDs and a 21-day date gap. A Groq proposal for this pair received confidence ≥ 0.90, which would have routed both records to `AI_AUTO_ACCEPTED` — a false accept. The root cause was that Layer 3's auto-accept gate only checked confidence threshold and same-source duplicate guardrails, without verifying that the proposal had sufficient financial evidence (matching amounts AND dates within a reasonable window). The fix added `_check_financial_evidence` (in `reconciliation/layer3.py`, line 86), which rejects auto-acceptance when date gap exceeds a configurable threshold even if the LLM assigns high confidence. The regression test `TestLate008Regression` (in `tests/unit/test_layer3_routing.py`, line 248) pins this: it constructs the exact LATE-008 record pair, feeds a confidence-0.90 proposal through `route()`, and asserts neither record lands in `AI_AUTO_ACCEPTED`.

### Same-source duplicate overinclusion in Layer 2 proposals

Layer 2 proposals sometimes included extra records beyond the minimal matching pair — for example, proposing that three same-source settlement records match each other when only two are warranted. This overinclusion could cause correct two-record matches to fail validation or produce false positives. The fix added `_check_same_source_duplicate_overinclusion` (in `reconciliation/proposal_validation.py`, line 64), which rejects proposals that propose matching more than two same-source records when a tighter two-record match exists. This guardrail is exercised by the existing test suite and by the guardrail-impact comparison in `scripts/run_full_clean_eval.py` (line 741), which quantifies how many proposals the guardrail diverts from `AI_AUTO_ACCEPTED` to `EXCEPTION`.

### TPM vs TPD rate-limit misclassification

During the Layer 2 evaluation, Groq returned 429 errors for both tokens-per-minute (TPM) and tokens-per-day (TPD) rate limits. The original error classifier in `reconciliation/groq_provider.py` only checked the `type` field of the error body (e.g. `"tokens"`), which is the same for both TPM and TPD errors. This caused TPD daily-quota exhaustion to be classified as `"transient"` instead of `"quota_exhausted"`, triggering futile retries that wasted API budget. The fix (`_classify_api_error` at `reconciliation/groq_provider.py`, line 64) now checks both the `type` and `code` fields, and inspects the error message for daily-quota markers ("per day", "TPD", "tokens per day") via `_has_daily_quota_marker`. When a `rate_limit_exceeded` code is paired with a daily-quota marker, the error is classified as `"quota_exhausted"` and retries are suppressed. The tests `test_tpd_rate_limit_exceeded_classified_as_quota_exhausted` and `test_tpd_rate_limit_exceeded_not_unknown` (in `tests/unit/test_groq_provider.py`, lines 428 and 448) verify this behavior.

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

### Running Locally

Start the FastAPI backend from the **repository root**:

```bash
uvicorn reconciliation.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

The `--factory` flag is required because `reconciliation/api/app.py` exposes `create_app()` (an application factory), not a module-level `app` object. Run the command from the repository root so that the default `data_dir` (the parent of the default `data/concord.db`, i.e. `data/`) resolves to the folder containing the frozen dataset manifest (`data/dataset_manifest.json`) and the canonical Layer 2 audit artifact (`data/layer2_clean_audit.jsonl`). Starting from any other directory would point `data_dir` at the wrong folder, and frozen-dataset fingerprint verification (and artifact replay) would not engage.

The built frontend (from `npm run build` inside `frontend/`) is served from `/` by the same process when `frontend/dist` exists; without a build the backend serves the API only (the Vite source tree is not servable). A quick end-to-end smoke test:

```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/batches \
  -F "settlement=@data/settlements.csv" \
  -F "bank=@data/bank.csv" \
  -F "ledger=@data/ledger.csv"
# then GET /batches/{batch_id}/eval for the routing composition
```

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

### Deployment: Vercel (frontend) + Render (backend)

The intended production topology is a Vite/React frontend on Vercel talking to the FastAPI backend on Render.

**Render (backend).**

- **Root directory:** repository root.
- **Build command:** `pip install -e ".[api]"`
- **Start command:** `uvicorn reconciliation.api.app:create_app --factory --host 0.0.0.0 --port $PORT`
  - The `--factory` flag is required: `reconciliation/api/app.py` exposes `create_app()`, not a module-level `app`.
  - Start from the repository root so the default `data/` directory resolves to the frozen dataset manifest and canonical Layer 2 artifact that the API ships with (`data/dataset_manifest.json`, `data/layer2_clean_audit.jsonl`, the three CSVs, `residuals.csv`, `ground_truth.json` are all tracked in git).
  - The SQLite database is created at `data/concord.db` at runtime. Render's filesystem is writable, so uploads persist for the life of the instance (ephemeral across restarts on free plans — consistent with the SQLite limitations documented above and fine for the demo).
  - No frontend build is required on Render; when `frontend/dist` is absent the backend serves the API only.
- **Environment variables:**
  - `GROQ_API_KEY` — required only for running the Layer 2 Groq inference scripts locally. The API itself never calls the LLM (it replays frozen artifacts for the evaluation dataset and reports `layer2_mode: not_executed` for arbitrary uploads), so the deployed API works without it.
  - `CONCORD_CORS_ORIGINS` — comma-separated list of origins allowed to call the API, e.g. `https://concord-demo.vercel.app`. When unset the code defaults to `*` (permissive, fine for the demo); set it to the exact Vercel origin for a stricter deployment.
- **Health check:** `GET /health` returns 200 when the process is alive. Interactive docs at `GET /docs`.

**Vercel (frontend).**

- **Root directory:** `frontend`
- **Framework preset:** Vite
- **Build command:** `npm run build`
- **Output directory:** `dist`
- **Environment variables:**
  - `VITE_API_BASE_URL` — must be set to the Render backend URL, e.g. `https://concord-api.onrender.com`. In production the app reads only this variable; the `http://127.0.0.1:8000` fallback in `frontend/src/api/concord.ts` is for local development only. Never prefix a secret with `VITE_`.
- **SPA routing:** `frontend/vercel.json` rewrites unmatched paths to `/index.html` so deep links such as `/app/queue` and `/app/records/{id}` work on refresh.
- The demo-data button on the Upload page fetches the frozen evaluation dataset from `/fixtures/*.csv`, so one click exercises the full path: upload → fingerprint match → Layer 2 artifact replay → Layer 3 guardrails.

## AI Transparency

AI tools, including Claude, were used as development and thinking assistants during the project.

AI assisted with exploring implementation approaches, reviewing architecture and code, debugging, improving documentation, and challenging design decisions. I remained responsible for the final engineering decisions and verification of the system.

I personally reviewed and tested the implementation, validated evaluation results against the frozen dataset and canonical artifacts, investigated false accepts and provider failures, and decided what functionality and limitations were safe to claim publicly.

The metrics and limitations documented in this repository are based on the project's evaluation artifacts and tests. AI assistance was used to accelerate development and reasoning, not as a substitute for verification.

## Data Retention

Audit artifacts follow a clear lifecycle:

- **Final canonical artifact** (`data/layer2_clean_audit.jsonl`): committed to git. The 77/77 evaluation is complete — the manifest records status `final` under both `canonical_layer2_artifact` and `final_canonical_artifact`, with the artifact's SHA-256 verified at load time and by `scripts/build_current_report.py` before any metric is computed. It is the authoritative source for artifact replay through the API and for `--resume`/verification operations.
- **Historical full-run artifacts** (`data/layer2_full_audit.*.jsonl` and timestamped clean-audit archives): gitignored (pattern: `data/layer2_full_audit.*.jsonl`). Committed in `c44d6da` under the old fingerprint `8dca28fe…` and superseded when the dataset fingerprint changed; the timestamped archives are the only record of intermediate or failed runs. Policy: keep all of them — they are small and infrequent.
- **Historical artifacts** (`data/layer2_full_audit.20260829T181300.053055.jsonl`, `data/layer2_full_audit.legacy.jsonl`, and 9 other timestamped files): gitignored (pattern: `data/layer2_full_audit.*.jsonl`). These are the only record of intermediate or failed runs. Policy: keep all of them. They are small (47-78K each) and infrequent (one per run). If disk usage becomes a concern, delete the oldest archives manually — there is no automated cleanup.
- **Historical evaluation report** (`data/day5_full_pipeline_report.json`, `.md`): committed to git. Generated on 2026-08-30 by the evaluation harness from a previous artifact version. Retained for historical reference only.
- **Dataset manifest** (`data/dataset_manifest.json`): committed to git. Frozen fingerprint of the evaluation dataset.
- **SQLite database** (`data/concord.db`): gitignored. Created by the API's `BatchStore`. Ephemeral — not backed up.

To inspect run history: `ls -lt data/layer2_full_audit.*.jsonl` shows archived runs in reverse chronological order. Each filename contains the ISO-8601 timestamp of when the run completed.
