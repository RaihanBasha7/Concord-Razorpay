import { useState } from 'react';
import { Search, Bell, Info, X } from 'lucide-react';

interface TopbarProps {
  title: string;
  subtitle: string;
}

export function Topbar({ title, subtitle }: TopbarProps) {
  const [demoOpen, setDemoOpen] = useState(false);

  return (
    <header className="sticky top-0 z-30 bg-ink-950/80 backdrop-blur-md border-b border-amber-500/10">
      <div className="px-6 py-4 flex items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-cream-50 tracking-tight">{title}</h1>
          <p className="text-sm text-cream-500/70 mt-0.5">{subtitle}</p>
        </div>

        <div className="flex items-center gap-3">
          {/* Search */}
          <div className="hidden lg:flex items-center gap-2 px-3 py-2 rounded-lg bg-ink-800 border border-amber-500/10 w-64">
            <Search className="w-3.5 h-3.5 text-cream-500/50" />
            <input
              type="text"
              placeholder="Search records, UTRs..."
              className="bg-transparent text-sm text-cream-100 placeholder:text-cream-500/40 focus:outline-none flex-1"
            />
          </div>

          {/* Notifications */}
          <button className="relative p-2 rounded-lg text-cream-400 hover:text-cream-100 hover:bg-ink-750 transition-colors">
            <Bell className="w-4 h-4" />
            <span className="absolute top-1.5 right-1.5 w-1.5 h-1.5 rounded-full bg-amber-500" />
          </button>

          {/* Pipeline status indicator */}
          <button
            onClick={() => setDemoOpen(!demoOpen)}
            className="flex items-center gap-2 px-3 py-2 rounded-lg bg-signal-matchedDim/10 border border-signal-matched/20 text-signal-matched text-xs font-medium hover:bg-signal-matchedDim/20 transition-colors"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-signal-matched animate-pulse-soft" />
            LIVE PIPELINE
          </button>
        </div>
      </div>

      {/* Demo panel */}
      {demoOpen && (
        <div className="absolute top-full right-6 mt-2 w-80 panel-elevated p-4 shadow-xl z-40 animate-fade-in">
          <div className="flex items-start justify-between mb-3">
            <div className="flex items-center gap-2">
              <Info className="w-4 h-4 text-signal-matched" />
              <span className="text-sm font-medium text-cream-100">Pipeline Info</span>
            </div>
            <button onClick={() => setDemoOpen(false)} className="text-cream-500 hover:text-cream-100">
              <X className="w-4 h-4" />
            </button>
          </div>
          <ul className="space-y-2 text-xs text-cream-400">
            <li className="flex gap-2">
              <span className="text-amber-500">·</span>
              All data comes from the live FastAPI pipeline
            </li>
            <li className="flex gap-2">
              <span className="text-amber-500">·</span>
              Landing page uses illustrative examples, not live data
            </li>
            <li className="flex gap-2">
              <span className="text-amber-500">·</span>
              Every routing decision is traceable per record
            </li>
          </ul>
        </div>
      )}
    </header>
  );
}
