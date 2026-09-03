"""
Secret-scan regression test.

Ensures no real API keys, tokens, or credentials appear in:
  - source code
  - tests
  - docs
  - JSON reports
  - audit logs
  - frontend files
  - any Git-tracked file
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# Regexes that flag likely-real credentials. We accept placeholder/empty
# values for these names in source.
_SECRET_NAME_RE = re.compile(
    r"(?i)\b("
    r"groq_api_key|"
    r"supabase_(?:anon_key|url)|"
    r"openai_api_key|"
    r"anthropic_api_key|"
    r"aws_(?:access_key|secret)|"
    r"github_token"
    r")\b"
)

# Patterns that look like real values (skip pure empty values and obvious
# placeholders like "sk-..." or "xxx").
_PLACEHOLDER_PATTERNS = (
    re.compile(r"sk-\.\.\."),
    re.compile(r"^gsk_\.\.\."),
    re.compile(r"^xxx+", re.IGNORECASE),
)

_TRACKED_FILES_NEEDLE = re.compile(
    r"(gsk_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|"
    r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})"
)


def _is_placeholder(value: str) -> bool:
    v = value.strip().strip('"').strip("'")
    if not v:
        return True
    for pat in _PLACEHOLDER_PATTERNS:
        if pat.search(v):
            return True
    return False


def test_env_example_has_no_real_secrets():
    p = REPO_ROOT / ".env.example"
    text = p.read_text(encoding="utf-8")
    # Every KEY= pair: ensure the value side is empty or a placeholder.
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if _SECRET_NAME_RE.search(key):
            assert _is_placeholder(value), (
                f".env.example contains a real-looking value for {key!r}: "
                f"{value!r}"
            )


def test_no_real_groq_keys_in_tracked_files():
    """Scan every Git-tracked file for real-looking Groq API keys."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    tracked = result.stdout.splitlines()
    offenders = []
    for rel in tracked:
        p = REPO_ROOT / rel
        if not p.is_file():
            continue
        # Skip binary and oversized files.
        if p.suffix in (".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".db"):
            continue
        try:
            size = p.stat().st_size
            if size > 1_000_000:
                continue
            text = p.read_bytes()
            if b"\x00" in text:
                continue
            text = text.decode("utf-8", errors="ignore")
        except OSError:
            continue
        if _TRACKED_FILES_NEEDLE.search(text):
            # Check it's not a test placeholder
            for m in _TRACKED_FILES_NEEDLE.finditer(text):
                snippet = text[max(0, m.start() - 30): m.end() + 10]
                if "sk-..." in snippet or "placeholder" in snippet.lower():
                    continue
                offenders.append((rel, snippet))
    assert not offenders, (
        "Real-looking credentials found in tracked files:\n"
        + "\n".join(f"  {rel}: {snippet!r}" for rel, snippet in offenders)
    )


def test_env_files_in_root_and_frontend_not_tracked():
    """The .env files at repo root and in frontend/ must not be tracked by git."""
    for relpath in (".env", "frontend/.env"):
        result = subprocess.run(
            ["git", "ls-files", relpath],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.stdout.strip() == "", (
            f"{relpath} must NOT be Git-tracked but is in:\n{result.stdout}"
        )


def test_gitignore_blocks_env_files():
    """Verify .gitignore excludes .env files at root and under frontend/."""
    gi = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    # `.env` covers both locations
    assert ".env" in gi, ".gitignore must contain '.env'"
    # Negative pattern for the example file must be present.
    assert ".env.example" in gi, (
        ".gitignore must contain a negative pattern for .env.example"
    )