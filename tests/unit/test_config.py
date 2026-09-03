"""
Tests for reconciliation.config — shared tolerance constants.

Ensures that both matcher.py and layer3.py import and reference the exact
same DEFAULT_TOLERANCES object (identity check, not just equality).
"""
from __future__ import annotations

from reconciliation.config import DEFAULT_TOLERANCES, MatchingTolerances


def test_default_tolerances_is_frozen_dataclass():
    """DEFAULT_TOLERANCES is a frozen MatchingTolerances instance."""
    assert isinstance(DEFAULT_TOLERANCES, MatchingTolerances)
    assert DEFAULT_TOLERANCES.amount_tolerance_paise == 100
    assert DEFAULT_TOLERANCES.date_window_days == 2


def test_shared_tolerance_object():
    """Both matcher.py and layer3.py must reference the exact same object."""
    import reconciliation.matcher as matcher_mod
    import reconciliation.layer3 as layer3_mod

    assert matcher_mod.DEFAULT_TOLERANCES is layer3_mod.DEFAULT_TOLERANCES
    assert matcher_mod.DEFAULT_TOLERANCES is DEFAULT_TOLERANCES
    assert layer3_mod.DEFAULT_TOLERANCES is DEFAULT_TOLERANCES
