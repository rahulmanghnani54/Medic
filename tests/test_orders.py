"""Order lifecycle: the two gates, the state machine, the invariant layer and the SLA clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from medic.domain.orders import (
    ALLOWED,
    ALLOWED_CHEMIST_GATE,
    ALLOWED_RX_GATE,
    MAX_SOURCING_ATTEMPTS_CEILING,
    IllegalGateTransition,
    IllegalTransition,
    SourcingExhausted,
    assert_can_transition,
    assert_gate_transition,
    assert_sourcing_attempt,
    gates_cleared,
    sla_view,
)
from medic.models import GateState, OrderStatus

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


# --- The order state machine ----------------------------------------------


def test_a_new_order_moves_into_sourcing():
    assert_can_transition(OrderStatus.CREATED, OrderStatus.SEEKING_CHEMIST)


def test_a_new_order_cannot_arrive_with_a_chemist_already_bound():
    """Checkout no longer assigns: every order reaches a chemist through the offer loop."""
    with pytest.raises(IllegalTransition):
        assert_can_transition(OrderStatus.CREATED, OrderStatus.AWAITING_FULFILMENT)


def test_an_order_cannot_skip_straight_to_delivered():
    with pytest.raises(IllegalTransition):
        assert_can_transition(OrderStatus.CREATED, OrderStatus.DELIVERED)


def test_an_order_cannot_be_dispatched_before_it_is_ready():
    with pytest.raises(IllegalTransition):
        assert_can_transition(OrderStatus.AWAITING_FULFILMENT, OrderStatus.OUT_FOR_DELIVERY)


def test_an_accepted_offer_moves_a_sourced_order_to_awaiting_fulfilment():
    assert_can_transition(OrderStatus.SEEKING_CHEMIST, OrderStatus.AWAITING_FULFILMENT)


def test_a_chemist_who_hands_an_accepted_order_back_returns_it_to_sourcing():
    """A withdrawal after acceptance is a routing event, not the death of the order."""
    assert_can_transition(OrderStatus.AWAITING_FULFILMENT, OrderStatus.SEEKING_CHEMIST)


def test_a_chemist_can_still_withdraw_after_the_order_is_ready_for_pickup():
    """Until the rider collects, the basket is still on the chemist's shelf to hand back."""
    assert_can_transition(OrderStatus.READY_FOR_PICKUP, OrderStatus.SEEKING_CHEMIST)


def test_an_order_being_sourced_cannot_become_ready_for_pickup():
    """READY_FOR_PICKUP asserts a named chemist is holding the basket, so sourcing cannot skip."""
    with pytest.raises(IllegalTransition):
        assert_can_transition(OrderStatus.SEEKING_CHEMIST, OrderStatus.READY_FOR_PICKUP)


def test_terminal_states_are_final():
    for terminal in (
        OrderStatus.DELIVERED,
        OrderStatus.CANCELLED_STOCK_OUT,
        OrderStatus.REJECTED_PRESCRIPTION,
        OrderStatus.BLOCKED_NOT_DISPENSABLE,
        OrderStatus.CANCELLED_BY_CUSTOMER,
        OrderStatus.CANCELLED_NO_CHEMIST,
    ):
        with pytest.raises(IllegalTransition):
            assert_can_transition(terminal, OrderStatus.OUT_FOR_DELIVERY)


def test_a_customer_cannot_cancel_once_a_rider_holds_the_medicines():
    with pytest.raises(IllegalTransition):
        assert_can_transition(OrderStatus.OUT_FOR_DELIVERY, OrderStatus.CANCELLED_BY_CUSTOMER)


def test_an_order_with_a_courier_has_exactly_one_way_onward():
    """Delivery is the only move left once scheduled goods are in a courier's bag.

    Every other live state has a cancellation edge of some kind; this one has none, so no
    actor can have the order cancelled under a rider who is already carrying the stock.
    """
    assert ALLOWED[OrderStatus.OUT_FOR_DELIVERY] == frozenset({OrderStatus.DELIVERED})
    for requested in OrderStatus:
        if requested is OrderStatus.DELIVERED:
            continue
        with pytest.raises(IllegalTransition):
            assert_can_transition(OrderStatus.OUT_FOR_DELIVERY, requested)


# --- The chemist gate -----------------------------------------------------


