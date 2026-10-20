"""Clinical urgency: the sequencing rules that stand in for surge pricing."""

from __future__ import annotations

import pytest

from medic.domain import triage
from medic.domain.triage import (
    ClinicalUrgency,
    batch_eligible,
    cold_chain_overrides,
    detour_budget_minutes,
    dispatch_priority,
    urgency_for,
)

#: Every tier, written down in the order the dispatcher should serve them.
TIERS_MOST_URGENT_FIRST = (
    ClinicalUrgency.CRITICAL,
    ClinicalUrgency.TIME_SENSITIVE,
    ClinicalUrgency.ROUTINE,
    ClinicalUrgency.ELECTIVE,
)


def test_a_basket_takes_the_urgency_of_its_most_urgent_line():
    assert (
        urgency_for([ClinicalUrgency.ELECTIVE, ClinicalUrgency.TIME_SENSITIVE])
        is ClinicalUrgency.TIME_SENSITIVE
    )


def test_one_critical_line_makes_the_whole_basket_critical():
    """Urgency unions upward like the schedule controls do, rather than averaging.

    Three vitamin tubs beside one rescue inhaler is a critical delivery: the
    basket leaves the counter as a single package, so the least tolerable line
    sets the tolerance for all of them.
    """
    assert (
        urgency_for(
            [
                ClinicalUrgency.ELECTIVE,
                ClinicalUrgency.ELECTIVE,
                ClinicalUrgency.ELECTIVE,
                ClinicalUrgency.CRITICAL,
            ]
        )
        is ClinicalUrgency.CRITICAL
    )


def test_a_basket_of_one_tier_keeps_that_tier():
    for tier in TIERS_MOST_URGENT_FIRST:
        assert urgency_for([tier, tier]) is tier


def test_an_elective_basket_is_not_dragged_up_by_being_mixed():
    assert (
        urgency_for([ClinicalUrgency.ELECTIVE, ClinicalUrgency.ROUTINE]) is ClinicalUrgency.ROUTINE
    )


def test_an_empty_basket_defaults_to_routine():
    """Nothing to judge means no claim on the queue, and no excuse to jump it either."""
    assert urgency_for([]) is ClinicalUrgency.ROUTINE


def test_urgency_accepts_plain_strings_as_well_as_enum_members():
    assert urgency_for(["ROUTINE", "CRITICAL"]) is ClinicalUrgency.CRITICAL


def test_urgency_accepts_a_basket_mixing_strings_and_enum_members():
    assert (
        urgency_for(["ELECTIVE", ClinicalUrgency.TIME_SENSITIVE]) is ClinicalUrgency.TIME_SENSITIVE
    )


def test_an_unrecognised_urgency_label_is_rejected():
    """A typo in the catalogue must fail loudly, not quietly become routine."""
    with pytest.raises(ValueError):
        urgency_for(["URGENT-ISH"])


def test_dispatch_priority_serves_the_tiers_in_clinical_order():
    priorities = [dispatch_priority(tier) for tier in TIERS_MOST_URGENT_FIRST]
    assert priorities == sorted(priorities)
    assert len(set(priorities)) == len(TIERS_MOST_URGENT_FIRST)


def test_critical_sorts_ahead_of_every_other_tier():
    assert dispatch_priority(ClinicalUrgency.CRITICAL) < min(
        dispatch_priority(tier) for tier in TIERS_MOST_URGENT_FIRST[1:]
    )


def test_a_mixed_queue_sorts_critical_to_the_front_and_elective_to_the_back():
    queue = [
        ClinicalUrgency.ROUTINE,
        ClinicalUrgency.ELECTIVE,
        ClinicalUrgency.CRITICAL,
        ClinicalUrgency.TIME_SENSITIVE,
    ]
    assert sorted(queue, key=dispatch_priority) == list(TIERS_MOST_URGENT_FIRST)


def test_dispatch_priority_accepts_a_plain_string():
    assert dispatch_priority("CRITICAL") == dispatch_priority(ClinicalUrgency.CRITICAL)


def test_a_critical_order_is_allowed_no_detour_at_all():
    assert detour_budget_minutes(ClinicalUrgency.CRITICAL) == 0


def test_the_detour_budget_widens_as_the_tier_gets_less_urgent():
    budgets = [detour_budget_minutes(tier) for tier in TIERS_MOST_URGENT_FIRST]
    assert budgets == [0, 4, 8, 12]


def test_detour_budget_accepts_a_plain_string():
    assert detour_budget_minutes("ELECTIVE") == detour_budget_minutes(ClinicalUrgency.ELECTIVE)


def test_a_critical_order_is_never_batchable():
    assert not batch_eligible(ClinicalUrgency.CRITICAL)


def test_every_tier_below_critical_may_share_a_courier_trip():
    for tier in TIERS_MOST_URGENT_FIRST[1:]:
        assert batch_eligible(tier)


def test_batch_eligibility_accepts_a_plain_string():
    assert batch_eligible("ROUTINE")
    assert not batch_eligible("CRITICAL")


def test_cold_chain_clamps_the_detour_budget_to_zero_for_every_tier():
    """Refrigeration is a product-integrity limit, so it overrides the tier rather than ranking.

    An elective insulin pen does not earn twelve spare minutes out of the
    cold box just because nobody is in danger this hour.
    """
    for tier in TIERS_MOST_URGENT_FIRST:
        assert cold_chain_overrides(tier, requires_cold_chain=True) == 0


def test_an_ambient_order_keeps_the_full_budget_of_its_tier():
    for tier in TIERS_MOST_URGENT_FIRST:
        assert cold_chain_overrides(tier, requires_cold_chain=False) == detour_budget_minutes(tier)


def test_cold_chain_override_accepts_a_plain_string():
    assert cold_chain_overrides("ELECTIVE", requires_cold_chain=False) == 12
    assert cold_chain_overrides("ELECTIVE", requires_cold_chain=True) == 0


def test_scarcity_is_rationed_by_sequence_and_never_by_price():
    """Why this module exists at all.

    A ride-hailing platform rations excess demand with money: the fare rises,
    the marginal rider gives up, and the market clears. That half of surge
    pricing is unavailable to a pharmacy. Medicine prices are capped by the
    printed MRP and by the DPCO/NPPA ceilings, so there is no headroom to
    raise a price with -- and pricing a rescue inhaler out of someone's reach
    during a heatwave would be the harm, not the mechanism working.

    Sequencing is the lawful substitute. When supply is short the only thing
    that moves is the ORDER in which baskets are served, decided by clinical
    need; what the patient pays does not move at all. So this module offers a
    queue position, a detour tolerance and a batching verdict -- and no price
    lever of any kind.
    """
    heatwave_queue = [
        ClinicalUrgency.ELECTIVE,
        ClinicalUrgency.CRITICAL,
        ClinicalUrgency.ROUTINE,
    ]
    assert sorted(heatwave_queue, key=dispatch_priority)[0] is ClinicalUrgency.CRITICAL

    price_levers = [
        name
        for name in vars(triage)
        if not name.startswith("__")
        and any(word in name.lower() for word in ("price", "surge", "fare", "fee", "multiplier"))
    ]
    assert price_levers == []
