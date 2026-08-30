# Concord API Contract

Base URL: `/batches` (all batch endpoints). Health is at root.

Source of truth: `reconciliation/api/routes.py`, `reconciliation/api/store.py`,
`reconciliation/api/pipeline.py`.

---

## GET /health

Liveness probe. No auth.

```json
{
  "status": "ok"
}
```

---

## POST /batches

Multipart form upload — three separate file fields:

| Field       | Description        |
|-------------|--------------------|
| `settlement`| Settlement CSV file|
| `bank`      | Bank CSV file      |
| `ledger`    | Ledger CSV file    |

### 201 — success

```json
{
  "batch_id": "string (uuid hex)",
  "status": "completed",
  "record_count": 42
}
```

### 422 — CSV validation error

```json
{
  "batch_id": "string",
  "detail": "string (human message)",
  "error_type": "validation_error | unreadable_csv | malformed_csv | missing_columns | normalization_error | empty_input",
  "...details": "additional keys depending on error_type, e.g. missing_groups, found_columns, row_number, source_type"
}
```

### 500 — internal error

```json
{
  "batch_id": "string",
  "detail": "Internal processing error.",
  "error_type": "internal_error"
}
```

---

## GET /batches/{batch_id}/status

```json
{
  "batch_id": "string",
  "status": "processing | completed | failed",
  "created_at": "string (ISO 8601)",
  "completed_at": "string (ISO 8601) | null",
  "error_message": "string | null",
  "record_count": 42,
  "layer1_decision_count": 10,
  "layer2_mode": "not_executed | null",
  "routing_composition": {
    "DETERMINISTIC_MATCH": 30,
    "AI_AUTO_ACCEPTED": 5,
    "HUMAN_REVIEW": 2,
    "EXCEPTION": 5
  }
}
```

`routing_composition` is empty `{}` when batch is not completed. Buckets with zero records may be omitted (the frontend treats missing keys as `0`).
`layer1_decision_count` is `0` when not completed.
`layer2_mode` is `null` when not completed.

---

## GET /batches/{batch_id}/results?bucket=...

Optional query param `bucket` filters by one of:
`DETERMINISTIC_MATCH | AI_AUTO_ACCEPTED | HUMAN_REVIEW | EXCEPTION`

Omitting `bucket` returns all records.

### 200

```json
{
  "batch_id": "string",
  "bucket": "DETERMINISTIC_MATCH | AI_AUTO_ACCEPTED | HUMAN_REVIEW | EXCEPTION | ALL",
  "count": 42,
  "records": [
    {
      "record_id": "string",
      "source_type": "SETTLEMENT | BANK | LEDGER",
      "source_native_id": "string",
      "order_id_hint": "string | null",
      "amount_paise": 1500000,
      "date": "2024-01-15",
      "narration": "string | null",
      "bucket": "DETERMINISTIC_MATCH | AI_AUTO_ACCEPTED | HUMAN_REVIEW | EXCEPTION",
      "reason": "LAYER1_DETERMINISTIC | AI_CONFIDENT | AI_NEEDS_REVIEW | LOW_CONFIDENCE | AI_RESPONSE_INVALID | NO_CANDIDATE",
      "confidence": 1.0 | 0.85 | null,
      "source_decision_id": "string | null",
      "source_outcome": "PROPOSAL_VALID | NO_PROPOSAL | VALIDATION_FAILED | API_ERROR | TIMEOUT | null"
    }
  ]
}
```

---

## GET /batches/{batch_id}/eval

### 200

```json
{
  "batch_id": "string",
  "eval": {
    "total_records": 42,
    "records_by_source": {
      "SETTLEMENT": 14,
      "BANK": 14,
      "LEDGER": 14
    },
    "layer1": {
      "total_records": 42,
      "decisions_count": 10,
      "matched_records": 20,
      "residual_records": 22,
      "decisions_by_rule": {
        "EXACT_ID": 5,
        "AMOUNT_AND_DATE": 5
      }
    },
    "layer2": {
      "scenarios_processed": 0,
      "outcomes_by_type": {}
    },
    "layer3_routing_composition": {
      "DETERMINISTIC_MATCH": 20,
      "AI_AUTO_ACCEPTED": 0,
      "HUMAN_REVIEW": 0,
      "EXCEPTION": 22
    },
    "thresholds": {
      "auto_accept": 0.90,
      "review": 0.60
    },
    "layer2_mode": "not_executed"
  }
}
```

---

## GET /batches/{batch_id}/records/{record_id}

### 200

```json
{
  "batch_id": "string",
  "record": {
    "record_id": "string",
    "timestamp": "string (ISO 8601)",
    "source_type": "SETTLEMENT | BANK | LEDGER",
    "source_native_id": "string",
    "order_id_hint": "string | null",
    "amount_paise": 1500000,
    "date": "2024-01-15",
    "narration": "string | null",
    "resolved_by": "LAYER_1 | LAYER_2 | null",
    "layer1": {
      "decision_id": "string",
      "member_record_ids": ["string"],
      "rule_or_rationale": "string",
      "confidence": 1.0
    } | null,
    "layer2": {
      "scenario_id": null,
      "outcome_type": "PROPOSAL_VALID | NO_PROPOSAL | VALIDATION_FAILED | API_ERROR | TIMEOUT",
      "proposed_match_ids": ["string"],
      "confidence": 0.95 | null,
      "rationale": "string | null",
      "reason": "string",
      "invalid_ids": ["string"]
    } | null,
    "routing": {
      "bucket": "DETERMINISTIC_MATCH | AI_AUTO_ACCEPTED | HUMAN_REVIEW | EXCEPTION",
      "reason": "LAYER1_DETERMINISTIC | AI_CONFIDENT | AI_NEEDS_REVIEW | LOW_CONFIDENCE | AI_RESPONSE_INVALID | NO_CANDIDATE",
      "confidence": 1.0 | null,
      "source_decision_id": "string | null",
      "source_outcome": "PROPOSAL_VALID | NO_PROPOSAL | VALIDATION_FAILED | API_ERROR | TIMEOUT | null"
    }
  }
}
```

---

## Enums Reference

### SourceType
```
SETTLEMENT | BANK | LEDGER
```

### RoutingBucket (maps to `bucket` field)
```
DETERMINISTIC_MATCH | AI_AUTO_ACCEPTED | HUMAN_REVIEW | EXCEPTION
```

### RoutingReason (maps to `reason` field)
```
LAYER1_DETERMINISTIC | AI_CONFIDENT | AI_NEEDS_REVIEW | LOW_CONFIDENCE | AI_RESPONSE_INVALID | NO_CANDIDATE
```

### ProposalOutcomeType (maps to `source_outcome` field)
```
PROPOSAL_VALID | NO_PROPOSAL | VALIDATION_FAILED | API_ERROR | TIMEOUT
```

### Batch status
```
processing | completed | failed
```
