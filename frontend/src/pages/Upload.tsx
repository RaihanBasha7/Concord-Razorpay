import { useState, useRef, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  UploadCloud,
  Loader2,
  CheckCircle2,
  AlertTriangle,
  FileText,
  ArrowRight,
  File,
  Download,
  X,
} from 'lucide-react';
import { Topbar } from '@/components/navigation/Topbar';
import { PageTransition } from '@/components/ui/Transitions';
import { useAmbientGlow } from '@/hooks/useMouseInteraction';
import { uploadBatch } from '@/api/concord';

type Phase = 'idle' | 'uploading' | 'done' | 'error';

interface UploadResult {
  batchId: string;
  total: number;
}

// ─── Helpers ─────────────────────────────────────────────────────────

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Convert Blob to File, working around @types/node shadowing the DOM File constructor. */
function blobToFile(blob: Blob, name: string): File {
  const f = new Blob([blob], { type: blob.type }) as File;
  Object.defineProperty(f, 'name', { value: name, writable: false });
  return f;
}

// ─── Upload Page ─────────────────────────────────────────────────────

export function Upload() {
  const navigate = useNavigate();
  const [phase, setPhase] = useState<Phase>('idle');
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [loadingDemo, setLoadingDemo] = useState(false);
  const { ref: uploadRef, glowRef } = useAmbientGlow<HTMLDivElement>();

  const [settlementFile, setSettlementFile] = useState<File | null>(null);
  const [bankFile, setBankFile] = useState<File | null>(null);
  const [ledgerFile, setLedgerFile] = useState<File | null>(null);

  const allSelected = settlementFile !== null && bankFile !== null && ledgerFile !== null;

  // Auto-navigate to dashboard 2s after success
  useEffect(() => {
    if (phase !== 'done') return;
    const timer = window.setTimeout(() => navigate('/app'), 2000);
    return () => clearTimeout(timer);
  }, [phase, navigate]);

  const handleUpload = useCallback(async () => {
    if (!settlementFile || !bankFile || !ledgerFile) return;

    setPhase('uploading');
    setError(null);
    try {
      const res = await uploadBatch(settlementFile, bankFile, ledgerFile);
      localStorage.setItem('concord:lastBatchId', res.batch_id);
      setResult({ batchId: res.batch_id, total: res.record_count });
      setPhase('done');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed');
      setPhase('error');
    }
  }, [settlementFile, bankFile, ledgerFile]);

  async function handleLoadDemo() {
    setLoadingDemo(true);
    try {
      const [sRes, bRes, lRes] = await Promise.all([
        fetch('/fixtures/settlement.csv'),
        fetch('/fixtures/bank.csv'),
        fetch('/fixtures/ledger.csv'),
      ]);
      if (!sRes.ok || !bRes.ok || !lRes.ok) {
        throw new Error('Failed to load demo fixtures');
      }
      const [sBlob, bBlob, lBlob] = await Promise.all([
        sRes.blob(),
        bRes.blob(),
        lRes.blob(),
      ]);
      setSettlementFile(blobToFile(sBlob, 'settlement.csv'));
      setBankFile(blobToFile(bBlob, 'bank.csv'));
      setLedgerFile(blobToFile(lBlob, 'ledger.csv'));
    } catch {
      setError('Could not load demo fixtures. Make sure the dev server is running.');
      setPhase('error');
    } finally {
      setLoadingDemo(false);
    }
  }

  return (
    <>
      <Topbar
        title="Upload Batch"
        subtitle="Submit settlement, bank, and ledger CSV files for reconciliation."
      />
      <PageTransition>
        <div className="flex-1 p-6 max-w-3xl space-y-6">
          {/* ── Upload form ──────────────────────────────────── */}
          {phase === 'idle' && (
            <div className="space-y-4">
              <div className="panel panel-hover p-5 space-y-5">
                <div className="flex items-center gap-2 mb-1">
                  <FileText className="w-4 h-4 text-amber-500" />
                  <h2 className="text-sm font-semibold text-cream-100 tracking-wide">
                    Upload CSV Files
                  </h2>
                </div>
                <p className="text-xs text-cream-500/70 leading-relaxed">
                  Upload three CSV files — one per source type. The backend normalises,
                  matches, routes, and returns results in a single synchronous call.
                </p>

                <div className="grid grid-cols-1 gap-3">
                  <FileInput
                    file={settlementFile}
                    onFileChange={setSettlementFile}
                    label="Settlement CSV"
                    description="Columns: settlement_id, gross_amount, settlement_date"
                  />
                  <FileInput
                    file={bankFile}
                    onFileChange={setBankFile}
                    label="Bank CSV"
                    description="Columns: bank_utr, credit_amount, value_date"
                  />
                  <FileInput
                    file={ledgerFile}
                    onFileChange={setLedgerFile}
                    label="Ledger CSV"
                    description="Columns: order_id, gross_amount, transaction_date"
                  />
                </div>

                <button
                  onClick={handleUpload}
                  disabled={!allSelected}
                  className="btn-primary w-full disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <UploadCloud className="w-4 h-4" />
                  Upload &amp; Reconcile
                </button>
              </div>

              {/* Demo data */}
              <div className="panel p-4 flex items-center justify-between">
                <div>
                  <div className="text-xs text-cream-100 font-medium">No CSVs handy?</div>
                  <div className="text-[10px] text-cream-500/60 mt-0.5">
                    Load 5-record demo fixtures to try the pipeline.
                  </div>
                </div>
                <button
                  onClick={handleLoadDemo}
                  disabled={loadingDemo}
                  className="btn-ghost text-xs"
                >
                  {loadingDemo ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  ) : (
                    <Download className="w-3.5 h-3.5" />
                  )}
                  Load Demo Data
                </button>
              </div>
            </div>
          )}

          {/* ── Uploading ────────────────────────────────────── */}
          {phase === 'uploading' && (
            <div
              ref={uploadRef}
              className="panel p-8 flex flex-col items-center justify-center relative overflow-hidden"
            >
              <div ref={glowRef} className="ambient-glow" />
              <div className="relative z-10 flex flex-col items-center">
                <Loader2 className="w-8 h-8 text-amber-500 animate-spin mb-4" />
                <h3 className="text-sm font-semibold text-cream-100 mb-1">
                  Uploading and processing…
                </h3>
                <p className="text-xs text-cream-500/70">
                  Normalising → Layer 1 matching → Layer 3 routing
                </p>
              </div>
            </div>
          )}

          {/* ── Done ─────────────────────────────────────────── */}
          {phase === 'done' && result && (
            <motion.div
              initial={{ opacity: 0, scale: 0.97 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
              className="panel p-8 flex flex-col items-center justify-center"
            >
              <motion.div
                initial={{ scale: 0 }}
                animate={{ scale: 1 }}
                transition={{
                  delay: 0.1,
                  type: 'spring',
                  stiffness: 200,
                  damping: 15,
                }}
              >
                <CheckCircle2 className="w-10 h-10 text-signal-matched mb-4" />
              </motion.div>
              <h3 className="text-sm font-semibold text-cream-100 mb-1">
                Batch processed successfully
              </h3>
              <p className="text-xs text-cream-500/70 mb-1">
                {result.total} records reconciled through the pipeline.
              </p>
              <p className="text-[10px] text-cream-500/50 mono mb-4">
                batch {result.batchId}
              </p>
              <div className="flex gap-3">
                <button onClick={() => navigate('/app')} className="btn-primary">
                  View Dashboard <ArrowRight className="w-4 h-4" />
                </button>
                <button
                  onClick={() => navigate('/app/queue')}
                  className="btn-secondary"
                >
                  Open Queue
                </button>
              </div>
            </motion.div>
          )}

          {/* ── Error ────────────────────────────────────────── */}
          {phase === 'error' && error && (
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.3 }}
              className="panel p-8 flex flex-col items-center justify-center border-signal-exception/20"
            >
              <AlertTriangle className="w-8 h-8 text-signal-exception mb-4" />
              <h3 className="text-sm font-semibold text-cream-100 mb-1">
                Upload failed
              </h3>
              <p className="text-xs text-cream-500/70 mb-4 text-center max-w-md">
                {error}
              </p>
              <button
                onClick={() => {
                  setPhase('idle');
                  setError(null);
                }}
                className="btn-secondary"
              >
                Try Again
              </button>
            </motion.div>
          )}
        </div>
      </PageTransition>
    </>
  );
}

