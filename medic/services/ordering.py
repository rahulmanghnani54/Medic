"""Order orchestration.

The domain modules hold the rules; this module applies them to the database.
Since the move to an Uber-style dispatch loop it no longer chooses a chemist:
`place_order` prices the basket, checks that somebody could fill it, and hands
the order to `services.dispatching`, which offers it to candidates in turn.

What stayed here is the demand side -- catalogue, prescriptions, pricing,
cancellation -- plus thin delegations kept so existing callers and the admin
console do not have to know where the work moved.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from medic.config import Settings, settings
from medic.domain import matching as mt
from medic.domain import orders as od
from medic.domain import prescriptions as rx
from medic.domain import triage
from medic.domain.deadlines import promise_band
from medic.domain.orders import GateState, OrderStatus, assert_gate_transition
from medic.domain.pricing import (
    OrderTotals,
    PricedLine,
    contribution_for,
    price_line,
    total_order,
)
from medic.domain.schedules import (
    DrugSchedule,
    controls_for_basket,
    policy_for,
)
from medic.models import (
    Chemist,
    Customer,
    Medicine,
    Offer,
    OfferState,
    Order,
    OrderItem,
    Prescription,
    PrescriptionItem,
    PrescriptionStatus,
)
from medic.services import dispatching, dispensing, stock, supply
from medic.services.audit import audit


class OrderingError(Exception):
    """A request that the platform's rules refuse."""


@dataclass(frozen=True)
class LineRequest:
    medicine_id: int
    qty: int


def _now() -> datetime:
    return datetime.now(UTC)


def _audit(
    session: Session,
    *,
    actor: str,
    action: str,
    subject_type: str,
    subject_id: int,
    detail: str | None = None,
) -> None:
    """Kept as a thin alias so existing call sites stay unchanged."""
    audit(
        session,
        actor=actor,
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        detail=detail,
    )


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------


def search_medicines(session: Session, query: str, limit: int = 20) -> list[Medicine]:
    stmt = select(Medicine)
    if query:
        like = f"%{query.strip()}%"
        stmt = stmt.where(Medicine.name.ilike(like) | Medicine.composition.ilike(like))
    return list(session.scalars(stmt.order_by(Medicine.name).limit(limit)))


# --------------------------------------------------------------------------
# Prescriptions
# --------------------------------------------------------------------------


def store_prescription(
    session: Session,
    *,
    customer_id: int,
    image_bytes: bytes,
    filename: str,
    prescriber_name: str | None = None,
    prescriber_reg_no: str | None = None,
    issued_on: datetime | None = None,
    config: Settings = settings,
) -> Prescription:
    """Store an uploaded prescription and flag an exact re-upload for the pharmacist."""
    if not image_bytes:
        raise OrderingError("the prescription file is empty")
    if session.get(Customer, customer_id) is None:
        raise OrderingError(f"unknown customer {customer_id}")

    fingerprint = rx.image_fingerprint(image_bytes)
    earlier = session.scalars(
        select(Prescription)
        .where(Prescription.image_sha256 == fingerprint)
        .order_by(Prescription.id)
    ).first()

    os.makedirs(config.upload_dir, exist_ok=True)
    suffix = os.path.splitext(filename)[1][:10] or ".bin"
    path = os.path.join(config.upload_dir, f"{fingerprint}{suffix}")
    if not os.path.exists(path):
        with open(path, "wb") as handle:
            handle.write(image_bytes)

    prescription = Prescription(
        customer_id=customer_id,
        image_sha256=fingerprint,
        stored_path=path,
        prescriber_name=prescriber_name,
        prescriber_reg_no=prescriber_reg_no,
        issued_on=issued_on,
        status=PrescriptionStatus.PENDING,
        max_uses=config.default_prescription_max_uses,
        duplicate_of_id=earlier.id if earlier else None,
    )
    session.add(prescription)
    session.flush()
    _audit(
        session,
        actor=f"customer:{customer_id}",
        action="prescription.uploaded",
        subject_type="prescription",
        subject_id=prescription.id,
        detail=f"duplicate_of={earlier.id}" if earlier else None,
    )
    return prescription


