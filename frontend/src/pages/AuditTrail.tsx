import { useEffect, useState } from 'react';
import { AlertTriangle, ScrollText } from 'lucide-react';
import { Topbar } from '@/components/navigation/Topbar';
import { StatusPill, RoutingReasonBadge } from '@/components/ui/StatusPill';
import { PageTransition, StaggerGroup, StaggerItem, Skeleton } from '@/components/ui/Transitions';
import { getBatchStatus, getBatchResults } from '@/api/concord';
import type { RoutingRecord } from '@/lib/types';

const LAST_BATCH_KEY = 'concord:lastBatchId';

export function AuditTrail() {
  const [records, setRecords] = useState<RoutingRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [batchId, setBatchId] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        const storedId = localStorage.getItem(LAST_BATCH_KEY);
        if (!storedId) {
          setLoading(false);
          return;
        }
        setBatchId(storedId);
        const status = await getBatchStatus(storedId);
        if (status.status !== 'completed') {
          setLoading(false);
          return;
        }
        const results = await getBatchResults(storedId);
        setRecords(results.records);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load audit trail');
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  return (
    <>
      <Topbar title="Routing Decisions" subtitle="Per-record routing outcomes from the reconciliation pipeline." />
      <PageTransition>
        <div className="flex-1 p-6 max-w-5xl">
          {loading ? (
            <div className="panel overflow-hidden">
              <div className="px-5 py-4 border-b border-amber-500/10 flex items-center justify-between">
                <Skeleton className="h-4 w-32" />
                <Skeleton className="h-4 w-20" />
              </div>
              <div className="p-5 space-y-2">
                {Array.from({ length: 8 }).map((_, i) => (
                  <div key={i} className="flex gap-3 items-center">
                    <Skeleton className="h-3 w-20" />
                    <Skeleton className="h-3 w-24" />
                    <Skeleton className="h-3 w-48" />
                    <Skeleton className="h-3 flex-1" />
                    <Skeleton className="h-3 w-16" />
                  </div>
                ))}
              </div>
            </div>
          ) : error ? (
            <div className="flex items-center justify-center py-20">
              <div className="text-center">
                <AlertTriangle className="w-8 h-8 text-signal-exception mx-auto mb-3" />
                <p className="text-cream-300 text-sm">{error}</p>
              </div>
            </div>
          ) : records.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-20">
              <ScrollText className="w-8 h-8 text-cream-500/40 mb-3" />
              <p className="text-cream-500/60 text-sm">No routing decisions yet. Upload a batch to see results.</p>
            </div>
          ) : (
            <div className="panel overflow-hidden">
              <div className="px-5 py-4 border-b border-amber-500/10 flex items-center justify-between">
                <h2 className="text-sm font-semibold text-cream-100 tracking-wide uppercase">Routing Decisions</h2>
                <span className="text-xs text-cream-500/60 mono">{records.length} records · batch {batchId?.slice(0, 8)}…</span>
              </div>
              <StaggerGroup className="p-5 mono text-xs space-y-1 max-h-[70vh] overflow-y-auto">
                {records.map((rec) => (
                  <StaggerItem key={rec.record_id}>
                    <div className="flex gap-3 py-1 hover:bg-ink-750 px-2 -mx-2 rounded transition-colors duration-150">
                      <span className="text-cream-500/50 shrink-0 w-16">{rec.source_type}</span>
                      <span className="shrink-0 w-28 text-cream-300">{rec.source_native_id}</span>
                      <span className="shrink-0 w-20 text-cream-400">₹{(rec.amount_paise / 100).toLocaleString('en-IN')}</span>
                      <span className="shrink-0 w-28">
                        <RoutingReasonBadge reason={rec.reason} />
                      </span>
                      <span className="text-cream-400 flex-1 min-w-0 truncate">
                        {rec.confidence !== null ? `conf=${rec.confidence.toFixed(2)}` : '—'}
                      </span>
                      <StatusPill status={rec.bucket} size="sm" />
                    </div>
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
