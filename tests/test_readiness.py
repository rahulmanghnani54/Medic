"""When the counter is ready, and how late a courier can be summoned for it.

Pure arithmetic over rolling means and configured constants: nothing here
touches a database, and every call takes `now`, so a fixed instant keeps the
whole suite independent of the hour it runs.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from medic.config import Settings, settings
from medic.domain import geo
from medic.domain.readiness import (
    MIN_RECENT_SAMPLES,
    RECENT_WEIGHT,
    PrepEstimate,
    ReadinessPrediction,
    blend_prep,
    courier_summon_at,
    predict_ready_at,
    verification_minutes,
)

#: Noon IST, matching the rest of the suite. Nothing in this module reads a
#: clock, so the instant is arbitrary -- it just has to be fixed.
NOON = datetime(2026, 10, 20, 6, 30, tzinfo=UTC)  # 12:00 IST

FALLBACK_MINUTES = settings.default_chemist_prep_minutes
UNQUEUED_MINUTES = settings.rx_verification_target_minutes
PER_QUEUED_MINUTES = settings.verification_minutes_per_queued_order
EARLY_ARRIVAL_MINUTES = settings.courier_early_arrival_minutes

#: Sample counts and the label each one earns, including both sides of every
#: threshold the module draws.
CONFIDENCE_LADDER: tuple[tuple[int, str], ...] = (
    (0, "none"),
    (1, "low"),
    (9, "low"),
    (10, "medium"),
    (39, "medium"),
    (40, "high"),
    (400, "high"),
)


def after(minutes: int) -> datetime:
    """The moment `minutes` after this prediction was made."""
    return NOON + timedelta(minutes=minutes)


def history(minutes: int, *, samples: int = 50) -> PrepEstimate:
    """A chemist whose prep history plans for exactly `minutes`."""
    return blend_prep(float(minutes), samples, None, 0)


def prediction(*, prep: PrepEstimate, queue_depth: int = 0) -> ReadinessPrediction:
    """A prediction made at NOON from one prep estimate and one queue depth."""
    minutes = verification_minutes(queue_depth)
    return ReadinessPrediction(
        predicted_at=NOON,
        ready_at=predict_ready_at(NOON, prep=prep, verification_minutes=minutes),
        prep=prep,
        verification_minutes=minutes,
    )


# --- Prep with no history to go on ---------------------------------------


def test_a_chemist_with_no_history_falls_back_to_the_configured_prep_default():
    assert blend_prep(None, 0, None, 0).minutes == FALLBACK_MINUTES


def test_the_fallback_is_read_from_the_config_handed_to_the_blend():
    """`blend_prep` is the one place config is read, so `.minutes` stays a property."""
    slow = Settings(default_chemist_prep_minutes=25)
    estimate = blend_prep(None, 0, None, 0, config=slow)
    assert estimate.fallback_minutes == 25
    assert estimate.minutes == 25


def test_an_average_with_no_rows_behind_it_is_not_treated_as_history():
    """A SQL `AVG` over no rows arrives paired with a count of zero, and the count wins."""
    assert blend_prep(99.0, 0, None, 0).minutes == FALLBACK_MINUTES
    assert blend_prep(None, 0, 99.0, 0).minutes == FALLBACK_MINUTES


# --- Prep as a blend of two windows --------------------------------------


def test_the_long_mean_carries_the_estimate_when_only_it_has_samples():
    assert blend_prep(14.2, 60, None, 0).minutes == 15


def test_a_chemists_own_handful_of_baskets_beats_the_platform_wide_default():
    """One recent basket of this chemist's own outranks a default about no chemist."""
    assert blend_prep(None, 0, 30.0, 1).minutes == 30
    assert blend_prep(None, 0, 30.0, 1).minutes != FALLBACK_MINUTES


def test_the_recent_window_outweighs_the_long_one_once_it_has_enough_samples():
    """What changed since yesterday is the queue standing in the shop right now."""
    estimate = blend_prep(10.0, 100, 40.0, MIN_RECENT_SAMPLES)
    expected = math.ceil(40.0 * RECENT_WEIGHT + 10.0 * (1.0 - RECENT_WEIGHT))
    assert estimate.minutes == expected == 31
    assert abs(estimate.minutes - 40) < abs(estimate.minutes - 10)


def test_a_thin_recent_window_is_ignored_rather_than_left_to_swing_the_estimate():
    """One awkward basket would otherwise move the estimate for every order behind it."""
    estimate = blend_prep(10.0, 100, 40.0, MIN_RECENT_SAMPLES - 1)
    assert estimate.minutes == 10  # the multi-day mean, unmoved by the spike


def test_prep_minutes_are_rounded_up_to_the_integer_budgets_the_domain_works_in():
    """Rounding up keeps these comparable with the minute budgets, and nothing more.

    The bias that protects the package lives in `courier_early_arrival_minutes`,
    in one place, so this rounding must not be read as a second safety margin.
    """
    assert blend_prep(12.1, 20, None, 0).minutes == 13
    assert blend_prep(None, 0, 9.2, 5).minutes == 10
    assert blend_prep(10.0, 100, 12.0, MIN_RECENT_SAMPLES).minutes == 12  # 11.4 blended