def _prescription_view(prescription: Prescription) -> rx.PrescriptionView:
    return rx.PrescriptionView(
        id=prescription.id,
        customer_id=prescription.customer_id,
        is_verified=prescription.status == PrescriptionStatus.VERIFIED,
        issued_on=prescription.issued_on,
        max_uses=prescription.max_uses,
        uses_count=prescription.uses_count,
        authorisations=tuple(
            rx.Authorisation(
                medicine_id=item.medicine_id,
                authorised_qty=item.authorised_qty,
                dispensed_qty=item.dispensed_qty,
            )
            for item in prescription.items
        ),
    )


def verification_queue(session: Session) -> list[Prescription]:
    return list(
        session.scalars(
            select(Prescription)
            .where(Prescription.status == PrescriptionStatus.PENDING)
            .order_by(Prescription.created_at)
        )
    )


def verify_prescription(
    session: Session,
    *,
    prescription_id: int,
    authorisations: list[tuple[int, int]],
    pharmacist: str,
    prescriber_name: str | None = None,
    prescriber_reg_no: str | None = None,
    issued_on: datetime | None = None,
    now: datetime | None = None,
) -> Prescription:
    """Record a pharmacist's verification: which medicines, in what quantity."""
    prescription = session.get(Prescription, prescription_id)
    if prescription is None:
        raise OrderingError(f"unknown prescription {prescription_id}")
    if prescription.status != PrescriptionStatus.PENDING:
        raise OrderingError(f"prescription {prescription_id} is already {prescription.status}")
    if not authorisations:
        raise OrderingError("a verification must authorise at least one medicine")

    for medicine_id, qty in authorisations:
        if qty <= 0:
            raise OrderingError("authorised quantity must be positive")
        if session.get(Medicine, medicine_id) is None:
            raise OrderingError(f"unknown medicine {medicine_id}")
        session.add(
            PrescriptionItem(
                prescription_id=prescription.id,
                medicine_id=medicine_id,
                authorised_qty=qty,
            )
        )

    prescription.status = PrescriptionStatus.VERIFIED
    prescription.verified_by = pharmacist
    prescription.verified_at = _now()
    if prescriber_name:
        prescription.prescriber_name = prescriber_name
    if prescriber_reg_no:
        prescription.prescriber_reg_no = prescriber_reg_no
    if issued_on:
        prescription.issued_on = issued_on
    session.flush()

    _audit(
        session,
        actor=pharmacist,
        action="prescription.verified",
        subject_type="prescription",
        subject_id=prescription.id,
        detail=f"authorised={authorisations}",
    )
    for order in _orders_awaiting(session, prescription.id):
        _clear_rx_gate(session, order, pharmacist, now=now)
    return prescription


def reject_prescription(
    session: Session, *, prescription_id: int, reason: str, pharmacist: str
) -> Prescription:
    prescription = session.get(Prescription, prescription_id)
    if prescription is None:
        raise OrderingError(f"unknown prescription {prescription_id}")
    if prescription.status != PrescriptionStatus.PENDING:
        raise OrderingError(f"prescription {prescription_id} is already {prescription.status}")
    if not reason.strip():
        raise OrderingError("a rejection needs a reason the customer can be told")

    prescription.status = PrescriptionStatus.REJECTED
    prescription.rejection_reason = reason
    prescription.verified_by = pharmacist
    prescription.verified_at = _now()
    session.flush()
    _audit(
        session,
        actor=pharmacist,
        action="prescription.rejected",
        subject_type="prescription",
        subject_id=prescription.id,
        detail=reason,
    )

    for order in _orders_awaiting(session, prescription.id):
        assert_gate_transition("prescription", order.rx_gate, GateState.FAILED)
        order.rx_gate = GateState.FAILED
        # Whatever stage the order had reached, everything it was holding goes
        # back: any open offer is superseded and any leased stock released.
        # Without this a rejected prescription can leave units reserved at a
        # chemist for an order that will never be dispensed.
        dispatching.supersede_open_offers(
            session, order.id, reason=f"prescription rejected: {reason}"
        )
        stock.release(session, order_id=order.id)
        order.chemist_id = None
        _close_order(
            session,
            order,
            status=OrderStatus.REJECTED_PRESCRIPTION,
            reason=f"prescription rejected: {reason}",
            actor=pharmacist,
        )
    return prescription


#: Every live status at which an order can still be waiting on a pharmacist.
#: SEEKING_CHEMIST matters most: an order being re-offered while verification
#: is pending must still be closed when the prescription is REJECTED. Missing
#: it would leave the dispatcher happily offering an order whose prescription
#: a pharmacist has already refused.
_AWAITING_RX_STATUSES = (
    OrderStatus.SEEKING_CHEMIST,
    OrderStatus.AWAITING_FULFILMENT,
    OrderStatus.READY_FOR_PICKUP,
)


