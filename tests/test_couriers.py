"""Courier supply: availability, stop lifecycle and custody of dispensed goods."""

from __future__ import annotations

import pytest

from medic.domain.couriers import (
    ALLOWED_COURIER,
    CourierState,
    CustodyHeld,
    IllegalCourierTransition,
    IllegalStopTransition,
    StopKind,
    StopState,
    Waypoint,
    WaypointPlan,
    assert_courier_transition,
    assert_may_go_offline,
    assert_stop_transition,
)

# Somewhere in Jaipur; the plan logic never looks at coordinates.
LAT, LNG = 26.9124, 75.7873


def _waypoint(
    order_id: int,
    kind: StopKind,
    state: StopState,
    sequence: int,
    *,
    carries_scheduled_goods: bool = False,
) -> Waypoint:
    return Waypoint(
        stop_id=sequence * 100 + order_id,
        order_id=order_id,
        kind=kind,
        state=state,
        lat=LAT,
        lng=LNG,
        sequence=sequence,
        carries_scheduled_goods=carries_scheduled_goods,
    )


def collect(
    order_id: int,
    state: StopState = StopState.PLANNED,
    *,
    sequence: int = 1,
    carries_scheduled_goods: bool = False,
) -> Waypoint:
    return _waypoint(
        order_id,
        StopKind.COLLECT,
        state,
        sequence,
        carries_scheduled_goods=carries_scheduled_goods,
    )


def hand_over(
    order_id: int,
    state: StopState = StopState.PLANNED,
    *,
    sequence: int = 2,
    carries_scheduled_goods: bool = False,
) -> Waypoint:
    return _waypoint(
        order_id,
        StopKind.HAND_OVER,
        state,
        sequence,
        carries_scheduled_goods=carries_scheduled_goods,
    )


def plan(*stops: Waypoint, courier_id: int = 77) -> WaypointPlan:
    return WaypointPlan(courier_id=courier_id, stops=stops)


# --- courier availability ---------------------------------------------------


def test_an_offline_courier_comes_on_shift_as_idle():
    assert_courier_transition(CourierState.OFFLINE, CourierState.IDLE)


def test_an_offline_courier_cannot_be_handed_a_plan_directly():
    with pytest.raises(IllegalCourierTransition):
        assert_courier_transition(CourierState.OFFLINE, CourierState.ON_PLAN)


def test_an_idle_courier_can_be_given_a_plan():
    assert_courier_transition(CourierState.IDLE, CourierState.ON_PLAN)


def test_an_idle_courier_can_end_their_shift():
    assert_courier_transition(CourierState.IDLE, CourierState.OFFLINE)


def test_a_courier_on_a_plan_cannot_go_offline():
    """The one correction to the earner machine: open stops may mean held goods.

    The courier finishes or hands back the plan first; the service layer checks
    actual custody on top of this.
    """
    with pytest.raises(IllegalCourierTransition):
        assert_courier_transition(CourierState.ON_PLAN, CourierState.OFFLINE)


def test_a_courier_on_a_plan_can_be_taken_out_of_dispatch_for_a_break():
    assert_courier_transition(CourierState.ON_PLAN, CourierState.UNASSIGNABLE)


def test_a_courier_finishing_their_plan_returns_to_idle():
    assert_courier_transition(CourierState.ON_PLAN, CourierState.IDLE)


def test_an_unassignable_courier_may_go_offline_without_passing_through_idle():
    assert_courier_transition(CourierState.UNASSIGNABLE, CourierState.OFFLINE)


def test_an_unassignable_courier_cannot_be_given_a_plan_before_becoming_idle():
    with pytest.raises(IllegalCourierTransition):
        assert_courier_transition(CourierState.UNASSIGNABLE, CourierState.ON_PLAN)


def test_no_courier_state_is_a_transition_to_itself():
    for state in CourierState:
        with pytest.raises(IllegalCourierTransition):
            assert_courier_transition(state, state)


def test_every_courier_state_has_an_entry_in_the_transition_table():
    assert set(ALLOWED_COURIER) == set(CourierState)


def test_an_illegal_courier_transition_reports_both_states():
    with pytest.raises(IllegalCourierTransition) as raised:
        assert_courier_transition(CourierState.ON_PLAN, CourierState.OFFLINE)
    assert raised.value.current is CourierState.ON_PLAN
    assert raised.value.requested is CourierState.OFFLINE


# --- stop lifecycle --------------------------------------------------------


def test_a_courier_must_arrive_at_a_stop_before_completing_it():
    with pytest.raises(IllegalStopTransition):
        assert_stop_transition(StopState.PLANNED, StopState.DONE)


