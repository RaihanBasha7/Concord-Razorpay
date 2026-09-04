import { Link } from 'react-router-dom';
import { motion, useScroll } from 'framer-motion';
import { ShieldCheck, ArrowRight, ArrowDown, CheckCircle2, Brain, Shield, FileCheck2 } from 'lucide-react';
import { ReconciliationOrbit } from '@/components/visual/ReconciliationOrbit';
import { useAmbientGlow } from '@/hooks/useMouseInteraction';

export function Landing() {
  const { scrollYProgress } = useScroll();

  return (
    <div className="min-h-screen bg-ink-950 text-cream-100">
      {/* Scroll progress */}
      <motion.div
        className="fixed top-0 left-0 right-0 h-0.5 bg-amber-500/60 z-50 origin-left"
        style={{ scaleX: scrollYProgress }}
      />

      {/* Navbar */}
      <Navbar />

      {/* Section 01 — Hero */}
      <HeroSection />

      {/* Section 02 — The Problem */}
      <ProblemSection />

      {/* Section 03 — How Concord Works */}
      <HowItWorksSection />

      {/* Section 04 — Product Preview */}
      <ProductPreviewSection />

      {/* Final CTA */}
      <FinalCTA />

      {/* Footer */}
      <Footer />
    </div>
  );
}

function Navbar() {
  return (
    <nav className="fixed top-1 left-0 right-0 z-40 px-6 py-4 backdrop-blur-md bg-ink-950/60 border-b border-amber-500/10">
      <div className="max-w-7xl mx-auto flex items-center justify-between">
        <Link to="/" className="flex items-center gap-2.5">
          <div className="w-7 h-7 rounded-lg bg-amber-500/10 border border-amber-500/30 flex items-center justify-center">
            <ShieldCheck className="w-3.5 h-3.5 text-amber-500" />
          </div>
          <div>
            <span className="text-cream-50 font-semibold text-sm tracking-tight">Concord</span>
            <span className="text-cream-500/50 text-[10px] ml-2 tracking-wider uppercase hidden sm:inline">Settlement Reconciliation</span>
          </div>
        </Link>

        <div className="hidden md:flex items-center gap-6 text-sm text-cream-400">
          <a href="#problem" className="hover:text-cream-100 transition-colors">Problem</a>
          <a href="#how" className="hover:text-cream-100 transition-colors">How it works</a>
          <a href="#preview" className="hover:text-cream-100 transition-colors">Evidence</a>
        </div>

        <Link to="/app" className="btn-primary text-xs px-4 py-2">
          Open Console <ArrowRight className="w-3.5 h-3.5" />
        </Link>
      </div>
    </nav>
  );
}

function HeroSection() {
  const { ref, glowRef } = useAmbientGlow<HTMLElement>();
  return (
    <section ref={ref} className="relative min-h-screen flex items-center justify-center overflow-hidden pt-20">
      {/* Ambient cursor glow */}
      <div ref={glowRef} className="ambient-glow" />
      {/* Background grid */}
      <div className="absolute inset-0 grid-bg opacity-40" />
      <div className="absolute inset-0 noise-bg" />

      <div className="relative z-10 max-w-7xl mx-auto px-6 grid grid-cols-1 lg:grid-cols-2 gap-12 items-center">
        {/* Left: copy */}
        <div className="space-y-6">
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6 }}
          >
            <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-amber-500/10 border border-amber-500/20 text-xs text-amber-400 mb-6">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse-soft" />
              Built for the Razorpay AI Buildathon
            </div>

            <h1 className="text-display font-semibold text-cream-50 tracking-tight">
              Reconciliation that's honest
              <br />
              about what it{' '}
              <span className="font-serif italic text-amber-500">can't resolve.</span>
            </h1>

            <p className="text-lg text-cream-400 mt-6 max-w-lg leading-relaxed">
              Concord matches settlement reports, bank statements, and internal ledgers —
              deterministic rules first, AI reasoning for the rest, guardrails on everything.
            </p>

            <div className="flex flex-wrap gap-3 mt-8">
              <Link to="/app" className="btn-primary">
                Open Reconciliation Console <ArrowRight className="w-4 h-4" />
              </Link>
              <a href="#problem" className="btn-secondary">
                See how Concord works <ArrowDown className="w-4 h-4" />
              </a>
            </div>
          </motion.div>

          {/* Micro-stat cards */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, delay: 0.3 }}
            className="grid grid-cols-3 gap-3 pt-4"
          >
            <MicroStat label="Layer 1 Coverage" value="35%" sublabel="FROZEN DEMO DATA" />
            <MicroStat label="Residuals to AI" value="65%" sublabel="FROZEN DEMO DATA" />
            <MicroStat label="Guardrails Active" value="3" sublabel="L3 routing rules" />
          </motion.div>
        </div>

        {/* Right: Orbit visual */}
        <motion.div
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.8, delay: 0.2 }}
          className="flex items-center justify-center"
        >
          <ReconciliationOrbit />
        </motion.div>
      </div>
    </section>
  );
}

