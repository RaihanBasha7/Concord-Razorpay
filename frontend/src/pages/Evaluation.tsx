import { useEffect, useState } from 'react';
import { AlertTriangle, ShieldCheck, Brain, AlertCircle } from 'lucide-react';
import { Topbar } from '@/components/navigation/Topbar';
import { AnimatedNumber } from '@/components/ui/AnimatedNumber';
import { PageTransition, StaggerGroup, StaggerItem, Skeleton } from '@/components/ui/Transitions';
import { getBatchStatus, getBatchEval } from '@/api/concord';
import type { EvalReport, RoutingBucket } from '@/lib/types';

const LAST_BATCH_KEY = 'concord:lastBatchId';

export function Evaluation() {
  const [evalReport, setEvalReport] = useState<EvalReport | null>(null);
  const [batchId, setBatchId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        const storedId = localStorage.getItem(LAST_BATCH_KEY);
        if (!storedId) {
          setError('No batch found. Upload a batch first.');
          setLoading(false);
          return;
        }
        setBatchId(storedId);
        const status = await getBatchStatus(storedId);
        if (status.status !== 'completed') {
          setError(`Batch is ${status.status}, not completed.`);
          setLoading(false);
          return;
        }
        const evalRes = await getBatchEval(storedId);
        setEvalReport(evalRes.eval);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load evaluation data');
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  return (
    <>
      <Topbar title="Evaluation Dashboard" subtitle="Layer-by-layer reconciliation statistics for the current batch." />
      <PageTransition>
        <div className="flex-1 p-6 space-y-6 max-w-5xl">
          {loading ? (
            <div className="space-y-6">
              <div className="space-y-4">
                <Skeleton className="h-5 w-48" />
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                  {[0, 1, 2, 3].map((i) => (
                    <div key={i} className="panel p-5 space-y-3">
                      <Skeleton className="h-3 w-20" />
                      <Skeleton className="h-6 w-16" />
                      <Skeleton className="h-1.5 w-full rounded-full" />
                    </div>
                  ))}
                </div>
              </div>
            </div>
          ) : error ? (
            <div className="flex items-center justify-center py-20">
              <div className="text-center">
                <AlertTriangle className="w-8 h-8 text-signal-exception mx-auto mb-3" />
                <p className="text-cream-300 text-sm">{error}</p>
              </div>
            </div>
          ) : evalReport ? (
            <>
              {/* Batch info header */}
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold text-cream-100 tracking-wide">
                  Batch {batchId?.slice(0, 8)}…
                </h2>
                <span className="text-xs text-cream-500/60 mono">Layer 2 mode: {evalReport.layer2_mode}</span>
              </div>

              {/* Source breakdown */}
              <StaggerGroup className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <StaggerItem>
                  <SourceCard label="Settlement" count={evalReport.records_by_source.SETTLEMENT} total={evalReport.total_records} />
                </StaggerItem>
                <StaggerItem>
                  <SourceCard label="Bank" count={evalReport.records_by_source.BANK} total={evalReport.total_records} />
                </StaggerItem>
                <StaggerItem>
                  <SourceCard label="Ledger" count={evalReport.records_by_source.LEDGER} total={evalReport.total_records} />
                </StaggerItem>
              </StaggerGroup>

              {/* Layer 1 stats */}
              <StaggerItem>
                <div className="panel p-5">
                  <div className="flex items-center gap-2 mb-4">
                    <ShieldCheck className="w-4 h-4 text-signal-matched" />
                    <h3 className="text-sm font-semibold text-cream-100">Layer 1 — Deterministic Matching</h3>
                  </div>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                    <StatBlock label="Total Records" value={evalReport.layer1.total_records} />
                    <StatBlock label="Decisions" value={evalReport.layer1.decisions_count} accent="matched" />
                    <StatBlock label="Matched Records" value={evalReport.layer1.matched_records} accent="matched" />
                    <StatBlock label="Residual" value={evalReport.layer1.residual_records} accent="exception" />
                  </div>
                  {Object.keys(evalReport.layer1.decisions_by_rule).length > 0 && (
                    <div className="mt-4 pt-4 border-t border-amber-500/10">
                      <div className="text-xs text-cream-500/60 tracking-wide uppercase mb-2">Decisions by Rule</div>
                      <div className="flex flex-wrap gap-2">
                        {Object.entries(evalReport.layer1.decisions_by_rule).map(([rule, count]) => (
                          <span key={rule} className="text-xs text-cream-300 bg-ink-700 px-2.5 py-1 rounded-md mono">
                            {rule}: {count}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </StaggerItem>

              {/* Layer 2 stats */}
              <StaggerItem>
                <div className="panel p-5">
                  <div className="flex items-center gap-2 mb-4">
                    <Brain className="w-4 h-4 text-amber-500" />
                    <h3 className="text-sm font-semibold text-cream-100">Layer 2 — AI Reconstruction</h3>
                  </div>
                  <div className="grid grid-cols-2 gap-4">
                    <StatBlock label="Residual Records" value={evalReport.layer2.residual_records_processed} />
                    <div>
                      <div className="text-xs text-cream-500/60 tracking-wide uppercase mb-1">Outcomes by Type</div>
                      {Object.keys(evalReport.layer2.outcomes_by_type).length > 0 ? (
                        <div className="flex flex-wrap gap-2">
                          {Object.entries(evalReport.layer2.outcomes_by_type).map(([type, count]) => (
                            <span key={type} className="text-xs text-cream-300 bg-ink-700 px-2.5 py-1 rounded-md mono">
                              {type}: {count}
                            </span>
                          ))}
                        </div>
                      ) : (
                        <span className="text-xs text-cream-500/50">No Layer 2 outcomes (not executed)</span>
                      )}
                    </div>
                  </div>
                </div>
              </StaggerItem>

              {/* Layer 3 routing composition */}
              <StaggerItem>
                <div className="panel p-5">
                  <div className="flex items-center gap-2 mb-4">
                    <AlertCircle className="w-4 h-4 text-cream-300" />
                    <h3 className="text-sm font-semibold text-cream-100">Layer 3 — Routing Composition</h3>
                  </div>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                    {(Object.entries(evalReport.layer3_routing_composition) as [RoutingBucket, number][]).map(([bucket, count]) => (
                      <RoutingBucketCard key={bucket} bucket={bucket} count={count} total={evalReport.total_records} />
                    ))}
                  </div>
                </div>
              </StaggerItem>

              {/* Thresholds */}
              <StaggerItem>
                <div className="panel p-5">
                  <h3 className="text-xs text-cream-500/60 tracking-wide uppercase mb-3">Guardrail Thresholds</h3>
                  <div className="flex flex-wrap gap-4 text-xs text-cream-400">
                    <span>Auto-accept: <span className="mono text-cream-300">{(evalReport.thresholds.auto_accept * 100).toFixed(0)}%</span></span>
                    <span>Review: <span className="mono text-cream-300">{(evalReport.thresholds.review * 100).toFixed(0)}%</span></span>
                  </div>
                </div>
              </StaggerItem>
            </>
          ) : null}
        </div>
      </PageTransition>
    </>
  );
}

function SourceCard({ label, count, total }: { label: string; count: number; total: number }) {
  const pct = total > 0 ? (count / total) * 100 : 0;
  return (
    <div className="panel panel-hover p-5">
      <div className="text-xs text-cream-500/70 tracking-wide uppercase mb-1">{label}</div>
      <div className="text-2xl font-semibold text-cream-50 tracking-tight">
        <AnimatedNumber value={count} />
        <span className="text-sm text-cream-500/50 ml-1">/ {total}</span>
      </div>
      <div className="h-1.5 rounded-full bg-ink-600 overflow-hidden mt-3">
        <div
          className="h-full rounded-full bg-amber-500"
          style={{ width: `${pct}%`, transition: 'width 0.7s cubic-bezier(0.22, 1, 0.36, 1)' }}
        />
      </div>
      <div className="text-[10px] text-cream-500/50 mt-1 mono">{pct.toFixed(1)}%</div>
    </div>
  );
}

function StatBlock({ label, value, accent }: { label: string; value: number; accent?: 'matched' | 'exception' }) {
  const color = accent === 'matched' ? 'text-signal-matched' : accent === 'exception' ? 'text-signal-exception' : 'text-cream-100';
  return (
    <div>
      <div className="text-xs text-cream-500/60 tracking-wide uppercase mb-1">{label}</div>
      <div className={`text-xl font-semibold mono ${color}`}>
        <AnimatedNumber value={value} />
      </div>
    </div>
  );
}

function RoutingBucketCard({ bucket, count, total }: { bucket: RoutingBucket; count: number; total: number }) {
  const pct = total > 0 ? (count / total) * 100 : 0;
  const color = bucket === 'DETERMINISTIC_MATCH' ? 'text-signal-matched'
    : bucket === 'AI_AUTO_ACCEPTED' ? 'text-signal-matched'
    : bucket === 'HUMAN_REVIEW' ? 'text-amber-400'
    : 'text-signal-exception';

  const label = bucket === 'DETERMINISTIC_MATCH' ? 'Deterministic'
    : bucket === 'AI_AUTO_ACCEPTED' ? 'AI Accepted'
    : bucket === 'HUMAN_REVIEW' ? 'Human Review'
    : 'Exception';

  return (
    <div className="rounded-lg border border-amber-500/10 bg-ink-900 p-4">
      <div className="text-xs text-cream-500/60 tracking-wide uppercase mb-1">{label}</div>
      <div className={`text-xl font-semibold mono ${color}`}>
        <AnimatedNumber value={count} />
      </div>
      <div className="h-1 rounded-full bg-ink-600 overflow-hidden mt-2">
        <div
          className={`h-full rounded-full ${color.replace('text-', 'bg-')}`}
          style={{ width: `${pct}%`, transition: 'width 0.7s cubic-bezier(0.22, 1, 0.36, 1)' }}
        />
      </div>
      <div className="text-[10px] text-cream-500/50 mt-1 mono">{pct.toFixed(1)}%</div>
    </div>
  );
}