def _orders_awaiting(session: Session, prescription_id: int) -> list[Order]:
    return list(
        session.scalars(
            select(Order).where(
                Order.prescription_id == prescription_id,
                Order.status.in_(_AWAITING_RX_STATUSES),
                Order.rx_gate == GateState.PENDING,
            )
        )
    )


# --------------------------------------------------------------------------
# Quoting and placing orders
# --------------------------------------------------------------------------


def _basket(session: Session, lines: list[LineRequest]) -> list[tuple[Medicine, int]]:
    if not lines:
        raise OrderingError("the basket is empty")
    resolved: list[tuple[Medicine, int]] = []
    for line in lines:
        if line.qty <= 0:
            raise OrderingError("quantity must be positive")
        medicine = session.get(Medicine, line.medicine_id)
        if medicine is None:
            raise OrderingError(f"unknown medicine {line.medicine_id}")
        resolved.append((medicine, line.qty))
    return resolved


@dataclass(frozen=True)
class PricedBasket:
    lines: tuple[PricedLine, ...]
    totals: OrderTotals


def price_basket(
    session: Session,
    *,
    lines: list[LineRequest],
    commission_bps: int | None = None,
    config: Settings = settings,
) -> tuple[tuple[PricedLine, ...], OrderTotals]:
    """Price a basket without reference to any chemist.

    Pricing used to be computed against the matched chemist's own commission
    rate, which coupled the price a patient sees to who happens to be nearest.
    Under the offer loop the chemist is not known at checkout, so the customer
    is priced at the platform rate and the settlement rate is recorded per
    chemist when one accepts.
    """
    basket = _basket(session, lines)
    priced = tuple(
        price_line(
            medicine_id=medicine.id,
            medicine_name=medicine.name,
            qty=qty,
            mrp_paise=medicine.mrp_paise,
            dpco_ceiling_paise=medicine.dpco_ceiling_paise,
        )
        for medicine, qty in basket
    )
    totals = total_order(
        list(priced),
        commission_bps=commission_bps or config.commission_bps_default,
        config=config,
    )
    return priced, totals


@dataclass(frozen=True)
class Feasibility:
    """Can anyone fill this, and roughly how fast -- with no names attached."""

    feasible: bool
    candidate_count: int
    eta_minutes_low: int
    eta_minutes_high: int
    requires_prescription: bool
    requires_verification: bool
    promise_minutes: int
    reason: str = ""


def assess_feasibility(
    session: Session,
    *,
    lines: list[LineRequest],
    delivery_lat: float,
    delivery_lng: float,
    now: datetime | None = None,
    config: Settings = settings,
) -> Feasibility:
    """A two-sided predicate, not an assignment.

    A quote must never name a chemist or state a firm promise: nobody has
    agreed to anything yet. It answers only "is there at least one chemist who
    could fill this in time", and returns a band.
    """
    now = now or _now()
    basket = _basket(session, lines)
    controls = controls_for_basket([(m.name, m.schedule) for m, _ in basket])

    basket_lines = [
        mt.BasketLine(
            medicine_id=medicine.id,
            medicine_name=medicine.name,
            qty=qty,
            schedule=DrugSchedule(medicine.schedule),
            requires_cold_chain=medicine.requires_cold_chain,
        )
        for medicine, qty in basket
    ]
    result = mt.find_matches(
        basket_lines,
        delivery_lat=delivery_lat,
        delivery_lng=delivery_lng,
        candidates=supply.candidates_near(
            session, lat=delivery_lat, lng=delivery_lng, config=config
        ),
        at=now,
        config=config,
    )
    if not result.full_fills:
        detail = ""
        if result.partial_fills:
            missing = ", ".join(result.partial_fills[0].missing)
            detail = f" Nearest partner chemist is short of: {missing}."
        return Feasibility(
            feasible=False,
            candidate_count=0,
            eta_minutes_low=0,
            eta_minutes_high=0,
            requires_prescription=controls.requires_prescription,
            requires_verification=controls.requires_pharmacist_verification,
            promise_minutes=config.delivery_promise_minutes,
            reason=(
                "No partner chemist can fill this basket inside the "
                f"{config.delivery_promise_minutes}-minute window." + detail
            ),
        )

    best = result.full_fills[0].eta.total_minutes
    low, high = promise_band(best, config=config)
    return Feasibility(
        feasible=True,
        candidate_count=len(result.full_fills),
        eta_minutes_low=low,
        eta_minutes_high=high,
        requires_prescription=controls.requires_prescription,
        requires_verification=controls.requires_pharmacist_verification,
        promise_minutes=config.delivery_promise_minutes,
    )


