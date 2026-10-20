"""Money handling.

Every amount in this system is an integer number of paise. Drug pricing is
capped by MRP and, for scheduled formulations, by DPCO/NPPA ceilings, so
rounding errors are a compliance problem rather than a cosmetic one.
"""

from __future__ import annotations


def rupees(amount: float) -> int:
    """Convert rupees to paise. Only for fixtures and config, never for arithmetic."""
    return round(amount * 100)


def to_rupee_string(paise: int) -> str:
    return f"{paise / 100:.2f}"


def apply_bps(paise: int, bps: int) -> int:
    """Apply a basis-point rate, rounding half-up to the nearest paisa."""
    if bps < 0:
        raise ValueError("bps must not be negative")
    return (paise * bps + 5000) // 10000
