"""
Build submission-only redacted evaluation artifacts for Concord.

The canonical Layer 2 audit artifact (data/layer2_clean_audit.jsonl) is
SHA-256-pinned in dataset_manifest.json and cross-checked by
final_verification.py, build_current_report.py, and the test suite. It
contains real Groq provider diagnostics (an organization ID and a billing
console URL) in some records' reason/diagnostic fields. It must not be
edited in place — doing so would break every fingerprint check in the
project.

This script produces a redacted submission copy
(data/layer2_clean_audit.submission.jsonl) with provider org IDs and URLs
replaced by [REDACTED] in the reason/diagnostic string fields ONLY. Every
other field (correlation_id, outcome, proposal, confidence,
dataset_fingerprint, ...) is preserved byte-identically.

The redacted copy is a submission-only convenience file. It is never
registered as a canonical artifact and no fingerprint references it; the
canonical file remains the source of truth for evaluation.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Pattern

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
CANONICAL_ARTIFACT_PATH = DATA_DIR / "layer2_clean_audit.jsonl"
REDACTED_ARTIFACT_PATH = DATA_DIR / "layer2_clean_audit.submission.jsonl"

REDACTED_PLACEHOLDER = "[REDACTED]"

# Provider diagnostics leaked into error reason/diagnostic strings:
#   - Groq organization ID, e.g. org_<opaque-id>
#   - Groq console billing URLs
# All URLs (not only console.groq.com) are redacted so the copy fails
# closed on any provider URL that appears in these fields.
_REDACT_PATTERNS: List[Pattern[str]] = [
    re.compile(r"org_[a-zA-Z0-9]+"),
    re.compile(r"https?://[^\s'\x22\x5c]+"),
]

# Only these string fields may carry provider diagnostics. Everything else
# (correlation_id, outcome, proposal, confidence, dataset_fingerprint, ...)
# must be preserved byte-identically.
_REDACTABLE_FIELDS = ("reason", "diagnostic")


def _redact_text(text: str) -> str:
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub(REDACTED_PLACEHOLDER, text)
    return text


def _redact_record(record: Dict[str, Any]) -> Dict[str, Any]:
    redacted = dict(record)
    for field in _REDACTABLE_FIELDS:
        value = redacted.get(field)
        if isinstance(value, str):
            redacted[field] = _redact_text(value)
    return redacted


def _redact_raw_line(line: str) -> str:
    """Apply the same patterns to the raw line text.

    Used as a cross-check: field-level redaction must equal raw-line
    redaction, proving that no byte outside the redacted substrings changed.
    """
    for pattern in _REDACT_PATTERNS:
        line = pattern.sub(REDACTED_PLACEHOLDER, line)
    return line


def build_redacted_artifact(
    canonical_path: Path = CANONICAL_ARTIFACT_PATH,
    output_path: Path = REDACTED_ARTIFACT_PATH,
) -> Path:
    """Write a redacted copy of the canonical artifact.

    Each parsed line is re-serialized with json.dumps, which round-trips the
    canonical file byte-for-byte (verified 77/77), so every non-redacted
    field is preserved exactly. Fails closed if any provider pattern remains
    in the output or if the field-level redaction diverges from the raw-line
    redaction.
    """
    if not canonical_path.is_file():
        raise FileNotFoundError(
            f"Canonical artifact missing: {canonical_path}. "
            "Refusing to build a redacted copy without the source of truth."
        )

    out_lines: List[str] = []
    with canonical_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            raw = line.rstrip("\n").rstrip("\r")
            if not raw.strip():
                continue
            record = json.loads(raw)
            redacted_line = json.dumps(_redact_record(record))
            # The redacted line must differ from the original only by the
            # redacted substrings (raw-line substitution is the reference).
            if redacted_line != _redact_raw_line(raw):
                raise RuntimeError(
                    f"Redaction invariant violated for line of record "
                    f"{record.get('correlation_id')!r}: field-level and "
                    "raw-level redaction diverge. Refusing to write output."
                )
            out_lines.append(redacted_line)

    payload = "\n".join(out_lines) + "\n"

    # Fail closed: the redacted copy must contain no provider org IDs or URLs.
    for pattern in _REDACT_PATTERNS:
        if pattern.search(payload):
            raise RuntimeError(
                f"Redaction failed: pattern {pattern.pattern!r} is still "
                f"present in {output_path.name}. Refusing to write output."
            )

    output_path.write_text(payload, encoding="utf-8")
    return output_path


def main() -> int:
    output = build_redacted_artifact()
    print(f"Wrote {output}")
    print(f"Redacted provider org IDs / URLs in reason/diagnostic fields only.")
    print("Canonical artifact untouched (see sha256 verification).")
    return 0


if __name__ == "__main__":
    sys.exit(main())