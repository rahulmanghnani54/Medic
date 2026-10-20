"""Price ceilings, commission band and contribution."""

from __future__ import annotations

import pytest

from medic.config import Settings
from medic.domain.money import apply_bps, rupees
from medic.domain.pricing import (
    CommissionOutOfBand,
    PriceCapBreach,
    break_even_orders_per_month,
    contribution_for,
    price_line,
    total_order,
)

HUNDRED_RUPEES = 10_000  # paise


def line(mrp=HUNDRED_RUPEES, ceiling=None, discount_bps=0, qty=1, unit_price=None):
    return price_line(
        medicine_id=1,
        medicine_name="Test Tablet",
        qty=qty,
        mrp_paise=mrp,
        dpco_ceiling_paise=ceiling,
        discount_bps=discount_bps,
        unit_price_paise=unit_price,
    )


def test_apply_bps_rounds_half_up():
    assert apply_bps(100, 1000) == 10
    assert apply_bps(45, 1000) == 5  # 4.5 paise rounds up


def test_price_at_mrp_is_allowed():
    assert line().unit_price_paise == rupees(100)


def test_dpco_ceiling_binds_automatically():
    """A capped formulation is sold at its ceiling, not at MRP.

    The cap applies whether or not anyone remembers to apply it, so this is the
    default path rather than an error path.
    """
    priced = line(mrp=rupees(100), ceiling=rupees(80))
    assert priced.cap_paise == rupees(80)
    assert priced.unit_price_paise == rupees(80)


def test_a_ceiling_above_mrp_does_not_raise_the_price():
    priced = line(mrp=rupees(100), ceiling=rupees(150))
    assert priced.unit_price_paise == rupees(100)


def test_discount_applies_below_the_cap_not_below_mrp():
    priced = line(mrp=rupees(100), ceiling=rupees(80), discount_bps=2500)
    assert priced.unit_price_paise == rupees(60)


def test_the_mrp_to_cap_gap_is_not_counted_as_a_chemist_discount():
    """The price control is not a concession anyone funded."""
    priced = line(mrp=rupees(100), ceiling=rupees(80), qty=2)
    assert priced.statutory_reduction_paise == rupees(40)
    assert priced.discount_paise == 0


def test_an_explicit_price_above_the_cap_is_refused():
    with pytest.raises(PriceCapBreach):
        line(mrp=rupees(100), ceiling=rupees(80), unit_price=rupees(90))


def test_an_explicit_price_above_mrp_is_refused_even_with_no_dpco_ceiling():
    with pytest.raises(PriceCapBreach):
        line(mrp=rupees(100), unit_price=rupees(120))


def test_an_explicit_price_at_the_cap_is_allowed():
    assert line(mrp=rupees(100), ceiling=rupees(80), unit_price=rupees(80)).unit_price_paise == (
        rupees(80)
    )


def test_a_negative_price_is_refused():
    with pytest.raises(ValueError):
        line(unit_price=-1)


def test_quantity_must_be_positive():
    with pytest.raises(ValueError):
        line(qty=0)


def test_chemist_funded_discount_is_tracked_separately():
    priced = line(mrp=rupees(200), discount_bps=1000, qty=2)
    totals = total_order([priced], commission_bps=1000)
    assert totals.subtotal_paise == rupees(360)
    assert totals.chemist_funded_discount_paise == rupees(40)
    assert totals.platform_funded_discount_paise == 0


def test_commission_follows_the_discounted_subtotal():
    """A chemist-funded discount reduces our commission with it."""
    full = total_order([line(mrp=rupees(200))], commission_bps=1000)
    discounted = total_order([line(mrp=rupees(200), discount_bps=1000)], commission_bps=1000)
    assert discounted.commission_paise < full.commission_paise


def test_commission_outside_the_agreed_band_is_refused():
    with pytest.raises(CommissionOutOfBand):
        total_order([line()], commission_bps=2500)
    with pytest.raises(CommissionOutOfBand):
        total_order([line()], commission_bps=100)


def test_delivery_fee_waived_above_the_threshold():
    config = Settings()
    big = total_order(
        [line(mrp=config.free_delivery_threshold_paise)], commission_bps=1000, config=config
    )
    assert big.delivery_fee_paise == 0
    small = total_order([line(mrp=rupees(100))], commission_bps=1000, config=config)
    assert small.delivery_fee_paise == config.delivery_fee_paise


def test_platform_discount_cannot_exceed_order_value():
    with pytest.raises(ValueError):
        total_order(
            [line(mrp=rupees(100))],
            commission_bps=1000,
            platform_funded_discount_paise=rupees(10_000),
        )


def test_a_small_order_does_not_cover_its_rider():
    """The brief's core economic claim: small fast orders are costly to deliver.

    A Rs 100 order earns Rs 10 commission plus Rs 25 delivery fee against a
    Rs 40 rider, so it loses money. This is a property of the configured
    assumptions, and the test exists so that changing them is a deliberate act.
    """
    totals = total_order([line(mrp=rupees(100))], commission_bps=1000)
    assert contribution_for(totals).contribution_paise < 0


def test_a_chronic_care_refill_does_cover_its_rider():
    totals = total_order([line(mrp=rupees(1500))], commission_bps=1000)
    assert contribution_for(totals).contribution_paise > 0


def test_break_even_is_undefined_when_contribution_is_negative():
    assert break_even_orders_per_month(-100) is None
    assert break_even_orders_per_month(0) is None


def test_break_even_scales_with_contribution():
    assert break_even_orders_per_month(1000) > break_even_orders_per_month(2000)


def test_the_models_contribution_matches_the_brief_at_its_stated_aov():
    """Pins the model against the brief's section 6 worked example.

    The brief assumes a Rs 450 order yielding roughly Rs 16 contribution. This
    test exists so that a change to any cost assumption in config.py shows up as
    a deliberate break against the figure the business case was argued on.

    We land on Rs 15.50 rather than Rs 16.00 because the gateway fee is charged
    on the amount actually collected (order plus delivery fee) rather than on
    order value alone.
    """
    priced = line(mrp=rupees(450))
    totals = total_order([priced], commission_bps=1000)
    contribution = contribution_for(totals)
    assert contribution.revenue_commission_paise == rupees(45)
    assert contribution.revenue_delivery_fee_paise == rupees(25)
    assert contribution.cost_rider_paise == rupees(40)
    assert contribution.contribution_paise == rupees(15.50)


def test_break_even_matches_the_briefs_stated_range():
    """The brief claims 40,000-50,000 orders a month against Rs 6-8 lakh fixed."""
    priced = line(mrp=rupees(450))
    totals = total_order([priced], commission_bps=1000)
    contribution = contribution_for(totals).contribution_paise
    orders = break_even_orders_per_month(contribution)
    assert 40_000 <= orders <= 50_000


def test_the_free_delivery_threshold_creates_a_contribution_trough():
    """A known flaw in the current pricing, pinned so it is not forgotten.

    Just above the free-delivery threshold we give up the whole Rs 25 fee while
    commission rises by only about Rs 15, so an order worth Rs 600 contributes
    LESS than one worth Rs 450. Either the threshold or the fee needs rethinking
    once real margins are known; see docs/economics.md.
    """
    config = Settings()
    just_below = contribution_for(
        total_order([line(mrp=rupees(450))], commission_bps=1000, config=config), config=config
    )
    just_above = contribution_for(
        total_order([line(mrp=rupees(600))], commission_bps=1000, config=config), config=config
    )
    assert just_above.contribution_paise < just_below.contribution_paise
