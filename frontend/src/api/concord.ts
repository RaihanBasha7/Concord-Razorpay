// API client for the real Concord FastAPI backend.
//
// All functions call the real endpoints documented in api-contract.md.
// No simulated, hashed, or fabricated data is produced here.

import type {
  BatchCreateResponse,
  BatchStatusResponse,
  BatchResultsResponse,
  BatchEvalResponse,
  RecordDetailResponse,
  HealthResponse,
} from '@/lib/types';

// ─── Base URL ────────────────────────────────────────────────────────

const API_BASE: string =
  import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000';

// ─── Helpers ─────────────────────────────────────────────────────────

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`;
  const res = await fetch(url, init);

  if (!res.ok) {
    let detail: string;
    try {
      const body = await res.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      detail = await res.text().catch(() => 'No response body');
    }
    throw new Error(`HTTP ${res.status} ${res.statusText}: ${detail}`);
  }

  return res.json() as Promise<T>;
}

// ─── Endpoints ───────────────────────────────────────────────────────

/**
 * POST /batches — upload three CSV files as multipart form-data.
 * The endpoint is synchronous: it returns 201 on completion or 422/500 on error.
 */
export async function uploadBatch(
  settlementFile: File,
  bankFile: File,
  ledgerFile: File,
): Promise<BatchCreateResponse> {
  const form = new FormData();
  form.append('settlement', settlementFile);
  form.append('bank', bankFile);
  form.append('ledger', ledgerFile);

  return request<BatchCreateResponse>('/batches', {
    method: 'POST',
    body: form,
  });
}

/**
 * GET /batches/{batchId}/status
 */
export async function getBatchStatus(
  batchId: string,
): Promise<BatchStatusResponse> {
  return request<BatchStatusResponse>(`/batches/${batchId}/status`);
}

/**
 * GET /batches/{batchId}/results?bucket=...
 */
export async function getBatchResults(
  batchId: string,
  bucket?: string,
): Promise<BatchResultsResponse> {
  const params = bucket ? `?bucket=${encodeURIComponent(bucket)}` : '';
  return request<BatchResultsResponse>(`/batches/${batchId}/results${params}`);
}

/**
 * GET /batches/{batchId}/eval
 */
export async function getBatchEval(
  batchId: string,
): Promise<BatchEvalResponse> {
  return request<BatchEvalResponse>(`/batches/${batchId}/eval`);
}

/**
 * GET /batches/{batchId}/records/{recordId}
 */
export async function getRecordDetail(
  batchId: string,
  recordId: string,
): Promise<RecordDetailResponse> {
  return request<RecordDetailResponse>(
    `/batches/${batchId}/records/${recordId}`,
  );
}

/**
 * GET /health — liveness probe.
 */
export async function checkHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/health');
}
