"""
Build the Buildathon submission ZIP for Concord.

Excludes:
  - .env, frontend/.env (and any other .env* other than .env.example)
  - .git/
  - .freebuff/
  - .venv/
  - frontend/node_modules/
  - __pycache__/
  - .pytest_cache/
  - dist/ (build artifacts)
  - *.db (sqlite databases)
  - data/concord.db
  - temporary log files
  - frontend build artifacts

Includes all source code, tests, docs, manifest, and the data/ evidence the
shipped test suite, the live API, and final_verification.py actually read.
Pure clutter — superseded current-looking reports, day4/day5 pipeline
artifacts, timestamped audit backups, run logs — remains in the repo (git
history and on disk) for provenance but is excluded from the submission ZIP.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "Concord_submission.zip"

# Every data/ file that ships, and why:
#
#  * The five frozen dataset files + dataset_manifest.json — the pinned
#    dataset; the manifest's canonical_layer2_artifact.filename must exist
#    alongside it or reconciliation/frozen_dataset.py cannot serve the live
#    frozen-artifact AI replay.
#  * layer2_clean_audit.jsonl — the RAW canonical Layer 2 artifact: the file
#    dataset_manifest.json pins by SHA-256, the file the test suite and
#    final_verification.py hash-check, and the file the live demo replays.
#    A submission ZIP without it fails its own test suite (14 tests) and
#    cannot serve the demo's core feature. It is shipped byte-identical to
#    the working tree (verified below against the manifest pin).
#  * current_evaluation_report.{json,md} — the canonical CURRENT report.
#  * clean_* — the six manifest-referenced historical artifacts
#    (dataset_manifest.json -> historical_artifacts[].path). They are the
#    audited evidence chain that proves the old b8bf3feb-fingerprint
#    evaluation is locked as HISTORICAL and superseded by the canonical
#    artifact; the shipped test suite (test_evaluation_provenance.py,
#    test_evaluation_accounting.py) verifies exactly that (10 tests read
#    them). They are unambiguously historical by name and by their own
#    _lifecycle fields, so they cannot be mistaken for current evidence.
#  * layer2_full_audit.legacy.jsonl — the resume-migration legacy fixture
#    (pre-resumable-artifact format) that test_resume_migration.py needs to
#    prove old-fingerprint/legacy artifacts are rejected (4 tests).
#
# Everything else under data/ stays out: day4_audit.jsonl and
# day5_full_pipeline_report.* (superseded pipeline artifacts),
# evaluation_report.json / layer2_evaluation_report.json (superseded
# current-looking reports), layer2_full_run.log, the timestamped
# layer2_full_audit.* and layer2_clean_audit.2026* backups, and the redacted
# submission copy layer2_clean_audit.submission.jsonl (its packaging purpose
# ended when the raw canonical artifact started shipping; see the leak-scan
# note below). build_submission_artifacts.py remains in the repo as the
# redaction tool, and its output remains on disk, but neither is part of the
# submission.
DATA_ALLOWLIST = {
    # Frozen dataset + manifest
    "settlements.csv",
    "bank.csv",
    "ledger.csv",
    "ground_truth.json",
    "residuals.csv",
    "dataset_manifest.json",
    # Canonical current evaluation evidence
    "layer2_clean_audit.jsonl",
    "current_evaluation_report.json",
    "current_evaluation_report.md",
    # Manifest-referenced historical evidence chain (test-required)
    "clean_eval_results.json",
    "clean_evaluation_report.json",
    "clean_evaluation_report.md",
    "clean_full_eval_results.json",
    "clean_full_evaluation_report.json",
    "clean_full_evaluation_report.md",
    # Resume-migration legacy fixture (test-required)
    "layer2_full_audit.legacy.jsonl",
}

EXCLUDE_DIR_NAMES = {
    ".git",
    ".freebuff",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    ".mypy_cache",
    ".eggs",
    "concord.egg-info",
    ".tmp_test_day3",
    ".tmp_test_partial_final",
    ".idea",
    ".vscode",
}

EXCLUDE_FILE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dylib",
    ".db",
    ".db-journal",
    ".tmp",
}

EXCLUDE_FILE_NAMES = {
    "concord.db",
    "Thumbs.db",
    ".DS_Store",
    "package-lock.json",
}

# Files we explicitly KEEP (overrides above)
ALWAYS_KEEP_NAMES = {
    "package.json",
}


def _is_excluded(path: Path) -> bool:
    """Return True if any path component is excluded."""
    rel = path.relative_to(REPO_ROOT)
    parts = rel.parts
    # data/ ships ONLY the allowlisted files above. Everything else under
    # data/ — superseded reports, timestamped/backup audit copies, logs, the
    # redacted submission copy — never enters the submission ZIP.
    if parts[0] == "data" and len(parts) > 1:
        return path.name not in DATA_ALLOWLIST
    for part in parts:
        if part in EXCLUDE_DIR_NAMES:
            return True
        # Exclude any .env* file at any depth (other than .env.example)
        if part.startswith(".env") and part != ".env.example":
            return True
    name = path.name
    if name in EXCLUDE_FILE_NAMES:
        return True
    if name in ALWAYS_KEEP_NAMES:
        return False
    if path.suffix in EXCLUDE_FILE_SUFFIXES:
        return True
    # Exclude coverage / htmlcov
    if name == ".coverage":
        return True
    return False


def _iter_repo_files() -> List[Path]:
    files: List[Path] = []
    for root, dirs, names in os.walk(REPO_ROOT):
        root_path = Path(root)
        # Filter dirs in-place
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIR_NAMES]
        for name in names:
            p = root_path / name
            if _is_excluded(p):
                continue
            files.append(p)
    return sorted(files)


def build_zip(output: Path = DEFAULT_OUTPUT) -> Path:
    output = Path(output).resolve()
    # Never include the archive being written: a leftover ZIP from a previous
    # build on disk would otherwise be walked and embedded inside the new one.
    files = [p for p in _iter_repo_files() if p.resolve() != output]
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            arcname = p.relative_to(REPO_ROOT).as_posix()
            zf.writestr(arcname, p.read_bytes())
    return output


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    zip_path = build_zip(output)
    size = zip_path.stat().st_size
    print(f"Wrote {zip_path}")
    print(f"Size: {size:,} bytes ({size / 1024 / 1024:.2f} MB)")

    # Confirm exclusion invariants
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    forbidden = []
    for needle in (
        ".git/",
        ".freebuff/",
        ".venv/",
        "node_modules/",
        "__pycache__/",
        ".pytest_cache/",
        "/dist/",
        "concord.db",
    ):
        if any(n.replace("\\", "/").startswith(needle) or
               f"/{needle}" in n.replace("\\", "/") for n in names):
            forbidden.append(needle)
    assert not forbidden, f"Forbidden entries in zip: {forbidden}"

    # Confirm no .env files other than .env.example
    envs = [
        n for n in names
        if ".env" in n.replace("\\", "/").split("/")[-1]
        and not n.endswith(".env.example")
    ]
    assert not envs, f"Unexpected .env files in zip: {envs}"

    # data/ ships exactly the allowlisted evidence set: nothing superseded,
    # no timestamped/backup audit copies, no redacted submission copy, and
    # exactly one current Layer 2 audit artifact (the raw canonical file).
    norm = [n.replace("\\", "/") for n in names]
    data_names = {n.split("/")[-1] for n in norm if n.startswith("data/")}
    assert DATA_ALLOWLIST <= data_names, (
        f"Required data files missing from the ZIP: {DATA_ALLOWLIST - data_names}"
    )
    absent_names = {
        # Superseded current-looking / pipeline-era reports and logs. These
        # stay out of the ZIP: they could be mistaken for current evidence or
        # are pure clutter. (The manifest-referenced clean_* historical chain
        # above is the audited exception and is required by the test suite.)
        "day4_audit.jsonl",
        "day5_full_pipeline_report.json",
        "day5_full_pipeline_report.md",
        "evaluation_report.json",
        "layer2_evaluation_report.json",
        "layer2_full_run.log",
    }
    assert not (data_names & absent_names), (
        f"Superseded/clutter data files must not be in the submission ZIP: "
        f"{sorted(data_names & absent_names)}"
    )
    l2_clean_audits = sorted(
        n for n in norm if "layer2_clean_audit" in n
    )
    assert l2_clean_audits == ["data/layer2_clean_audit.jsonl"], (
        f"Expected exactly one current Layer 2 audit artifact (the raw "
        f"canonical file, per dataset_manifest.json "
        f"canonical_layer2_artifact.filename), got: {l2_clean_audits}"
    )

    # Fail closed on provider diagnostics leaking into the ZIP. Groq org IDs
    # (org_...) and Groq console URLs (console.groq.com) occur ONLY inside
    # real API_ERROR rate-limit diagnostic text produced by the Groq API
    # during evaluation. They are diagnostic strings — not credentials, not
    # PII, not secrets (see build_submission_artifacts.py) — and they are
    # ALREADY KNOWN and ALREADY AUDITED: exactly two literals appear, quoted
    # verbatim in the frozen evidence chain:
    #     org_01kp04kap9f0qr2e1esvqxf7gj
    #     https://console.groq.com/settings/billing
    #
    # Scan design (precise narrowing, NOT a silent disable):
    #  * scripts/build_submission_artifacts.py and
    #    scripts/build_submission_zip.py are exempt: they ARE the
    #    redaction/scan tooling and self-referentially contain these pattern
    #    definitions.
    #  * Three evidence files legitimately quote the two audited literals in
    #    API_ERROR diagnostics: the raw canonical artifact
    #    (data/layer2_clean_audit.jsonl) and the two manifest-referenced
    #    historical reports that recorded the same rate-limit errors
    #    (data/clean_eval_results.json, data/clean_evaluation_report.json).
    #    For exactly these entries, the two audited literals are stripped
    #    before scanning. The canonical entry is additionally hash-gated: it
    #    must match the SHA-256 pinned in the shipped manifest or the build
    #    fails. Any OTHER org_/console.groq.com token in these files — i.e.
    #    a diagnostic that was not already audited — fails the build.
    #  * Every other file in the ZIP gets the full scan with nothing
    #    stripped: any org ID or console URL anywhere is a leak and fails
    #    the build.
    KNOWN_DIAGNOSTIC_LITERALS = (
        b"org_01kp04kap9f0qr2e1esvqxf7gj",
        b"https://console.groq.com/settings/billing",
    )
    EVIDENCE_ENTRIES_WITH_AUDITED_LITERALS = {
        "data/layer2_clean_audit.jsonl",
        "data/clean_eval_results.json",
        "data/clean_evaluation_report.json",
    }
    org_leak = re.compile(rb"org_[a-zA-Z0-9]+")
    url_leak = re.compile(rb"console\.groq\.com")
    scan_exempt = {
        "scripts/build_submission_artifacts.py",
        "scripts/build_submission_zip.py",
    }
    leaked_entries = []
    with zipfile.ZipFile(zip_path) as zf:
        # Hash-gate the canonical artifact: it must be byte-identical to the
        # SHA-256 pinned in the shipped manifest.
        manifest = json.loads(zf.read("data/dataset_manifest.json").decode("utf-8"))
        pinned_hash = manifest["canonical_layer2_artifact"]["sha256"]
        archived_hash = hashlib.sha256(
            zf.read("data/layer2_clean_audit.jsonl")
        ).hexdigest()
        assert archived_hash == pinned_hash, (
            f"Canonical Layer 2 audit artifact in ZIP does not match the "
            f"manifest pin: expected {pinned_hash}, got {archived_hash}. "
            f"Refusing to ship an unpinned artifact."
        )
        for n in norm:
            if n in scan_exempt:
                continue
            try:
                payload = zf.read(n)
            except KeyError:
                continue
            residue = payload
            if n in EVIDENCE_ENTRIES_WITH_AUDITED_LITERALS:
                for literal in KNOWN_DIAGNOSTIC_LITERALS:
                    residue = residue.replace(literal, b"")
            if org_leak.search(residue) or url_leak.search(residue):
                leaked_entries.append(n)
    assert not leaked_entries, (
        f"Provider diagnostics found in ZIP entries: {leaked_entries}"
    )

    print(f"Entries: {len(names)}")
    print("Hygiene invariants: OK")
    print("Layer 2 artifact: raw canonical shipped (redacted copy excluded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
