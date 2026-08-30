"""
Freeze the current on-disk evaluation dataset into a content-addressed manifest.

This script reads the dataset files in ``data/`` *as they are* and computes a
reproducible fingerprint (SHA-256 over each file + the generator seed). It does
NOT regenerate or modify the dataset CSVs / ground_truth.json — it only hashes
what is already on disk.

Run once when the dataset is to be locked as the canonical evaluation dataset:

    python scripts/freeze_dataset.py
    python scripts/freeze_dataset.py --data-dir data

If a manifest already exists, this script verifies the on-disk dataset against
it instead of overwriting, and errors out on drift so an accidental regeneration
can never silently change the frozen dataset.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reconciliation.evaluation.dataset_fingerprint import (
    freeze_or_verify_dataset,
    manifest_path,
    read_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Freeze or verify the Concord evaluation dataset manifest."
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(Path(__file__).parent.parent / "data"),
        help="Directory containing the frozen dataset files.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic generator seed the frozen dataset was produced with.",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    manifest_path_resolved = manifest_path(data_dir)

    if read_manifest(data_dir) is not None:
        existing = read_manifest(data_dir)
        print(
            f"Existing manifest found at {manifest_path_resolved} "
            f"(fingerprint {existing.fingerprint()[:12]}…, seed {existing.dataset_seed})."
        )

    try:
        manifest, status = freeze_or_verify_dataset(
            data_dir, dataset_seed=args.seed
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if status == "frozen":
        print(
            f"Froze dataset: wrote {manifest_path_resolved}\n"
            f"  fingerprint: {manifest.fingerprint()}\n"
            f"  seed:        {manifest.dataset_seed}"
        )
    else:
        print(
            f"Verified dataset against frozen manifest.\n"
            f"  fingerprint: {manifest.fingerprint()}\n"
            f"  seed:        {manifest.dataset_seed}\n"
            f"  status:      {status}"
        )

    print("\nFile hashes in manifest:")
    for name, digest in manifest.files.items():
        print(f"  {name:20s} {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
