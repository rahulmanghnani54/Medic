"""Customer app endpoints: search, prescription upload, checkout, tracking."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from medic.api.presenters import medicine_out, order_out, prescription_out
from medic.auth import require_role
from medic.db import get_session
from medic.schemas import (
    MedicineOut,
    OrderOut,
    PlaceOrderIn,
    PrescriptionOut,
    QuoteIn,
    QuoteLineOut,
    QuoteOut,
    RejectIn,
    rupees_out,
)
from medic.services import ordering

router = APIRouter(prefix="/api/customer", tags=["customer"])
_auth = require_role("customer")

MAX_PRESCRIPTION_BYTES = 8 * 1024 * 1024


@router.get("/medicines", response_model=list[MedicineOut])
def search(
    q: str = "",
    session: Session = Depends(get_session),
    _: str = _auth,
) -> list[MedicineOut]:
    return [medicine_out(m) for m in ordering.search_medicines(session, q)]


@router.post("/quote", response_model=QuoteOut)
def quote(
    body: QuoteIn,
    session: Session = Depends(get_session),
    _: str = _auth,
) -> QuoteOut:
    """Price the basket and say whether anyone can fill it -- naming nobody."""
    priced, totals = ordering.price_basket(
        session, lines=[ordering.LineRequest(line.medicine_id, line.qty) for line in body.lines]
    )
    verdict = ordering.assess_feasibility(
        session,
        lines=[ordering.LineRequest(line.medicine_id, line.qty) for line in body.lines],
        delivery_lat=body.delivery_lat,
        delivery_lng=body.delivery_lng,
    )
    return QuoteOut(
        lines=[
            QuoteLineOut(
                medicine_id=line.medicine_id,
                medicine_name=line.medicine_name,
                qty=line.qty,
                unit_price=rupees_out(line.unit_price_paise),
                mrp=rupees_out(line.mrp_paise),
                line_total=rupees_out(line.line_total_paise),
            )
            for line in priced
        ],
        subtotal=rupees_out(totals.subtotal_paise),
        chemist_funded_discount=rupees_out(totals.chemist_funded_discount_paise),
        delivery_fee=rupees_out(totals.delivery_fee_paise),
        total=rupees_out(totals.total_paise),
        feasible=verdict.feasible,
        candidate_count=verdict.candidate_count,
        eta_minutes_low=verdict.eta_minutes_low,
        eta_minutes_high=verdict.eta_minutes_high,
        promise_minutes=verdict.promise_minutes,
        requires_prescription=verdict.requires_prescription,
        requires_pharmacist_verification=verdict.requires_verification,
    )


@router.post("/prescriptions", response_model=PrescriptionOut, status_code=201)
async def upload_prescription(
    customer_id: int = Form(...),
    file: UploadFile = File(...),
    prescriber_name: str | None = Form(default=None),
    prescriber_reg_no: str | None = Form(default=None),
    issued_on: datetime | None = Form(default=None),
    session: Session = Depends(get_session),
    _: str = _auth,
) -> PrescriptionOut:
    image_bytes = await file.read()
    if len(image_bytes) > MAX_PRESCRIPTION_BYTES:
        raise HTTPException(status_code=413, detail="prescription image is too large (max 8 MB)")
    prescription = ordering.store_prescription(
        session,
        customer_id=customer_id,
        image_bytes=image_bytes,
        filename=file.filename or "prescription",
        prescriber_name=prescriber_name,
        prescriber_reg_no=prescriber_reg_no,
        issued_on=issued_on,
    )
    return prescription_out(prescription)


@router.post("/orders", response_model=OrderOut, status_code=201)
def place(
    body: PlaceOrderIn,
    session: Session = Depends(get_session),
    _: str = _auth,
) -> OrderOut:
    order = ordering.place_order(
        session,
        customer_id=body.customer_id,
        lines=[ordering.LineRequest(line.medicine_id, line.qty) for line in body.lines],
        delivery_zone=body.delivery_zone,
        delivery_lat=body.delivery_lat,
        delivery_lng=body.delivery_lng,
        prescription_id=body.prescription_id,
    )
    return order_out(session, order)


@router.get("/orders/{order_id}", response_model=OrderOut)
def track(
    order_id: int,
    customer_id: int,
    session: Session = Depends(get_session),
    _: str = _auth,
) -> OrderOut:
    from medic.models import Order

    order = session.get(Order, order_id)
    if order is None or order.customer_id != customer_id:
        raise HTTPException(status_code=404, detail="order not found")
    return order_out(session, order)


@router.post("/orders/{order_id}/cancel", response_model=OrderOut)
def cancel(
    order_id: int,
    customer_id: int,
    body: RejectIn,
    session: Session = Depends(get_session),
    _: str = _auth,
) -> OrderOut:
    order = ordering.cancel_by_customer(
        session, order_id=order_id, customer_id=customer_id, reason=body.reason
    )
    return order_out(session, order)