def test_a_planned_stop_can_be_arrived_at():
    assert_stop_transition(StopState.PLANNED, StopState.ARRIVED)


def test_an_arrived_stop_can_be_completed():
    assert_stop_transition(StopState.ARRIVED, StopState.DONE)


def test_a_stop_can_be_voided_before_or_after_the_courier_arrives():
    assert_stop_transition(StopState.PLANNED, StopState.VOIDED)
    assert_stop_transition(StopState.ARRIVED, StopState.VOIDED)


def test_a_completed_stop_cannot_be_reopened():
    for requested in (StopState.PLANNED, StopState.ARRIVED, StopState.VOIDED):
        with pytest.raises(IllegalStopTransition):
            assert_stop_transition(StopState.DONE, requested)


def test_a_voided_stop_cannot_be_revived():
    for requested in (StopState.PLANNED, StopState.ARRIVED, StopState.DONE):
        with pytest.raises(IllegalStopTransition):
            assert_stop_transition(StopState.VOIDED, requested)


def test_an_illegal_stop_transition_reports_both_states():
    with pytest.raises(IllegalStopTransition) as raised:
        assert_stop_transition(StopState.DONE, StopState.ARRIVED)
    assert raised.value.current is StopState.DONE
    assert raised.value.requested is StopState.ARRIVED


# --- plan ordering ---------------------------------------------------------


def test_the_next_stop_is_the_lowest_open_sequence_whatever_order_stops_arrive_in():
    first = collect(1, sequence=1)
    second = hand_over(1, sequence=2)
    third = collect(2, sequence=3)
    assert plan(third, second, first).next_stop is first


def test_a_completed_stop_is_never_the_next_stop():
    pending = hand_over(1, sequence=2)
    assert plan(collect(1, StopState.DONE, sequence=1), pending).next_stop is pending


def test_a_voided_stop_is_skipped_when_choosing_the_next_stop():
    later = collect(2, sequence=3)
    assert plan(collect(1, StopState.VOIDED, sequence=1), later).next_stop is later


def test_a_stop_the_courier_has_arrived_at_is_still_the_next_stop():
    arrived = collect(1, StopState.ARRIVED, sequence=1)
    assert plan(arrived, hand_over(1, sequence=2)).next_stop is arrived


def test_a_plan_with_nothing_left_open_has_no_next_stop():
    finished = plan(
        collect(1, StopState.DONE, sequence=1),
        hand_over(1, StopState.DONE, sequence=2),
    )
    assert finished.next_stop is None


def test_an_empty_plan_has_no_next_stop_and_no_orders():
    empty = plan()
    assert empty.next_stop is None
    assert empty.open_stops == ()
    assert empty.order_ids == ()


def test_open_stops_are_the_planned_and_arrived_ones_in_plan_order():
    planned = collect(2, sequence=3)
    arrived = collect(1, StopState.ARRIVED, sequence=1)
    current = plan(
        arrived,
        hand_over(1, StopState.VOIDED, sequence=2),
        planned,
        hand_over(3, StopState.DONE, sequence=4),
    )
    assert current.open_stops == (arrived, planned)


def test_order_ids_collapse_the_collect_and_hand_over_of_one_order():
    assert plan(collect(5, sequence=1), hand_over(5, sequence=2)).order_ids == (5,)


def test_order_ids_keep_first_appearance_order_rather_than_sorting():
    """Batched plans are read in plan order, so order 9 stays ahead of order 4."""
    batched = plan(
        collect(9, sequence=1),
        collect(4, sequence=2),
        hand_over(9, sequence=3),
        hand_over(4, sequence=4),
    )
    assert batched.order_ids == (9, 4)


def test_order_ids_include_orders_whose_stops_were_all_voided():
    cancelled = plan(
        collect(1, StopState.DONE, sequence=1),
        collect(2, StopState.VOIDED, sequence=2),
    )
    assert cancelled.order_ids == (1, 2)


# --- custody ---------------------------------------------------------------


def test_a_collected_order_that_is_not_yet_handed_over_is_in_custody():
    carrying = plan(collect(1, StopState.DONE, sequence=1), hand_over(1, sequence=2))
    assert carrying.holds_custody_of(1)
    assert carrying.orders_in_custody == (1,)


def test_an_order_collected_and_handed_over_is_no_longer_in_custody():
    delivered = plan(
        collect(1, StopState.DONE, sequence=1),
        hand_over(1, StopState.DONE, sequence=2),
    )
    assert not delivered.holds_custody_of(1)
    assert delivered.orders_in_custody == ()