def place_order(
    session: Session,
    *,
    customer_id: int,
    lines: list[LineRequest],
    delivery_zone: str,
    delivery_lat: float,
    delivery_lng: float,
    prescription_id: int | None = None,
    now: datetime | None = None,
    config: Settings = settings,
) -> Order:
    now = now or _now()
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise OrderingError(f"unknown customer {customer_id}")

    basket = _basket(session, lines)

    # Validate the basket and its paperwork BEFORE looking for a chemist. A
    # customer who has not uploaded a prescription must be told that, not told
    # that nobody has stock -- and a Schedule X item must be refused whether or
    # not anyone stocks it.
    controls = controls_for_basket([(m.name, m.schedule) for m, _ in basket])

    prescription = None
    if prescription_id is not None:
        prescription = session.get(Prescription, prescription_id)
        if prescription is None:
            raise OrderingError(f"unknown prescription {prescription_id}")
        if prescription.customer_id != customer_id:
            raise OrderingError("this prescription belongs to a different customer")
        if prescription.status == PrescriptionStatus.REJECTED:
            raise OrderingError("this prescription was rejected by a pharmacist")

    requested = [
        rx.RequestedLine(
            medicine_id=medicine.id,
            medicine_name=medicine.name,
            qty=qty,
            schedule=DrugSchedule(medicine.schedule),
        )
        for medicine, qty in basket
    ]

    if controls.requires_prescription:
        if prescription is None:
            names = ", ".join(
                line.medicine_name
                for line in requested
                if policy_for(line.schedule).requires_prescription
            )
            raise OrderingError(f"a prescription is required for: {names}")
        # A pending prescription is allowed to ride along: the order waits at the
        # verification gate. An already-verified one must cover the basket now.
        if prescription.status == PrescriptionStatus.VERIFIED:
            try:
                rx.check_prescription_covers(
                    requested,
                    _prescription_view(prescription),
                    customer_id=customer_id,
                    now=now,
                    config=config,
                )
            except rx.PrescriptionProblem as exc:
                raise OrderingError(str(exc)) from exc

    # Price the basket and check that SOMEONE could fill it, without choosing
    # or freezing anyone. This is the central change: the request no longer
    # carries an assignment. Dispatch finds a chemist asynchronously, and the
    # firm promise is made when one accepts.
    assessment = assess_feasibility(
        session,
        lines=lines,
        delivery_lat=delivery_lat,
        delivery_lng=delivery_lng,
        now=now,
        config=config,
    )
    if not assessment.feasible:
        raise OrderingError(assessment.reason)
    priced, totals = price_basket(session, lines=lines, config=config)

    rx_gate = GateState.NOT_REQUIRED
    if controls.requires_pharmacist_verification:
        rx_gate = (
            GateState.CLEARED
            if prescription is not None and prescription.status == PrescriptionStatus.VERIFIED
            else GateState.PENDING
        )

    order = Order(
        customer_id=customer_id,
        chemist_id=None,
        prescription_id=prescription.id if prescription else None,
        status=OrderStatus.CREATED,
        chemist_gate=GateState.SEEKING,
        rx_gate=rx_gate,
        delivery_zone=delivery_zone,
        delivery_lat=delivery_lat,
        delivery_lng=delivery_lng,
        urgency=triage.urgency_for([m.urgency for m, _ in basket]),
        subtotal_paise=totals.subtotal_paise,
        chemist_discount_paise=totals.chemist_funded_discount_paise,
        platform_discount_paise=totals.platform_funded_discount_paise,
        delivery_fee_paise=totals.delivery_fee_paise,
        total_paise=totals.total_paise,
        commission_paise=totals.commission_paise,
        promised_minutes=config.delivery_promise_minutes,
        eta_minutes_at_checkout=assessment.eta_minutes_high,
        created_at=now,
    )
    session.add(order)
    session.flush()

    for priced_line, (medicine, _) in zip(priced, basket, strict=True):
        session.add(
            OrderItem(
                order_id=order.id,
                medicine_id=medicine.id,
                qty=priced_line.qty,
                medicine_name=medicine.name,
                schedule=DrugSchedule(medicine.schedule),
                unit_price_paise=priced_line.unit_price_paise,
                mrp_paise=priced_line.mrp_paise,
                line_total_paise=priced_line.line_total_paise,
            )
        )

    od.assert_can_transition(order.status, OrderStatus.SEEKING_CHEMIST)
    order.status = OrderStatus.SEEKING_CHEMIST
    session.flush()

    _audit(
        session,
        actor=f"customer:{customer_id}",
        action="order.placed",
        subject_type="order",
        subject_id=order.id,
        detail=(
            f"urgency={order.urgency} rx_gate={rx_gate} "
            f"eta_band={assessment.eta_minutes_low}-{assessment.eta_minutes_high}min "
            f"candidates={assessment.candidate_count}"
        ),
    )
    # Make the first offer immediately rather than waiting for the worker
    # tick: the 60-minute clock is already running.
    dispatching.advance_sourcing(session, order_id=order.id, now=now, config=config)
    return order


