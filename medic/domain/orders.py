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
from enum import StrEnum


class GateState(StrEnum):
    """State of one of the two gates an order must clear before pickup."""

    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    CLEARED = "CLEARED"
    FAILED = "FAILED"
    # Sourcing is in flight: no chemist is bound yet. Distinct from PENDING,
    # which means "a specific chemist has been asked and has not answered".
    SEEKING = "SEEKING"


class OrderStatus(StrEnum):
    # Live states
    CREATED = "CREATED"
    # Dispatch is running the offer loop. No chemist is bound.
    SEEKING_CHEMIST = "SEEKING_CHEMIST"
    AWAITING_FULFILMENT = "AWAITING_FULFILMENT"
    READY_FOR_PICKUP = "READY_FOR_PICKUP"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    # Terminal failures, kept distinct because they have different root causes
    # and the pilot's exit criteria are stated per cause.
    BLOCKED_NOT_DISPENSABLE = "BLOCKED_NOT_DISPENSABLE"
    CANCELLED_NO_CHEMIST = "CANCELLED_NO_CHEMIST"
    CANCELLED_STOCK_OUT = "CANCELLED_STOCK_OUT"
    REJECTED_PRESCRIPTION = "REJECTED_PRESCRIPTION"
    CANCELLED_BY_CUSTOMER = "CANCELLED_BY_CUSTOMER"


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
            OrderStatus.SEEKING_CHEMIST,
            OrderStatus.BLOCKED_NOT_DISPENSABLE,
            OrderStatus.CANCELLED_NO_CHEMIST,
            OrderStatus.CANCELLED_BY_CUSTOMER,
        }
    ),
    # Dispatch is offering the order to ranked candidates. A decline is not
    # fatal: it returns here for the next attempt. Only exhausting the
    # candidates or the delivery budget closes the order.
    OrderStatus.SEEKING_CHEMIST: frozenset(
        {
            OrderStatus.AWAITING_FULFILMENT,
            OrderStatus.CANCELLED_NO_CHEMIST,
            # Sourcing can also end because every chemist asked said the stock
            # was not there. That is a different signal from "nobody was in
            # range" and the pilot measures them separately.
            OrderStatus.CANCELLED_STOCK_OUT,
            OrderStatus.REJECTED_PRESCRIPTION,
            OrderStatus.CANCELLED_BY_CUSTOMER,
        }
    ),
    OrderStatus.AWAITING_FULFILMENT: frozenset(
        {
            OrderStatus.READY_FOR_PICKUP,
            # A chemist who accepted and then found the shelf empty hands the
            # order back to sourcing rather than killing it.
            OrderStatus.SEEKING_CHEMIST,
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
            # The chemist can still withdraw before the rider collects, which
            # re-opens sourcing rather than ending the order.
            OrderStatus.SEEKING_CHEMIST,
            OrderStatus.CANCELLED_STOCK_OUT,
            OrderStatus.CANCELLED_NO_CHEMIST,
            OrderStatus.REJECTED_PRESCRIPTION,
        }
    ),
    # Once a rider holds scheduled drugs the order is no longer cancellable by
    # the customer: the goods have to come back to the dispensing chemist.
    OrderStatus.OUT_FOR_DELIVERY: frozenset({OrderStatus.DELIVERED}),
}

# The re-sourcing cycle SEEKING_CHEMIST <-> AWAITING_FULFILMENT is deliberately
# cyclic, so the transition table alone cannot bound it. Attempt count lives on
# the Offer rows and the clock lives on the DeliveryBudget; both are checked by
# the dispatcher before every offer. assert_sourcing_attempt below is the
# invariant-layer backstop, so a dispatcher bug cannot cycle an order forever.
MAX_SOURCING_ATTEMPTS_CEILING = 25


class SourcingExhausted(Exception):
    """The offer loop has run as far as it is allowed to for this order."""


def assert_sourcing_attempt(attempt_seq: int, *, max_attempts: int) -> None:
    """Hard bound on the re-offer cycle, independent of the dispatcher."""
    ceiling = min(max_attempts, MAX_SOURCING_ATTEMPTS_CEILING)
    if attempt_seq > ceiling:
        raise SourcingExhausted(
            f"sourcing attempt {attempt_seq} exceeds the limit of {ceiling} for this order"
        )


def assert_can_transition(current: OrderStatus, requested: OrderStatus) -> None:
    if current in TERMINAL or requested not in ALLOWED.get(current, frozenset()):
        raise IllegalTransition(current, requested)


class IllegalGateTransition(Exception):
    def __init__(self, gate: str, current: GateState, requested: GateState) -> None:
        super().__init__(f"cannot move the {gate} gate from {current} to {requested}")
        self.gate = gate
        self.current = current
        self.requested = requested


# Gate states were previously written by bare assignment in several places,
# with no table saying which moves are legal. That is how a gate ends up
# CLEARED on an order with no chemist bound -- which reads as "ready to
# dispatch" rather than as an error. Every gate write now goes through
# assert_gate_transition.
ALLOWED_CHEMIST_GATE: dict[GateState, frozenset[GateState]] = {
    GateState.SEEKING: frozenset({GateState.PENDING, GateState.FAILED}),
    # PENDING means a named chemist holds an unanswered offer.
    GateState.PENDING: frozenset({GateState.CLEARED, GateState.SEEKING, GateState.FAILED}),
    # A chemist who accepted can still withdraw, which returns us to sourcing.
    GateState.CLEARED: frozenset({GateState.SEEKING, GateState.FAILED}),
    GateState.FAILED: frozenset(),
    GateState.NOT_REQUIRED: frozenset(),
}

# The prescription gate never re-opens. A pharmacist's decision is final for
# this order: there is no path from FAILED back to PENDING, and a CLEARED
# verification is not invalidated by re-sourcing the chemist.
ALLOWED_RX_GATE: dict[GateState, frozenset[GateState]] = {
    GateState.NOT_REQUIRED: frozenset(),
    GateState.PENDING: frozenset({GateState.CLEARED, GateState.FAILED}),
    GateState.CLEARED: frozenset(),
    GateState.FAILED: frozenset(),
    GateState.SEEKING: frozenset(),
}


def assert_gate_transition(gate: str, current: GateState, requested: GateState) -> None:
    """Single chokepoint for every gate write. `gate` is 'chemist' or 'prescription'."""
    table = ALLOWED_CHEMIST_GATE if gate == "chemist" else ALLOWED_RX_GATE
    if requested not in table.get(current, frozenset()):
        raise IllegalGateTransition(gate, current, requested)


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
