"""
SQLite-backed batch persistence for the Concord API.

Uses the simplest persistence approach for MVP: one batches table for
metadata, one routing_decisions table for per-record routing (needed for
bucket-filtered retrieval), and one batch_json table for Layer 1, Layer 2,
and evaluation artifacts keyed by name.

Layer 1 and Layer 2 artifacts are stored in separate JSON rows so each
layer remains independently inspectable.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class BatchStore:
    """SQLite-backed batch persistence for the Concord API."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'processing',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                error_message TEXT,
                record_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS routing_decisions (
                batch_id TEXT NOT NULL,
                record_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_native_id TEXT NOT NULL,
                order_id_hint TEXT,
                amount_paise INTEGER NOT NULL,
                record_date TEXT NOT NULL,
                narration TEXT,
                bucket TEXT NOT NULL,
                reason TEXT NOT NULL,
                confidence REAL,
                source_decision_id TEXT,
                source_outcome TEXT,
                PRIMARY KEY (batch_id, record_id)
            );

            CREATE TABLE IF NOT EXISTS batch_json (
                batch_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                PRIMARY KEY (batch_id, key)
            );

            CREATE TABLE IF NOT EXISTS audit_records (
                batch_id TEXT NOT NULL,
                record_id TEXT NOT NULL,
                audit_json TEXT NOT NULL,
                PRIMARY KEY (batch_id, record_id)
            );
            """
        )

        # Migration: add the content-fingerprint column to batches for
        # older databases created before batch idempotency.
        columns = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(batches)").fetchall()
        }
        if "fingerprint" not in columns:
            self._conn.execute("ALTER TABLE batches ADD COLUMN fingerprint TEXT")

        # DB-enforced uniqueness on the batch fingerprint: the same
        # three-file content can therefore never produce two logical
        # batches, even under concurrent requests. NULL fingerprints
        # (legacy batches) are exempt via the partial index.
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_batches_fingerprint "
            "ON batches(fingerprint) WHERE fingerprint IS NOT NULL"
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Batch lifecycle
    # ------------------------------------------------------------------

    def create_batch(self, fingerprint: str | None = None) -> str:
        """Create a new batch in 'processing' state. Returns the batch ID.

        ``fingerprint`` is the deterministic content hash of the three-file
        batch; a unique partial index prevents duplicate batches with the
        same fingerprint (raises ``sqlite3.IntegrityError`` on conflict).
        """
        batch_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO batches (id, status, created_at, record_count, fingerprint) "
            "VALUES (?, 'processing', ?, 0, ?)",
            (batch_id, now, fingerprint),
        )
        self._conn.commit()
        return batch_id

    def get_batch_by_fingerprint(
        self, fingerprint: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a batch by its content fingerprint, or None."""
        row = self._conn.execute(
            "SELECT * FROM batches WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        return dict(row) if row else None

    def reset_failed_batch(self, batch_id: str) -> None:
        """Re-arm a previously failed batch for a retry of the same upload.

        Keeps the same row (and fingerprint) so the unique index remains
        the single source of truth; clears error state and artifact residue
        from the failed attempt.
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE batches SET status='processing', completed_at=NULL, "
            "error_message=NULL WHERE id=?",
            (batch_id,),
        )
        self._conn.execute("DELETE FROM routing_decisions WHERE batch_id=?", (batch_id,))
        self._conn.execute("DELETE FROM batch_json WHERE batch_id=?", (batch_id,))
        self._conn.execute("DELETE FROM audit_records WHERE batch_id=?", (batch_id,))
        # Keep created_at so the retry preserves the original attempt time.
        self._conn.commit()

    def complete_batch(self, batch_id: str, record_count: int) -> None:
        """Mark a batch as completed with the total record count."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE batches SET status='completed', completed_at=?, record_count=? WHERE id=?",
            (now, record_count, batch_id),
        )
        self._conn.commit()

    def fail_batch(self, batch_id: str, error_message: str) -> None:
        """Mark a batch as failed with an error message."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE batches SET status='failed', completed_at=?, error_message=? WHERE id=?",
            (now, error_message, batch_id),
        )
        self._conn.commit()

    def get_batch(self, batch_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve batch metadata by ID, or None if not found."""
        row = self._conn.execute(
            "SELECT * FROM batches WHERE id=?", (batch_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_summary(self) -> Dict[str, Any]:
        """Aggregate statistics across all persisted completed batches.

        This is the backend source of truth for the dashboard: totals are
        reconstructed from the database on every call, so they survive
        refreshes and backend restarts.  Failed/processing batches are
        excluded from aggregates.
        """
        total_records = self._conn.execute(
            "SELECT COALESCE(SUM(record_count), 0) FROM batches "
            "WHERE status='completed'"
        ).fetchone()[0]

        batch_count = self._conn.execute(
            "SELECT COUNT(*) FROM batches WHERE status='completed'"
        ).fetchone()[0]

        latest = self._conn.execute(
            "SELECT id FROM batches WHERE status='completed' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()

        composition_rows = self._conn.execute(
            "SELECT r.bucket, COUNT(*) AS count FROM routing_decisions r "
            "JOIN batches b ON b.id = r.batch_id "
            "WHERE b.status='completed' GROUP BY r.bucket"
        ).fetchall()

        return {
            "total_record_count": total_records,
            "batch_count": batch_count,
            "routing_composition": {
                r["bucket"]: r["count"] for r in composition_rows
            },
            "latest_batch_id": latest["id"] if latest else None,
        }

    # ------------------------------------------------------------------
    # Routing decisions
    # ------------------------------------------------------------------

    def store_routing_decisions(
        self, batch_id: str, decisions: List[Dict[str, Any]]
    ) -> None:
        """Persist enriched routing decisions (record details + routing)."""
        self._conn.executemany(
            """INSERT INTO routing_decisions
               (batch_id, record_id, source_type, source_native_id, order_id_hint,
                amount_paise, record_date, narration, bucket, reason, confidence,
                source_decision_id, source_outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    batch_id,
                    d["record_id"],
                    d["source_type"],
                    d["source_native_id"],
                    d.get("order_id_hint"),
                    d["amount_paise"],
                    d["date"],
                    d.get("narration"),
                    d["bucket"],
                    d["reason"],
                    d.get("confidence"),
                    d.get("source_decision_id"),
                    d.get("source_outcome"),
                )
                for d in decisions
            ],
        )
        self._conn.commit()

    def get_routing_decisions(
        self, batch_id: str, bucket: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Retrieve routing decisions, optionally filtered by bucket."""
        if bucket:
            rows = self._conn.execute(
                "SELECT * FROM routing_decisions WHERE batch_id=? AND bucket=?",
                (batch_id, bucket),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM routing_decisions WHERE batch_id=?",
                (batch_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_routing_composition(self, batch_id: str) -> Dict[str, int]:
        """Return a count of routing decisions per bucket."""
        rows = self._conn.execute(
            "SELECT bucket, COUNT(*) as count FROM routing_decisions "
            "WHERE batch_id=? GROUP BY bucket",
            (batch_id,),
        ).fetchall()
        return {r["bucket"]: r["count"] for r in rows}

    # ------------------------------------------------------------------
    # Audit records
    # ------------------------------------------------------------------

    def store_audit_records(
        self, batch_id: str, audit_records: List[Dict[str, Any]]
    ) -> None:
        """Persist structured per-record audit entries."""
        self._conn.executemany(
            "INSERT INTO audit_records (batch_id, record_id, audit_json) VALUES (?, ?, ?)",
            [
                (batch_id, a["record_id"], json.dumps(a))
                for a in audit_records
            ],
        )
        self._conn.commit()

    def get_audit_record(
        self, batch_id: str, record_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve the structured audit record for a single record."""
        row = self._conn.execute(
            "SELECT audit_json FROM audit_records WHERE batch_id=? AND record_id=?",
            (batch_id, record_id),
        ).fetchone()
        return json.loads(row["audit_json"]) if row else None

    # ------------------------------------------------------------------
    # JSON artifacts (Layer 1, Layer 2, eval)
    # ------------------------------------------------------------------

    def store_json(self, batch_id: str, key: str, value: Any) -> None:
        """Store a JSON-serializable artifact under (batch_id, key)."""
        self._conn.execute(
            "INSERT OR REPLACE INTO batch_json (batch_id, key, value_json) VALUES (?, ?, ?)",
            (batch_id, key, json.dumps(value)),
        )
        self._conn.commit()

    def get_json(self, batch_id: str, key: str) -> Optional[Any]:
        """Retrieve a JSON artifact, or None if not found."""
        row = self._conn.execute(
            "SELECT value_json FROM batch_json WHERE batch_id=? AND key=?",
            (batch_id, key),
        ).fetchone()
        return json.loads(row["value_json"]) if row else None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
