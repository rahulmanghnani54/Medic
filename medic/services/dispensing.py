"""The dispensing event.

This module owns the moment scheduled goods leave a pharmacy's custody, and
everything the law attaches to it: the Schedule H1 register entry, the debit
against the prescription's authorised quantity, and the record of who
physically took the package.

It exists as its own module for one reason. Once transport is driven by a
courier calling "collected" rather than by an operator calling "dispatch",
the collection event and the register write can drift into different code
paths -- and then goods leave the shelf on a call that cannot write the
register. Putting the dispensing event behind one function, which the courier
path must go through, makes that drift impossible rather than merely unlikely.

Everything here is idempotent. At-least-once delivery makes a duplicate call a
question of when, not if, and a replayed dispense would duplicate a statutory
register row and double-debit a patient's authorisation. The uniqueness
constraint on (order_id, medicine_id) is the backstop; the state checks below
are the fast path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from medic.domain.orders import gates_cleared
from medic.domain.schedules import policy_for
from medic.models import (
    Chemist,
    Customer,
    Order,
    Prescription,
    RegisterEntry,
)
from medic.services import stock


class DispensingRefused(Exception):
    """The goods may not leave the pharmacy."""


@dataclass(frozen=True)
class DispenseResult:
    order_id: int
    chemist_id: int
    register_entries_written: int
    leases_consumed: int
    already_done: bool


def already_dispensed(session: Session, order_id: int) -> bool:
    """Have this order's goods left the pharmacy?

    Answered from `Order.dispensed_at`, not from the presence of a register
    entry: only scheduled lines write a register row, so an OTC basket
    already in a courier's bag would otherwise read as still on the shelf.
    """
    order = session.get(Order, order_id)
    if order is not None and order.dispensed_at is not None:
        return True
    return (
        session.scalars(
            select(RegisterEntry).where(RegisterEntry.order_id == order_id).limit(1)
        ).first()
        is not None
    )


def dispense(
    session: Session,
    *,
    order: Order,
    courier_id: int | None,
    actor: str,
    now: datetime | None = None,
) -> DispenseResult:
    """Record that this order's goods left the dispensing pharmacy.

    Call this and only this from any path that moves goods -- the courier's
    collection, or an operator override. It re-checks both gates itself rather
    than trusting the caller, because the caller is increasingly a device
    sending events that may arrive late, out of order, or twice.
    """
    now = now or datetime.now(UTC)

    if order.chemist_id is None:
        raise DispensingRefused(f"order {order.id} has no dispensing chemist")

    # Re-check the gates here, at the last possible moment. This is the
    # interlock that stops a scheduled drug leaving on a stale assumption:
    # a prescription can be rejected after a chemist has accepted and even
    # after stock is confirmed, and an asynchronous transport event must not
    # be able to outrun that.
    if not gates_cleared(order.chemist_gate, order.rx_gate):
        raise DispensingRefused(
            f"order {order.id} has not cleared both gates "
            f"(chemist={order.chemist_gate}, prescription={order.rx_gate})"
        )

    if already_dispensed(session, order.id):
        # A replay. Report it rather than writing a second register row.
        return DispenseResult(
            order_id=order.id,
            chemist_id=order.chemist_id,
            register_entries_written=0,
            leases_consumed=0,
            already_done=True,
        )

    chemist = session.get(Chemist, order.chemist_id)
    if chemist is None:
        raise DispensingRefused(f"order {order.id} points at a chemist that does not exist")
    customer = session.get(Customer, order.customer_id)
    assert customer is not None
    prescription = (
        session.get(Prescription, order.prescription_id) if order.prescription_id else None
    )

    # Debit the prescription BEFORE writing the register, so a basket that
    # would exceed its authorisation fails without leaving a partial record.
    if prescription is not None:
        for item in order.items:
            if not policy_for(item.schedule).requires_prescription:
                continue
            authorised = next(
                (row for row in prescription.items if row.medicine_id == item.medicine_id), None
            )
            if authorised is None:
                raise DispensingRefused(
                    f"{item.medicine_name} is not covered by prescription {prescription.id}"
                )
            if authorised.dispensed_qty + item.qty > authorised.authorised_qty:
                raise DispensingRefused(
                    f"{item.medicine_name}: dispensing {item.qty} would exceed the "
                    f"{authorised.authorised_qty} authorised on this prescription"
                )
            authorised.dispensed_qty += item.qty
        prescription.uses_count += 1

    written = 0
    for item in order.items:
        if not policy_for(item.schedule).requires_register_entry:
            continue
        session.add(
            RegisterEntry(
                order_id=order.id,
                medicine_id=item.medicine_id,
                chemist_id=chemist.id,
                medicine_name=item.medicine_name,
                schedule=item.schedule,
                qty=item.qty,
                patient_name=customer.name,
                patient_phone=customer.phone,
                prescriber_name=prescription.prescriber_name if prescription else None,
                prescriber_reg_no=prescription.prescriber_reg_no if prescription else None,
                pharmacist_reg_no=chemist.pharmacist_reg_no,
                custody_courier_id=courier_id,
                dispensed_at=now,
            )
        )
        written += 1

    consumed = stock.consume(session, order_id=order.id, chemist_id=chemist.id)
    order.dispensed_at = now
    session.flush()

    from medic.services.ordering import _audit

    _audit(
        session,
        actor=actor,
        action="order.dispensed",
        subject_type="order",
        subject_id=order.id,
        detail=(
            f"chemist={chemist.id} courier={courier_id} "
            f"register_entries={written} leases_consumed={consumed}"
        ),
    )
    return DispenseResult(
        order_id=order.id,
        chemist_id=chemist.id,
        register_entries_written=written,
        leases_consumed=consumed,
        already_done=False,
    )
