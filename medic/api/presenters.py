"""Shared mapping from ORM rows to API bodies."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from medic.domain.orders import sla_view
from medic.domain.schedules import policy_for
from medic.models import Chemist, GateState, Medicine, Order, OrderStatus, Prescription
from medic.schemas import (
    MedicineOut,
    OrderItemOut,
    OrderOut,
    PrescriptionOut,
    rupees_out,
)


def medicine_out(medicine: Medicine) -> MedicineOut:
    policy = policy_for(medicine.schedule)
    return MedicineOut(
        id=medicine.id,
        name=medicine.name,
        composition=medicine.composition,
        manufacturer=medicine.manufacturer,
        pack=medicine.pack,
        form=medicine.form,
        schedule=str(medicine.schedule),
        mrp=rupees_out(medicine.mrp_paise),
        requires_prescription=policy.requires_prescription,
        requires_pharmacist_verification=policy.requires_pharmacist_verification,
        is_chronic_care=medicine.is_chronic_care,
    )


def waiting_on(order: Order) -> str:
    """Plain-language statement of what the order is blocked on."""
    if order.status is OrderStatus.AWAITING_FULFILMENT:
        blocked = []
        if order.chemist_gate is GateState.PENDING:
            blocked.append("chemist to confirm stock")
        if order.rx_gate is GateState.PENDING:
            blocked.append("pharmacist to verify the prescription")
        return " and ".join(blocked) or "nothing"
    if order.status is OrderStatus.READY_FOR_PICKUP:
        return "rider pickup"
    if order.status is OrderStatus.OUT_FOR_DELIVERY:
        return "delivery"
    return "nothing"


_CUSTOMER_MESSAGE = {
    OrderStatus.CREATED: "Finding a partner chemist near you.",
    OrderStatus.READY_FOR_PICKUP: "Packed. A rider is on the way to collect it.",
    OrderStatus.OUT_FOR_DELIVERY: "Out for delivery.",
    OrderStatus.DELIVERED: "Delivered.",
    OrderStatus.BLOCKED_NOT_DISPENSABLE: (
        "This medicine cannot be supplied through the app. Please visit a pharmacy in person."
    ),
    OrderStatus.CANCELLED_NO_CHEMIST: "No partner chemist could fill this order in time.",
    OrderStatus.CANCELLED_STOCK_OUT: "The chemist was out of stock. You have not been charged.",
    OrderStatus.REJECTED_PRESCRIPTION: (
        "Our pharmacist could not accept this prescription. See the reason below."
    ),
    OrderStatus.CANCELLED_BY_CUSTOMER: "Cancelled at your request.",
}


def customer_message(order: Order) -> str:
    if order.status is OrderStatus.AWAITING_FULFILMENT:
        if order.rx_gate is GateState.PENDING:
            return "Our pharmacist is verifying your prescription. This usually takes 5-10 minutes."
        return "Your chemist is confirming stock and packing your order."
    return _CUSTOMER_MESSAGE.get(order.status, str(order.status))


def order_out(session: Session, order: Order, *, now: datetime | None = None) -> OrderOut:
    now = now or datetime.now(UTC)
    sla = sla_view(order.created_at, order.promised_minutes, now)
    chemist = session.get(Chemist, order.chemist_id) if order.chemist_id else None
    return OrderOut(
        id=order.id,
        status=str(order.status),
        chemist_gate=str(order.chemist_gate),
        prescription_gate=str(order.rx_gate),
        chemist_id=order.chemist_id,
        chemist_name=chemist.name if chemist else None,
        items=[
            OrderItemOut(
                medicine_name=item.medicine_name,
                schedule=str(item.schedule),
                qty=item.qty,
                line_total=rupees_out(item.line_total_paise),
            )
            for item in order.items
        ],
        total=rupees_out(order.total_paise),
        promise_minutes=order.promised_minutes,
        minutes_remaining=sla.minutes_remaining,
        sla_breached=sla.breached,
        promise_honoured=order.promise_honoured,
        created_at=order.created_at,
        customer_message=customer_message(order),
        closed_reason=order.closed_reason,
    )


def prescription_out(prescription: Prescription) -> PrescriptionOut:
    return PrescriptionOut(
        id=prescription.id,
        customer_id=prescription.customer_id,
        status=str(prescription.status),
        prescriber_name=prescription.prescriber_name,
        prescriber_reg_no=prescription.prescriber_reg_no,
        issued_on=prescription.issued_on,
        uses_count=prescription.uses_count,
        max_uses=prescription.max_uses,
        duplicate_of_id=prescription.duplicate_of_id,
        created_at=prescription.created_at,
        rejection_reason=prescription.rejection_reason,
    )