def test_an_order_not_collected_yet_is_not_in_custody():
    waiting = plan(collect(1, sequence=1), hand_over(1, sequence=2))
    assert not waiting.holds_custody_of(1)
    assert waiting.orders_in_custody == ()


def test_arriving_at_the_chemist_is_not_yet_custody():
    """Custody starts when the collection stop completes, not on arrival."""
    at_counter = plan(collect(1, StopState.ARRIVED, sequence=1), hand_over(1, sequence=2))
    assert not at_counter.holds_custody_of(1)


def test_custody_survives_a_voided_handover():
    """A cancellation does not teleport the goods out of the courier's bag.

    Only a DONE hand-over clears custody, so an order cancelled mid-flight
    leaves the courier holding goods that must go back to the chemist.
    """
    stranded = plan(
        collect(1, StopState.DONE, sequence=1),
        hand_over(1, StopState.VOIDED, sequence=2),
    )
    assert stranded.holds_custody_of(1)
    assert stranded.orders_in_custody == (1,)


def test_an_order_absent_from_the_plan_is_not_in_custody():
    assert not plan(collect(1, StopState.DONE, sequence=1)).holds_custody_of(999)


def test_custody_ignores_a_handover_completed_for_a_different_order():
    mixed = plan(
        collect(1, StopState.DONE, sequence=1),
        collect(2, StopState.DONE, sequence=2),
        hand_over(2, StopState.DONE, sequence=3),
        hand_over(1, sequence=4),
    )
    assert mixed.holds_custody_of(1)
    assert not mixed.holds_custody_of(2)
    assert mixed.orders_in_custody == (1,)


def test_a_courier_can_carry_one_order_while_still_collecting_another():
    """The case a scalar courier state could not represent.

    Order A is in the bag, order B has not been picked up, and the courier's
    position is the next open stop -- B's collection -- rather than a single
    ENROUTE_TO_CHEMIST or DELIVERING flag.
    """
    next_collection = collect(2, sequence=2)
    batched = plan(
        collect(1, StopState.DONE, sequence=1),
        next_collection,
        hand_over(1, sequence=3),
        hand_over(2, sequence=4),
    )
    assert batched.holds_custody_of(1)
    assert not batched.holds_custody_of(2)
    assert batched.orders_in_custody == (1,)
    assert batched.next_stop is next_collection


def test_orders_in_custody_lists_every_order_the_courier_is_holding():
    loaded = plan(
        collect(11, StopState.DONE, sequence=1),
        collect(12, StopState.DONE, sequence=2),
        collect(13, StopState.DONE, sequence=3),
        hand_over(12, StopState.DONE, sequence=4),
        hand_over(11, sequence=5),
        hand_over(13, sequence=6),
    )
    assert loaded.orders_in_custody == (11, 13)


# --- going offline ---------------------------------------------------------


def test_a_courier_holding_goods_cannot_be_released():
    carrying = plan(collect(1, StopState.DONE, sequence=1), hand_over(1, sequence=2))
    with pytest.raises(CustodyHeld):
        assert_may_go_offline(carrying)


def test_the_custody_refusal_names_the_courier_and_every_order_held():
    loaded = plan(
        collect(31, StopState.DONE, sequence=1),
        collect(32, StopState.DONE, sequence=2),
        hand_over(31, sequence=3),
        hand_over(32, sequence=4),
        courier_id=404,
    )
    with pytest.raises(CustodyHeld) as raised:
        assert_may_go_offline(loaded)
    message = str(raised.value)
    assert "404" in message
    assert "31" in message
    assert "32" in message


def test_a_courier_with_an_empty_plan_may_go_offline():
    assert_may_go_offline(plan())


def test_a_courier_who_handed_everything_over_may_go_offline():
    assert_may_go_offline(
        plan(
            collect(1, StopState.DONE, sequence=1),
            hand_over(1, StopState.DONE, sequence=2),
        )
    )


def test_a_courier_who_has_collected_nothing_yet_may_go_offline():
    """Custody, not busyness, is what this check guards.

    A courier with open stops but nothing in the bag passes here; the
    CourierState table is what stops them abandoning the plan mid-shift.
    """
    assert_may_go_offline(plan(collect(1, sequence=1), hand_over(1, sequence=2)))


def test_a_courier_stranded_with_goods_by_a_cancellation_still_cannot_go_offline():
    """Nothing is open, so the state machine would allow OFFLINE; custody does not.

    This is exactly why the released check reads the plan rather than the
    courier's availability state.
    """
    stranded = plan(
        collect(1, StopState.DONE, sequence=1),
        hand_over(1, StopState.VOIDED, sequence=2),
    )
    assert stranded.next_stop is None
    with pytest.raises(CustodyHeld):
        assert_may_go_offline(stranded)
