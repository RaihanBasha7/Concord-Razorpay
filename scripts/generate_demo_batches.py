"""
Generate the two additional demo batch sets (Batch 2 & Batch 3) used by the
Concord upload flow's "Load Demo Data" panel.

Each batch is a set of three CSV files (settlement, bank, ledger) produced by
the SAME synthetic dataset generator as the canonical evaluation dataset, so
the schema, amounts, dates, identifiers, and edge-case mix are consistent with
the deployed upload flow.

DEMO DATA ONLY:
  * Distinct deterministic seeds ("demo_batch_2", "demo_batch_3") — every ID,
    amount, and date differs from the canonical dataset (seed 42) and from the
    other demo batch.
  * These files are never added to the frozen evaluation dataset, ground
    truth, residuals, Layer 2 artifacts, or the dataset manifest.

Output: frontend/public/fixtures/{settlement,bank,ledger}-{2,3}.csv
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from reconciliation.api.pipeline import read_csv_content
from reconciliation.domain.models import SourceType
from reconciliation.evaluation.dataset_generator import generate_dataset

FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent / "frontend" / "public" / "fixtures"
)

DEMO_BATCHES = [
    {
        "seed": "demo_batch_2",
        "files": ("settlement-2.csv", "bank-2.csv", "ledger-2.csv"),
    },
    {
        "seed": "demo_batch_3",
        "files": ("settlement-3.csv", "bank-3.csv", "ledger-3.csv"),
    },
]


def _write_csv(rows, path: Path) -> None:
    """Write rows with the same header convention as the canonical generator."""
    if not rows:
        path.write_text("")
        return
    fieldnames = list(rows[0].keys())
    # LF line endings: the canonical dataset CSVs (and the gitattributes
    # normalization) use LF, so keep demo files byte-convention consistent.
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for spec in DEMO_BATCHES:
        seed = spec["seed"]
        s_name, b_name, l_name = spec["files"]
        dataset = generate_dataset(seed=seed)

        _write_csv(dataset.settlement_rows, FIXTURE_DIR / s_name)
        _write_csv(dataset.bank_rows, FIXTURE_DIR / b_name)
        _write_csv(dataset.ledger_rows, FIXTURE_DIR / l_name)

        # Self-check: every generated file must be a valid input to the
        # deployed API pipeline (parseable + required columns present).
        contents = (
            (FIXTURE_DIR / s_name).read_bytes(),
            (FIXTURE_DIR / b_name).read_bytes(),
            (FIXTURE_DIR / l_name).read_bytes(),
        )
        for content, source_type in zip(
            contents, (SourceType.SETTLEMENT, SourceType.BANK, SourceType.LEDGER)
        ):
            read_csv_content(content, source_type)

        total = (
            len(dataset.settlement_rows)
            + len(dataset.bank_rows)
            + len(dataset.ledger_rows)
        )
        print(
            f"{seed}: {len(dataset.settlement_rows)} settlement + "
            f"{len(dataset.bank_rows)} bank + {len(dataset.ledger_rows)} ledger "
            f"= {total} records -> {s_name} / {b_name} / {l_name}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())