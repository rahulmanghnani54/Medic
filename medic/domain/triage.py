"""Clinical urgency.

Uber rations excess demand with price: the fare rises, some riders abandon,
and the market clears. That mechanism cannot be copied here. Medicine prices
are capped by MRP and the DPCO/NPPA regime, so there is no headroom to raise;
and even if there were, suppressing demand for a rescue inhaler during a
heatwave is the harm, not the mechanism working.

So the demand-rationing half of surge is replaced by SEQUENCING. When supply
is short, orders are not repriced -- they are ordered by clinical need. This
module holds that ordering, and the detour and batching tolerances that follow
from it.

The tier is a property of the catalogue, set per medicine, because it is a
clinical judgement and not something to infer at runtime. Drug schedule is NOT
a usable proxy: a rescue inhaler and a sleeping tablet are both Schedule H.
"""

from __future__ import annotations

from enum import StrEnum


class ClinicalUrgency(StrEnum):
    """How badly a delay hurts. Set per medicine by a pharmacist, not guessed."""

    #: A delay can cause rapid, serious harm. Rescue medication.
    CRITICAL = "CRITICAL"
    #: An interruption matters within hours. Insulin, anticonvulsants, antibiotics mid-course.
    TIME_SENSITIVE = "TIME_SENSITIVE"
    #: Ongoing therapy where a few hours is tolerable. Most chronic-care refills.
    ROUTINE = "ROUTINE"
    #: Comfort and general health. Vitamins, antacids.
    ELECTIVE = "ELECTIVE"


#: Lower sorts first.
_RANK: dict[ClinicalUrgency, int] = {
    ClinicalUrgency.CRITICAL: 0,
    ClinicalUrgency.TIME_SENSITIVE: 1,
    ClinicalUrgency.ROUTINE: 2,
    ClinicalUrgency.ELECTIVE: 3,
}


def urgency_for(urgencies: list[ClinicalUrgency | str]) -> ClinicalUrgency:
    """A basket takes the urgency of its most urgent line.

    Like the schedule controls, this is a union and not an average: one
    critical item makes the whole order critical, because the basket is
    delivered as one package.
    """
    if not urgencies:
        return ClinicalUrgency.ROUTINE
    return min((ClinicalUrgency(u) for u in urgencies), key=lambda u: _RANK[u])


def dispatch_priority(urgency: ClinicalUrgency | str) -> int:
    """Sort key for the dispatcher queue. Lower is served first."""
    return _RANK[ClinicalUrgency(urgency)]


#: Added minutes a courier may spend on another order before this one is
#: handed over. Uber uses a single global detour scalar; here the tolerance is
#: a clinical question, so a critical order is never batched behind another.
_DETOUR_BUDGET_MINUTES: dict[ClinicalUrgency, int] = {
    ClinicalUrgency.CRITICAL: 0,
    ClinicalUrgency.TIME_SENSITIVE: 4,
    ClinicalUrgency.ROUTINE: 8,
    ClinicalUrgency.ELECTIVE: 12,
}


def detour_budget_minutes(urgency: ClinicalUrgency | str) -> int:
    return _DETOUR_BUDGET_MINUTES[ClinicalUrgency(urgency)]


def batch_eligible(urgency: ClinicalUrgency | str) -> bool:
    """Whether this order may share a courier trip with another."""
    return detour_budget_minutes(urgency) > 0


def cold_chain_overrides(urgency: ClinicalUrgency | str, requires_cold_chain: bool) -> int:
    """Cold-chain items get no detour tolerance regardless of urgency tier.

    Time out of refrigeration is a product-integrity question, not a clinical
    priority question, so it clamps rather than sorts.
    """
    if requires_cold_chain:
        return 0
    return detour_budget_minutes(urgency)
