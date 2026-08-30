interface ConfidenceBarProps {
  confidence: number;
  threshold?: number;
  showLabel?: boolean;
  size?: 'sm' | 'md';
}

export function ConfidenceBar({ confidence, threshold = 0.85, showLabel = true, size = 'md' }: ConfidenceBarProps) {
  const pct = Math.round(confidence * 100);
  const aboveThreshold = confidence >= threshold;
  const barColor = aboveThreshold ? 'bg-signal-matched' : 'bg-signal-review';
  const barHeight = size === 'sm' ? 'h-1' : 'h-1.5';

  return (
    <div className="flex items-center gap-2">
      <div className={`relative flex-1 min-w-[60px] ${barHeight} rounded-full bg-ink-600 overflow-hidden`}>
        <div
          className={`absolute inset-y-0 left-0 ${barColor} rounded-full`}
          style={{
            width: `${pct}%`,
            transition: 'width 0.6s cubic-bezier(0.22, 1, 0.36, 1)',
          }}
        />
        {threshold > 0 && (
          <div
            className="absolute inset-y-0 w-px bg-cream-300/40"
            style={{ left: `${threshold * 100}%` }}
          />
        )}
      </div>
      {showLabel && (
        <span className={`mono text-xs ${aboveThreshold ? 'text-signal-matched' : 'text-signal-review'} transition-colors duration-300`}>
          {pct}%
        </span>
      )}
    </div>
  );
}