def test_making_an_offer_moves_the_chemist_gate_from_sourcing_to_pending():
    assert_gate_transition("chemist", GateState.SEEKING, GateState.PENDING)


def test_an_acceptance_clears_the_chemist_gate():
    assert_gate_transition("chemist", GateState.PENDING, GateState.CLEARED)


def test_an_unanswered_offer_returns_the_chemist_gate_to_sourcing():
    assert_gate_transition("chemist", GateState.PENDING, GateState.SEEKING)


def test_a_chemist_who_cleared_the_gate_can_still_hand_the_order_back():
    assert_gate_transition("chemist", GateState.CLEARED, GateState.SEEKING)


def test_the_chemist_gate_cannot_clear_while_no_chemist_is_bound():
    """The write this table exists to stop: CLEARED with nobody bound reads as ready to dispatch."""
    with pytest.raises(IllegalGateTransition):
        assert_gate_transition("chemist", GateState.SEEKING, GateState.CLEARED)


def test_the_chemist_gate_cannot_re_offer_without_returning_to_sourcing():
    """PENDING names one chemist holding one unanswered offer, so it cannot be overwritten."""
    with pytest.raises(IllegalGateTransition):
        assert_gate_transition("chemist", GateState.PENDING, GateState.PENDING)


def test_the_chemist_gate_can_fail_from_every_state_that_is_still_live():
    for live in (GateState.SEEKING, GateState.PENDING, GateState.CLEARED):
        assert_gate_transition("chemist", live, GateState.FAILED)


def test_a_failed_chemist_gate_never_re_opens():
    for requested in GateState:
        with pytest.raises(IllegalGateTransition):
            assert_gate_transition("chemist", GateState.FAILED, requested)


def test_the_chemist_gate_can_never_be_written_as_not_required():
    """Every order needs a chemist: there is no basket that dispenses itself."""
    for current, allowed in ALLOWED_CHEMIST_GATE.items():
        assert GateState.NOT_REQUIRED not in allowed, current
    assert ALLOWED_CHEMIST_GATE[GateState.NOT_REQUIRED] == frozenset()


# --- The prescription gate ------------------------------------------------


def test_a_pharmacist_can_clear_the_prescription_gate():
    assert_gate_transition("prescription", GateState.PENDING, GateState.CLEARED)


def test_a_pharmacist_can_fail_the_prescription_gate():
    assert_gate_transition("prescription", GateState.PENDING, GateState.FAILED)


def test_a_rejected_prescription_is_never_reconsidered():
    for requested in GateState:
        with pytest.raises(IllegalGateTransition):
            assert_gate_transition("prescription", GateState.FAILED, requested)


def test_a_verified_prescription_is_never_invalidated():
    for requested in GateState:
        with pytest.raises(IllegalGateTransition):
            assert_gate_transition("prescription", GateState.CLEARED, requested)


def test_a_basket_needing_no_prescription_never_grows_a_prescription_gate():
    for requested in GateState:
        with pytest.raises(IllegalGateTransition):
            assert_gate_transition("prescription", GateState.NOT_REQUIRED, requested)


def test_the_prescription_gate_never_enters_sourcing():
    """SEEKING belongs to the chemist gate: a prescription is not sourced from candidates."""
    for current, allowed in ALLOWED_RX_GATE.items():
        assert GateState.SEEKING not in allowed, current
    assert ALLOWED_RX_GATE[GateState.SEEKING] == frozenset()


def test_only_the_chemist_gate_may_regress_once_it_has_cleared():
    """The asymmetry between the two tables, and why each side is shaped the way it is.

    The chemist gate may go CLEARED -> SEEKING because an acceptance is only a claim about
    declared stock: the chemist who made it can find the shelf empty and hand the order
    back, and the patient still wants the medicine, so sourcing re-opens.

    The prescription gate never re-opens. There is no edge out of CLEARED or out of FAILED
    at all, because a pharmacist's decision is final for this order: a rejection cannot be
    shopped to a second pharmacist, and a verification is not invalidated by re-sourcing
    the chemist. Re-sourcing is confined to the other gate precisely so that it cannot
    reach a verification that has already been made.
    """
    assert GateState.SEEKING in ALLOWED_CHEMIST_GATE[GateState.CLEARED]
    assert ALLOWED_RX_GATE[GateState.CLEARED] == frozenset()
    assert ALLOWED_RX_GATE[GateState.FAILED] == frozenset()


