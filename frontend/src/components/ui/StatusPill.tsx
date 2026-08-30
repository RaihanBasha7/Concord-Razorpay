import type { RoutingBucket, RoutingReason } from '@/lib/types';

interface StatusPillProps {
  status: RoutingBucket;
  size?: 'sm' | 'md';
}

const statusConfig: Record<RoutingBucket, { label: string; className: string; dot: string }> = {
  DETERMINISTIC_MATCH: { label: 'MATCHED', className: 'pill-matched', dot: 'bg-signal-matched' },
  AI_AUTO_ACCEPTED: { label: 'AI-ACCEPTED', className: 'pill-matched', dot: 'bg-signal-matched' },
  HUMAN_REVIEW: { label: 'REVIEW', className: 'pill-review', dot: 'bg-signal-review' },
  EXCEPTION: { label: 'EXCEPTION', className: 'pill-exception', dot: 'bg-signal-exception' },
};

export function StatusPill({ status, size = 'md' }: StatusPillProps) {
  const cfg = statusConfig[status];
  return (
    <span className={`${cfg.className} ${size === 'sm' ? 'px-2 py-0.5 text-[10px]' : ''} transition-all duration-200`}>
      <span className={`w-1.5 h-1.5 rounded-full ${cfg.dot} transition-all duration-200`} />
      {cfg.label}
    </span>
  );
}

const reasonConfig: Record<RoutingReason, { label: string; className: string }> = {
  LAYER1_DETERMINISTIC: { label: 'RULE', className: 'badge-rule' },
  AI_CONFIDENT: { label: 'AI', className: 'badge-ai' },
  AI_NEEDS_REVIEW: { label: 'AI → REVIEW', className: 'badge-ai' },
  LOW_CONFIDENCE: { label: 'LOW CONF', className: 'badge-ai' },
  AI_RESPONSE_INVALID: { label: 'AI INVALID', className: 'badge-ai' },
  NO_CANDIDATE: { label: 'NO MATCH', className: 'badge-rule' },
};

export function RoutingReasonBadge({ reason }: { reason: RoutingReason }) {
  const cfg = reasonConfig[reason];
  const isAI = reason !== 'LAYER1_DETERMINISTIC' && reason !== 'NO_CANDIDATE';
  return (
    <span className={`${cfg.className} transition-all duration-200`}>
      {isAI && (
        <span className="w-1 h-1 rounded-full bg-amber-400 mr-1 animate-pulse-soft" />
      )}
      {cfg.label}
    </span>
  );
}
