"""Shared default constants for matching tolerances.

These values are the single source of truth for the default amount tolerance
and date window used by both the Layer 1 deterministic matcher and Layer 3
guardrail routing.  Both modules must import from here — never hardcode
these values independently.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MatchingTolerances:
    """Default matching tolerance values."""

    amount_tolerance_paise: int = 100
    date_window_days: int = 2


# The single canonical instance — both matcher.py and layer3.py must
# reference this exact object (verified by test_shared_tolerance_object).
DEFAULT_TOLERANCES = MatchingTolerances()
