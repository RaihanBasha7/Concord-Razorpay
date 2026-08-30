import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  ArrowLeft,
  Loader2,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Brain,
  ShieldCheck,
  FileInput,
  CircleDot,
  ChevronRight,
} from 'lucide-react';
import { Topbar } from '@/components/navigation/Topbar';
import { StatusPill, RoutingReasonBadge } from '@/components/ui/StatusPill';
import { ConfidenceBar } from '@/components/ui/ConfidenceBar';
import { PageTransition, StaggerGroup, StaggerItem, Skeleton } from '@/components/ui/Transitions';
import { getRecordDetail } from '@/api/concord';
import type { RecordAuditDetail, RoutingBucket } from '@/lib/types';

const LAST_BATCH_KEY = 'concord:lastBatchId';

export function RecordDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [record, setRecord] = useState<RecordAuditDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      if (!id) return;
      const batchId = localStorage.getItem(LAST_BATCH_KEY);
      if (!batchId) {
        setError('No batch ID found. Upload a batch first.');
        setLoading(false);
        return;
      }
      try {
        const res = await getRecordDetail(batchId, id);
        setRecord(res.record);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load record');
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [id]);

  if (loading) {
    return (
      <>
        <Topbar title="Record Detail" subtitle="Decision trace" />
        <div className="flex-1 p-6 max-w-5xl space-y-4">
          <Skeleton className="h-8 w-32" />
          <div className="panel p-6 space-y-4">
            <Skeleton className="h-8 w-48" />
            <Skeleton className="h-4 w-64" />
            <Skeleton className="h-4 w-32" />
          </div>
          {[0, 1, 2].map((i) => (
            <div key={i} className="panel p-5 space-y-3">
              <Skeleton className="h-5 w-40" />
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-3/4" />
            </div>
          ))}
        </div>
      </>
    );
  }

  if (error || !record) {
    return (
      <>
        <Topbar title="Record Detail" subtitle="Decision trace" />
        <div className="flex-1 flex items-center justify-center p-6">
          <div className="text-center">
            <AlertTriangle className="w-8 h-8 text-signal-exception mx-auto mb-3" />
            <p className="text-cream-300 text-sm">{error || 'Record not found.'}</p>
            <button onClick={() => navigate('/app/queue')} className="btn-secondary mt-4">
              <ArrowLeft className="w-4 h-4" /> Back to Queue
            </button>
          </div>
        </div>
      </>
    );
  }

  const isAI = record.resolved_by === 'LAYER_2';
  const hasL1 = record.layer1 !== null;
  const hasL2 = record.layer2 !== null;

  return (
    <>
      <Topbar title="Decision Trace" subtitle="Every step, from ingestion to outcome." />
      <PageTransition>
        <div className="flex-1 p-6 space-y-6 max-w-5xl">
          {/* Back link */}
          <button onClick={() => navigate('/app/queue')} className="btn-ghost text-xs">
            <ArrowLeft className="w-3.5 h-3.5" /> Back to Queue
          </button>

          {/* Record header */}
          <StaggerItem>
            <div className="panel panel-hover p-6">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <div className="flex items-center gap-3 mb-2">
                    <h1 className="text-2xl font-semibold text-cream-50 mono tracking-tight">{record.source_native_id}</h1>
                    <StatusPill status={record.routing.bucket} />
                  </div>
                  <div className="flex items-center gap-4 text-sm text-cream-400">
                    <span className="mono">₹{(record.amount_paise / 100).toLocaleString('en-IN', { minimumFractionDigits: 0 })}</span>
                    <span className="text-cream-500/50">·</span>
                    <span>{record.source_type}</span>
                    {record.order_id_hint && (
                      <>
                        <span className="text-cream-500/50">·</span>
                        <span className="mono">{record.order_id_hint}</span>
                      </>
                    )}
                  </div>
                </div>
                <div className="flex flex-col items-end gap-2">
                  <RoutingReasonBadge reason={record.routing.reason} />
                  {record.routing.confidence !== null && (
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-cream-500/60">Confidence:</span>
                      <div className="w-32"><ConfidenceBar confidence={record.routing.confidence} /></div>
                    </div>
                  )}
                </div>
              </div>
              <div className="mt-4 pt-4 border-t border-amber-500/10">
                <div className="flex flex-wrap gap-4 text-xs text-cream-500/60">
                  <span>Date: <span className="text-cream-300 mono">{record.date}</span></span>
                  {record.narration && <span>Narration: <span className="text-cream-300">{record.narration}</span></span>}
                  <span>Resolved by: <span className={`mono ${record.resolved_by === 'LAYER_1' ? 'text-signal-matched' : record.resolved_by === 'LAYER_2' ? 'text-amber-400' : 'text-cream-400'}`}>{record.resolved_by ?? 'unresolved'}</span></span>
                </div>
              </div>
            </div>
          </StaggerItem>

          {/* Decision path diagram */}
          <StaggerItem>
            <AIVsRuleDiagram isAI={isAI} />
          </StaggerItem>

          {/* Decision trace */}
          <div className="space-y-3">
            <h2 className="text-sm font-semibold text-cream-100 tracking-wide uppercase">Decision Trace</h2>

            <StaggerGroup>
              {/* 01 Ingested */}
              <StaggerItem>
                <TraceStep
                  number="01"
                  title="Ingested"
                  icon={<FileInput className="w-4 h-4" />}
                  status="INGESTED"
                >
                  <p className="text-sm text-cream-300">
                    Record arrived from{' '}
                    <span className="text-cream-100">{record.source_type}</span>{' '}
                    with native ID <span className="mono text-cream-100">{record.source_native_id}</span>.
                  </p>
                </TraceStep>
              </StaggerItem>

              {/* 02 Layer 1 */}
              <StaggerItem>
                <TraceStep
                  number="02"
                  title="Layer 1 — Deterministic Rules"
                  icon={<CircleDot className="w-4 h-4" />}
                  status={hasL1 ? 'MATCHED' : 'NO_MATCH'}
                >
                  {hasL1 && record.layer1 ? (
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        <CheckCircle2 className="w-4 h-4 text-signal-matched" />
                        <span className="text-sm text-cream-100">Rule match: <span className="mono text-signal-matched">{record.layer1.rule_or_rationale}</span></span>
                      </div>
                      <p className="text-xs text-cream-500/70">
                        Decision ID: <span className="mono text-cream-300">{record.layer1.decision_id}</span>
                      </p>
                      <p className="text-xs text-cream-500/70">
                        Member records: <span className="mono text-cream-300">{record.layer1.member_record_ids.join(', ')}</span>
                      </p>
                    </div>
                  ) : (
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        <XCircle className="w-4 h-4 text-signal-exception" />
                        <span className="text-sm text-cream-100">No deterministic match found.</span>
                      </div>
                      <p className="text-xs text-cream-500/70">Routed to Layer 2 for AI reasoning.</p>
                    </div>
                  )}
                </TraceStep>
              </StaggerItem>

              {/* 03 Layer 2 — only if record reached this layer */}
              {hasL2 && record.layer2 && (
                <StaggerItem>
                  <TraceStep
                    number="03"
                    title="Layer 2 — AI Reasoning"
                    icon={<Brain className="w-4 h-4" />}
                    status={record.layer2.outcome_type}
                    badge={record.layer2.outcome_type === 'PROPOSAL_VALID' ? 'AI GENERATED' : record.layer2.outcome_type}
                    isAIStep
                  >
                    <div className="space-y-3">
                      {record.layer2.confidence !== null && (
                        <div className="flex items-center gap-3">
                          <span className="text-xs text-cream-500/60">Confidence score:</span>
                          <div className="w-40"><ConfidenceBar confidence={record.layer2.confidence} /></div>
                        </div>
                      )}
                      {record.layer2.rationale && (
                        <div className="bg-ink-900 border border-amber-500/10 rounded-lg p-3">
                          <div className="text-[10px] text-amber-500/70 tracking-widest uppercase mb-1.5">AI Rationale</div>
                          <p className="text-sm text-cream-200 leading-relaxed">{record.layer2.rationale}</p>
                        </div>
                      )}
                      <div className="text-xs text-cream-500/60">
                        Outcome: <span className="text-cream-300 mono">{record.layer2.outcome_type}</span>
                        {record.layer2.proposed_match_ids.length > 0 && (
                          <> · Proposed IDs: <span className="text-cream-300 mono">{record.layer2.proposed_match_ids.join(', ')}</span></>
                        )}
                      </div>
                      {record.layer2.invalid_ids.length > 0 && (
                        <div className="text-xs text-signal-exception">
                          Invalid IDs: <span className="mono">{record.layer2.invalid_ids.join(', ')}</span>
                        </div>
                      )}
                    </div>
                  </TraceStep>
                </StaggerItem>
              )}

              {/* Layer 3 Routing */}
              <StaggerItem>
                <TraceStep
                  number={hasL2 ? '04' : '03'}
                  title="Layer 3 — Routing"
                  icon={<ShieldCheck className="w-4 h-4" />}
                  status={record.routing.bucket}
                >
                  <div className="space-y-2">
                    <div className="flex items-center gap-2">
                      <StatusPill status={record.routing.bucket} />
                      <RoutingReasonBadge reason={record.routing.reason} />
                    </div>
                    {record.routing.source_decision_id && (
                      <p className="text-xs text-cream-500/70">
                        Source decision: <span className="mono text-cream-300">{record.routing.source_decision_id}</span>
                      </p>
                    )}
                    {record.routing.source_outcome && (
                      <p className="text-xs text-cream-500/70">
                        Source outcome: <span className="mono text-cream-300">{record.routing.source_outcome}</span>
                      </p>
                    )}
                  </div>
                </TraceStep>
              </StaggerItem>

              {/* Outcome */}
              <StaggerItem>
                <TraceStep
                  number={hasL2 ? '05' : '04'}
                  title="Outcome"
                  icon={<ChevronRight className="w-4 h-4" />}
                  status={record.routing.bucket}
                >
                  <OutcomeDetail record={record} />
                </TraceStep>
              </StaggerItem>
            </StaggerGroup>
          </div>
        </div>
      </PageTransition>
    </>
  );
}

