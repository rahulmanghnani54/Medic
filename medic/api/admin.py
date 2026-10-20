"""Admin and pharmacist console: verification queue, dispatch, SLA, economics."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from medic.api.presenters import order_out, prescription_out, waiting_on
from medic.auth import require_role
from medic.db import get_session
from medic.domain.orders import sla_view
from medic.models import (
    AuditEvent,
    Chemist,
    Courier,
    Medicine,
    Offer,
    OfferState,
    Order,
    OrderStatus,
    Prescription,
)
from medic.schemas import (
    DispatchIn,
    EconomicsOut,
    OrderOut,
    PrescriptionOut,
    RejectIn,
    SlaRowOut,
    VerifyIn,
    rupees_out,
)
from medic.services import ordering

router = APIRouter(prefix="/api/admin", tags=["admin"])
_pharmacist = require_role("pharmacist")
_admin = require_role("admin")

LIVE_STATUSES = (
    OrderStatus.SEEKING_CHEMIST,
    OrderStatus.AWAITING_FULFILMENT,
    OrderStatus.READY_FOR_PICKUP,
    OrderStatus.OUT_FOR_DELIVERY,
)


@router.get("/verification-queue")
def queue(
    session: Session = Depends(get_session),
    _: str = _pharmacist,
) -> list[dict]:
    """Pending prescriptions, oldest first, with what the pharmacist needs to decide.

    `orders_waiting` matters because each one has a 60-minute clock running, and
    `duplicate_of_id` is the re-upload flag: the same image seen before, which is
    the main abuse route for habit-forming drugs.
    """
    rows = []
    for prescription in ordering.verification_queue(session):
        orders = session.scalars(
            select(Order).where(Order.prescription_id == prescription.id)
        ).all()
        requested: list[dict] = []
        for order in orders:
            for item in order.items:
                requested.append(
                    {
                        "order_id": order.id,
                        "medicine_id": item.medicine_id,
                        "medicine_name": item.medicine_name,
                        "schedule": str(item.schedule),
                        "qty": item.qty,
                    }
                )
        now = datetime.now(UTC)
        rows.append(
            {
                "prescription": prescription_out(prescription).model_dump(mode="json"),
                "requested_items": requested,
                "orders_waiting": [
                    {
                        "order_id": order.id,
                        "minutes_remaining": sla_view(
                            order.created_at, order.promised_minutes, now
                        ).minutes_remaining,
                    }
                    for order in orders
                    if order.status in LIVE_STATUSES
                ],
                "is_reupload": prescription.duplicate_of_id is not None,
            }
        )
    return rows


@router.get("/prescriptions/{prescription_id}", response_model=PrescriptionOut)
def read_prescription(
    prescription_id: int,
    session: Session = Depends(get_session),
    _: str = _pharmacist,
) -> PrescriptionOut:
    prescription = session.get(Prescription, prescription_id)
    if prescription is None:
        raise HTTPException(status_code=404, detail="prescription not found")
    return prescription_out(prescription)


@router.post("/prescriptions/{prescription_id}/verify", response_model=PrescriptionOut)
def verify(
    prescription_id: int,
    body: VerifyIn,
    session: Session = Depends(get_session),
    _: str = _pharmacist,
) -> PrescriptionOut:
    prescription = ordering.verify_prescription(
        session,
        prescription_id=prescription_id,
        authorisations=[(row.medicine_id, row.authorised_qty) for row in body.authorisations],
        pharmacist=body.pharmacist,
        prescriber_name=body.prescriber_name,
        prescriber_reg_no=body.prescriber_reg_no,
        issued_on=body.issued_on,
    )
    return prescription_out(prescription)


@router.post("/prescriptions/{prescription_id}/reject", response_model=PrescriptionOut)
def reject(
    prescription_id: int,
    body: RejectIn,
    pharmacist: str = "pharmacist",
    session: Session = Depends(get_session),
    _: str = _pharmacist,
) -> PrescriptionOut:
    prescription = ordering.reject_prescription(
        session, prescription_id=prescription_id, reason=body.reason, pharmacist=pharmacist
    )
    return prescription_out(prescription)


@router.post("/orders/{order_id}/dispatch", response_model=OrderOut)
def dispatch(
    order_id: int,
    body: DispatchIn,
    session: Session = Depends(get_session),
    _: str = _admin,
) -> OrderOut:
    order = ordering.dispatch(session, order_id=order_id, rider=body.rider)
    return order_out(session, order)


@router.post("/orders/{order_id}/deliver", response_model=OrderOut)
def deliver(
    order_id: int,
    session: Session = Depends(get_session),
    _: str = _admin,
) -> OrderOut:
    order = ordering.deliver(session, order_id=order_id)
    return order_out(session, order)


@router.get("/sla", response_model=list[SlaRowOut])
def sla_board(
    session: Session = Depends(get_session),
    _: str = _admin,
) -> list[SlaRowOut]:
    """Live orders, most urgent first."""
    now = datetime.now(UTC)
    orders = session.scalars(
        select(Order).where(Order.status.in_(LIVE_STATUSES)).order_by(Order.created_at)
    ).all()
    rows = []
    for order in orders:
        sla = sla_view(order.created_at, order.promised_minutes, now)
        chemist = session.get(Chemist, order.chemist_id) if order.chemist_id else None
        rows.append(
            SlaRowOut(
                order_id=order.id,
                status=str(order.status),
                zone=order.delivery_zone,
                minutes_remaining=sla.minutes_remaining,
                at_risk=sla.at_risk,
                breached=sla.breached,
                chemist_name=chemist.name if chemist else None,
                waiting_on=waiting_on(order),
            )
        )
    return sorted(rows, key=lambda row: row.minutes_remaining)


@router.get("/economics", response_model=EconomicsOut)
def economics(
    session: Session = Depends(get_session),
    _: str = _admin,
) -> EconomicsOut:
    report = ordering.economics(session)
    return EconomicsOut(
        delivered_orders=report.delivered_orders,
        mean_order_value=rupees_out(report.mean_order_value_paise),
        mean_commission=rupees_out(report.mean_commission_paise),
        mean_delivery_fee=rupees_out(report.mean_delivery_fee_paise),
        mean_contribution=rupees_out(report.mean_contribution_paise),
        on_time_rate=report.on_time_rate,
        break_even_orders_per_month=report.break_even_orders_per_month,
        assumptions_note=report.assumptions_note,
    )


@router.get("/catalogue")
def catalogue(
    session: Session = Depends(get_session),
    _: str = _admin,
) -> list[dict]:
    """Catalogue with its schedule tagging, which drives every prescription rule."""
    from medic.domain.schedules import policy_for

    rows = session.scalars(select(Medicine).order_by(Medicine.name)).all()
    return [
        {
            "id": m.id,
            "name": m.name,
            "schedule": str(m.schedule),
            "mrp": rupees_out(m.mrp_paise),
            "dpco_ceiling": rupees_out(m.dpco_ceiling_paise) if m.dpco_ceiling_paise else None,
            "requires_prescription": policy_for(m.schedule).requires_prescription,
            "requires_register_entry": policy_for(m.schedule).requires_register_entry,
            "cold_chain": m.requires_cold_chain,
        }
        for m in rows
    ]


@router.get("/dispatch")
def dispatch_board(
    session: Session = Depends(get_session),
    _: str = _admin,
) -> dict:
    """The offer funnel: who was asked, what they said, and what is stranded.

    This is the diagnostic the synchronous design could not produce. It used
    to compute a ranked candidate list on every checkout and throw all of it
    away except the winner, so a chemist who was never asked and a chemist
    who refused looked identical afterwards.
    """
    now = datetime.now(UTC)
    offers = session.scalars(select(Offer).order_by(Offer.id.desc()).limit(200)).all()
    by_state: dict[str, int] = {}
    declines: dict[str, int] = {}
    for offer in offers:
        by_state[str(offer.state)] = by_state.get(str(offer.state), 0) + 1
        if offer.state is OfferState.DECLINED and offer.reason:
            declines[offer.reason] = declines.get(offer.reason, 0) + 1

    sourcing = session.scalars(
        select(Order).where(Order.status == OrderStatus.SEEKING_CHEMIST)
    ).all()
    chemists = {c.id: c.name for c in session.scalars(select(Chemist))}
    return {
        "offers_by_state": by_state,
        "decline_reasons": declines,
        "open_offers": [
            {
                "offer_id": o.id,
                "order_id": o.order_id,
                "chemist": chemists.get(o.chemist_id, "?"),
                "attempt": o.attempt_seq,
                "eta_minutes": o.eta_minutes,
                "expires_in_seconds": max(0, int((o.expires_at - now).total_seconds())),
            }
            for o in offers
            if o.state is OfferState.OFFERED
        ],
        "seeking_chemist": [
            {
                "order_id": o.id,
                "zone": o.delivery_zone,
                "urgency": str(o.urgency),
                "attempts": o.attempt_seq,
                "minutes_remaining": sla_view(
                    o.created_at, o.promised_minutes, now
                ).minutes_remaining,
            }
            for o in sourcing
        ],
        "couriers": [
            {
                "id": c.id,
                "name": c.name,
                "state": str(c.state),
                "cold_chain": c.can_cold_chain,
                "acceptance_rate": round(c.acceptance_rate, 2),
                "deliveries": c.deliveries_completed,
            }
            for c in session.scalars(select(Courier).order_by(Courier.id))
        ],
    }


@router.get("/audit")
def audit(
    limit: int = 100,
    session: Session = Depends(get_session),
    _: str = _admin,
) -> list[dict]:
    events = session.scalars(
        select(AuditEvent).order_by(AuditEvent.id.desc()).limit(min(limit, 500))
    ).all()
    return [
        {
            "at": event.at.isoformat(),
            "actor": event.actor,
            "action": event.action,
            "subject": f"{event.subject_type}:{event.subject_id}",
            "detail": event.detail,
        }
        for event in events
    ]
