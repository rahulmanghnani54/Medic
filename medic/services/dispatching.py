"""The dispatcher.

This is Uber's dispatch loop, applied to pharmacy supply. An order is not
assigned at checkout; it enters SEEKING_CHEMIST and the dispatcher offers it
to ranked candidates one at a time until someone accepts, the candidates run
out, or the delivery budget does.

Three properties are load-bearing, and each is here because copying Uber
naively would have broken something:

ATOMIC ACCEPT. Winning an offer, binding the chemist and leasing the stock
happen in one transaction on one database. Uber itself retreated from
application-layer sagas for exactly this path, having found they "left some
entities in inconsistent states that needed manual repair". The entity in
that window here would be a reservation of a controlled drug.

SEQUENTIAL OFFERS, NOT BROADCAST. Uber can show one trip to several drivers
at once because a driver is not consumed by being offered. Stock is. One open
offer per order, holding a lease, is the only safe shape.

FEASIBILITY RE-CHECKED EVERY ATTEMPT. A candidate judged feasible at 48
minutes is not feasible after a 5-minute offer round has timed out. Every
attempt re-scores against what is actually left of the hour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from medic.config import Settings, settings
from medic.domain import dispatch as od
from medic.domain import matching as mt
from medic.domain.deadlines import DeliveryBudget
from medic.domain.orders import (
    GateState,
    OrderStatus,
    assert_can_transition,
    assert_gate_transition,
    assert_sourcing_attempt,
    gates_cleared,
)
from medic.domain.schedules import DrugSchedule
from medic.models import Chemist, Offer, Order, Prescription, PrescriptionStatus
from medic.services import stock, supply
from medic.services.audit import audit


class DispatchError(Exception):
    """A dispatch request the rules refuse."""


def _now() -> datetime:
    return datetime.now(UTC)


def _basket_lines(order: Order) -> list[mt.BasketLine]:
    return [
        mt.BasketLine(
            medicine_id=item.medicine_id,
            medicine_name=item.medicine_name,
            qty=item.qty,
            schedule=DrugSchedule(item.schedule),
            requires_cold_chain=False,
        )
        for item in order.items
    ]


def _prescription_refused(session: Session, order: Order) -> bool:
    """Has a pharmacist refused the prescription backing this order?

    Checked both before an offer goes out and again when one is accepted,
    because an offer can already be open when the rejection lands.
    """
    if order.rx_gate is GateState.FAILED:
        return True
    if order.prescription_id is None:
        return False
    prescription = session.get(Prescription, order.prescription_id)
    return prescription is not None and prescription.status is PrescriptionStatus.REJECTED


def budget_for(order: Order) -> DeliveryBudget:
    return DeliveryBudget(created_at=order.created_at, promised_minutes=order.promised_minutes)


def open_offer(session: Session, order_id: int) -> Offer | None:
    return session.scalars(
        select(Offer).where(Offer.order_id == order_id, Offer.state == od.OfferState.OFFERED)
    ).first()


def offers_for(session: Session, order_id: int) -> list[Offer]:
    return list(
        session.scalars(select(Offer).where(Offer.order_id == order_id).order_by(Offer.attempt_seq))
    )


def tried_chemist_ids(session: Session, order_id: int) -> set[int]:
    return {o.chemist_id for o in offers_for(session, order_id)}


# --------------------------------------------------------------------------
# Sourcing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SourcingOutcome:
    action: str  # 'offered' | 'waiting' | 'exhausted' | 'not-sourcing'
    offer_id: int | None = None
    chemist_id: int | None = None
    reason: str = ""


def advance_sourcing(
    session: Session,
    *,
    order_id: int,
    now: datetime | None = None,
    config: Settings = settings,
) -> SourcingOutcome:
    """Move one order one step through the offer loop. Idempotent per state.

    Safe to call repeatedly: if an offer is already open it reports 'waiting'
    and changes nothing, which is what lets an at-least-once worker drive it.
    """
    now = now or _now()
    order = session.get(Order, order_id)
    if order is None:
        raise DispatchError(f"unknown order {order_id}")
    if order.status is not OrderStatus.SEEKING_CHEMIST:
        return SourcingOutcome(action="not-sourcing", reason=f"order is {order.status}")

    # A prescription rejected while the order was still being sourced must
    # stop the loop here. Without this check the dispatcher keeps offering,
    # a chemist accepts, and stock for a scheduled drug is leased against a
    # prescription a pharmacist has already refused.
    if _prescription_refused(session, order):
        return _exhaust(session, order, "the prescription was rejected", now=now)

    budget = budget_for(order)
    existing = open_offer(session, order_id)

    candidates = supply.candidates_near(
        session, lat=order.delivery_lat, lng=order.delivery_lng, config=config
    )
    tried = tried_chemist_ids(session, order_id)
    remaining = budget.remaining_minutes(now)
    matching = mt.find_matches(
        _basket_lines(order),
        delivery_lat=order.delivery_lat,
        delivery_lng=order.delivery_lng,
        candidates=candidates,
        at=now,
        exclude_chemist_ids=tried,
        budget_minutes=remaining,
        config=config,
    )
    ranked = [m.candidate.chemist_id for m in matching.full_fills]

    decision = od.next_offer(
        ranked_chemist_ids=ranked,
        already_tried=tried,
        open_offer_exists=existing is not None,
        attempt_seq=order.attempt_seq,
        max_attempts=config.max_sourcing_attempts,
        budget_allows=remaining > config.sla_feasibility_margin_minutes,
    )

    if decision.action == "wait":
        return SourcingOutcome(
            action="waiting", offer_id=existing.id if existing else None, reason=decision.reason
        )
    if decision.action == "exhausted":
        detail = decision.reason
        if not ranked and matching.partial_fills:
            short = ", ".join(matching.partial_fills[0].missing)
            detail = f"{detail}; nearest chemist short of: {short}"
        return _exhaust(session, order, detail, now=now)

    chemist_id = decision.chemist_id
    assert chemist_id is not None
    assert_sourcing_attempt(decision.attempt_seq, max_attempts=config.max_sourcing_attempts)
    chosen = next(m for m in matching.full_fills if m.candidate.chemist_id == chemist_id)

    offer = Offer(
        order_id=order.id,
        chemist_id=chemist_id,
        attempt_seq=decision.attempt_seq,
        state=od.OfferState.OFFERED,
        offered_at=now,
        # Committed in the same transaction as the state change, so a crashed
        # dispatcher cannot lose the timer -- the sweep rediscovers it here.
        expires_at=now + timedelta(minutes=config.chemist_accept_window_minutes),
        eta_minutes=chosen.eta.total_minutes,
    )
    session.add(offer)
    order.attempt_seq = decision.attempt_seq
    assert_gate_transition("chemist", order.chemist_gate, GateState.PENDING)
    order.chemist_gate = GateState.PENDING
    chemist = session.get(Chemist, chemist_id)
    if chemist is not None:
        chemist.orders_offered += 1
    session.flush()

    audit(
        session,
        actor="dispatcher",
        action="offer.made",
        subject_type="offer",
        subject_id=offer.id,
        detail=f"order={order.id} chemist={chemist_id} attempt={offer.attempt_seq} "
        f"eta={offer.eta_minutes}min budget_left={remaining}min",
    )
    return SourcingOutcome(action="offered", offer_id=offer.id, chemist_id=chemist_id)


def _exhaust(
    session: Session, order: Order, reason: str, *, now: datetime
) -> SourcingOutcome:
    """Close an order that sourcing cannot fill, releasing everything it held."""
    supersede_open_offers(session, order.id, reason=reason, now=now)
    stock.release(session, order_id=order.id)
    status = _closing_status(session, order)
    assert_can_transition(order.status, status)
    if order.chemist_gate not in (GateState.FAILED, GateState.NOT_REQUIRED):
        assert_gate_transition("chemist", order.chemist_gate, GateState.FAILED)
        order.chemist_gate = GateState.FAILED
    order.status = status
    order.closed_reason = reason
    order.chemist_id = None
    session.flush()
    audit(
        session,
        actor="dispatcher",
        action=f"order.{status.lower()}",
        subject_type="order",
        subject_id=order.id,
        detail=reason,
    )
    return SourcingOutcome(action="exhausted", reason=reason)


def _closing_status(session: Session, order: Order) -> OrderStatus:
    """Why did this order fail? The cause matters to the pilot's exit criteria.

    "Most orders delivered within 60 min, low stock-out cancellations" needs
    stock-outs to stay separable from "nobody was in range". So an order whose
    every answer was a stock-out closes as CANCELLED_STOCK_OUT, and one that
    simply ran out of reachable chemists closes as CANCELLED_NO_CHEMIST.
    """
    if order.rx_gate is GateState.FAILED:
        return OrderStatus.REJECTED_PRESCRIPTION
    answered = [
        o
        for o in offers_for(session, order.id)
        if o.state in (od.OfferState.DECLINED, od.OfferState.WITHDRAWN)
    ]
    if answered and all(o.stock_out for o in answered):
        return OrderStatus.CANCELLED_STOCK_OUT
    return OrderStatus.CANCELLED_NO_CHEMIST


def supersede_open_offers(
    session: Session, order_id: int, *, reason: str, now: datetime | None = None
) -> int:
    """Close any open offer for an order that no longer needs one."""
    now = now or _now()
    count = 0
    for offer in session.scalars(
        select(Offer).where(Offer.order_id == order_id, Offer.state == od.OfferState.OFFERED)
    ):
        od.assert_offer_transition(offer.state, od.OfferState.SUPERSEDED)
        offer.state = od.OfferState.SUPERSEDED
        offer.answered_at = now
        offer.reason = reason
        count += 1
    if count:
        session.flush()
    return count


# --------------------------------------------------------------------------
# The chemist's answer
# --------------------------------------------------------------------------


def accept_offer(
    session: Session,
    *,
    offer_id: int,
    chemist_id: int,
    now: datetime | None = None,
    config: Settings = settings,
) -> Order:
    """Win the offer, bind the chemist and lease the stock, atomically.

    Everything here must succeed together. If the lease fails because the
    shelf moved while the offer was open, nothing is bound and the order
    stays in sourcing.
    """
    now = now or _now()
    offer = session.get(Offer, offer_id)
    if offer is None:
        raise DispatchError(f"unknown offer {offer_id}")
    if offer.chemist_id != chemist_id:
        raise DispatchError("this offer was not made to you")
    if offer.state is not od.OfferState.OFFERED:
        raise DispatchError(f"this offer is already {offer.state.lower()}")
    if now >= offer.expires_at:
        # Treat a late accept as the expiry it is, rather than letting a slow
        # answer win a window it missed.
        expire_offer(session, offer, now=now)
        raise DispatchError("this offer has expired")

    order = session.get(Order, offer.order_id)
    assert order is not None
    if order.status is not OrderStatus.SEEKING_CHEMIST:
        raise DispatchError(f"order {order.id} is {order.status} and is no longer being sourced")

    # The same refusal advance_sourcing makes before offering, repeated here.
    # An offer can already be open when a pharmacist rejects, so if the
    # rejection hook is ever lost this is what stops a chemist accepting and
    # leasing scheduled units against a refused prescription.
    if _prescription_refused(session, order):
        raise DispatchError(
            f"order {order.id} cannot be accepted: the prescription was rejected"
        )

    # Re-run the full admission chain at accept time. The offer may have been
    # open for minutes: a licence can lapse, the shop can close, and the
    # budget has certainly shrunk.
    candidate = next(
        (
            c
            for c in supply.candidates_near(
                session, lat=order.delivery_lat, lng=order.delivery_lng, config=config
            )
            if c.chemist_id == chemist_id
        ),
        None,
    )
    if candidate is None:
        raise DispatchError("you are no longer a candidate for this order")

    lines = _basket_lines(order)
    verdict = mt.admits(
        candidate,
        delivery_lat=order.delivery_lat,
        delivery_lng=order.delivery_lng,
        needs_cold_chain=any(line.requires_cold_chain for line in lines),
        needs_verification=order.rx_gate is not GateState.NOT_REQUIRED,
        at=now,
        budget_minutes=budget_for(order).remaining_minutes(now),
        config=config,
    )
    if not verdict.ok:
        raise DispatchError(f"cannot accept: {verdict.reason}")

    # Raises InsufficientStock if the shelf cannot cover it, which aborts the
    # whole transaction and leaves the order unbound.
    stock.hold(session, order=order, chemist_id=chemist_id)

    od.assert_offer_transition(offer.state, od.OfferState.ACCEPTED)
    offer.state = od.OfferState.ACCEPTED
    offer.answered_at = now

    order.chemist_id = chemist_id
    assert_gate_transition("chemist", order.chemist_gate, GateState.CLEARED)
    order.chemist_gate = GateState.CLEARED
    assert_can_transition(order.status, OrderStatus.AWAITING_FULFILMENT)
    order.status = OrderStatus.AWAITING_FULFILMENT
    order.accepted_at = now
    # The firm promise is made here, not at checkout: before a chemist
    # accepts there is no one to keep it.
    order.committed_eta_minutes = verdict.eta.total_minutes if verdict.eta else None

    chemist = session.get(Chemist, chemist_id)
    if chemist is not None:
        chemist.orders_accepted += 1
    session.flush()

    audit(
        session,
        actor=f"chemist:{chemist_id}",
        action="offer.accepted",
        subject_type="offer",
        subject_id=offer.id,
        detail=f"order={order.id} eta={order.committed_eta_minutes}min",
    )
    maybe_ready(session, order)
    return order


def decline_offer(
    session: Session,
    *,
    offer_id: int,
    chemist_id: int,
    reason: str,
    stock_out: bool = False,
    now: datetime | None = None,
) -> Order:
    """A chemist passes. The order goes back to sourcing, it does not die."""
    now = now or _now()
    offer = session.get(Offer, offer_id)
    if offer is None:
        raise DispatchError(f"unknown offer {offer_id}")
    if offer.chemist_id != chemist_id:
        raise DispatchError("this offer was not made to you")
    if offer.state is not od.OfferState.OFFERED:
        raise DispatchError(f"this offer is already {offer.state.lower()}")

    od.assert_offer_transition(offer.state, od.OfferState.DECLINED)
    offer.state = od.OfferState.DECLINED
    offer.answered_at = now
    offer.reason = reason
    offer.stock_out = stock_out

    order = session.get(Order, offer.order_id)
    assert order is not None
    assert_gate_transition("chemist", order.chemist_gate, GateState.SEEKING)
    order.chemist_gate = GateState.SEEKING
    session.flush()
    audit(
        session,
        actor=f"chemist:{chemist_id}",
        action="offer.declined",
        subject_type="offer",
        subject_id=offer.id,
        detail=f"order={order.id}: {reason}",
    )
    return order


def withdraw_after_accept(
    session: Session,
    *,
    order_id: int,
    chemist_id: int,
    reason: str,
    now: datetime | None = None,
) -> Order:
    """A chemist who accepted finds the shelf does not match. Re-source.

    This is the outcome that has no ride-hailing analogue: an accept here is
    a claim about declared stock, and shelf truth is revealed later. It is a
    routing event, not the end of the order.
    """
    now = now or _now()
    order = session.get(Order, order_id)
    if order is None:
        raise DispatchError(f"unknown order {order_id}")
    if order.chemist_id != chemist_id:
        raise DispatchError("this order is not assigned to you")
    if order.status not in (OrderStatus.AWAITING_FULFILMENT, OrderStatus.READY_FOR_PICKUP):
        raise DispatchError(f"order {order_id} is {order.status} and can no longer be handed back")

    from medic.services.dispensing import already_dispensed

    if already_dispensed(session, order_id):
        raise DispatchError(
            "these goods have already been dispensed and are in a courier's custody"
        )

    accepted = session.scalars(
        select(Offer).where(
            Offer.order_id == order_id,
            Offer.chemist_id == chemist_id,
            Offer.state == od.OfferState.ACCEPTED,
        )
    ).first()
    if accepted is not None:
        od.assert_offer_transition(accepted.state, od.OfferState.WITHDRAWN)
        accepted.state = od.OfferState.WITHDRAWN
        accepted.answered_at = now
        accepted.reason = reason
        # A withdrawal after acceptance is a stock-out by definition: the
        # chemist agreed on declared stock and the shelf disagreed.
        accepted.stock_out = True

    stock.release(session, order_id=order_id, chemist_id=chemist_id)
    assert_gate_transition("chemist", order.chemist_gate, GateState.SEEKING)
    order.chemist_gate = GateState.SEEKING
    order.chemist_id = None
    order.committed_eta_minutes = None
    assert_can_transition(order.status, OrderStatus.SEEKING_CHEMIST)
    order.status = OrderStatus.SEEKING_CHEMIST
    session.flush()
    audit(
        session,
        actor=f"chemist:{chemist_id}",
        action="offer.withdrawn",
        subject_type="order",
        subject_id=order_id,
        detail=reason,
    )
    return order


# --------------------------------------------------------------------------
# Timers
# --------------------------------------------------------------------------


def expire_offer(session: Session, offer: Offer, *, now: datetime) -> None:
    od.assert_offer_transition(offer.state, od.OfferState.EXPIRED)
    offer.state = od.OfferState.EXPIRED
    offer.answered_at = now
    offer.reason = "no answer inside the offer window"
    order = session.get(Order, offer.order_id)
    if order is not None and order.chemist_gate is GateState.PENDING:
        assert_gate_transition("chemist", order.chemist_gate, GateState.SEEKING)
        order.chemist_gate = GateState.SEEKING
    session.flush()
    audit(
        session,
        actor="dispatcher",
        action="offer.expired",
        subject_type="offer",
        subject_id=offer.id,
        detail=f"order={offer.order_id} chemist={offer.chemist_id}",
    )


def expire_due_offers(session: Session, *, now: datetime | None = None) -> list[int]:
    """Sweep offers whose window has closed. Returns the affected order ids."""
    now = now or _now()
    due = list(
        session.scalars(
            select(Offer).where(
                Offer.state == od.OfferState.OFFERED, Offer.expires_at <= now
            )
        )
    )
    for offer in due:
        expire_offer(session, offer, now=now)
    return [offer.order_id for offer in due]


# --------------------------------------------------------------------------
# Gate coupling
# --------------------------------------------------------------------------


def maybe_ready(session: Session, order: Order) -> None:
    """Promote to READY_FOR_PICKUP once both gates have cleared."""
    if order.status is not OrderStatus.AWAITING_FULFILMENT:
        return
    if not gates_cleared(order.chemist_gate, order.rx_gate):
        return
    assert_can_transition(order.status, OrderStatus.READY_FOR_PICKUP)
    order.status = OrderStatus.READY_FOR_PICKUP
    session.flush()
    audit(
        session,
        actor="system",
        action="order.ready_for_pickup",
        subject_type="order",
        subject_id=order.id,
    )