function TraceStep({
  number,
  title,
  icon,
  status,
  badge,
  isAIStep = false,
  children,
}: {
  number: string;
  title: string;
  icon: React.ReactNode;
  status: string;
  badge?: string;
  isAIStep?: boolean;
  children: React.ReactNode;
}) {
  const statusColor = (() => {
    if (['PASSED', 'MATCHED', 'INGESTED', 'DETERMINISTIC_MATCH', 'AI_AUTO_ACCEPTED'].includes(status)) return 'text-signal-matched';
    if (['BLOCKED', 'NO_MATCH', 'EXCEPTION'].includes(status)) return 'text-signal-exception';
    if (['HUMAN_REVIEW', 'AI_PROPOSAL'].includes(status)) return 'text-amber-400';
    return 'text-cream-500';
  })();

  return (
    <div className={`panel panel-hover p-5 ${isAIStep ? 'ai-distinction' : ''}`}>
      <div className="flex items-center gap-3 mb-3">
        <div className={`w-8 h-8 rounded-lg flex items-center justify-center transition-colors ${
          isAIStep ? 'bg-amber-500/10 border border-amber-500/25 text-amber-500' : 'bg-ink-700 border border-amber-500/15 text-amber-500'
        }`}>
          {icon}
        </div>
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <span className="mono text-xs text-cream-500/50">{number}</span>
            <h3 className="text-sm font-semibold text-cream-100">{title}</h3>
          </div>
        </div>
        {badge && (
          <span className="pill badge-ai text-[10px]">{badge}</span>
        )}
        <span className={`mono text-xs ${statusColor}`}>{status}</span>
      </div>
      <div className="pl-11">{children}</div>
    </div>
  );
}

