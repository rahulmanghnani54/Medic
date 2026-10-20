"""Order lifecycle: the two gates, the state machine and the SLA clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from medic.domain.orders import (
    IllegalTransition,
    assert_can_transition,
    gates_cleared,
    sla_view,
)
from medic.models import GateState, OrderStatus

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def test_a_new_order_moves_to_awaiting_fulfilment():
    assert_can_transition(OrderStatus.CREATED, OrderStatus.AWAITING_FULFILMENT)


def test_an_order_cannot_skip_straight_to_delivered():
    with pytest.raises(IllegalTransition):
        assert_can_transition(OrderStatus.CREATED, OrderStatus.DELIVERED)


def test_an_order_cannot_be_dispatched_before_it_is_ready():
    with pytest.raises(IllegalTransition):
        assert_can_transition(OrderStatus.AWAITING_FULFILMENT, OrderStatus.OUT_FOR_DELIVERY)


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


def test_both_gates_must_clear():
    assert gates_cleared(GateState.CLEARED, GateState.CLEARED)
    assert gates_cleared(GateState.CLEARED, GateState.NOT_REQUIRED)
    assert not gates_cleared(GateState.CLEARED, GateState.PENDING)
    assert not gates_cleared(GateState.PENDING, GateState.CLEARED)
    assert not gates_cleared(GateState.CLEARED, GateState.FAILED)
    assert not gates_cleared(GateState.FAILED, GateState.NOT_REQUIRED)


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
