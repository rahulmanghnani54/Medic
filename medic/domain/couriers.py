"""Courier supply.

The courier is the leg of this marketplace that genuinely behaves like Uber's
driver side: mobile, dense, near-fungible, and price-elastic. So the earner
state machine transfers, with one correction the architecture review caught.

Uber's own lesson is that supply holds an ordered PLAN OF WAYPOINTS, not a
`current_trip_id`. A scalar state like ENROUTE_TO_CHEMIST is only well defined
for one order at a time, and a courier carrying order A while collecting
order B then has no representable state. So CourierState here describes the
courier's AVAILABILITY, and where they physically are comes from the next
open stop in their plan.

Custody matters more here than at Uber. A courier holding scheduled drugs is
holding goods that belong to a dispensing pharmacy's register, so they cannot
simply go offline: the goods have to be accounted for.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CourierState(StrEnum):
    OFFLINE = "OFFLINE"
    # On shift, no open stops.
    IDLE = "IDLE"
    # On shift with at least one open stop in the plan.
    ON_PLAN = "ON_PLAN"
    # On shift but not dispatchable: a break, or an expired credential.
    UNASSIGNABLE = "UNASSIGNABLE"


ALLOWED_COURIER: dict[CourierState, frozenset[CourierState]] = {
    CourierState.OFFLINE: frozenset({CourierState.IDLE}),
    CourierState.IDLE: frozenset(
        {CourierState.ON_PLAN, CourierState.UNASSIGNABLE, CourierState.OFFLINE}
    ),
    # No edge to OFFLINE: a courier with open stops may be holding scheduled
    # goods. They finish or hand back the plan first. Enforced again in the
    # service layer against actual custody.
    CourierState.ON_PLAN: frozenset({CourierState.IDLE, CourierState.UNASSIGNABLE}),
    CourierState.UNASSIGNABLE: frozenset({CourierState.IDLE, CourierState.OFFLINE}),
}


class IllegalCourierTransition(Exception):
    def __init__(self, current: CourierState, requested: CourierState) -> None:
        super().__init__(f"cannot move a courier from {current} to {requested}")
        self.current = current
        self.requested = requested


def assert_courier_transition(current: CourierState, requested: CourierState) -> None:
    if requested not in ALLOWED_COURIER.get(current, frozenset()):
        raise IllegalCourierTransition(current, requested)


class StopKind(StrEnum):
    COLLECT = "COLLECT"
    HAND_OVER = "HAND_OVER"


class StopState(StrEnum):
    PLANNED = "PLANNED"
    ARRIVED = "ARRIVED"
    DONE = "DONE"
    # The stop was cancelled before the courier got there.
    VOIDED = "VOIDED"


ALLOWED_STOP: dict[StopState, frozenset[StopState]] = {
    StopState.PLANNED: frozenset({StopState.ARRIVED, StopState.VOIDED}),
    StopState.ARRIVED: frozenset({StopState.DONE, StopState.VOIDED}),
    StopState.DONE: frozenset(),
    StopState.VOIDED: frozenset(),
}


class IllegalStopTransition(Exception):
    def __init__(self, current: StopState, requested: StopState) -> None:
        super().__init__(f"cannot move a stop from {current} to {requested}")
        self.current = current
        self.requested = requested


def assert_stop_transition(current: StopState, requested: StopState) -> None:
    if requested not in ALLOWED_STOP.get(current, frozenset()):
        raise IllegalStopTransition(current, requested)


@dataclass(frozen=True)
class Waypoint:
    """One stop in a courier's plan, decoupled from the ORM."""

    stop_id: int
    order_id: int
    kind: StopKind
    state: StopState
    lat: float
    lng: float
    sequence: int
    #: True when completing this stop puts scheduled goods into the courier's
    #: hands, or takes them out.
    carries_scheduled_goods: bool = False


@dataclass(frozen=True)
class WaypointPlan:
    """A courier's ordered stops, possibly spanning several orders."""

    courier_id: int
    stops: tuple[Waypoint, ...]

    @property
    def open_stops(self) -> tuple[Waypoint, ...]:
        return tuple(s for s in self.stops if s.state in (StopState.PLANNED, StopState.ARRIVED))

    @property
    def next_stop(self) -> Waypoint | None:
        pending = sorted(self.open_stops, key=lambda s: s.sequence)
        return pending[0] if pending else None

    @property
    def order_ids(self) -> tuple[int, ...]:
        seen: list[int] = []
        for stop in self.stops:
            if stop.order_id not in seen:
                seen.append(stop.order_id)
        return tuple(seen)

    def holds_custody_of(self, order_id: int) -> bool:
        """True once the collection is done and the handover is not.

        This is the question that decides whether a courier may go offline,
        whether a chemist may still withdraw, and who is on the register's
        custody line. It is answered from the plan, never from the order's
        status column: once transport events arrive asynchronously, status is
        no longer a sound proxy for where the goods physically are.
        """
        collected = any(
            s.order_id == order_id and s.kind is StopKind.COLLECT and s.state is StopState.DONE
            for s in self.stops
        )
        handed_over = any(
            s.order_id == order_id and s.kind is StopKind.HAND_OVER and s.state is StopState.DONE
            for s in self.stops
        )
        return collected and not handed_over

    @property
    def orders_in_custody(self) -> tuple[int, ...]:
        return tuple(oid for oid in self.order_ids if self.holds_custody_of(oid))


class CustodyHeld(Exception):
    """A courier holding dispensed goods cannot be released."""


def assert_may_go_offline(plan: WaypointPlan) -> None:
    held = plan.orders_in_custody
    if held:
        raise CustodyHeld(
            f"courier {plan.courier_id} is holding dispensed goods for order(s) "
            f"{', '.join(str(o) for o in held)}; those must be handed over or returned "
            f"to the dispensing chemist first"
        )