def test_both_gate_tables_cover_every_gate_state():
    assert set(ALLOWED_CHEMIST_GATE) == set(GateState)
    assert set(ALLOWED_RX_GATE) == set(GateState)


def test_an_illegal_gate_transition_names_the_gate_and_both_ends_of_the_move():
    with pytest.raises(IllegalGateTransition) as raised:
        assert_gate_transition("prescription", GateState.FAILED, GateState.CLEARED)
    assert raised.value.gate == "prescription"
    assert raised.value.current is GateState.FAILED
    assert raised.value.requested is GateState.CLEARED
    assert "prescription" in str(raised.value)
    assert "FAILED" in str(raised.value)
    assert "CLEARED" in str(raised.value)


# --- Both gates together --------------------------------------------------


def test_both_gates_must_clear():
    assert gates_cleared(GateState.CLEARED, GateState.CLEARED)
    assert gates_cleared(GateState.CLEARED, GateState.NOT_REQUIRED)
    assert not gates_cleared(GateState.CLEARED, GateState.PENDING)
    assert not gates_cleared(GateState.PENDING, GateState.CLEARED)
    assert not gates_cleared(GateState.CLEARED, GateState.FAILED)
    assert not gates_cleared(GateState.FAILED, GateState.NOT_REQUIRED)


def test_an_order_still_being_sourced_has_not_cleared_its_gates():
    assert not gates_cleared(GateState.SEEKING, GateState.CLEARED)
    assert not gates_cleared(GateState.SEEKING, GateState.NOT_REQUIRED)


# --- The sourcing-attempt backstop ----------------------------------------


def test_an_attempt_inside_the_configured_limit_is_allowed():
    """Why this backstop exists even though the dispatcher counts attempts itself.

    SEEKING_CHEMIST <-> AWAITING_FULFILMENT is deliberately a cycle, so unlike an acyclic
    flow the order transition table cannot bound the loop: every re-offer is a legal move.
    The only thing that bounds it is the attempt counter, and that lives on rows the
    dispatcher writes. A dispatcher that failed to advance attempt_seq, or that re-read a
    stale order, would re-offer the same order forever and keep leasing stock for it. This
    check sits in the invariant layer, independent of the dispatcher's own arithmetic.
    """
    assert_sourcing_attempt(3, max_attempts=4)


def test_the_attempt_that_lands_on_the_configured_limit_is_still_allowed():
    assert_sourcing_attempt(4, max_attempts=4)


def test_an_attempt_past_the_configured_limit_is_refused():
    with pytest.raises(SourcingExhausted, match="exceeds the limit of 4"):
        assert_sourcing_attempt(5, max_attempts=4)


def test_the_hard_ceiling_caps_a_configured_limit_that_exceeds_it():
    """Configuration can tighten the bound but never loosen it past the ceiling."""
    assert_sourcing_attempt(MAX_SOURCING_ATTEMPTS_CEILING, max_attempts=10_000)
    with pytest.raises(SourcingExhausted, match=f"limit of {MAX_SOURCING_ATTEMPTS_CEILING}"):
        assert_sourcing_attempt(MAX_SOURCING_ATTEMPTS_CEILING + 1, max_attempts=10_000)


def test_the_refusal_names_the_attempt_and_the_limit_that_was_applied():
    with pytest.raises(SourcingExhausted) as raised:
        assert_sourcing_attempt(9, max_attempts=2)
    assert "9" in str(raised.value)
    assert "limit of 2" in str(raised.value)


# --- The SLA clock --------------------------------------------------------


def test_sla_counts_down_from_order_time():
    sla = sla_view(NOW - timedelta(minutes=20), 60, NOW)
    assert sla.minutes_remaining == 40
    assert not sla.breached
    assert not sla.at_risk


def test_sla_flags_an_order_at_risk():
    sla = sla_view(NOW - timedelta(minutes=50), 60, NOW)
    assert sla.minutes_remaining == 10
    assert sla.at_risk
    assert not sla.breached


def test_sla_reports_a_breach_with_negative_time():
    sla = sla_view(NOW - timedelta(minutes=75), 60, NOW)
    assert sla.minutes_remaining < 0
    assert sla.breached
    assert not sla.at_risk
