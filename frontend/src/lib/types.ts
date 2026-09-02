// Core domain types for Concord — matching the real FastAPI backend.
//
// Source of truth: reconciliation/api/routes.py, store.py, pipeline.py.
// See api-contract.md in this directory for full JSON shapes.

// ─── Enums ────────────────────────────────────────────────────────────

export type SourceType = 'SETTLEMENT' | 'BANK' | 'LEDGER';

export type RoutingBucket =
  | 'DETERMINISTIC_MATCH'
  | 'AI_AUTO_ACCEPTED'
  | 'HUMAN_REVIEW'
  | 'EXCEPTION';

export type RoutingReason =
  | 'LAYER1_DETERMINISTIC'
  | 'AI_CONFIDENT'
  | 'AI_NEEDS_REVIEW'
  | 'LOW_CONFIDENCE'
  | 'AI_RESPONSE_INVALID'
  | 'NO_CANDIDATE';

export type ProposalOutcomeType =
  | 'PROPOSAL_VALID'
  | 'NO_PROPOSAL'
  | 'VALIDATION_FAILED'
  | 'API_ERROR'
  | 'TIMEOUT';

export type BatchStatus = 'processing' | 'completed' | 'failed';

export type ResolvedBy = 'LAYER_1' | 'LAYER_2';

// ─── POST /batches response ──────────────────────────────────────────

export interface BatchCreateResponse {
  batch_id: string;
  status: 'completed';
  record_count: number;
}

export interface BatchCreateError {
  batch_id: string;
  detail: string;
  error_type: string;
}

// ─── GET /batches/{id}/status ────────────────────────────────────────

export interface BatchStatusResponse {
  batch_id: string;
  status: BatchStatus;
  created_at: string;
  completed_at: string | null;
  error_message: string | null;
  record_count: number;
  layer1_decision_count: number;
  layer2_mode: string | null;
  routing_composition: Partial<Record<RoutingBucket, number>>;
}

// ─── GET /batches/{id}/results ───────────────────────────────────────

export interface RoutingRecord {
  record_id: string;
  source_type: SourceType;
  source_native_id: string;
  order_id_hint: string | null;
  amount_paise: number;
  date: string;
  narration: string | null;
  bucket: RoutingBucket;
  reason: RoutingReason;
  confidence: number | null;
  source_decision_id: string | null;
  source_outcome: ProposalOutcomeType | null;
}

export interface BatchResultsResponse {
  batch_id: string;
  bucket: RoutingBucket | 'ALL';
  count: number;
  records: RoutingRecord[];
}

// ─── GET /batches/{id}/eval ──────────────────────────────────────────

export interface EvalReport {
  total_records: number;
  records_by_source: Record<SourceType, number>;
  layer1: {
    total_records: number;
    decisions_count: number;
    matched_records: number;
    residual_records: number;
    decisions_by_rule: Record<string, number>;
  };
  layer2: {
    residual_records_processed: number;
    outcomes_by_type: Record<string, number>;
  };
  layer3_routing_composition: Partial<Record<RoutingBucket, number>>;
  thresholds: {
    auto_accept: number;
    review: number;
  };
  layer2_mode: string;
  demo_mode?: string;
  demo_mode_note?: string;
}

export interface BatchEvalResponse {
  batch_id: string;
  eval: EvalReport;
}

// ─── GET /batches/{id}/records/{record_id} ───────────────────────────

export interface Layer1AuditInfo {
  decision_id: string;
  member_record_ids: string[];
  rule_or_rationale: string;
  confidence: number;
}

export interface Layer2AuditInfo {
  scenario_id: string | null;
  outcome_type: ProposalOutcomeType;
  proposed_match_ids: string[];
  confidence: number | null;
  rationale: string | null;
  reason: string;
  invalid_ids: string[];
}

export interface RecordAuditRouting {
  bucket: RoutingBucket;
  reason: RoutingReason;
  confidence: number | null;
  source_decision_id: string | null;
  source_outcome: ProposalOutcomeType | null;
}

export interface RecordAuditDetail {
  record_id: string;
  timestamp: string;
  source_type: SourceType;
  source_native_id: string;
  order_id_hint: string | null;
  amount_paise: number;
  date: string;
  narration: string | null;
  resolved_by: ResolvedBy | null;
  layer1: Layer1AuditInfo | null;
  layer2: Layer2AuditInfo | null;
  routing: RecordAuditRouting;
}

export interface RecordDetailResponse {
  batch_id: string;
  record: RecordAuditDetail;
}

// ─── GET /health ──────────────────────────────────────────────────────

export interface HealthResponse {
  status: string;
}

// ─── Guardrail thresholds (constants, not from API) ──────────────────

export const GUARDRAIL_THRESHOLDS = {
  autoAcceptConfidence: 0.90,
  reviewConfidence: 0.60,
  amountTolerancePct: 0.01,
  dateWindowDays: 2,
  duplicateCheck: true,
} as const;