# --------------------------------------------------------------------------
# Chemist gate -- now thin delegations to the dispatcher
# --------------------------------------------------------------------------


def chemist_queue(session: Session, chemist_id: int) -> list[Order]:
    """Orders this chemist is currently holding, offered or accepted."""
    offered = {
        o.order_id
        for o in session.scalars(
            select(Offer).where(
                Offer.chemist_id == chemist_id, Offer.state == OfferState.OFFERED
            )
        )
    }
    assigned = {
        o.id
        for o in session.scalars(
            select(Order).where(
                Order.chemist_id == chemist_id,
                Order.status.in_(
                    [
                        OrderStatus.AWAITING_FULFILMENT,
                        OrderStatus.READY_FOR_PICKUP,
                        OrderStatus.OUT_FOR_DELIVERY,
                    ]
                ),
            )
        )
    }
    ids = offered | assigned
    if not ids:
        return []
    orders = list(session.scalars(select(Order).where(Order.id.in_(ids))))
    return sorted(orders, key=lambda o: o.created_at)


def _close_order(
    session: Session, order: Order, *, status: OrderStatus, reason: str, actor: str
) -> None:
    od.assert_can_transition(order.status, status)
    order.status = status
    order.closed_reason = reason
    session.flush()
    _audit(
        session,
        actor=actor,
        action=f"order.{status.lower()}",
        subject_type="order",
        subject_id=order.id,
        detail=reason,
    )


def _clear_rx_gate(
    session: Session, order: Order, actor: str, *, now: datetime | None = None
) -> None:
    now = now or _now()
    assert_gate_transition("prescription", order.rx_gate, GateState.CLEARED)
    order.rx_gate = GateState.CLEARED
    order.verified_at = now
    session.flush()
    _audit(
        session,
        actor=actor,
        action="order.rx_cleared",
        subject_type="order",
        subject_id=order.id,
    )
    dispatching.maybe_ready(session, order)
    # A verification that lands while the order is still being sourced does
    # not make it ready -- there is no chemist yet -- but it does unblock the
    # next offer round, so nudge the dispatcher.
    if order.status is OrderStatus.SEEKING_CHEMIST:
        # Pass the clock through. Falling back to the wall clock here would
        # re-score chemist opening hours and the remaining budget against the
        # moment of verification rather than the order's own clock, so a
        # replayed or delayed verification could exhaust an order simply
        # because the shops are shut at the replay instant.
        dispatching.advance_sourcing(session, order_id=order.id, now=now)


def cancel_by_customer(session: Session, *, order_id: int, customer_id: int, reason: str) -> Order:
    order = session.get(Order, order_id)
    if order is None:
        raise OrderingError(f"unknown order {order_id}")
    if order.customer_id != customer_id:
        raise OrderingError("this is not your order")
    if order.status is OrderStatus.OUT_FOR_DELIVERY:
        raise OrderingError(
            "the rider already holds this order; call support so the medicines can be "
            "returned to the dispensing chemist"
        )
    # Custody is read from the dispensing record, not from the status column.
    # Once transport events arrive asynchronously, status is no longer a sound
    # proxy for where the goods physically are.
    if dispensing.already_dispensed(session, order_id):
        raise OrderingError(
            "these medicines have already left the pharmacy; call support so they can be "
            "returned to the dispensing chemist"
        )
    dispatching.supersede_open_offers(session, order_id, reason="cancelled by the customer")
    stock.release(session, order_id=order_id)
    _close_order(
        session,
        order,
        status=OrderStatus.CANCELLED_BY_CUSTOMER,
        reason=reason,
        actor=f"customer:{customer_id}",
    )
    return order