# --- How much history backed the estimate --------------------------------


def test_confidence_climbs_through_its_labels_with_the_baskets_behind_the_estimate():
    for samples, label in CONFIDENCE_LADDER:
        assert prediction(prep=blend_prep(18.0, samples, None, 0)).confidence == label, samples


def test_the_recent_window_is_compared_with_the_long_one_rather_than_added_to_it():
    """The last hour is a subset of the multi-day window, so adding would double-count.

    Thirty baskets of which twenty landed in the last hour is thirty baskets of
    history, not fifty, and only the comparison keeps the label honest at the
    thresholds.
    """
    estimate = blend_prep(18.0, 30, 18.0, 20)
    assert prediction(prep=estimate).confidence == "medium"


def test_confidence_reports_how_much_history_backed_a_prediction_not_how_accurate_it_is():
    """An ordinal label for a sample count, never a scored accuracy.

    No prediction this module makes has yet been measured against a realized
    arrival, so two predictions can name the same minute with wildly different
    amounts of evidence behind them. The label is for deciding how much slack to
    quote on top, and is misleading read as a probability.
    """
    thin = prediction(prep=history(20, samples=1))
    thick = prediction(prep=history(20, samples=80))
    assert thin.ready_at == thick.ready_at
    assert thin.confidence == "low"
    assert thick.confidence == "high"


# --- The pharmacist queue ------------------------------------------------


def test_an_unqueued_basket_waits_exactly_the_flat_verification_target():
    """Depth 1 counts this order's own verification with nobody ahead of it."""
    assert verification_minutes(1) == UNQUEUED_MINUTES


def test_the_two_modules_quote_the_same_verification_for_an_unqueued_order():
    """`geo.estimate_eta` quotes the flat target, so the unqueued case must match it.

    The modules are consulted at different moments -- one at checkout, one at
    dispatch -- and an order whose verification estimate changed between them
    with nothing having happened would be a disagreement, not a refinement.
    """
    eta = geo.estimate_eta(2.0, requires_verification=True)
    assert verification_minutes(1) == eta.verification_minutes


def test_verification_grows_with_every_order_already_queued_ahead_of_this_one():
    """The queue is the part of the hour no amount of courier positioning recovers."""
    assert verification_minutes(2) == UNQUEUED_MINUTES + PER_QUEUED_MINUTES
    for depth in range(1, 8):
        expected = UNQUEUED_MINUTES + (depth - 1) * PER_QUEUED_MINUTES
        assert verification_minutes(depth) == expected, depth


def test_the_queue_is_divided_across_the_pharmacists_working_it():
    assert verification_minutes(5, pharmacists_on_duty=1) == UNQUEUED_MINUTES + 12
    assert verification_minutes(5, pharmacists_on_duty=2) == UNQUEUED_MINUTES + 6


def test_a_part_filled_round_of_verifications_still_costs_a_whole_round():
    """Three baskets ahead across two pharmacists is two rounds, not one and a half.

    Half a round is not half a wait: the second pharmacist being idle for part of
    it does not get this order looked at any sooner.
    """
    assert verification_minutes(4, pharmacists_on_duty=2) == UNQUEUED_MINUTES + 6
    assert verification_minutes(4, pharmacists_on_duty=2) == verification_minutes(
        5, pharmacists_on_duty=2
    )


def test_a_basket_that_needs_no_pharmacist_waits_no_verification_time_at_all():
    assert verification_minutes(0) == 0


def test_verification_refuses_to_model_a_queue_that_nobody_is_working():
    """Clamping to one pharmacist would make `ready_at` fiction rather than an estimate."""
    with pytest.raises(ValueError, match="at least one pharmacist"):
        verification_minutes(4, pharmacists_on_duty=0)


def test_the_cost_of_each_queued_order_is_configurable():
    slower = Settings(verification_minutes_per_queued_order=5)
    assert verification_minutes(3, config=slower) == UNQUEUED_MINUTES + 10


# --- Prep and verification overlap ---------------------------------------


def test_prep_and_verification_run_concurrently_so_the_slower_gate_sets_ready_at():
    """The counter picks the basket while the pharmacist clears the prescription.

    Adding the two would charge the order twice for one window and push every
    predicted `ready_at` past what the shop actually does.
    """
    slow_prep = predict_ready_at(NOON, prep=history(25), verification_minutes=10)
    slow_queue = predict_ready_at(NOON, prep=history(10), verification_minutes=25)
    assert slow_prep == after(25)
    assert slow_queue == after(25)
    assert slow_prep != after(35)  # not the sum


