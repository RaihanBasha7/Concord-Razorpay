import { NavLink } from 'react-router-dom';
import { LayoutDashboard, ListChecks, FlaskConical, ScrollText, Upload, ShieldCheck } from 'lucide-react';

const navItems = [
  { to: '/app', label: 'Overview', icon: LayoutDashboard, end: true },
  { to: '/app/queue', label: 'Reconciliation Queue', icon: ListChecks, end: false },
  { to: '/app/evaluation', label: 'Evaluation', icon: FlaskConical, end: false },
  { to: '/app/audit', label: 'Audit Trail', icon: ScrollText, end: false },
  { to: '/app/upload', label: 'Upload Batch', icon: Upload, end: false },
];

export function Sidebar() {
  return (
    <aside className="hidden md:flex flex-col w-64 shrink-0 bg-ink-900 border-r border-amber-500/10 h-screen sticky top-0">
      {/* Logo */}
      <div className="px-5 py-5 border-b border-amber-500/10">
        <NavLink to="/" className="flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-lg bg-amber-500/10 border border-amber-500/30 flex items-center justify-center">
            <ShieldCheck className="w-4 h-4 text-amber-500" />
          </div>
          <div>
            <div className="text-cream-50 font-semibold text-sm tracking-tight">Concord</div>
            <div className="text-cream-500/60 text-[10px] tracking-wider uppercase">Settlement Reconciliation</div>
          </div>
        </NavLink>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-3 py-4 space-y-1">
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              `nav-link ${isActive ? 'nav-link-active' : ''}`
            }
          >
            <item.icon className="w-4 h-4 shrink-0" />
            {item.label}
          </NavLink>
        ))}
      </nav>

      {/* Bottom status */}
      <div className="px-3 py-4 border-t border-amber-500/10 space-y-3">
        <div className="px-3 py-2.5 rounded-lg bg-ink-800 border border-amber-500/10 transition-all duration-200 hover:border-amber-500/20">
          <div className="flex items-center gap-2 mb-1">
            <span className="w-2 h-2 rounded-full bg-signal-matched animate-pulse-soft" />
            <span className="text-xs text-cream-300 font-medium">System Status</span>
          </div>
          <div className="text-[10px] text-cream-500/70 mono">Pipeline operational</div>
        </div>
        <div className="px-3 py-2.5 rounded-lg bg-signal-matchedDim/10 border border-signal-matched/15 transition-all duration-200 hover:border-signal-matched/30">
          <div className="flex items-center gap-2 mb-0.5">
            <span className="w-2 h-2 rounded-full bg-signal-matched" />
            <span className="text-xs text-signal-matched font-medium tracking-wide">LIVE PIPELINE</span>
          </div>
          <div className="text-[10px] text-cream-500/70">Real data · FastAPI backend</div>
        </div>
      </div>
    </aside>
  );
}
