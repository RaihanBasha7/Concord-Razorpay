from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MatcherConfig:
    """Configuration for the deterministic reconciliation matcher."""

    amount_tolerance_paise: int
    date_window_days: int

    def __post_init__(self) -> None:
        if self.amount_tolerance_paise < 0:
            raise ValueError(
                "amount_tolerance_paise must be a non-negative integer."
            )
        if self.date_window_days < 0:
            raise ValueError(
                "date_window_days must be a non-negative integer."
            )
