"""
Day 4.3 — Groq client boundary (provider abstraction).

The rest of the application must not depend directly on the Groq SDK. This
module defines a small, mockable provider boundary that returns a parsed
JSON object from the model via Groq's supported structured-output mechanism.

The boundary is responsible only for:
  * resolving the API key from configuration/environment (never hardcoded),
  * invoking the configured model with a JSON schema response format,
  * returning the parsed JSON object or raising a clear provider error.

It does NOT know about the proposal schema's semantics, prompts, or routing.
"""
from __future__ import annotations

import json
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from groq import APIConnectionError, APIError, APITimeoutError, Groq


# Patterns that indicate secret material. Matches are redacted before any
# diagnostic text leaves the provider boundary for local display.
_SECRET_PATTERNS = [
    re.compile(r"gsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"Authorization[^\n]*", re.IGNORECASE),
    re.compile(r"x-api-key[^\n]*", re.IGNORECASE),
    re.compile(r"api[_-]?key[=:][^\n]*", re.IGNORECASE),
]


def _sanitize(text: Optional[str]) -> str:
    """Redact any secret-like material from a diagnostic string."""
    if not text:
        return ""
    sanitized = text
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


_TRANSIENT_ERROR_TYPES = frozenset({"rate_limit_error", "server_error"})
_PERMANENT_ERROR_TYPES = frozenset(
    {
        "authentication_error",
        "invalid_request_error",
        "not_found_error",
        "permission_error",
    }
)


def _classify_api_error(exc: APIError) -> str:
    """Classify a Groq APIError as transient, permanent, or unknown.

    Uses the structured ``body`` attribute (server response) when available.
    Never inspects or logs API keys, Authorization headers, or request payloads.
    """
    body = getattr(exc, "body", None) or {}
    error_info = body.get("error") if isinstance(body, dict) else None
    if isinstance(error_info, dict):
        error_type = error_info.get("type", "")
        if error_type in _TRANSIENT_ERROR_TYPES:
            return "transient"
        if error_type in _PERMANENT_ERROR_TYPES:
            return "permanent"
    return "unknown"


def _is_transient_api_error(exc: APIError) -> bool:
    return _classify_api_error(exc) == "transient"


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def safe_diagnostic(exc: Exception) -> str:
    """
    Return a secret-safe, locally-useful diagnostic for an exception.

    Surfaces the underlying error type and a sanitized message so CLI debugging
    can identify the failure (e.g. AuthenticationError vs RateLimitError) without
    ever exposing API keys, Authorization headers, or request headers.
    """
    error_type = getattr(exc, "error_type", None)
    safe_detail = getattr(exc, "safe_detail", None)
    if error_type or safe_detail:
        et = error_type or type(exc).__name__
        detail = _sanitize(safe_detail) if safe_detail else _sanitize(str(exc))
        return f"{et}: {detail}"
    return f"{type(exc).__name__}: {_sanitize(str(exc))}"


class GroqProviderError(RuntimeError):
    """
    Clear, infrastructure-level error from the Groq provider boundary.

    Carries optional ``error_type`` (the underlying provider exception class
    name) and ``safe_detail`` (a sanitized message) for local diagnostics. The
    audit trail and outcome ``reason`` remain safe; ``safe_diagnostic`` is the
    only path that exposes this detail, and it redacts secrets.
    """

    def __init__(
        self,
        message: str,
        *,
        error_type: Optional[str] = None,
        safe_detail: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.safe_detail = safe_detail


class GroqTimeoutError(GroqProviderError):
    """Provider timeout; treated distinctly from a generic API error."""


DEFAULT_MODEL = "openai/gpt-oss-20b"


@dataclass(frozen=True)
class GroqClientConfig:
    """
    Configuration for the Groq client boundary.

    The API key is read from the environment variable named by
    ``api_key_env``; it is never stored as a literal in source or logged.
    """

    model: str = DEFAULT_MODEL
    api_key_env: str = "GROQ_API_KEY"

    def resolve_api_key(self) -> str:
        """
        Return the Groq API key from the environment.

        Raises GroqProviderError if the key is not configured, rather than
        silently falling back to a hardcoded value.
        """
        key = os.environ.get(self.api_key_env)
        if not key:
            raise GroqProviderError(
                f"Groq API key not found in environment variable "
                f"'{self.api_key_env}'."
            )
        return key


class StructuredCompletionProvider(ABC):
    """
    Provider boundary abstraction for structured (JSON) completions.

    Implementations are mockable in unit tests; the application code depends
    only on this protocol, not on the Groq SDK.
    """

    @abstractmethod
    def complete_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """
        Return a parsed JSON object produced by the model under the given
        JSON schema. Implementations raise GroqProviderError on any
        infrastructure or parsing failure.
        """


class GroqStructuredProvider(StructuredCompletionProvider):
    """
    Groq SDK implementation of the structured completion provider boundary.

    Uses Groq's JSON-schema response format (strict) so the model returns a
    parseable object rather than relying on prompt-only JSON instructions.
    """

    def __init__(self, config: GroqClientConfig | None = None) -> None:
        self._config = config or GroqClientConfig()

    def complete_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        for attempt in range(2):
            try:
                client = Groq(api_key=self._config.resolve_api_key())
                response = client.chat.completions.create(
                    model=self._config.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": dict(json_schema),
                    },
                    temperature=0.0,
                )
                break
            except GroqProviderError:
                raise
            except APITimeoutError as exc:
                if attempt == 0:
                    _sleep(2.0)
                    continue
                raise GroqTimeoutError(
                    f"Groq request timed out: {exc}",
                    error_type=type(exc).__name__,
                    safe_detail=str(exc),
                ) from exc
            except APIConnectionError as exc:
                if attempt == 0:
                    _sleep(2.0)
                    continue
                raise GroqProviderError(
                    f"Groq connection failed: {exc}",
                    error_type=type(exc).__name__,
                    safe_detail=str(exc),
                ) from exc
            except APIError as exc:
                if _is_transient_api_error(exc) and attempt == 0:
                    _sleep(2.0)
                    continue
                classification = _classify_api_error(exc)
                raise GroqProviderError(
                    f"Groq API error ({classification}): {exc}",
                    error_type=f"{type(exc).__name__}[{classification}]",
                    safe_detail=str(exc),
                ) from exc
            except Exception as exc:  # noqa: BLE001 - surface as provider error
                raise GroqProviderError(
                    f"Unexpected Groq provider error: {exc}",
                    error_type=type(exc).__name__,
                    safe_detail=str(exc),
                ) from exc

        content = response.choices[0].message.content
        if not content:
            raise GroqProviderError(
                "Groq returned empty completion content."
            )
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise GroqProviderError(
                f"Failed to parse Groq response as JSON: {exc}"
            ) from exc
