"""Chemist matching and the feasibility of the 60-minute promise."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from medic.config import Settings
from medic.domain.geo import estimate_eta, haversine_km, promise_is_feasible
from medic.domain.matching import BasketLine, ChemistCandidate, find_matches, is_open_at
from medic.domain.schedules import DrugSchedule, NotDispensableOnline

MALVIYA = (26.8505, 75.8060)
NOON_IST = datetime(2026, 10, 20, 6, 30, tzinfo=UTC)  # 12:00 IST
THREE_AM_IST = datetime(2026, 10, 20, 21, 30, tzinfo=UTC)  # 03:00 IST next day


def chemist(cid=1, *, lat=MALVIYA[0], lng=MALVIYA[1], stock=None, **kwargs):
    defaults = dict(
        name=f"Chemist {cid}",
        zone="Malviya Nagar",
        is_active=True,
        is_24x7=True,
        opens_hour_ist=8,
        closes_hour_ist=22,
        can_cold_chain=False,
    )
    defaults.update(kwargs)
    return ChemistCandidate(
        chemist_id=cid,
        lat=lat,
        lng=lng,
        stock={1: 10} if stock is None else stock,
        **defaults,
    )


def basket(schedule=DrugSchedule.OTC, qty=1, cold=False):
    return [
        BasketLine(
            medicine_id=1,
            medicine_name="Test Tablet",
            qty=qty,
            schedule=schedule,
            requires_cold_chain=cold,
        )
    ]


def match(candidates, *, at=NOON_IST, lines=None, config=None):
    return find_matches(
        basket() if lines is None else lines,
        delivery_lat=MALVIYA[0],
        delivery_lng=MALVIYA[1],
        candidates=candidates,
        at=at,
        config=config or Settings(),
    )


def test_nearest_stocked_chemist_wins():
    near = chemist(1)
    far = chemist(2, lat=26.8800, lng=75.8400)
    assert match([far, near]).best.candidate.chemist_id == 1


def test_a_chemist_outside_the_radius_is_rejected_with_a_reason():
    result = match([chemist(1, lat=26.9500, lng=75.9000)])
    assert result.best is None
    assert "outside radius" in result.rejected[0][1]


def test_an_inactive_chemist_is_skipped():
    assert match([chemist(1, is_active=False)]).best is None


def test_an_expired_drug_licence_disqualifies_a_chemist():
    expired = chemist(1, licence_expires_on=NOON_IST - timedelta(days=1))
    result = match([expired])
    assert result.best is None
    assert "licence expired" in result.rejected[0][1]


def test_a_licence_expiring_later_is_fine():
    valid = chemist(1, licence_expires_on=NOON_IST + timedelta(days=30))
    assert match([valid]).best is not None


def test_a_closed_chemist_is_skipped_at_3am():
    result = match([chemist(1, is_24x7=False, opens_hour_ist=8, closes_hour_ist=22)],
                   at=THREE_AM_IST)
    assert result.best is None
    assert "closed" in result.rejected[0][1]


def test_a_24x7_chemist_is_matched_at_3am():
    assert match([chemist(1, is_24x7=True)], at=THREE_AM_IST).best is not None


def test_overnight_opening_hours_wrap_past_midnight():
    late = chemist(1, is_24x7=False, opens_hour_ist=20, closes_hour_ist=4)
    assert is_open_at(late, THREE_AM_IST)
    assert not is_open_at(late, NOON_IST)


def test_cold_chain_items_only_go_to_cold_chain_chemists():
    plain = chemist(1, can_cold_chain=False)
    cold = chemist(2, can_cold_chain=True)
    result = match([plain, cold], lines=basket(cold=True))
    assert result.best.candidate.chemist_id == 2


def test_insufficient_stock_is_a_partial_fill_not_a_match():
    result = match([chemist(1, stock={1: 1})], lines=basket(qty=5))
    assert result.best is None
    assert result.partial_fills[0].missing == ("Test Tablet",)


def test_partial_fills_are_ranked_by_how_much_they_cover():
    lines = [
        BasketLine(1, "A", 1, DrugSchedule.OTC),
        BasketLine(2, "B", 1, DrugSchedule.OTC),
    ]
    half = chemist(1, stock={1: 5})
    none = chemist(2, stock={})
    result = match([none, half], lines=lines)
    assert result.best is None
    assert result.partial_fills[0].candidate.chemist_id == 1
    assert result.partial_fills[0].fill_ratio == 0.5


def test_schedule_x_is_refused_before_any_chemist_is_considered():
    with pytest.raises(NotDispensableOnline):
        match([chemist(1)], lines=basket(schedule=DrugSchedule.X))


def test_an_empty_basket_is_rejected():
    with pytest.raises(ValueError):
        match([chemist(1)], lines=[])


def test_verification_time_is_counted_against_the_promise():
    distance = haversine_km(*MALVIYA, 26.8600, 75.8200)
    plain = estimate_eta(distance, requires_verification=False)
    with_rx = estimate_eta(distance, requires_verification=True)
    assert with_rx.total_minutes >= plain.total_minutes


def test_prep_and_verification_run_in_parallel():
    """Verification happens while the chemist packs, so the two do not add up."""
    eta = estimate_eta(1.0, requires_verification=True, config=Settings())
    assert eta.total_minutes < (
        eta.prep_minutes + eta.verification_minutes + eta.travel_minutes
    )


def test_a_distant_chemist_leaves_no_safety_margin():
    config = Settings()
    assert not promise_is_feasible(estimate_eta(20.0, requires_verification=True), config)
    assert promise_is_feasible(estimate_eta(1.0, requires_verification=True), config)


def test_better_acceptance_rate_breaks_an_eta_tie():
    reliable = chemist(1, acceptance_rate=0.95, on_time_rate=0.95)
    flaky = chemist(2, acceptance_rate=0.40, on_time_rate=0.50)
    assert match([flaky, reliable]).best.candidate.chemist_id == 1
