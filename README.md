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