function OutcomeDetail({ record }: { record: RecordAuditDetail }) {
  const bucket = record.routing.bucket;
  const accepted = bucket === 'DETERMINISTIC_MATCH' || bucket === 'AI_AUTO_ACCEPTED';

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        {accepted ? (
          <CheckCircle2 className="w-5 h-5 text-signal-matched" />
        ) : (
          <AlertTriangle className="w-5 h-5 text-signal-review" />
        )}
        <span className="text-sm text-cream-100 font-medium">
          {accepted ? 'Match accepted and routed to ledger.' : `Routed to ${bucket === 'HUMAN_REVIEW' ? 'human review' : 'exception handling'}.`}
        </span>
      </div>
      <p className="text-xs text-cream-500/60 mono">
        Timestamp: {new Date(record.timestamp).toLocaleString('en-IN')}
      </p>
    </div>
  );
}

function AIVsRuleDiagram({ isAI }: { isAI: boolean }) {
  return (
    <div className="panel p-5">
      <h3 className="text-xs text-cream-500/60 tracking-wide uppercase mb-4">Decision Path</h3>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Deterministic path */}
        <div className={`rounded-lg p-4 border transition-all duration-300 ${!isAI ? 'border-signal-matched/30 bg-signal-matchedDim/10' : 'border-amber-500/10 bg-ink-900'}`}>
          <div className="flex items-center gap-2 mb-3">
            <span className="badge-rule text-[10px]">RULE</span>
            <span className="text-xs text-cream-400">Deterministic path</span>
          </div>
          <div className="flex items-center gap-2 text-xs text-cream-300">
            <span>Field match</span>
            <ChevronRight className="w-3 h-3 text-cream-500/40" />
            <span>Rule engine</span>
            <ChevronRight className="w-3 h-3 text-cream-500/40" />
            <span className={isAI ? 'text-cream-500/50' : 'text-signal-matched'}>Match accepted</span>
          </div>
        </div>
        {/* AI fallback path */}
        <div className={`rounded-lg p-4 border transition-all duration-300 ${isAI ? 'border-amber-500/30 bg-amber-500/5' : 'border-amber-500/10 bg-ink-900'}`}>
          <div className="flex items-center gap-2 mb-3">
            <span className="badge-ai text-[10px]">AI</span>
            <span className="badge-guardrail text-[10px]">GUARDRAIL</span>
          </div>
          <div className="flex items-center gap-2 text-xs text-cream-300 flex-wrap">
            <span>No det. match</span>
            <ChevronRight className="w-3 h-3 text-cream-500/40" />
            <span>AI reasoning</span>
            <ChevronRight className="w-3 h-3 text-cream-500/40" />
            <span>Confidence + rationale</span>
            <ChevronRight className="w-3 h-3 text-cream-500/40" />
            <span className={isAI ? 'text-amber-400' : 'text-cream-500/50'}>Route</span>
          </div>
        </div>
      </div>
    </div>
  );
}
