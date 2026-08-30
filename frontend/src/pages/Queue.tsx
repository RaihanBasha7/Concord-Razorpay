import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Loader2, AlertTriangle, ArrowRight, Filter } from 'lucide-react';
import { Topbar } from '@/components/navigation/Topbar';
import { StatusPill, RoutingReasonBadge } from '@/components/ui/StatusPill';
import { ConfidenceBar } from '@/components/ui/ConfidenceBar';
import { PageTransition, StaggerGroup, StaggerItem, TableRowSkeleton } from '@/components/ui/Transitions';
import { getBatchStatus, getBatchResults } from '@/api/concord';
import type { RoutingRecord, RoutingBucket } from '@/lib/types';

const LAST_BATCH_KEY = 'concord:lastBatchId';

const statusFilters: { key: RoutingBucket | 'ALL'; label: string }[] = [
  { key: 'ALL', label: 'All' },
  { key: 'DETERMINISTIC_MATCH', label: 'Matched' },
  { key: 'AI_AUTO_ACCEPTED', label: 'AI-Accepted' },
  { key: 'HUMAN_REVIEW', label: 'Needs Review' },
  { key: 'EXCEPTION', label: 'Exception' },
];

export function Queue() {
  const navigate = useNavigate();
  const [records, setRecords] = useState<RoutingRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<RoutingBucket | 'ALL'>('ALL');

  useEffect(() => {
    async function load() {
      try {
        const batchId = localStorage.getItem(LAST_BATCH_KEY);
        if (!batchId) {
          setLoading(false);
          return;
        }
        const status = await getBatchStatus(batchId);
        if (status.status !== 'completed') {
          setLoading(false);
          return;
        }
        const results = await getBatchResults(batchId);
        setRecords(results.records);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load records');
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  const filtered = filter === 'ALL' ? records : records.filter((r) => r.bucket === filter);

  return (
    <>
      <Topbar title="Reconciliation Queue" subtitle="Every record, every decision, every trace." />
      <PageTransition>
        <div className="flex-1 p-6 space-y-4">
          {/* Filters */}
          <div className="flex items-center gap-2 flex-wrap">
            <Filter className="w-4 h-4 text-cream-500/50 mr-1" />
            {statusFilters.map((f) => (
              <button
                key={f.key}
                onClick={() => setFilter(f.key)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium tracking-wide transition-all duration-200 ${
                  filter === f.key
                    ? 'bg-amber-500/15 text-amber-400 border border-amber-500/30'
                    : 'bg-ink-800 text-cream-400 border border-transparent hover:border-amber-500/15 hover:text-cream-100'
                }`}
              >
                {f.label}
              </button>
            ))}
            <span className="ml-auto text-xs text-cream-500/60 mono">{filtered.length} records</span>
          </div>

          {loading ? (
            <div className="panel overflow-hidden">
              <div className="grid grid-cols-12 gap-4 px-5 py-3 border-b border-amber-500/10 text-xs text-cream-500/60 tracking-wide uppercase">
                <div className="col-span-2">Record</div>
                <div className="col-span-2 hidden md:block">Source</div>
                <div className="col-span-1">Amount</div>
                <div className="col-span-2 hidden lg:block">Reason</div>
                <div className="col-span-2 hidden md:block">Confidence</div>
                <div className="col-span-2">Status</div>
              </div>
              <TableRowSkeleton count={6} />
            </div>
          ) : error ? (
            <div className="flex items-center justify-center py-20">
              <div className="text-center">
                <AlertTriangle className="w-8 h-8 text-signal-exception mx-auto mb-3" />
                <p className="text-cream-300 text-sm">{error}</p>
              </div>
            </div>
          ) : filtered.length === 0 ? (
            <div className="flex items-center justify-center py-20">
              <p className="text-cream-500/60 text-sm">No records match this filter.</p>
            </div>
          ) : (
            <div className="panel overflow-hidden">
              {/* Table header */}
              <div className="grid grid-cols-12 gap-4 px-5 py-3 border-b border-amber-500/10 text-xs text-cream-500/60 tracking-wide uppercase">
                <div className="col-span-2">Record</div>
                <div className="col-span-2 hidden md:block">Source</div>
                <div className="col-span-1">Amount</div>
                <div className="col-span-2 hidden lg:block">Reason</div>
                <div className="col-span-2 hidden md:block">Confidence</div>
                <div className="col-span-2">Status</div>
              </div>
              {/* Rows */}
              <StaggerGroup className="divide-y divide-amber-500/5">
                {filtered.map((rec) => (
                  <StaggerItem key={rec.record_id}>
                    <button
                      onClick={() => navigate(`/app/records/${rec.record_id}`)}
                      className="w-full grid grid-cols-12 gap-4 px-5 py-3.5 row-hover transition-colors duration-150 text-left items-center"
                    >
                      <div className="col-span-2">
                        <div className="mono text-sm text-cream-100">{rec.source_native_id}</div>
                        <div className="text-[10px] text-cream-500/50 mt-0.5">{rec.source_type}</div>
                      </div>
                      <div className="col-span-2 hidden md:block">
                        <div className="flex flex-wrap gap-1">
                          <span className="text-[10px] text-cream-500/70 bg-ink-700 px-1.5 py-0.5 rounded transition-colors hover:text-cream-300">
                            {rec.source_type}
                          </span>
                          {rec.order_id_hint && (
                            <span className="text-[10px] text-cream-500/70 bg-ink-700 px-1.5 py-0.5 rounded transition-colors hover:text-cream-300">
                              {rec.order_id_hint}
                            </span>
                          )}
                        </div>
                      </div>
                      <div className="col-span-1 text-sm text-cream-100 mono">
                        ₹{(rec.amount_paise / 100).toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                      </div>
                      <div className="col-span-2 hidden lg:block">
                        <RoutingReasonBadge reason={rec.reason} />
                      </div>
                      <div className="col-span-2 hidden md:block">
                        {rec.confidence !== null ? (
                          <ConfidenceBar confidence={rec.confidence} size="sm" />
                        ) : (
                          <span className="text-cream-500/40 text-xs">—</span>
                        )}
                      </div>
                      <div className="col-span-2 flex items-center justify-between">
                        <StatusPill status={rec.bucket} size="sm" />
                        <ArrowRight className="w-3.5 h-3.5 text-cream-500/30 transition-all duration-200 group-hover:translate-x-0.5" />
                      </div>
                    </button>
                  </StaggerItem>
                ))}
              </StaggerGroup>
            </div>
          )}
        </div>
      </PageTransition>
    </>
  );
}
