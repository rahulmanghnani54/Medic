"""Order lifecycle.

An order has to clear two independent gates before a rider can collect it:

  chemist gate -- a licensed chemist has accepted it and confirmed the stock;
  prescription gate -- a pharmacist has verified the prescription, where the
                       basket needs one.

The gates run concurrently, which is what makes the 60-minute promise
achievable on a basket that needs verification. The order only becomes
READY_FOR_PICKUP once both have cleared.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from medic.models import GateState, OrderStatus


class IllegalTransition(Exception):
    def __init__(self, current: OrderStatus, requested: OrderStatus) -> None:
        super().__init__(f"cannot move an order from {current} to {requested}")
        self.current = current
        self.requested = requested


TERMINAL: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.DELIVERED,
        OrderStatus.BLOCKED_NOT_DISPENSABLE,
        OrderStatus.CANCELLED_NO_CHEMIST,
        OrderStatus.CANCELLED_STOCK_OUT,
        OrderStatus.REJECTED_PRESCRIPTION,
        OrderStatus.CANCELLED_BY_CUSTOMER,
    }
)

ALLOWED: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset(
        {
            OrderStatus.AWAITING_FULFILMENT,
            OrderStatus.BLOCKED_NOT_DISPENSABLE,
            OrderStatus.CANCELLED_NO_CHEMIST,
            OrderStatus.CANCELLED_BY_CUSTOMER,
        }
    ),
    OrderStatus.AWAITING_FULFILMENT: frozenset(
        {
            OrderStatus.READY_FOR_PICKUP,
            OrderStatus.CANCELLED_STOCK_OUT,
            OrderStatus.CANCELLED_NO_CHEMIST,
            OrderStatus.REJECTED_PRESCRIPTION,
            OrderStatus.CANCELLED_BY_CUSTOMER,
        }
    ),
    OrderStatus.READY_FOR_PICKUP: frozenset(
        {
            OrderStatus.OUT_FOR_DELIVERY,
            OrderStatus.CANCELLED_BY_CUSTOMER,
            # The chemist can still withdraw before the rider collects.
            OrderStatus.CANCELLED_STOCK_OUT,
            OrderStatus.CANCELLED_NO_CHEMIST,
        }
    ),
    # Once a rider holds scheduled drugs the order is no longer cancellable by
    # the customer: the goods have to come back to the dispensing chemist.
    OrderStatus.OUT_FOR_DELIVERY: frozenset({OrderStatus.DELIVERED}),
}


def assert_can_transition(current: OrderStatus, requested: OrderStatus) -> None:
    if current in TERMINAL or requested not in ALLOWED.get(current, frozenset()):
        raise IllegalTransition(current, requested)


def gates_cleared(chemist_gate: GateState, rx_gate: GateState) -> bool:
    chemist_ok = chemist_gate is GateState.CLEARED
    rx_ok = rx_gate in (GateState.CLEARED, GateState.NOT_REQUIRED)
    return chemist_ok and rx_ok


@dataclass(frozen=True)
class SlaView:
    promised_minutes: int
    deadline: datetime
    minutes_remaining: int
    breached: bool

    @property
    def at_risk(self) -> bool:
        return not self.breached and self.minutes_remaining <= 15


def sla_view(created_at: datetime, promised_minutes: int, now: datetime) -> SlaView:
    deadline = created_at + timedelta(minutes=promised_minutes)
    remaining = int((deadline - now).total_seconds() // 60)
    return SlaView(
        promised_minutes=promised_minutes,
        deadline=deadline,
        minutes_remaining=remaining,
        breached=remaining < 0,
    )