function MicroStat({ label, value, sublabel }: { label: string; value: string; sublabel: string }) {
  return (
    <div className="panel panel-hover p-3 transition-all duration-200">
      <div className="text-[10px] text-cream-500/60 tracking-wide uppercase mb-1">{label}</div>
      <div className="text-xl font-semibold text-cream-50 mono">{value}</div>
      <div className="text-[9px] text-amber-500/60 tracking-widest uppercase mt-0.5">{sublabel}</div>
    </div>
  );
}

function ProblemSection() {
  const failureTypes = [
    { label: 'Reference-ID typo', desc: 'A single character mismatch breaks exact-match rules.' },
    { label: 'Split settlement', desc: 'One record maps to multiple partial settlements across days.' },
    { label: 'Timing mismatch', desc: 'Settlement date falls outside the matching window.' },
    { label: 'Currency rounding', desc: 'Sub-rupee rounding differences cause amount mismatches.' },
    { label: 'Duplicate entries', desc: 'The same settlement appears twice in the report.' },
  ];

  return (
    <section id="problem" className="relative py-24 px-6 border-t border-amber-500/10">
      <div className="max-w-5xl mx-auto">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.5 }}
        >
          <div className="text-xs text-amber-500/70 tracking-widest uppercase mb-3">Section 02 — The Problem</div>
          <h2 className="text-hero font-semibold text-cream-50 tracking-tight mb-4">
            A residual isn't one problem.
          </h2>
          <p className="text-lg text-cream-400 max-w-2xl mb-12">
            Same residual event, different resolution path. Deterministic rules break down at the edges —
            and that's where most reconciliation teams lose hours to spreadsheets.
          </p>
        </motion.div>

        {/* Failure types */}
        <div className="space-y-3 mb-12">
          {failureTypes.map((ft, i) => (
            <motion.div
              key={ft.label}
              initial={{ opacity: 0, x: -20 }}
              whileInView={{ opacity: 1, x: 0 }}
              viewport={{ once: true }}
              transition={{ duration: 0.4, delay: i * 0.1 }}
              className="panel panel-hover p-4 flex items-center gap-4 transition-all duration-200"
            >
              <span className="mono text-xs text-amber-500/50 w-8">0{i + 1}</span>
              <div className="flex-1">
                <div className="text-sm font-semibold text-cream-100">{ft.label}</div>
                <div className="text-xs text-cream-500/70 mt-0.5">{ft.desc}</div>
              </div>
            </motion.div>
          ))}
        </div>

        {/* Contrast: Naive vs Concord */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.5 }}
          className="grid grid-cols-1 md:grid-cols-2 gap-4"
        >
          {/* Naive */}
          <div className="rounded-xl2 border border-cream-500/10 bg-ink-900 p-5 opacity-60">
            <div className="text-xs text-cream-500/60 tracking-wide uppercase mb-3">Naive Reconciliation</div>
            <div className="flex items-center gap-2 mono text-sm text-cream-500 flex-wrap">
              <span>UNMATCHED</span>
              <ArrowRight className="w-3.5 h-3.5" />
              <span>MANUAL SPREADSHEET</span>
              <ArrowRight className="w-3.5 h-3.5" />
              <span>HOPE</span>
            </div>
          </div>

          {/* Concord */}
          <div className="rounded-xl2 border border-amber-500/30 bg-amber-500/5 p-5 shadow-glow">
            <div className="text-xs text-amber-500 tracking-wide uppercase mb-3">Concord</div>
            <div className="flex items-center gap-2 mono text-sm text-cream-100 flex-wrap">
              <span>UNMATCHED</span>
              <ArrowRight className="w-3.5 h-3.5 text-amber-500" />
              <span className="text-amber-400">REASON</span>
              <ArrowRight className="w-3.5 h-3.5 text-amber-500" />
              <span className="text-amber-400">SCORE</span>
              <ArrowRight className="w-3.5 h-3.5 text-amber-500" />
              <span className="text-amber-400">GUARD</span>
              <ArrowRight className="w-3.5 h-3.5 text-amber-500" />
              <span className="text-signal-matched">RESOLVE OR ESCALATE</span>
            </div>
          </div>
        </motion.div>
      </div>
    </section>
  );
}

