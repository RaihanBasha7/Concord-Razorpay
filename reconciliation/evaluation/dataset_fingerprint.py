"""
Dataset fingerprint / manifest for the Concord evaluation dataset.

The synthetic evaluation dataset (ground_truth.json + the source CSVs +
residuals.csv) is the single source of truth for all Layer 2 / full-pipeline
metrics. Without provenance, a downstream Layer 2 artifact (e.g. day4_audit.jsonl)
cannot be trusted to correspond to the dataset currently on disk.

This module provides a lightweight, reproducible, content-addressed manifest so
that:

  * the current on-disk dataset can be "frozen" into a manifest (a snapshot of
    SHA-256 hashes over the dataset files plus the deterministic generator
    seed), and
  * evaluation artifacts can record which dataset version they were produced
    from and later be verified to match.

No external dependencies. The manifest is plain JSON stored next to the dataset.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DATASET_SCHEMA_VERSION = "1.0"

# Files that constitute the frozen evaluation dataset. Order is fixed so the
# manifest is reproducible regardless of platform.
FROZEN_DATASET_FILES: Tuple[str, ...] = (
    "ground_truth.json",
    "settlements.csv",
    "bank.csv",
    "ledger.csv",
    "residuals.csv",
)

MANIFEST_FILENAME = "dataset_manifest.json"


def manifest_path(data_dir: Path | str) -> Path:
    """Return the path where the dataset manifest is stored."""
    return Path(data_dir) / MANIFEST_FILENAME


def compute_file_hash(path: Path, algo: str = "sha256") -> str:
    """Return the hex digest of a file's contents."""
    h = hashlib.new(algo)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class DatasetManifest:
    """A reproducible fingerprint of a dataset directory.

    The fingerprint is derived solely from on-disk file content and the
    deterministic generator seed, so two identical datasets always produce the
    same fingerprint.
    """

    schema_version: str
    dataset_seed: int
    files: Dict[str, str]

    def fingerprint(self) -> str:
        """A single deterministic hash summarizing the whole manifest."""
        payload = json.dumps(
            {
                "schema_version": self.schema_version,
                "dataset_seed": self.dataset_seed,
                "files": self.files,
            },
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "dataset_seed": self.dataset_seed,
            "fingerprint": self.fingerprint(),
            "files": dict(self.files),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "DatasetManifest":
        return cls(
            schema_version=str(data["schema_version"]),
            dataset_seed=int(data["dataset_seed"]),
            files={str(k): str(v) for k, v in (data["files"] or {}).items()},
        )


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of verifying a dataset directory against an expected manifest."""

    ok: bool
    dataset_fingerprint: str
    mismatches: Tuple[str, ...]
    missing: Tuple[str, ...]
    details: Tuple[str, ...]


def compute_dataset_manifest(
    data_dir: Path | str,
    dataset_seed: int = 42,
    files: Optional[Tuple[str, ...]] = None,
) -> DatasetManifest:
    """Compute a manifest by hashing every file in ``files`` under ``data_dir``."""
    data_dir = Path(data_dir)
    file_list = files or FROZEN_DATASET_FILES
    hashes: Dict[str, str] = {}
    for name in file_list:
        path = data_dir / name
        if not path.exists():
            raise FileNotFoundError(
                f"Dataset file not found for manifest: {path}"
            )
        hashes[name] = compute_file_hash(path)
    return DatasetManifest(
        schema_version=DATASET_SCHEMA_VERSION,
        dataset_seed=dataset_seed,
        files=hashes,
    )


def write_manifest(manifest: DatasetManifest, data_dir: Path | str) -> Path:
    """Persist a manifest to ``<data_dir>/dataset_manifest.json``."""
    path = manifest_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.to_dict(), indent=2), encoding="utf-8"
    )
    return path


def read_manifest(data_dir: Path | str) -> Optional[DatasetManifest]:
    """Read a previously written manifest, or return None if absent."""
    path = manifest_path(data_dir)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return DatasetManifest.from_dict(data)


def verify_dataset(
    data_dir: Path | str,
    expected: Optional[DatasetManifest] = None,
) -> VerificationResult:
    """Verify the on-disk dataset matches an expected manifest.

    If ``expected`` is None, the manifest is read from ``data_dir``; if none is
    present there, the current files are hashed (read-only) and the result is
    marked ok with a detail explaining that no manifest was available to compare
    against.
    """
    data_dir = Path(data_dir)
    if expected is None:
        expected = read_manifest(data_dir)

    if expected is None:
        try:
            manifest = compute_dataset_manifest(data_dir)
        except FileNotFoundError as exc:
            return VerificationResult(
                ok=False,
                dataset_fingerprint="",
                mismatches=(),
                missing=(str(exc),),
                details=(
                    "No manifest present and not all frozen files exist "
                    "to compute a fingerprint.",
                ),
            )
        return VerificationResult(
            ok=True,
            dataset_fingerprint=manifest.fingerprint(),
            mismatches=(),
            missing=(),
            details=(
                "No manifest present; computed current fingerprint only.",
            ),
        )

    mismatches: List[str] = []
    missing: List[str] = []
    for name in expected.files:
        path = data_dir / name
        if not path.exists():
            missing.append(name)
            continue
        if compute_file_hash(path) != expected.files[name]:
            mismatches.append(name)

    ok = not mismatches and not missing
    if ok:
        detail = "All frozen dataset files match the manifest."
    else:
        detail = (
            f"{len(mismatches)} file(s) differ from the manifest; "
            f"{len(missing)} file(s) missing."
        )
    return VerificationResult(
        ok=ok,
        dataset_fingerprint=expected.fingerprint(),
        mismatches=tuple(mismatches),
        missing=tuple(missing),
        details=(detail,),
    )


def freeze_or_verify_dataset(
    data_dir: Path | str,
    dataset_seed: int = 42,
    *,
    allow_freeze: bool = True,
) -> Tuple[DatasetManifest, str]:
    """Freeze the dataset if unfrozen, or verify it if already frozen.

    Returns ``(manifest, status)`` where status is one of:
      * "frozen"  - a new manifest was written (first freeze)
      * "verified" - an existing manifest matched the on-disk dataset
      * "drift"  - (never returned) drift is signaled by raising ValueError

    Raises ``ValueError`` if a frozen manifest already exists and the on-disk
    dataset has drifted from it, so evaluation can never silently run against a
    mutated dataset.
    """
    data_dir = Path(data_dir)
    existing = read_manifest(data_dir)
    if existing is not None:
        result = verify_dataset(data_dir, existing)
        if not result.ok:
            raise ValueError(
                "Dataset drift detected relative to the frozen manifest: "
                + "; ".join(result.details)
                + f". Mismatched files: {result.mismatches}; "
                f"Missing files: {result.missing}"
            )
        return existing, "verified"

    if not allow_freeze:
        raise ValueError(
            "No dataset manifest present and freezing is disabled."
        )

    manifest = compute_dataset_manifest(data_dir, dataset_seed=dataset_seed)
    write_manifest(manifest, data_dir)
    return manifest, "frozen"