// ─── File Input ──────────────────────────────────────────────────────

interface FileInputProps {
  label: string;
  description: string;
  file: File | null;
  onFileChange: (file: File | null) => void;
}

const FileInput = ({
  label,
  description,
  file,
  onFileChange,
}: FileInputProps) => {
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <div
      className={`relative rounded-lg border p-4 transition-all ${
        file
          ? 'border-signal-matched/30 bg-signal-matchedDim/5'
          : 'border-amber-500/15 bg-ink-900 hover:border-amber-500/30'
      }`}
    >
      <div className="flex items-center gap-3">
        {file ? (
          <CheckCircle2 className="w-5 h-5 text-signal-matched shrink-0" />
        ) : (
          <File className="w-5 h-5 text-amber-500/70 shrink-0" />
        )}
        <div className="flex-1 min-w-0">
          <div className="text-sm text-cream-100 font-medium">{label}</div>
          <div className="text-[10px] text-cream-500/60 mt-0.5">{description}</div>
        </div>
        {file && (
          <>
            <span className="text-xs text-signal-matched mono truncate max-w-[140px]">
              {file.name}
            </span>
            <span className="text-[10px] text-cream-500/50 mono shrink-0">
              {formatBytes(file.size)}
            </span>
            <button
              onClick={(e) => {
                e.stopPropagation();
                onFileChange(null);
                if (inputRef.current) inputRef.current.value = '';
              }}
              className="w-5 h-5 rounded flex items-center justify-center text-cream-500/50 hover:text-cream-100 hover:bg-ink-700 transition-colors shrink-0"
              aria-label={`Remove ${label}`}
            >
              <X className="w-3 h-3" />
            </button>
          </>
        )}
      </div>
      <input
        ref={inputRef}
        type="file"
        accept=".csv"
        onChange={(e) => onFileChange(e.target.files?.[0] ?? null)}
        className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
      />
    </div>
  );
};