function HowItWorksSection() {
  const steps = [
    {
      num: '01',
      title: 'Match',
      icon: CheckCircle2,
      desc: 'Deterministic rules resolve what they can, with no AI involved. Exact UTR matches, amount + gateway matches — fast, reliable, auditable.',
    },
    {
      num: '02',
      title: 'Reason',
      icon: Brain,
      desc: 'Residuals go to Groq-powered structured reasoning, which returns a confidence score and rationale — never a bare answer. The AI proposes; it does not decide.',
    },
    {
      num: '03',
      title: 'Guard',
      icon: Shield,
      desc: 'Every proposal, rule-based or AI, is checked against thresholds and limits before acceptance. Confidence, amount tolerance, date window, duplicate check — all enforced.',
    },
    {
      num: '04',
      title: 'Resolve',
      icon: FileCheck2,
      desc: 'Accepted matches are logged to the ledger. Everything else is routed to a human with full context — confidence score, rationale, and the exact guardrail that blocked it.',
    },
  ];

  return (
    <section id="how" className="relative py-24 px-6 border-t border-amber-500/10">
      <div className="max-w-5xl mx-auto">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.5 }}
        >
          <div className="text-xs text-amber-500/70 tracking-widest uppercase mb-3">Section 03 — How Concord Works</div>
          <h2 className="text-hero font-semibold text-cream-50 tracking-tight mb-4">
            Every match has a reason.
          </h2>
        </motion.div>

        <div className="space-y-4 mt-12">
          {steps.map((step, i) => (
            <motion.div
              key={step.num}
              initial={{ opacity: 0, x: -20 }}
              whileInView={{ opacity: 1, x: 0 }}
              viewport={{ once: true }}
              transition={{ duration: 0.4, delay: i * 0.1 }}
              className="panel panel-hover p-5 flex items-start gap-5 transition-all duration-200"
            >
              <div className="w-12 h-12 rounded-xl2 bg-amber-500/10 border border-amber-500/20 flex items-center justify-center shrink-0">
                <step.icon className="w-5 h-5 text-amber-500" />
              </div>
              <div className="flex-1">
                <div className="flex items-center gap-3 mb-2">
                  <span className="mono text-xs text-cream-500/50">{step.num}</span>
                  <h3 className="text-lg font-semibold text-cream-50">{step.title}</h3>
                </div>
                <p className="text-sm text-cream-400 leading-relaxed">{step.desc}</p>
              </div>
            </motion.div>
          ))}
        </div>

        {/* Bottom line */}
        <motion.div
          initial={{ opacity: 0 }}
          whileInView={{ opacity: 1 }}
          viewport={{ once: true }}
          transition={{ duration: 0.5, delay: 0.3 }}
          className="mt-12 text-center"
        >
          <p className="text-xl text-cream-100 font-medium">
            AI where judgment helps.{' '}
            <span className="text-amber-500">Rules where the ledger is concerned.</span>
          </p>
        </motion.div>
      </div>
    </section>
  );
}

