import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { TrendingUp, ShieldCheck, AlertTriangle, ArrowRight, Brain } from 'lucide-react';
import { Topbar } from '@/components/navigation/Topbar';
import { AnimatedNumber } from '@/components/ui/AnimatedNumber';
import { StatusPill, RoutingReasonBadge } from '@/components/ui/StatusPill';
import { ConfidenceBar } from '@/components/ui/ConfidenceBar';
import { PageTransition, StaggerGroup, StaggerItem, MetricCardSkeleton } from '@/components/ui/Transitions';
import { useTilt } from '@/hooks/useMouseInteraction';
import { getBatchStatus, getBatchResults } from '@/api/concord';
import type { BatchStatusResponse, RoutingRecord } from '@/lib/types';

const LAST_BATCH_KEY = 'concord:lastBatchId';

function getStoredBatchId(): string | null {
  return localStorage.getItem(LAST_BATCH_KEY);
}

export function Dashboard() {
  const navigate = useNavigate();
  const [batch, setBatch] = useState<BatchStatusResponse | null>(null);
  const [records, setRecords] = useState<RoutingRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        const batchId = getStoredBatchId();
        if (!batchId) {
          setError('No batches found. Upload a batch to get started.');
          setLoading(false);
          return;
        }
        const status = await getBatchStatus(batchId);
        setBatch(status);
        if (status.status === 'completed') {
          const results = await getBatchResults(batchId);
          setRecords(results.records);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load data');
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  if (loading) {
    return (
      <>
        <Topbar title="Reconciliation Command Center" subtitle="Monitor unresolved records and the decisions Concord is making." />
        <div className="flex-1 p-6 space-y-6">
          <StaggerGroup className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {[0, 1, 2, 3].map((i) => (
              <StaggerItem key={i}>
                <MetricCardSkeleton />
              </StaggerItem>
            ))}
          </StaggerGroup>
        </div>
      </>
    );
  }

  if (error || !batch) {
    return (
      <>
        <Topbar title="Reconciliation Command Center" subtitle="Monitor unresolved records and the decisions Concord is making." />
        <div className="flex-1 flex items-center justify-center p-6">
          <div className="text-center max-w-md">
            <AlertTriangle className="w-8 h-8 text-signal-exception mx-auto mb-3" />
            <p className="text-cream-300 text-sm">{error || 'No batches found. Upload a batch to get started.'}</p>
            <button onClick={() => navigate('/app/upload')} className="btn-primary mt-4">
              Upload a Batch <ArrowRight className="w-4 h-4" />
            </button>
          </div>
        </div>
      </>
    );
  }

  const comp = batch.routing_composition;
  const total = batch.record_count;
  const matched = comp.DETERMINISTIC_MATCH ?? 0;
  const aiAccepted = comp.AI_AUTO_ACCEPTED ?? 0;
  const review = comp.HUMAN_REVIEW ?? 0;
  const exception = comp.EXCEPTION ?? 0;
  const matchedPct = total > 0 ? (matched / total) * 100 : 0;
  const aiPct = total > 0 ? (aiAccepted / total) * 100 : 0;

  const recentRecords = records.slice(0, 8);

  return (
    <>
      <Topbar title="Reconciliation Command Center" subtitle="Monitor unresolved records and the decisions Concord is making." />
      <PageTransition>
        <div className="flex-1 p-6 space-y-6">
          {/* Metrics */}
          <StaggerGroup className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <StaggerItem>
              <MetricCard
                label="Total Records"
                value={<AnimatedNumber value={total} />}
                icon={<ShieldCheck className="w-4 h-4" />}
                sublabel={`Batch ${batch.batch_id.slice(0, 8)}…`}
              />
            </StaggerItem>
            <StaggerItem>
              <MetricCard
                label="Deterministic Match"
                value={<AnimatedNumber value={matched} />}
                suffix={<span className="text-cream-500/60 text-sm ml-1">/ {total}</span>}
                icon={<TrendingUp className="w-4 h-4" />}
                sublabel={<span className="text-signal-matched">{matchedPct.toFixed(1)}% of total</span>}
                accent="matched"
              />
            </StaggerItem>
            <StaggerItem>
              <MetricCard
                label="AI Auto-Accepted"
                value={<AnimatedNumber value={aiAccepted} />}
                suffix={<span className="text-cream-500/60 text-sm ml-1">/ {total}</span>}
                icon={<Brain className="w-4 h-4" />}
                sublabel={<span className="text-amber-500">{aiPct.toFixed(1)}% via Layer 2</span>}
                accent={aiAccepted > 0 ? 'amber' : 'neutral'}
              />
            </StaggerItem>
            <StaggerItem>
              <MetricCard
                label="Needs Review"
                value={<AnimatedNumber value={review} />}
                icon={<ShieldCheck className="w-4 h-4" />}
                sublabel={<span className="text-signal-review">Routed to human review</span>}
                accent={review > 0 ? 'amber' : 'neutral'}
              />
            </StaggerItem>
            <StaggerItem>
              <MetricCard
                label="Exceptions"
                value={<AnimatedNumber value={exception} />}
                icon={<AlertTriangle className="w-4 h-4" />}
                sublabel={<span className="text-signal-exception">Needs investigation</span>}
                accent={exception > 0 ? 'exception' : 'neutral'}
              />
            </StaggerItem>
          </StaggerGroup>

          {/* Pipeline flow diagram */}
          <StaggerItem>
            <PipelineFlow batch={batch} />
          </StaggerItem>

          {/* Recent records preview */}
          {recentRecords.length > 0 && (
            <StaggerItem>
              <div className="panel overflow-hidden">
                <div className="flex items-center justify-between px-5 py-4 border-b border-amber-500/10">
                  <h2 className="text-sm font-semibold text-cream-100 tracking-wide">Recent Records</h2>
                  <button onClick={() => navigate('/app/queue')} className="btn-ghost text-xs">
                    View all <ArrowRight className="w-3 h-3" />
                  </button>
                </div>
                <div className="divide-y divide-amber-500/5">
                  {recentRecords.map((rec) => (
                    <button
                      key={rec.record_id}
                      onClick={() => navigate(`/app/records/${rec.record_id}`)}
                      className="w-full flex items-center gap-4 px-5 py-3 row-hover transition-colors duration-150 text-left"
                    >
                      <div className="mono text-xs text-cream-300 w-20 shrink-0">{rec.source_native_id}</div>
                      <div className="text-sm text-cream-100 w-24 shrink-0">
                        ₹{(rec.amount_paise / 100).toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                      </div>
                      <div className="hidden sm:flex shrink-0">
                        <RoutingReasonBadge reason={rec.reason} />
                      </div>
                      <div className="hidden md:block flex-1 min-w-0">
                        {rec.confidence !== null && (
                          <div className="w-32"><ConfidenceBar confidence={rec.confidence} showLabel size="sm" /></div>
                        )}
                      </div>
                      <div className="ml-auto shrink-0">
                        <StatusPill status={rec.bucket} size="sm" />
                      </div>
                    </button>
                  ))}
                </div>
              </div>
            </StaggerItem>
          )}
        </div>
      </PageTransition>
    </>
  );
}

function MetricCard({
  label,
  value,
  suffix,
  icon,
  sublabel,
  accent = 'neutral',
}: {
  label: string;
  value: React.ReactNode;
  suffix?: React.ReactNode;
  icon: React.ReactNode;
  sublabel?: React.ReactNode;
  accent?: 'neutral' | 'matched' | 'amber' | 'exception';
}) {
  const { ref, transform, transition } = useTilt<HTMLDivElement>(2);
  const accentColor = {
    neutral: 'text-cream-300',
    matched: 'text-signal-matched',
    amber: 'text-amber-500',
    exception: 'text-signal-exception',
  }[accent];

  const iconBg = {
    neutral: 'bg-ink-700 text-cream-400',
    matched: 'bg-signal-matchedDim/30 text-signal-matched',
    amber: 'bg-amber-500/15 text-amber-500',
    exception: 'bg-signal-exceptionDim/30 text-signal-exception',
  }[accent];

  const accentBorder = {
    neutral: '',
    matched: 'hover:border-signal-matched/20',
    amber: 'hover:border-amber-500/20',
    exception: 'hover:border-signal-exception/20',
  }[accent];

  return (
    <div
      ref={ref}
      className={`panel panel-hover p-5 ${accentBorder}`}
      style={{ transform, transition, transformStyle: 'preserve-3d' }}
    >
      <div className="flex items-center justify-between mb-3" style={{ transform: 'translateZ(20px)' }}>
        <span className="text-xs text-cream-500/70 tracking-wide uppercase">{label}</span>
        <span className={`w-7 h-7 rounded-lg flex items-center justify-center ${iconBg} transition-all duration-200`}>
          {icon}
        </span>
      </div>
      <div className="text-2xl font-semibold text-cream-50 tracking-tight flex items-baseline" style={{ transform: 'translateZ(15px)' }}>
        {value}{suffix}
      </div>
      {sublabel && <div className={`text-xs mt-1.5 ${accentColor}`} style={{ transform: 'translateZ(10px)' }}>{sublabel}</div>}
    </div>
  );
}

function PipelineFlow({ batch }: { batch: BatchStatusResponse }) {
  const comp = batch.routing_composition;
  const total = batch.record_count;
  const matched = comp.DETERMINISTIC_MATCH ?? 0;
  const aiAccepted = comp.AI_AUTO_ACCEPTED ?? 0;
  const review = comp.HUMAN_REVIEW ?? 0;
  const exception = comp.EXCEPTION ?? 0;
  const l1Pct = total > 0 ? (matched / total) * 100 : 0;
  const aiPct = total > 0 ? (aiAccepted / total) * 100 : 0;
  const reviewPct = total > 0 ? (review / total) * 100 : 0;
  const exceptionPct = total > 0 ? (exception / total) * 100 : 0;

  return (
    <div className="panel p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-semibold text-cream-100 tracking-wide">Pipeline Flow</h2>
        <span className="text-[10px] text-cream-500/50 tracking-widest uppercase">Layer 1 → Layer 2 → Layer 3 → Route</span>
      </div>
      <div className="flex items-center gap-2">
        <FlowStep label="Ingested" value={total} pct={100} color="bg-cream-400" delay={0} />
        <FlowArrow />
        <FlowStep label="L1 Matched" value={matched} pct={l1Pct} color="bg-signal-matched" delay={100} />
        <FlowArrow />
        <FlowStep label="L2 AI" value={aiAccepted} pct={aiPct} color="bg-amber-500" delay={200} />
        <FlowArrow />
        <FlowStep label="Review" value={review} pct={reviewPct} color="bg-signal-review" delay={300} />
        <FlowArrow />
        <FlowStep label="Exception" value={exception} pct={exceptionPct} color="bg-signal-exception" delay={400} />
      </div>
    </div>
  );
}

function FlowStep({ label, value, pct, color, delay }: { label: string; value: number; pct: number; color: string; delay: number }) {
  return (
    <div className="flex-1 text-center group">
      <div className="text-xs text-cream-500/70 mb-1.5 transition-colors group-hover:text-cream-300">{label}</div>
      <div className="relative h-2 rounded-full bg-ink-600 overflow-hidden mb-1.5">
        <div
          className={`absolute inset-y-0 left-0 ${color} rounded-full`}
          style={{
            width: `${pct}%`,
            transition: `width 0.8s cubic-bezier(0.22, 1, 0.36, 1) ${delay}ms`,
          }}
        />
      </div>
      <div className="mono text-sm text-cream-100 transition-colors group-hover:text-cream-50">{value}</div>
    </div>
  );
}

function FlowArrow() {
  return <div className="text-cream-500/30 text-lg shrink-0 transition-colors hover:text-amber-500/50">→</div>;
}
