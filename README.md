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
