"""Order orchestration.

The domain modules hold the rules; this module applies them to the database and
writes the audit trail. API handlers stay thin so that the tests can drive the
whole lifecycle without going through HTTP.
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
from medic.domain.geo import EtaBreakdown
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
    AuditEvent,
    Chemist,
    ChemistStock,
    Customer,
    GateState,
    Medicine,
    Order,
    OrderItem,
    OrderStatus,
    Prescription,
    PrescriptionItem,
    PrescriptionStatus,
    RegisterEntry,
)


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
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            detail=detail,
        )
    )
    # Flush here rather than leaving it to the next write: the audit trail has to
    # be readable the moment the step it records has happened, and the last step
    # in a flow has no later write to piggyback on.
    session.flush()


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
        _clear_rx_gate(session, order, pharmacist)
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
        order.rx_gate = GateState.FAILED
        _close_order(
            session,
            order,
            status=OrderStatus.REJECTED_PRESCRIPTION,
            reason=f"prescription rejected: {reason}",
            actor=pharmacist,
        )
    return prescription


def _orders_awaiting(session: Session, prescription_id: int) -> list[Order]:
    return list(
        session.scalars(
            select(Order).where(
                Order.prescription_id == prescription_id,
                Order.status == OrderStatus.AWAITING_FULFILMENT,
                Order.rx_gate == GateState.PENDING,
            )
        )
    )


# --------------------------------------------------------------------------
# Quoting and placing orders
# --------------------------------------------------------------------------


def _candidates(session: Session) -> list[mt.ChemistCandidate]:
    chemists = list(session.scalars(select(Chemist)))
    stock_rows = list(session.scalars(select(ChemistStock)))
    by_chemist: dict[int, dict[int, int]] = {}
    for row in stock_rows:
        by_chemist.setdefault(row.chemist_id, {})[row.medicine_id] = row.qty_on_hand
    return [
        mt.ChemistCandidate(
            chemist_id=chemist.id,
            name=chemist.name,
            zone=chemist.zone,
            lat=chemist.lat,
            lng=chemist.lng,
            is_active=chemist.is_active,
            is_24x7=chemist.is_24x7,
            opens_hour_ist=chemist.opens_hour_ist,
            closes_hour_ist=chemist.closes_hour_ist,
            can_cold_chain=chemist.can_cold_chain,
            stock=by_chemist.get(chemist.id, {}),
            acceptance_rate=chemist.acceptance_rate,
            on_time_rate=chemist.on_time_rate,
            licence_expires_on=chemist.licence_expires_on,
        )
        for chemist in chemists
    ]


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
class Quote:
    """What the customer sees before paying."""

    lines: tuple[PricedLine, ...]
    totals: OrderTotals
    chemist_id: int
    chemist_name: str
    eta: EtaBreakdown
    requires_prescription: bool
    requires_verification: bool
    promise_minutes: int

    @property
    def contribution_paise(self) -> int:
        return contribution_for(self.totals).contribution_paise


def quote_order(
    session: Session,
    *,
    customer_id: int,
    lines: list[LineRequest],
    delivery_lat: float,
    delivery_lng: float,
    now: datetime | None = None,
    config: Settings = settings,
) -> Quote:
    """Price and match a basket without committing to it.

    Raises rather than returning a partial quote: a customer should not be shown
    a 60-minute promise we have no chemist to keep.
    """
    now = now or _now()
    if session.get(Customer, customer_id) is None:
        raise OrderingError(f"unknown customer {customer_id}")
    basket = _basket(session, lines)

    # Fails closed for Schedule X and anything unclassified.
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
        candidates=_candidates(session),
        at=now,
        config=config,
    )
    best = result.best
    if best is None:
        detail = ""
        if result.partial_fills:
            missing = ", ".join(result.partial_fills[0].missing)
            detail = f" Nearest partner chemist is short of: {missing}."
        raise OrderingError(
            "No partner chemist can fill this basket inside the 60-minute window." + detail
        )

    chemist = session.get(Chemist, best.candidate.chemist_id)
    assert chemist is not None
    priced = [
        price_line(
            medicine_id=medicine.id,
            medicine_name=medicine.name,
            qty=qty,
            mrp_paise=medicine.mrp_paise,
            dpco_ceiling_paise=medicine.dpco_ceiling_paise,
        )
        for medicine, qty in basket
    ]
    totals = total_order(priced, commission_bps=chemist.commission_bps, config=config)

    return Quote(
        lines=tuple(priced),
        totals=totals,
        chemist_id=chemist.id,
        chemist_name=chemist.name,
        eta=best.eta,
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

    # Only now, with the basket lawful and its paperwork in hand, look for a
    # chemist who can actually fill it inside the window.
    quote = quote_order(
        session,
        customer_id=customer_id,
        lines=lines,
        delivery_lat=delivery_lat,
        delivery_lng=delivery_lng,
        now=now,
        config=config,
    )

    rx_gate = GateState.NOT_REQUIRED
    if controls.requires_pharmacist_verification:
        rx_gate = (
            GateState.CLEARED
            if prescription is not None and prescription.status == PrescriptionStatus.VERIFIED
            else GateState.PENDING
        )

    order = Order(
        customer_id=customer_id,
        chemist_id=quote.chemist_id,
        prescription_id=prescription.id if prescription else None,
        status=OrderStatus.CREATED,
        chemist_gate=GateState.PENDING,
        rx_gate=rx_gate,
        delivery_zone=delivery_zone,
        delivery_lat=delivery_lat,
        delivery_lng=delivery_lng,
        subtotal_paise=quote.totals.subtotal_paise,
        chemist_discount_paise=quote.totals.chemist_funded_discount_paise,
        platform_discount_paise=quote.totals.platform_funded_discount_paise,
        delivery_fee_paise=quote.totals.delivery_fee_paise,
        total_paise=quote.totals.total_paise,
        commission_paise=quote.totals.commission_paise,
        promised_minutes=quote.promise_minutes,
        eta_minutes_at_checkout=quote.eta.total_minutes,
        created_at=now,
    )
    session.add(order)
    session.flush()

    for priced, (medicine, _) in zip(quote.lines, basket, strict=True):
        session.add(
            OrderItem(
                order_id=order.id,
                medicine_id=medicine.id,
                qty=priced.qty,
                medicine_name=medicine.name,
                schedule=DrugSchedule(medicine.schedule),
                unit_price_paise=priced.unit_price_paise,
                mrp_paise=priced.mrp_paise,
                line_total_paise=priced.line_total_paise,
            )
        )

    od.assert_can_transition(order.status, OrderStatus.AWAITING_FULFILMENT)
    order.status = OrderStatus.AWAITING_FULFILMENT
    chemist = session.get(Chemist, quote.chemist_id)
    assert chemist is not None
    chemist.orders_offered += 1
    session.flush()

    _audit(
        session,
        actor=f"customer:{customer_id}",
        action="order.placed",
        subject_type="order",
        subject_id=order.id,
        detail=f"chemist={quote.chemist_id} eta={quote.eta.total_minutes}min rx_gate={rx_gate}",
    )
    return order


# --------------------------------------------------------------------------
# Chemist gate
# --------------------------------------------------------------------------


def chemist_queue(session: Session, chemist_id: int) -> list[Order]:
    return list(
        session.scalars(
            select(Order)
            .where(
                Order.chemist_id == chemist_id,
                Order.status.in_(
                    [
                        OrderStatus.AWAITING_FULFILMENT,
                        OrderStatus.READY_FOR_PICKUP,
                        OrderStatus.OUT_FOR_DELIVERY,
                    ]
                ),
            )
            .order_by(Order.created_at)
        )
    )


def _reserve_stock(session: Session, order: Order) -> None:
    """Hold the stock the chemist just confirmed, so it is not promised twice."""
    for item in order.items:
        row = session.scalars(
            select(ChemistStock).where(
                ChemistStock.chemist_id == order.chemist_id,
                ChemistStock.medicine_id == item.medicine_id,
            )
        ).first()
        if row is None or row.qty_on_hand < item.qty:
            have = row.qty_on_hand if row else 0
            raise OrderingError(
                f"{item.medicine_name}: {item.qty} needed but {have} on hand. "
                f"Reject the order as a stock-out instead of confirming it."
            )
        row.qty_on_hand -= item.qty


def _release_stock(session: Session, order: Order) -> None:
    for item in order.items:
        row = session.scalars(
            select(ChemistStock).where(
                ChemistStock.chemist_id == order.chemist_id,
                ChemistStock.medicine_id == item.medicine_id,
            )
        ).first()
        if row is not None:
            row.qty_on_hand += item.qty


def chemist_accept(session: Session, *, order_id: int, chemist_id: int) -> Order:
    """Chemist accepts the order and confirms the stock is physically there."""
    order = session.get(Order, order_id)
    if order is None:
        raise OrderingError(f"unknown order {order_id}")
    if order.chemist_id != chemist_id:
        raise OrderingError("this order was not offered to you")
    if order.status != OrderStatus.AWAITING_FULFILMENT:
        raise OrderingError(f"order {order_id} is {order.status} and cannot be accepted")
    if order.chemist_gate is not GateState.PENDING:
        raise OrderingError(f"order {order_id} has already been {order.chemist_gate.lower()}")

    _reserve_stock(session, order)
    order.chemist_gate = GateState.CLEARED
    order.accepted_at = _now()
    chemist = session.get(Chemist, chemist_id)
    assert chemist is not None
    chemist.orders_accepted += 1
    session.flush()
    _audit(
        session,
        actor=f"chemist:{chemist_id}",
        action="order.accepted",
        subject_type="order",
        subject_id=order.id,
    )
    _maybe_ready(session, order)
    return order


def chemist_reject(
    session: Session, *, order_id: int, chemist_id: int, reason: str, stock_out: bool = True
) -> Order:
    order = session.get(Order, order_id)
    if order is None:
        raise OrderingError(f"unknown order {order_id}")
    if order.chemist_id != chemist_id:
        raise OrderingError("this order was not offered to you")
    # A chemist can pull out any time before the rider collects: they may have
    # accepted on a declared stock figure and then found the strip missing,
    # damaged or short-dated on the shelf.
    if order.status not in (OrderStatus.AWAITING_FULFILMENT, OrderStatus.READY_FOR_PICKUP):
        raise OrderingError(f"order {order_id} is {order.status} and cannot be rejected")

    if order.chemist_gate is GateState.CLEARED:
        _release_stock(session, order)
    order.chemist_gate = GateState.FAILED
    _close_order(
        session,
        order,
        status=OrderStatus.CANCELLED_STOCK_OUT if stock_out else OrderStatus.CANCELLED_NO_CHEMIST,
        reason=reason,
        actor=f"chemist:{chemist_id}",
    )
    return order


def _clear_rx_gate(session: Session, order: Order, actor: str) -> None:
    order.rx_gate = GateState.CLEARED
    order.verified_at = _now()
    session.flush()
    _audit(
        session,
        actor=actor,
        action="order.rx_cleared",
        subject_type="order",
        subject_id=order.id,
    )
    _maybe_ready(session, order)


def _maybe_ready(session: Session, order: Order) -> None:
    """Promote to READY_FOR_PICKUP once both gates have cleared."""
    if order.status != OrderStatus.AWAITING_FULFILMENT:
        return
    if not od.gates_cleared(order.chemist_gate, order.rx_gate):
        return
    od.assert_can_transition(order.status, OrderStatus.READY_FOR_PICKUP)
    order.status = OrderStatus.READY_FOR_PICKUP
    session.flush()
    _audit(
        session,
        actor="system",
        action="order.ready_for_pickup",
        subject_type="order",
        subject_id=order.id,
    )


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
    if order.chemist_gate is GateState.CLEARED:
        _release_stock(session, order)
    _close_order(
        session,
        order,
        status=OrderStatus.CANCELLED_BY_CUSTOMER,
        reason=reason,
        actor=f"customer:{customer_id}",
    )
    return order


# --------------------------------------------------------------------------
# Dispatch and delivery
# --------------------------------------------------------------------------


def dispatch(session: Session, *, order_id: int, rider: str, actor: str = "admin") -> Order:
    """Rider collects from the chemist. This is the dispensing event.

    The Schedule H1 register entry and the prescription's use count are both
    written here rather than on delivery, because the medicine leaves the
    pharmacy's custody at pickup.
    """
    order = session.get(Order, order_id)
    if order is None:
        raise OrderingError(f"unknown order {order_id}")
    od.assert_can_transition(order.status, OrderStatus.OUT_FOR_DELIVERY)
    if not od.gates_cleared(order.chemist_gate, order.rx_gate):
        raise OrderingError(
            f"order {order_id} has not cleared both gates "
            f"(chemist={order.chemist_gate}, prescription={order.rx_gate})"
        )

    chemist = session.get(Chemist, order.chemist_id) if order.chemist_id else None
    if chemist is None:
        raise OrderingError(f"order {order_id} has no assigned chemist")
    customer = session.get(Customer, order.customer_id)
    assert customer is not None
    prescription = (
        session.get(Prescription, order.prescription_id) if order.prescription_id else None
    )

    register_lines = [
        item for item in order.items if policy_for(item.schedule).requires_register_entry
    ]
    for item in register_lines:
        session.add(
            RegisterEntry(
                order_id=order.id,
                chemist_id=chemist.id,
                medicine_name=item.medicine_name,
                schedule=item.schedule,
                qty=item.qty,
                patient_name=customer.name,
                patient_phone=customer.phone,
                prescriber_name=prescription.prescriber_name if prescription else None,
                prescriber_reg_no=prescription.prescriber_reg_no if prescription else None,
                pharmacist_reg_no=chemist.pharmacist_reg_no,
            )
        )

    if prescription is not None:
        for item in order.items:
            if not policy_for(item.schedule).requires_prescription:
                continue
            authorised = next(
                (row for row in prescription.items if row.medicine_id == item.medicine_id), None
            )
            if authorised is None:
                raise OrderingError(
                    f"{item.medicine_name} is not covered by prescription {prescription.id}"
                )
            if authorised.dispensed_qty + item.qty > authorised.authorised_qty:
                raise OrderingError(
                    f"{item.medicine_name}: dispensing {item.qty} would exceed the "
                    f"{authorised.authorised_qty} authorised on this prescription"
                )
            authorised.dispensed_qty += item.qty
        prescription.uses_count += 1

    order.status = OrderStatus.OUT_FOR_DELIVERY
    order.dispatched_at = _now()
    session.flush()
    _audit(
        session,
        actor=actor,
        action="order.dispatched",
        subject_type="order",
        subject_id=order.id,
        detail=f"rider={rider} register_entries={len(register_lines)}",
    )
    return order


def deliver(session: Session, *, order_id: int, actor: str = "admin") -> Order:
    order = session.get(Order, order_id)
    if order is None:
        raise OrderingError(f"unknown order {order_id}")
    od.assert_can_transition(order.status, OrderStatus.DELIVERED)

    now = _now()
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