def dispatch(
    session: Session,
    *,
    order_id: int,
    rider: str,
    actor: str = "admin",
    now: datetime | None = None,
) -> Order:
    """Operator override for collection, kept for the admin console.

    Routed through the dispensing service so it cannot diverge from the
    courier's own collection path: the register write happens in exactly one
    place regardless of who triggers it.
    """
    order = session.get(Order, order_id)
    if order is None:
        raise OrderingError(f"unknown order {order_id}")
    now = now or _now()
    od.assert_can_transition(order.status, OrderStatus.OUT_FOR_DELIVERY)
    dispensing.dispense(session, order=order, courier_id=None, actor=actor, now=now)
    order.status = OrderStatus.OUT_FOR_DELIVERY
    order.dispatched_at = now
    session.flush()
    _audit(
        session,
        actor=actor,
        action="order.dispatched",
        subject_type="order",
        subject_id=order.id,
        detail=f"rider={rider} (manual override)",
    )
    return order


def deliver(
    session: Session, *, order_id: int, actor: str = "admin", now: datetime | None = None
) -> Order:
    order = session.get(Order, order_id)
    if order is None:
        raise OrderingError(f"unknown order {order_id}")
    od.assert_can_transition(order.status, OrderStatus.DELIVERED)

    now = now or _now()
    order.status = OrderStatus.DELIVERED
    order.delivered_at = now
    sla = od.sla_view(order.created_at, order.promised_minutes, now)
    order.promise_honoured = not sla.breached

    chemist = session.get(Chemist, order.chemist_id) if order.chemist_id else None
    if chemist is not None:
        chemist.orders_completed += 1
        if order.promise_honoured:
            chemist.orders_on_time += 1
    session.flush()
    _audit(
        session,
        actor=actor,
        action="order.delivered",
        subject_type="order",
        subject_id=order.id,
        detail=f"on_time={order.promise_honoured} minutes_remaining={sla.minutes_remaining}",
    )
    return order


# --------------------------------------------------------------------------
# Measured economics -- replaces the brief's assumptions with observed numbers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EconomicsReport:
    delivered_orders: int
    mean_order_value_paise: int
    mean_commission_paise: int
    mean_delivery_fee_paise: int
    mean_contribution_paise: int
    on_time_rate: float | None
    break_even_orders_per_month: int | None
    assumptions_note: str


def economics(session: Session, *, config: Settings = settings) -> EconomicsReport:
    from medic.domain.pricing import break_even_orders_per_month

    delivered = list(
        session.scalars(select(Order).where(Order.status == OrderStatus.DELIVERED))
    )
    note = (
        "Revenue lines are measured from delivered orders. Rider, gateway and "
        "pharmacist costs are still the configured assumptions from the brief; "
        "replace them in medic/config.py with real Jaipur quotes."
    )
    if not delivered:
        return EconomicsReport(0, 0, 0, 0, 0, None, None, note)

    count = len(delivered)
    contributions = [
        contribution_for(
            OrderTotals(
                # Only the fields contribution_for() reads are reconstructed here;
                # MRP-level detail is not needed for the cost side.
                mrp_total_paise=order.subtotal_paise,
                statutory_reduction_paise=0,
                subtotal_paise=order.subtotal_paise,
                chemist_funded_discount_paise=order.chemist_discount_paise,
                platform_funded_discount_paise=order.platform_discount_paise,
                delivery_fee_paise=order.delivery_fee_paise,
                total_paise=order.total_paise,
                commission_paise=order.commission_paise,
            ),
            config=config,
        ).contribution_paise
        for order in delivered
    ]
    mean_contribution = sum(contributions) // count
    on_time = [order for order in delivered if order.promise_honoured]

    return EconomicsReport(
        delivered_orders=count,
        mean_order_value_paise=sum(o.total_paise for o in delivered) // count,
        mean_commission_paise=sum(o.commission_paise for o in delivered) // count,
        mean_delivery_fee_paise=sum(o.delivery_fee_paise for o in delivered) // count,
        mean_contribution_paise=mean_contribution,
        on_time_rate=len(on_time) / count,
        break_even_orders_per_month=break_even_orders_per_month(mean_contribution, config=config),
        assumptions_note=note,
    )