function ProductPreviewSection() {
  const previewRows = [
    { nativeId: 'STL-001', amount: '₹49,999', source: 'SETTLEMENT', bucket: 'DETERMINISTIC_MATCH', confidence: null },
    { nativeId: 'BNK-003', amount: '₹14,950', source: 'BANK', bucket: 'DETERMINISTIC_MATCH', confidence: null },
    { nativeId: 'ORD-1012', amount: '₹49,999', source: 'LEDGER', bucket: 'DETERMINISTIC_MATCH', confidence: null },
    { nativeId: 'STL-007', amount: '₹34,950', source: 'SETTLEMENT', bucket: 'HUMAN_REVIEW', confidence: 0.72 },
    { nativeId: 'BNK-011', amount: '₹4,999', source: 'BANK', bucket: 'EXCEPTION', confidence: null },
    { nativeId: 'ORD-1020', amount: '₹67,500', source: 'LEDGER', bucket: 'DETERMINISTIC_MATCH', confidence: null },
  ];

  return (
    <section id="preview" className="relative py-24 px-6 border-t border-amber-500/10">
      <div className="max-w-5xl mx-auto">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.5 }}
        >
          <div className="text-xs text-amber-500/70 tracking-widest uppercase mb-3">Section 04 — Product Preview</div>
          <h2 className="text-hero font-semibold text-cream-50 tracking-tight mb-4">
            Meet the Reconciliation Command Center.
          </h2>
          <p className="text-lg text-cream-400 max-w-2xl mb-8">
            One queue. Every decision explained.
          </p>
        </motion.div>

        {/* Preview panel */}
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6 }}
          className="panel-elevated overflow-hidden shadow-glow"
        >
          {/* Preview header */}
          <div className="px-5 py-4 border-b border-amber-500/10 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <div className="w-2.5 h-2.5 rounded-full bg-signal-exception/60" />
              <div className="w-2.5 h-2.5 rounded-full bg-signal-review/60" />
              <div className="w-2.5 h-2.5 rounded-full bg-signal-matched/60" />
            </div>
            <span className="text-[10px] text-amber-500/70 tracking-widest uppercase">ILLUSTRATIVE EXAMPLE — REAL DATA IN CONSOLE</span>
          </div>

          {/* Metrics row */}
          <div className="grid grid-cols-4 gap-3 p-5 border-b border-amber-500/10">
            <PreviewMetric label="Total" value="12" />
            <PreviewMetric label="Matched" value="4" color="text-signal-matched" />
            <PreviewMetric label="Review" value="1" color="text-amber-500" />
            <PreviewMetric label="Exceptions" value="1" color="text-signal-exception" />
          </div>

          {/* Table */}
          <div className="grid grid-cols-12 gap-3 px-5 py-2.5 border-b border-amber-500/10 text-[10px] text-cream-500/50 tracking-wide uppercase">
            <div className="col-span-2">Record</div>
            <div className="col-span-2">Amount</div>
            <div className="col-span-2">Source</div>
            <div className="col-span-2">Confidence</div>
            <div className="col-span-4">Routing</div>
          </div>
          <div className="divide-y divide-amber-500/5">
            {previewRows.map((row) => (
              <div key={row.nativeId} className="grid grid-cols-12 gap-3 px-5 py-2.5 items-center text-sm">
                <div className="col-span-2 mono text-xs text-cream-300">{row.nativeId}</div>
                <div className="col-span-2 mono text-xs text-cream-100">{row.amount}</div>
                <div className="col-span-2">
                  <span className="pill text-[9px] badge-rule">{row.source}</span>
                </div>
                <div className="col-span-2">
                  {row.confidence !== null ? (
                    <span className="mono text-xs text-amber-400">{Math.round(row.confidence * 100)}%</span>
                  ) : (
                    <span className="text-cream-500/30 text-xs">—</span>
                  )}
                </div>
                <div className="col-span-4">
                  <span className={`pill text-[9px] ${
                    row.bucket === 'DETERMINISTIC_MATCH' ? 'pill-matched' :
                    row.bucket === 'HUMAN_REVIEW' ? 'pill-review' : 'pill-exception'
                  }`}>{row.bucket.replace(/_/g, ' ')}</span>
                </div>
              </div>
            ))}
          </div>
        </motion.div>

        <div className="text-center mt-6">
          <Link to="/app" className="btn-primary">
            Open Live Console <ArrowRight className="w-4 h-4" />
          </Link>
        </div>
      </div>
    </section>
  );
}

function PreviewMetric({ label, value, color = 'text-cream-100' }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div className="text-[10px] text-cream-500/60 tracking-wide uppercase mb-1">{label}</div>
      <div className={`text-xl font-semibold mono ${color}`}>{value}</div>
    </div>
  );
}

function FinalCTA() {
  return (
    <section className="relative py-24 px-6 border-t border-amber-500/10 overflow-hidden">
      <div className="absolute inset-0 grid-bg opacity-30" />
      <div className="absolute inset-0 noise-bg" />

      <div className="relative z-10 max-w-3xl mx-auto text-center">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6 }}
        >
          <h2 className="text-hero font-semibold text-cream-50 tracking-tight mb-4">
            Resolve what the spreadsheet couldn't.
          </h2>
          <p className="text-lg text-cream-400 mb-8 max-w-xl mx-auto">
            Concord turns unresolved settlement residuals into bounded, explainable, auditable decisions.
          </p>
          <Link to="/app" className="btn-primary text-base px-6 py-3">
            Open Reconciliation Console <ArrowRight className="w-5 h-5" />
          </Link>
          <p className="text-xs text-cream-500/50 mt-4">
            Synthetic evaluation data available · Built for the Razorpay AI Buildathon
          </p>
        </motion.div>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer className="border-t border-amber-500/10 px-6 py-8">
      <div className="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-2.5">
          <div className="w-6 h-6 rounded-lg bg-amber-500/10 border border-amber-500/20 flex items-center justify-center">
            <ShieldCheck className="w-3 h-3 text-amber-500" />
          </div>
          <span className="text-sm text-cream-400">Concord — Settlement Reconciliation Infrastructure</span>
        </div>
        <div className="flex items-center gap-5 text-xs text-cream-500/60">
          <Link to="/" className="hover:text-cream-100 transition-colors">Product</Link>
          <Link to="/app" className="hover:text-cream-100 transition-colors">Console</Link>
          <Link to="/app/evaluation" className="hover:text-cream-100 transition-colors">Evaluation</Link>
          <Link to="/app/audit" className="hover:text-cream-100 transition-colors">Audit Trail</Link>
        </div>
      </div>
    </footer>
  );
}