def test_ready_at_never_disagrees_with_the_eta_breakdown_about_the_same_two_gates():
    """One concurrency assumption, held in `geo.EtaBreakdown` and read from there.

    `readiness` deliberately does not restate it, so the property worth pinning
    is the agreement rather than the `max` literal: the two would drift apart the
    first time one of them was revised.
    """
    for prep_minutes in range(0, 41, 5):
        for queue_minutes in range(0, 41, 5):
            breakdown = geo.EtaBreakdown(
                distance_km=0.0,
                road_km=0.0,
                prep_minutes=prep_minutes,
                travel_minutes=0,
                verification_minutes=queue_minutes,
            )
            ready_at = predict_ready_at(
                NOON, prep=history(prep_minutes), verification_minutes=queue_minutes
            )
            assert ready_at == after(breakdown.total_minutes), (prep_minutes, queue_minutes)


def test_ready_at_is_when_the_goods_are_ready_and_not_when_they_could_reach_anyone():
    """Travel is absent on purpose: this is the moment the courier is collecting from."""
    eta = geo.estimate_eta(3.0, requires_verification=True)
    assert eta.travel_minutes > 0
    ready_at = predict_ready_at(
        NOON, prep=history(eta.prep_minutes), verification_minutes=eta.verification_minutes
    )
    assert ready_at == after(eta.total_minutes - eta.travel_minutes)


def test_a_prediction_carries_the_parts_it_was_built_from_so_it_can_be_replayed():
    """`predicted_at` is a field rather than a clock read, so the residual stays computable.

    Scoring a prediction later means joining it to a realized arrival, and that
    join needs to know when the prediction was made without guessing.
    """
    entry = prediction(prep=history(18), queue_depth=4)
    assert entry.predicted_at == NOON
    assert entry.verification_minutes == verification_minutes(4)
    assert entry.ready_at - entry.predicted_at == timedelta(
        minutes=max(entry.prep.minutes, entry.verification_minutes)
    )


# --- Scheduling the courier backwards ------------------------------------


def test_the_courier_is_summoned_backwards_from_ready_at_rather_than_forwards_from_now():
    ready_at = after(30)
    summon_at = courier_summon_at(ready_at, 9)
    assert summon_at == ready_at - timedelta(minutes=9 + EARLY_ARRIVAL_MINUTES)
    assert summon_at == after(30 - 9 - EARLY_ARRIVAL_MINUTES)
    assert summon_at > NOON  # nobody is sent at order time


def test_the_courier_is_asked_to_arrive_a_little_before_the_package_is_ready():
    """The slack leans one way on purpose: a courier waiting a little beats a package waiting."""
    ready_at = after(30)
    arrival = courier_summon_at(ready_at, 9) + timedelta(minutes=9)
    assert arrival == ready_at - timedelta(minutes=EARLY_ARRIVAL_MINUTES)
    generous = Settings(courier_early_arrival_minutes=10)
    assert courier_summon_at(ready_at, 9, config=generous) == after(30 - 9 - 10)


def test_a_further_courier_is_started_earlier_for_the_very_same_counter():
    ready_at = after(40)
    assert courier_summon_at(ready_at, 15) < courier_summon_at(ready_at, 5)


def test_a_summon_time_already_in_the_past_is_a_signal_to_send_someone_now():
    """Not an error: this layer holds no clock, so comparing is the caller's job.

    A courier 20 minutes away from a counter that is ready in 5 is already late,
    and clamping the answer to now would hide exactly that.
    """
    summon_at = courier_summon_at(after(5), 20)
    assert summon_at < NOON
    assert summon_at == after(5 - 20 - EARLY_ARRIVAL_MINUTES)


def test_summoning_the_nearest_courier_at_order_time_only_moves_where_the_waiting_happens():
    """The insight the module exists for: the pickup is not ready when the order arrives.

    A ride-hailing dispatcher can send the nearest driver the instant a rider
    taps, because the rider is already standing at the kerb. A chemist counter is
    not: the package becomes ready at a predicted future time gated by prep and
    by a pharmacist queue. So summoning immediately buys the customer nothing --
    it converts courier idle time into courier-waiting-at-a-counter time, both
    spent inside the same 60-minute budget, and the second kind also holds a
    courier out of the pool for the next order. The handover is identical either
    way; the only thing the eager summon spends is courier capacity.
    """
    travel = 9
    ready_at = predict_ready_at(NOON, prep=history(25), verification_minutes=10)
    summon_at = courier_summon_at(ready_at, travel)

    waiting_if_summoned_now = ready_at - (NOON + timedelta(minutes=travel))
    waiting_if_summoned_late = ready_at - (summon_at + timedelta(minutes=travel))
    assert waiting_if_summoned_now == timedelta(minutes=16)
    assert waiting_if_summoned_late == timedelta(minutes=EARLY_ARRIVAL_MINUTES)
    # Every minute of delay in summoning is a minute the courier does not spend
    # standing at a counter, and none of it is taken from the customer.
    assert waiting_if_summoned_now - waiting_if_summoned_late == summon_at - NOON
