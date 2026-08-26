# Reconciliation Contract — Concord MVP

This document defines the behavioral contract for the deterministic reconciliation engine in Concord — Intelligent Settlement Reconciliation. It is an engineering contract, not a specification for implementation.

## 1. Core Safety Principle

Concord prioritizes precision over coverage. A false match is strictly worse than an unresolved record. The engine must automatically reconcile only when deterministic evidence is sufficiently strong and unambiguous. Ambiguous records must remain explicitly unresolved for later review or AI-assisted processing. Unresolved records must never disappear silently.

## 2. Day 2 Supported Automatic Matches

The deterministic MVP supports the following automatic match types:

### Exact Shared Identifier
- **Evidence requirement:** Two or more records share the same order or transaction identifier (e.g., `order_id_hint`).
- **Behavior:** Records are accepted as a matched set with no additional tolerance logic.
- **Priority:** Highest. Exact identifier evidence supersedes weaker signals.
- **Unambiguity constraint:** An identifier-based match must be unambiguous within the current reconciliation context. If duplicate or ambiguous candidate records share the identifier, the deterministic MVP must not automatically force a match and should leave them unresolved or explicitly flag them.

### Amount Plus Date Window
- **Evidence requirement:** Record amounts match within an explicit, configurable tolerance, and record dates fall within a controlled date window.
- **Behavior:** Tolerance and window parameters are configured externally; the engine does not infer them ad hoc.
- **Constraint:** Amount alone is never sufficient evidence for an automatic match. The date-window constraint is mandatory.
- **Date-window definition:** The date window is the absolute difference between calendar dates being less than or equal to a configurable number of days. Directional source-specific settlement timing rules are deferred until source-specific settlement semantics are explicitly modeled.

### T+1 / T+2 Settlement Timing
- **Evidence requirement:** Other deterministic evidence (exact identifier or amount-plus-date) supports the match, and the date delta corresponds to a configured settlement timing offset.
- **Behavior:** Settlement timing differences are treated as corroborating evidence, not primary evidence.
- **Constraint:** Timing difference alone is never sufficient.

## 3. Explicitly Deferred or Refused Cases

The deterministic MVP must not automatically resolve the following cases. These records must remain explicitly unresolved rather than being force-matched:

- **Narration-only matches:** Text similarity in narration fields is ambiguous and non-deterministic.
- **Partial refunds:** Amounts that do not fully align represent incomplete settlements and require explicit handling outside the deterministic engine.
- **Ambiguous fee deductions:** Fee lines that lack clear linkage evidence must not be auto-matched to parent transactions.
- **Complex split or aggregated settlements:** Multi-leg splits require explicit aggregation rules that are out of scope for the Day 2 MVP.
- **True orphans:** Records with no counterpart evidence must remain unresolved.
- **Late-arriving counterpart records:** Records that arrive after the reconciliation run must not be force-matched retroactively by the deterministic engine.

## 4. Duplicate Safety

Duplicate records must not be silently deleted. The deterministic engine must not allow a duplicate record to participate in more than one accepted reconciliation decision within the same run. Duplicate handling must be explicit and conservative. If duplicates are detected, they must be surfaced as unresolved or escalated for manual review.

## 5. Matching Constraints

The following invariants apply to all deterministic matching logic:

- A record cannot belong to more than one accepted reconciliation decision within the same reconciliation run.
- A decision must contain at least two distinct records.
- Exact identifier evidence takes priority over weaker amount/date evidence.
- A match must be explainable by the deterministic rule that produced it. Unexplained matches are not permitted.
- All unmatched records must remain available as residuals for later layers or manual review.

## 6. Rule Priority

The initial deterministic matching order is:

1. Exact identifier matching
2. Amount plus date-window matching with explicit tolerance
3. Leave everything else unresolved

No other automatic rules are supported in the Day 2 MVP.
