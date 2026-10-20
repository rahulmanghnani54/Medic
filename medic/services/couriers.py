"""Courier supply: shifts, positions, delivery offers and the run.

The courier is the leg of this marketplace that genuinely behaves like
Uber's driver side, so the mechanics transfer directly: an earner goes on
shift, streams position, receives offers with a short window, and works an
ordered plan of stops.

Two things are not Uber's:

The pickup is not ready when the order is. A courier summoned the instant an
order lands just waits at a counter, inside the same 60-minute budget. So
delivery offers are scheduled backwards from the predicted ready_at.

Custody is a compliance fact. Completing a collection stop IS the dispensing
event, so it goes through `services.dispensing` and nowhere else, and a
courier holding dispensed goods cannot simply end their shift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from medic.config import Settings, settings
from medic.domain import couriers as cd
from medic.domain import dispatch as od
from medic.domain import geocells
from medic.domain.geo import haversine_km
from medic.domain.orders import OrderStatus, assert_can_transition, sla_view
from medic.domain.schedules import policy_for
from medic.domain.triage import dispatch_priority
from medic.models import (
    Chemist,
    Courier,
    CourierOffer,
    CourierPosition,
    Medicine,
    Order,
    Stop,
)
from medic.services import dispensing
from medic.services.audit import audit


class CourierError(Exception):
    """A courier request the rules refuse."""


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------


def plan_for(session: Session, courier_id: int) -> cd.WaypointPlan:
    stops = list(
        session.scalars(
            select(Stop).where(Stop.courier_id == courier_id).order_by(Stop.sequence)
        )
    )
    return cd.WaypointPlan(
        courier_id=courier_id,
        stops=tuple(
            cd.Waypoint(
                stop_id=s.id,
                order_id=s.order_id,
                kind=s.kind,
                state=s.state,
                lat=s.lat,
                lng=s.lng,
                sequence=s.sequence,
                carries_scheduled_goods=s.carries_scheduled_goods,
            )
            for s in stops
        ),
    )


def _sync_state(session: Session, courier: Courier) -> None:
    """Keep the courier's availability consistent with their open stops.

    The flush is load-bearing. The session runs with autoflush off, so stops
    added or completed in this transaction are invisible to the SELECT in
    plan_for() until they are written -- which made a courier who had just
    accepted a delivery read as IDLE, and therefore dispatchable for more work
    they had no room for.
    """
    session.flush()
    if courier.state in (cd.CourierState.OFFLINE, cd.CourierState.UNASSIGNABLE):
        return
    has_work = bool(plan_for(session, courier.id).open_stops)
    target = cd.CourierState.ON_PLAN if has_work else cd.CourierState.IDLE
    if courier.state is not target:
        cd.assert_courier_transition(courier.state, target)
        courier.state = target


# --------------------------------------------------------------------------
# Shift
# --------------------------------------------------------------------------


def go_online(session: Session, *, courier_id: int, now: datetime | None = None) -> Courier:
    now = now or _now()
    courier = session.get(Courier, courier_id)
    if courier is None:
        raise CourierError(f"unknown courier {courier_id}")
    if not courier.is_active:
        raise CourierError("this courier account is not active")
    cd.assert_courier_transition(courier.state, cd.CourierState.IDLE)
    courier.state = cd.CourierState.IDLE
    courier.last_seen_at = now
    _sync_state(session, courier)
    session.flush()
    audit(
        session,
        actor=f"courier:{courier_id}",
        action="courier.online",
        subject_type="courier",
        subject_id=courier_id,
    )
    return courier


def go_offline(session: Session, *, courier_id: int, now: datetime | None = None) -> Courier:
    """End a shift. Refused while the courier holds dispensed goods."""
    now = now or _now()
    courier = session.get(Courier, courier_id)
    if courier is None:
        raise CourierError(f"unknown courier {courier_id}")
    # Raises CustodyHeld, which the API maps to 409: this is a custody rule,
    # not a glitch, and the courier has to account for the goods first.
    cd.assert_may_go_offline(plan_for(session, courier_id))
    if courier.state is cd.CourierState.ON_PLAN:
        cd.assert_courier_transition(courier.state, cd.CourierState.IDLE)
        courier.state = cd.CourierState.IDLE
    cd.assert_courier_transition(courier.state, cd.CourierState.OFFLINE)
    courier.state = cd.CourierState.OFFLINE
    courier.last_seen_at = now
    session.flush()
    audit(
        session,
        actor=f"courier:{courier_id}",
        action="courier.offline",
        subject_type="courier",
        subject_id=courier_id,
    )
    return courier


def report_position(
    session: Session,
    *,
    courier_id: int,
    lat: float,
    lng: float,
    now: datetime | None = None,
    config: Settings = settings,
) -> Courier:
    now = now or _now()
    courier = session.get(Courier, courier_id)
    if courier is None:
        raise CourierError(f"unknown courier {courier_id}")
    cell = geocells.cell_for(lat, lng, config=config)
    courier.last_lat, courier.last_lng, courier.last_cell = lat, lng, cell
    courier.last_seen_at = now
    session.add(CourierPosition(courier_id=courier_id, lat=lat, lng=lng, cell=cell, at=now))
    session.flush()
    return courier


# --------------------------------------------------------------------------
# Delivery offers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CourierCandidate:
    courier_id: int
    name: str
    pickup_km: float
    acceptance_rate: float


def available_couriers(
    session: Session,
    *,
    chemist_lat: float,
    chemist_lng: float,
    needs_cold_chain: bool = False,
    config: Settings = settings,
) -> list[CourierCandidate]:
    """Idle couriers within reach of the pickup, nearest first.

    The pickup-distance cap is Uber's wild-goose-chase lesson: under excess
    demand a matcher that keeps reaching further turns pickup time into
    deadweight and throughput collapses for everyone.
    """
    rows = session.scalars(
        select(Courier).where(
            Courier.is_active.is_(True),
            Courier.state.in_([cd.CourierState.IDLE, cd.CourierState.ON_PLAN]),
        )
    ).all()
    out: list[CourierCandidate] = []
    for courier in rows:
        if courier.last_lat is None or courier.last_lng is None:
            continue
        if needs_cold_chain and not courier.can_cold_chain:
            continue
        km = haversine_km(chemist_lat, chemist_lng, courier.last_lat, courier.last_lng)
        if km > config.max_courier_pickup_km:
            continue
        out.append(
            CourierCandidate(
                courier_id=courier.id,
                name=courier.name,
                pickup_km=km,
                acceptance_rate=courier.acceptance_rate,
            )
        )
    out.sort(key=lambda c: (round(c.pickup_km, 2), -c.acceptance_rate))
    return out


def open_courier_offer(session: Session, order_id: int) -> CourierOffer | None:
    return session.scalars(
        select(CourierOffer).where(
            CourierOffer.order_id == order_id, CourierOffer.state == od.OfferState.OFFERED
        )
    ).first()


def offer_delivery(
    session: Session,
    *,
    order_id: int,
    now: datetime | None = None,
    config: Settings = settings,
) -> CourierOffer | None:
    """Offer one ready order to the best available courier."""
    now = now or _now()
    order = session.get(Order, order_id)
    if order is None:
        raise CourierError(f"unknown order {order_id}")
    if order.status is not OrderStatus.READY_FOR_PICKUP:
        return None
    if open_courier_offer(session, order_id) is not None:
        return None
    if session.scalars(select(Stop).where(Stop.order_id == order_id)).first() is not None:
        return None  # already has a courier

    chemist = session.get(Chemist, order.chemist_id) if order.chemist_id else None
    if chemist is None:
        raise CourierError(f"order {order_id} has no dispensing chemist")

    tried = {
        o.courier_id
        for o in session.scalars(select(CourierOffer).where(CourierOffer.order_id == order_id))
    }
    attempt = len(tried)
    if attempt >= config.max_courier_offer_attempts:
        return None

    # Cold chain is a hard filter on the courier too, not only on the chemist:
    # a courier without a cold box cannot carry insulin whatever their ETA.
    cold = any(
        (m := session.get(Medicine, item.medicine_id)) is not None and m.requires_cold_chain
        for item in order.items
    )
    candidates = [
        c
        for c in available_couriers(
            session,
            chemist_lat=chemist.lat,
            chemist_lng=chemist.lng,
            needs_cold_chain=cold,
            config=config,
        )
        if c.courier_id not in tried
    ]
    if not candidates:
        return None

    best = candidates[0]
    offer = CourierOffer(
        order_id=order_id,
        courier_id=best.courier_id,
        attempt_seq=attempt + 1,
        state=od.OfferState.OFFERED,
        offered_at=now,
        expires_at=now + timedelta(seconds=config.courier_offer_window_seconds),
        pickup_km=round(best.pickup_km, 3),
        summon_at=order.ready_at,
        fee_paise=order.delivery_fee_paise,
    )
    session.add(offer)
    courier = session.get(Courier, best.courier_id)
    if courier is not None:
        courier.offers_made += 1
    session.flush()
    audit(
        session,
        actor="dispatcher",
        action="courier_offer.made",
        subject_type="courier_offer",
        subject_id=offer.id,
        detail=f"order={order_id} courier={best.courier_id} pickup={best.pickup_km:.2f}km",
    )
    return offer


def accept_delivery(
    session: Session,
    *,
    offer_id: int,
    courier_id: int,
    now: datetime | None = None,
) -> cd.WaypointPlan:
    """Courier takes the job: two stops are appended to their plan."""
    now = now or _now()
    offer = session.get(CourierOffer, offer_id)
    if offer is None:
        raise CourierError(f"unknown offer {offer_id}")
    if offer.courier_id != courier_id:
        raise CourierError("this offer was not made to you")
    if offer.state is not od.OfferState.OFFERED:
        raise CourierError(f"this offer is already {offer.state.lower()}")
    if now >= offer.expires_at:
        _expire_courier_offer(session, offer, now=now)
        raise CourierError("this offer has expired")

    order = session.get(Order, offer.order_id)
    assert order is not None
    if order.status is not OrderStatus.READY_FOR_PICKUP:
        raise CourierError(f"order {order.id} is {order.status} and no longer needs a courier")

    chemist = session.get(Chemist, order.chemist_id) if order.chemist_id else None
    if chemist is None:
        raise CourierError("that order has no dispensing chemist")

    od.assert_offer_transition(offer.state, od.OfferState.ACCEPTED)
    offer.state = od.OfferState.ACCEPTED
    offer.answered_at = now

    scheduled = any(policy_for(i.schedule).requires_register_entry for i in order.items)
    last = session.scalars(
        select(Stop).where(Stop.courier_id == courier_id).order_by(Stop.sequence.desc())
    ).first()
    base = (last.sequence if last else 0) + 1

    session.add(
        Stop(
            courier_id=courier_id,
            order_id=order.id,
            kind=cd.StopKind.COLLECT,
            sequence=base,
            lat=chemist.lat,
            lng=chemist.lng,
            carries_scheduled_goods=scheduled,
            planned_for=order.ready_at,
        )
    )
    session.add(
        Stop(
            courier_id=courier_id,
            order_id=order.id,
            kind=cd.StopKind.HAND_OVER,
            sequence=base + 1,
            lat=order.delivery_lat,
            lng=order.delivery_lng,
            carries_scheduled_goods=scheduled,
        )
    )
    courier = session.get(Courier, courier_id)
    if courier is not None:
        courier.offers_accepted += 1
        _sync_state(session, courier)
    session.flush()
    audit(
        session,
        actor=f"courier:{courier_id}",
        action="courier_offer.accepted",
        subject_type="courier_offer",
        subject_id=offer.id,
        detail=f"order={order.id}",
    )
    return plan_for(session, courier_id)


def decline_delivery(
    session: Session, *, offer_id: int, courier_id: int, reason: str, now: datetime | None = None
) -> None:
    now = now or _now()
    offer = session.get(CourierOffer, offer_id)
    if offer is None:
        raise CourierError(f"unknown offer {offer_id}")
    if offer.courier_id != courier_id:
        raise CourierError("this offer was not made to you")
    if offer.state is not od.OfferState.OFFERED:
        raise CourierError(f"this offer is already {offer.state.lower()}")
    od.assert_offer_transition(offer.state, od.OfferState.DECLINED)
    offer.state = od.OfferState.DECLINED
    offer.answered_at = now
    offer.reason = reason
    session.flush()
    audit(
        session,
        actor=f"courier:{courier_id}",
        action="courier_offer.declined",
        subject_type="courier_offer",
        subject_id=offer.id,
        detail=reason,
    )


def _expire_courier_offer(session: Session, offer: CourierOffer, *, now: datetime) -> None:
    od.assert_offer_transition(offer.state, od.OfferState.EXPIRED)
    offer.state = od.OfferState.EXPIRED
    offer.answered_at = now
    offer.reason = "no answer inside the offer window"
    session.flush()


def expire_due_courier_offers(session: Session, *, now: datetime | None = None) -> list[int]:
    now = now or _now()
    due = list(
        session.scalars(
            select(CourierOffer).where(
                CourierOffer.state == od.OfferState.OFFERED, CourierOffer.expires_at <= now
            )
        )
    )
    for offer in due:
        _expire_courier_offer(session, offer, now=now)
    return [o.order_id for o in due]


# --------------------------------------------------------------------------
# Working the run
# --------------------------------------------------------------------------


def arrive_at(session: Session, *, stop_id: int, courier_id: int, now: datetime | None = None):
    now = now or _now()
    stop = session.get(Stop, stop_id)
    if stop is None or stop.courier_id != courier_id:
        raise CourierError("that stop is not on your plan")
    cd.assert_stop_transition(stop.state, cd.StopState.ARRIVED)
    stop.state = cd.StopState.ARRIVED
    stop.arrived_at = now
    session.flush()
    return stop


def complete_stop(
    session: Session, *, stop_id: int, courier_id: int, now: datetime | None = None
) -> Stop:
    """Finish a stop. A collection IS the dispensing event.

    Routing it through `services.dispensing` is the whole point: the register
    write and the physical movement of the goods stay in one transaction, so
    goods cannot leave a pharmacy on a call that failed to record it.
    """
    now = now or _now()
    stop = session.get(Stop, stop_id)
    if stop is None or stop.courier_id != courier_id:
        raise CourierError("that stop is not on your plan")
    if stop.state is cd.StopState.PLANNED:
        arrive_at(session, stop_id=stop_id, courier_id=courier_id, now=now)
    cd.assert_stop_transition(stop.state, cd.StopState.DONE)

    order = session.get(Order, stop.order_id)
    assert order is not None

    if stop.kind is cd.StopKind.COLLECT:
        dispensing.dispense(
            session,
            order=order,
            courier_id=courier_id,
            actor=f"courier:{courier_id}",
            now=now,
        )
        assert_can_transition(order.status, OrderStatus.OUT_FOR_DELIVERY)
        order.status = OrderStatus.OUT_FOR_DELIVERY
        order.dispatched_at = now
    else:
        assert_can_transition(order.status, OrderStatus.DELIVERED)
        order.status = OrderStatus.DELIVERED
        order.delivered_at = now
        sla = sla_view(order.created_at, order.promised_minutes, now)
        order.promise_honoured = not sla.breached
        chemist = session.get(Chemist, order.chemist_id) if order.chemist_id else None
        if chemist is not None:
            chemist.orders_completed += 1
            if order.promise_honoured:
                chemist.orders_on_time += 1
        courier = session.get(Courier, courier_id)
        if courier is not None:
            courier.deliveries_completed += 1
            if order.promise_honoured:
                courier.deliveries_on_time += 1

    stop.state = cd.StopState.DONE
    stop.completed_at = now
    courier = session.get(Courier, courier_id)
    if courier is not None:
        _sync_state(session, courier)
    session.flush()
    audit(
        session,
        actor=f"courier:{courier_id}",
        action=f"stop.{stop.kind.lower()}_done",
        subject_type="order",
        subject_id=order.id,
    )
    return stop


def dispatch_queue(session: Session) -> list[Order]:
    """Ready orders awaiting a courier, most clinically urgent first.

    This is the lawful substitute for surge's demand-rationing half: when
    supply is short the queue is re-sequenced by need, never repriced.
    """
    orders = list(
        session.scalars(select(Order).where(Order.status == OrderStatus.READY_FOR_PICKUP))
    )
    return sorted(orders, key=lambda o: (dispatch_priority(o.urgency), o.created_at))
