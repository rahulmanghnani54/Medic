"""The promise as a budget: what is left of it, what it admits, and where the hour went."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from medic.config import Settings, settings
from medic.domain import geo
from medic.domain.deadlines import (
    BAND_GRAIN_MINUTES,
    STAGES,
    BudgetLedger,
    DeliveryBudget,
    promise_band,
)
from medic.domain.orders import sla_view

#: Noon IST. Nothing in this module reads a clock -- every call takes `now` --
#: so a fixed instant keeps the arithmetic independent of the hour the suite runs.
NOON = datetime(2026, 10, 20, 6, 30, tzinfo=UTC)  # 12:00 IST

PROMISE_MINUTES = settings.delivery_promise_minutes
MARGIN_MINUTES = settings.sla_feasibility_margin_minutes
ACCEPT_WINDOW_MINUTES = settings.chemist_accept_window_minutes

BUDGET = DeliveryBudget(created_at=NOON, promised_minutes=PROMISE_MINUTES)


def after(minutes: int) -> datetime:
    """The moment `minutes` after this order was taken."""
    return NOON + timedelta(minutes=minutes)


def maybe_after(minutes: int | None) -> datetime | None:
    return None if minutes is None else after(minutes)


def ledger(
    *,
    as_of: int = PROMISE_MINUTES,
    accepted_at: int | None = None,
    verified_at: int | None = None,
    ready_at: int | None = None,
    delivered_at: int | None = None,
) -> BudgetLedger:
    """A ledger on BUDGET, with every milestone given as minutes after checkout."""
    return BudgetLedger(
        budget=BUDGET,
        as_of=after(as_of),
        accepted_at=maybe_after(accepted_at),
        verified_at=maybe_after(verified_at),
        ready_at=maybe_after(ready_at),
        delivered_at=maybe_after(delivered_at),
    )


def predicted(total_minutes: int) -> geo.EtaBreakdown:
    """An ETA estimate whose total is exactly `total_minutes`."""
    return geo.EtaBreakdown(
        distance_km=0.0,
        road_km=0.0,
        prep_minutes=0,
        travel_minutes=total_minutes,
        verification_minutes=0,
    )


#: Milestone sets worth holding the partition against: complete, still in
#: flight, and with each kind of recording gap. Minutes after checkout.
LEDGER_SHAPES: tuple[dict[str, int], ...] = (
    {},
    {"accepted_at": 10},
    {"accepted_at": 10, "verified_at": 25},
    {"accepted_at": 10, "verified_at": 25, "ready_at": 40},
    {"accepted_at": 10, "verified_at": 25, "ready_at": 40, "delivered_at": 55},
    {"accepted_at": 10, "ready_at": 40, "delivered_at": 55},
    {"ready_at": 40, "delivered_at": 55},
    {"accepted_at": 10, "delivered_at": 55},
    {"accepted_at": 10, "verified_at": 25, "delivered_at": 55},
    {"verified_at": 25, "ready_at": 40},
    {"accepted_at": 30, "verified_at": 15, "ready_at": 20, "delivered_at": 55},
)


# --- What is left of the promise -----------------------------------------


def test_the_deadline_is_the_promise_added_to_the_order_time():
    assert BUDGET.deadline == after(PROMISE_MINUTES)


def test_the_budget_counts_down_from_when_the_order_was_taken():
    assert BUDGET.remaining_minutes(after(20)) == 40
    assert not BUDGET.is_breached(after(20))


def test_the_budget_is_spent_but_unbroken_at_the_deadline_itself():
    """Nothing left is still a promise kept, so the breach cannot start here."""
    assert BUDGET.remaining_minutes(after(PROMISE_MINUTES)) == 0
    assert not BUDGET.is_breached(after(PROMISE_MINUTES))


def test_the_budget_runs_negative_once_the_promise_is_broken():
    assert BUDGET.remaining_minutes(after(75)) == -15
    assert BUDGET.is_breached(after(75))


def test_a_breach_begins_the_instant_the_deadline_passes():
    """Floored to the minute, as in `orders.sla_view`: a part-minute overrun is an overrun."""
    a_second_late = BUDGET.deadline + timedelta(seconds=1)
    assert BUDGET.remaining_minutes(a_second_late) == -1
    assert BUDGET.is_breached(a_second_late)


def test_spent_fraction_tracks_the_share_of_the_hour_consumed():
    assert BUDGET.spent_fraction(after(30)) == 0.5


def test_spent_fraction_is_left_uncapped_once_the_promise_is_blown():
    """The console needs to see how far past the hour an order ran, not merely that it did."""
    assert BUDGET.spent_fraction(after(90)) == 1.5
    assert BUDGET.spent_fraction(after(120)) == 2.0


def test_spent_fraction_is_clamped_at_zero_before_the_clock_starts():
    """A milestone stamped a minute early must not read as negative spend."""
    assert BUDGET.spent_fraction(NOON - timedelta(minutes=5)) == 0.0


def test_a_nonsense_promise_is_treated_as_spent_rather_than_dividing_by_zero():
    """A zero or negative promise is a data defect, and the console still has to render it."""
    for promised in (0, -15):
        budget = DeliveryBudget(created_at=NOON, promised_minutes=promised)
        assert budget.spent_fraction(after(10)) == 1.0


# --- The at-risk threshold -----------------------------------------------


def test_an_order_turns_at_risk_fifteen_minutes_before_the_deadline():
    assert BUDGET.remaining_minutes(after(44)) == 16
    assert not BUDGET.at_risk(after(44))
    assert BUDGET.remaining_minutes(after(45)) == 15
    assert BUDGET.at_risk(after(45))


def test_the_budget_and_the_customer_facing_sla_view_never_disagree_about_risk():
    """`at_risk` reads the threshold off `orders.SlaView` instead of restating the 15.

    A budget calling an order at risk while the SLA view the customer sees did
    not would have the console and the customer reading one order two different
    ways, and the two would drift apart the first time the threshold moved. The
    agreement, not the literal, is the property worth pinning.
    """
    for elapsed in range(0, 91):
        now = after(elapsed)
        assert BUDGET.at_risk(now) is sla_view(NOON, PROMISE_MINUTES, now).at_risk


def test_a_breached_order_is_no_longer_merely_at_risk():
    """The two are exclusive: escalating a breach and warning about one are different jobs."""
    assert BUDGET.is_breached(after(75))
    assert not BUDGET.at_risk(after(75))


# --- What the remaining budget admits ------------------------------------


def test_work_that_cleared_the_full_hour_is_refused_once_an_offer_round_has_timed_out():
    """The bug this prevents: feasibility judged once, at checkout, against the whole hour.

    `geo.promise_is_feasible` asks whether a candidate fits the FULL 60-minute
    promise, and checkout asks it once. A chemist predicted to need 48 minutes
    clears it -- 48 plus the 10-minute margin fits in 60 -- and nothing re-asks
    the question, so that same candidate is still judged feasible after an offer
    round has timed out. By then five of the customer's minutes have gone to a
    chemist who never answered, 48 plus 10 no longer fits the 55 that are left,
    and offering anyway spends a further accept window on someone who was never
    going to make the window. `admits` asks against what is actually left.
    """
    assert geo.promise_is_feasible(predicted(48))
    assert BUDGET.admits(48, NOON)
    assert not BUDGET.admits(48, after(ACCEPT_WINDOW_MINUTES))


def test_against_an_untouched_budget_admits_matches_the_checkout_feasibility_check():
    """At t0 what remains IS the whole promise, so the per-attempt form cannot differ."""
    for work in range(0, PROMISE_MINUTES + 11):
        assert BUDGET.admits(work, NOON) is geo.promise_is_feasible(predicted(work))


def test_admits_keeps_the_same_safety_margin_the_checkout_check_applies():
    assert BUDGET.admits(PROMISE_MINUTES - MARGIN_MINUTES, NOON)
    assert not BUDGET.admits(PROMISE_MINUTES - MARGIN_MINUTES + 1, NOON)


def test_an_explicit_margin_overrides_the_configured_one():
    """The dispatcher can ask the bare arithmetic question when that is what it means."""
    assert not BUDGET.admits(55, NOON)
    assert BUDGET.admits(55, NOON, margin_minutes=0)


def test_a_breached_budget_admits_no_work_at_all():
    """Not even instantaneous work: there is no promise left to spend on an offer."""
    assert not BUDGET.admits(0, after(75))


# --- The quoted band -----------------------------------------------------


def test_the_band_high_end_carries_one_timed_out_offer_round():
    """The low end assumes the first chemist accepts; the high end prices in one who does not.

    A quote is built from the best-ranked candidate's ETA, which is the
    optimistic case. One timed-out offer round costs the whole accept window
    before sourcing even moves on, so the accept window is what the high end
    adds -- read off the config rather than padded with a literal.
    """
    assert promise_band(40) == (40, 40 + ACCEPT_WINDOW_MINUTES)
    wider = Settings(chemist_accept_window_minutes=15)
    assert promise_band(40, config=wider) == (40, 55)


def test_the_band_is_rounded_outward_to_the_quoting_grain():
    """A band shifting minute by minute would claim a precision we do not have."""
    assert promise_band(43) == (40, 50)
    for eta in range(0, 61):
        low, high = promise_band(eta)
        assert low % BAND_GRAIN_MINUTES == 0
        assert high % BAND_GRAIN_MINUTES == 0


def test_the_band_always_contains_the_eta_it_was_built_from():
    for eta in range(0, 61):
        low, high = promise_band(eta)
        assert low <= eta <= high


def test_a_quote_that_clears_feasibility_never_bands_past_the_promise():
    """Feasibility is what decides the window is offered at all, so the band must fit inside it.

    Every ETA that clears `geo.promise_is_feasible` -- margin included -- has to
    still band inside 60 minutes once the accept window and the grain rounding
    are added, or the quote would contradict the promise it is quoting for.
    """
    feasible = [
        eta for eta in range(PROMISE_MINUTES + 1) if geo.promise_is_feasible(predicted(eta))
    ]
    assert feasible
    for eta in feasible:
        assert promise_band(eta)[1] <= PROMISE_MINUTES


def test_a_negative_eta_bands_from_zero():
    """A sub-zero estimate is nonsense, not a reason to quote a negative window."""
    assert promise_band(-4) == (0, ACCEPT_WINDOW_MINUTES)


# --- Where the hour went -------------------------------------------------


def test_prep_and_verification_split_the_fulfilment_window_rather_than_each_claiming_it():
    """The breakdown is a strict partition of the timeline, not per-stage wall clock.

    Verification and prep genuinely overlap -- the pharmacist clears the
    prescription while the counter picks the basket -- so charging each its own
    wall-clock span double-counts the window they share. On this order that
    reports sourcing 10 + verification 15 + prep 30 + transport 20 = 75 minutes
    of work inside an hour that elapsed 60, and it hands the console the wrong
    answer to "which gate held this order up".

    The convention instead cuts the acceptance-to-ready window once, at the
    pharmacist's clearance: verification until then, prep after, because once
    verification has cleared, prep is provably the only gate still holding it.
    """
    entry = ledger(as_of=60, accepted_at=10, verified_at=25, ready_at=40, delivered_at=60)
    parts = entry.breakdown()
    assert parts == {"sourcing": 10, "verification": 15, "prep": 15, "transport": 20}
    assert parts["verification"] + parts["prep"] == 30  # one window, cut in two
    assert sum(parts.values()) == entry.budget.elapsed_minutes(entry.horizon) == 60
    assert entry.unattributed_minutes == 0


def test_a_milestone_missing_while_a_later_one_is_known_is_charged_to_no_stage():
    """A gap in the record is charged to nothing, which is what makes unattributed real.

    The acceptance stamp is missing while the ready and delivery stamps are not,
    so the 40 minutes before the basket was ready cannot be divided between
    sourcing and fulfilment: nothing in the record says where one ended. Loading
    them onto whichever stage happens to sit beside the gap would read as a slow
    chemist or a slow pharmacist and send someone to fix the wrong thing. They
    are reported as unattributed instead, so `unattributed_minutes` means
    "minutes no stage can account for" rather than "rounding".
    """
    entry = ledger(as_of=60, ready_at=40, delivered_at=60)
    parts = entry.breakdown()
    assert parts["sourcing"] == 0
    assert parts["verification"] == 0
    assert parts["prep"] == 0
    assert parts["transport"] == 20  # the stages after the gap stay measurable
    assert entry.unattributed_minutes == 40
    assert sum(parts.values()) + entry.unattributed_minutes == 60


def test_a_stage_whose_milestone_is_merely_unreached_runs_to_the_horizon():
    """Not yet reached is not a gap: the open window belongs to the stage still holding it.

    That distinction is the whole value of `unattributed_minutes`. Here nothing
    later than the acceptance is known, so the order is simply still being
    prepared and those minutes are prep. Compare the gap case, where a LATER
    milestone is known and the same silence means the record is broken.
    """
    entry = ledger(as_of=30, accepted_at=10)
    parts = entry.breakdown()
    assert parts["sourcing"] == 10
    assert parts["prep"] == 20
    assert parts["transport"] == 0
    assert entry.unattributed_minutes == 0


def test_an_order_still_looking_for_a_chemist_charges_everything_elapsed_to_sourcing():
    entry = ledger(as_of=12)
    assert entry.breakdown() == {"sourcing": 12, "verification": 0, "prep": 0, "transport": 0}
    assert entry.unattributed_minutes == 0


def test_a_basket_that_needed_no_pharmacist_charges_its_whole_window_to_prep():
    """A missing clearance stamp is not time we can honestly call verification.

    Either the basket needed no pharmacist or none has finished yet; neither is
    verification work, and charging it would invent a gate this order never had.
    """
    entry = ledger(as_of=60, accepted_at=10, ready_at=40, delivered_at=60)
    parts = entry.breakdown()
    assert parts["verification"] == 0
    assert parts["prep"] == 30
    assert entry.unattributed_minutes == 0


def test_verification_can_never_outrun_the_window_it_is_cut_from():
    """Milestones come from separate actors, so a late clearance must not make prep negative."""
    entry = ledger(as_of=50, accepted_at=10, ready_at=20, verified_at=40, delivered_at=50)
    parts = entry.breakdown()
    assert parts["verification"] == 10  # the whole acceptance-to-ready window, no more
    assert parts["prep"] == 0
    assert sum(parts.values()) == 50


def test_a_delivered_orders_ledger_stops_at_handover():
    """Measuring a finished order to `now` would grow its unattributed time for ever."""
    entry = ledger(as_of=300, accepted_at=10, verified_at=25, ready_at=40, delivered_at=55)
    assert entry.horizon == after(55)
    assert entry.breakdown() == {"sourcing": 10, "verification": 15, "prep": 15, "transport": 15}
    assert entry.unattributed_minutes == 0


def test_a_ledger_replayed_mid_flight_attributes_nothing_past_its_as_of():
    """`as_of` is a field rather than a clock read, so last week's breach replays exactly."""
    entry = ledger(as_of=20, accepted_at=10, ready_at=40, delivered_at=55)
    assert entry.horizon == after(20)
    parts = entry.breakdown()
    assert parts["sourcing"] == 10
    assert parts["prep"] == 10
    assert parts["transport"] == 0
    assert sum(parts.values()) == entry.budget.elapsed_minutes(entry.horizon) == 20


def test_every_ledger_shape_accounts_for_exactly_the_elapsed_minutes():
    """Attributed plus unattributed equals elapsed, whatever the order has reached.

    Both of the module's conventions rest on this identity. Overlapping stages
    would push the attributed sum past the elapsed time, and a gap quietly
    charged to a neighbouring stage would hide inside it; only an exact
    partition leaves `unattributed_minutes` readable.
    """
    for shape in LEDGER_SHAPES:
        entry = ledger(**shape)
        elapsed = entry.budget.elapsed_minutes(entry.horizon)
        assert sum(entry.breakdown().values()) + entry.unattributed_minutes == elapsed, shape


def test_no_stage_is_ever_charged_a_negative_span():
    for shape in LEDGER_SHAPES:
        assert all(minutes >= 0 for minutes in ledger(**shape).breakdown().values()), shape


def test_a_breakdown_always_carries_every_stage():
    """The console renders the same four rows whatever the order has or has not reached."""
    for shape in LEDGER_SHAPES:
        assert tuple(ledger(**shape).breakdown()) == STAGES
