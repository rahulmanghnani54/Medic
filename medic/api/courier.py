"""Courier app endpoints.

Every route here authenticates the individual courier from their own secret
rather than a shared role token, because completing a collection is the
dispensing event and the register records who took custody. The courier id in
the path must match the authenticated courier.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from medic.auth import courier_from_token
from medic.db import get_session
from medic.domain.couriers import StopKind
from medic.domain.money import to_rupee_string
from medic.models import Chemist, Courier, CourierOffer, OfferState, Order, Stop
from medic.schemas import CourierOfferOut, CourierOut, PositionIn, RejectIn, StopOut
from medic.services import couriers as courier_service
from medic.services.couriers import CourierError

router = APIRouter(prefix="/api/courier", tags=["courier"])


def _me(session: Session, token: str | None) -> Courier:
    return courier_from_token(session, token)


def _mine(session: Session, courier_id: int, token: str | None) -> Courier:
    courier = _me(session, token)
    if courier.id != courier_id:
        raise HTTPException(status_code=403, detail="that is not your courier account")
    return courier


def _courier_out(session: Session, courier: Courier) -> CourierOut:
    plan = courier_service.plan_for(session, courier.id)
    return CourierOut(
        id=courier.id,
        name=courier.name,
        state=str(courier.state),
        vehicle=courier.vehicle,
        can_cold_chain=courier.can_cold_chain,
        orders_in_custody=list(plan.orders_in_custody),
    )


@router.post("/{courier_id}/online", response_model=CourierOut)
def online(
    courier_id: int,
    session: Session = Depends(get_session),
    x_medic_token: str | None = Header(default=None),
) -> CourierOut:
    _mine(session, courier_id, x_medic_token)
    return _courier_out(session, courier_service.go_online(session, courier_id=courier_id))


@router.post("/{courier_id}/offline", response_model=CourierOut)
def offline(
    courier_id: int,
    session: Session = Depends(get_session),
    x_medic_token: str | None = Header(default=None),
) -> CourierOut:
    _mine(session, courier_id, x_medic_token)
    # CustodyHeld propagates to the app-level handler, which maps it to a 409
    # in the same {error, detail} shape as every other refusal. Catching it
    # here would give this one endpoint a different error body.
    return _courier_out(session, courier_service.go_offline(session, courier_id=courier_id))


@router.post("/{courier_id}/position", response_model=CourierOut)
def position(
    courier_id: int,
    body: PositionIn,
    session: Session = Depends(get_session),
    x_medic_token: str | None = Header(default=None),
) -> CourierOut:
    _mine(session, courier_id, x_medic_token)
    courier = courier_service.report_position(
        session, courier_id=courier_id, lat=body.lat, lng=body.lng
    )
    return _courier_out(session, courier)


@router.get("/{courier_id}/offers", response_model=list[CourierOfferOut])
def offers(
    courier_id: int,
    session: Session = Depends(get_session),
    x_medic_token: str | None = Header(default=None),
) -> list[CourierOfferOut]:
    _mine(session, courier_id, x_medic_token)
    now = datetime.now(UTC)
    rows = session.scalars(
        select(CourierOffer).where(
            CourierOffer.courier_id == courier_id, CourierOffer.state == OfferState.OFFERED
        )
    ).all()
    out: list[CourierOfferOut] = []
    for offer in rows:
        order = session.get(Order, offer.order_id)
        if order is None:
            continue
        chemist = session.get(Chemist, order.chemist_id) if order.chemist_id else None
        scheduled = any(s.carries_scheduled_goods for s in _stops_for(session, order.id))
        out.append(
            CourierOfferOut(
                offer_id=offer.id,
                order_id=order.id,
                chemist_name=chemist.name if chemist else "?",
                chemist_lat=chemist.lat if chemist else 0.0,
                chemist_lng=chemist.lng if chemist else 0.0,
                patient_zone=order.delivery_zone,
                pickup_km=round(offer.pickup_km, 2),
                expires_in_seconds=max(0, int((offer.expires_at - now).total_seconds())),
                carries_scheduled_goods=scheduled or _has_scheduled_lines(order),
                urgency=str(order.urgency),
                fee=to_rupee_string(offer.fee_paise),
            )
        )
    return out


def _stops_for(session: Session, order_id: int) -> list[Stop]:
    return list(session.scalars(select(Stop).where(Stop.order_id == order_id)))


def _has_scheduled_lines(order: Order) -> bool:
    from medic.domain.schedules import policy_for

    return any(policy_for(item.schedule).requires_register_entry for item in order.items)


@router.post("/offers/{offer_id}/accept", response_model=list[StopOut])
def accept(
    offer_id: int,
    session: Session = Depends(get_session),
    x_medic_token: str | None = Header(default=None),
) -> list[StopOut]:
    courier = _me(session, x_medic_token)
    courier_service.accept_delivery(session, offer_id=offer_id, courier_id=courier.id)
    return plan(courier.id, session=session, x_medic_token=x_medic_token)


@router.post("/offers/{offer_id}/decline")
def decline(
    offer_id: int,
    body: RejectIn,
    session: Session = Depends(get_session),
    x_medic_token: str | None = Header(default=None),
) -> dict[str, str]:
    courier = _me(session, x_medic_token)
    courier_service.decline_delivery(
        session, offer_id=offer_id, courier_id=courier.id, reason=body.reason
    )
    return {"status": "declined"}


def _label(session: Session, stop: Stop) -> str:
    if stop.kind is StopKind.COLLECT:
        order = session.get(Order, stop.order_id)
        chemist = (
            session.get(Chemist, order.chemist_id) if order and order.chemist_id else None
        )
        return f"Collect order #{stop.order_id} from {chemist.name if chemist else 'chemist'}"
    order = session.get(Order, stop.order_id)
    return f"Hand over order #{stop.order_id} in {order.delivery_zone if order else ''}"


@router.get("/{courier_id}/plan", response_model=list[StopOut])
def plan(
    courier_id: int,
    session: Session = Depends(get_session),
    x_medic_token: str | None = Header(default=None),
) -> list[StopOut]:
    _mine(session, courier_id, x_medic_token)
    rows = session.scalars(
        select(Stop).where(Stop.courier_id == courier_id).order_by(Stop.sequence)
    ).all()
    return [
        StopOut(
            stop_id=s.id,
            order_id=s.order_id,
            kind=str(s.kind),
            state=str(s.state),
            sequence=s.sequence,
            lat=s.lat,
            lng=s.lng,
            label=_label(session, s),
            carries_scheduled_goods=s.carries_scheduled_goods,
        )
        for s in rows
    ]


@router.post("/stops/{stop_id}/arrive", response_model=StopOut)
def arrive(
    stop_id: int,
    session: Session = Depends(get_session),
    x_medic_token: str | None = Header(default=None),
) -> StopOut:
    courier = _me(session, x_medic_token)
    stop = courier_service.arrive_at(session, stop_id=stop_id, courier_id=courier.id)
    return StopOut(
        stop_id=stop.id,
        order_id=stop.order_id,
        kind=str(stop.kind),
        state=str(stop.state),
        sequence=stop.sequence,
        lat=stop.lat,
        lng=stop.lng,
        label=_label(session, stop),
        carries_scheduled_goods=stop.carries_scheduled_goods,
    )


@router.post("/stops/{stop_id}/complete", response_model=StopOut)
def complete(
    stop_id: int,
    session: Session = Depends(get_session),
    x_medic_token: str | None = Header(default=None),
) -> StopOut:
    """Completing a COLLECT stop IS the dispensing event.

    It goes through `services.couriers.complete_stop`, which routes to
    `services.dispensing`, so the register write and the physical movement of
    the goods stay in one transaction.
    """
    courier = _me(session, x_medic_token)
    stop = courier_service.complete_stop(session, stop_id=stop_id, courier_id=courier.id)
    return StopOut(
        stop_id=stop.id,
        order_id=stop.order_id,
        kind=str(stop.kind),
        state=str(stop.state),
        sequence=stop.sequence,
        lat=stop.lat,
        lng=stop.lng,
        label=_label(session, stop),
        carries_scheduled_goods=stop.carries_scheduled_goods,
    )


@router.get("/{courier_id}/me", response_model=CourierOut)
def me(
    courier_id: int,
    session: Session = Depends(get_session),
    x_medic_token: str | None = Header(default=None),
) -> CourierOut:
    return _courier_out(session, _mine(session, courier_id, x_medic_token))


__all__ = ["router", "CourierError"]
