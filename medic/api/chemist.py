"""Chemist dashboard endpoints: queue, accept/reject, stock, register export."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from medic.api.presenters import order_out
from medic.auth import require_role
from medic.db import get_session
from medic.models import Chemist, ChemistStock, Medicine, Offer, OfferState, RegisterEntry
from medic.schemas import ChemistRejectIn, OfferOut, OrderOut, StockIn
from medic.services import dispatching, ordering

router = APIRouter(prefix="/api/chemist", tags=["chemist"])
_auth = require_role("chemist")


def _chemist_or_404(session: Session, chemist_id: int) -> Chemist:
    chemist = session.get(Chemist, chemist_id)
    if chemist is None:
        raise HTTPException(status_code=404, detail="chemist not found")
    return chemist


@router.get("/{chemist_id}/orders", response_model=list[OrderOut])
def queue(
    chemist_id: int,
    session: Session = Depends(get_session),
    _: str = _auth,
) -> list[OrderOut]:
    _chemist_or_404(session, chemist_id)
    return [order_out(session, order) for order in ordering.chemist_queue(session, chemist_id)]


@router.get("/{chemist_id}/offers", response_model=list[OfferOut])
def offers(
    chemist_id: int,
    session: Session = Depends(get_session),
    _: str = _auth,
) -> list[OfferOut]:
    """Open offers waiting on this chemist, with their countdown."""
    _chemist_or_404(session, chemist_id)
    now = datetime.now(UTC)
    rows = session.scalars(
        select(Offer).where(
            Offer.chemist_id == chemist_id, Offer.state == OfferState.OFFERED
        )
    ).all()
    return [
        OfferOut(
            offer_id=o.id,
            order_id=o.order_id,
            chemist_id=o.chemist_id,
            attempt_seq=o.attempt_seq,
            state=str(o.state),
            eta_minutes=o.eta_minutes,
            expires_in_seconds=max(0, int((o.expires_at - now).total_seconds())),
        )
        for o in rows
    ]


@router.post("/offers/{offer_id}/accept", response_model=OrderOut)
def accept(
    offer_id: int,
    chemist_id: int,
    session: Session = Depends(get_session),
    _: str = _auth,
) -> OrderOut:
    """Accept an OFFER, not an order id.

    Keying on the offer is what makes acceptance authorised: previously any
    chemist who could guess an order id could accept it.
    """
    order = dispatching.accept_offer(session, offer_id=offer_id, chemist_id=chemist_id)
    return order_out(session, order)


@router.post("/offers/{offer_id}/decline", response_model=OrderOut)
def decline(
    offer_id: int,
    chemist_id: int,
    body: ChemistRejectIn,
    session: Session = Depends(get_session),
    _: str = _auth,
) -> OrderOut:
    """Pass on an offer. The order goes back to sourcing; it does not die."""
    order = dispatching.decline_offer(
        session, offer_id=offer_id, chemist_id=chemist_id, reason=body.reason
    )
    dispatching.advance_sourcing(session, order_id=order.id)
    return order_out(session, order)


@router.post("/{chemist_id}/orders/{order_id}/withdraw", response_model=OrderOut)
def withdraw(
    chemist_id: int,
    order_id: int,
    body: ChemistRejectIn,
    session: Session = Depends(get_session),
    _: str = _auth,
) -> OrderOut:
    """Accepted, then found the shelf empty. Hand it back for re-sourcing."""
    order = dispatching.withdraw_after_accept(
        session, order_id=order_id, chemist_id=chemist_id, reason=body.reason
    )
    dispatching.advance_sourcing(session, order_id=order_id)
    return order_out(session, order)


@router.put("/{chemist_id}/stock")
def upsert_stock(
    chemist_id: int,
    rows: list[StockIn],
    session: Session = Depends(get_session),
    _: str = _auth,
) -> dict[str, int]:
    _chemist_or_404(session, chemist_id)
    written = 0
    for row in rows:
        if session.get(Medicine, row.medicine_id) is None:
            raise HTTPException(status_code=400, detail=f"unknown medicine {row.medicine_id}")
        existing = session.scalars(
            select(ChemistStock).where(
                ChemistStock.chemist_id == chemist_id,
                ChemistStock.medicine_id == row.medicine_id,
            )
        ).first()
        if existing is None:
            session.add(
                ChemistStock(
                    chemist_id=chemist_id,
                    medicine_id=row.medicine_id,
                    qty_on_hand=row.qty_on_hand,
                    source=row.source,
                )
            )
        else:
            existing.qty_on_hand = row.qty_on_hand
            existing.source = row.source
        written += 1
    return {"rows_written": written}


@router.get("/{chemist_id}/stock")
def read_stock(
    chemist_id: int,
    session: Session = Depends(get_session),
    _: str = _auth,
) -> list[dict]:
    _chemist_or_404(session, chemist_id)
    rows = session.scalars(
        select(ChemistStock).where(ChemistStock.chemist_id == chemist_id)
    ).all()
    out = []
    for row in rows:
        medicine = session.get(Medicine, row.medicine_id)
        out.append(
            {
                "medicine_id": row.medicine_id,
                "medicine_name": medicine.name if medicine else "?",
                "schedule": str(medicine.schedule) if medicine else "?",
                "qty_on_hand": row.qty_on_hand,
                "source": row.source,
                "updated_at": row.updated_at.isoformat(),
            }
        )
    return sorted(out, key=lambda r: r["medicine_name"])


@router.get("/{chemist_id}/register.csv")
def register_export(
    chemist_id: int,
    session: Session = Depends(get_session),
    _: str = _auth,
) -> StreamingResponse:
    """Schedule H1 register for this pharmacy, as CSV.

    The obligation to maintain this register sits with the dispensing pharmacy,
    so the export is per chemist and includes every field the register needs.
    """
    _chemist_or_404(session, chemist_id)
    entries = session.scalars(
        select(RegisterEntry)
        .where(RegisterEntry.chemist_id == chemist_id)
        .order_by(RegisterEntry.dispensed_at)
    ).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "dispensed_at",
            "order_id",
            "medicine",
            "schedule",
            "quantity",
            "patient_name",
            "patient_phone",
            "prescriber_name",
            "prescriber_reg_no",
            "pharmacist_reg_no",
        ]
    )
    for entry in entries:
        writer.writerow(
            [
                entry.dispensed_at.isoformat(),
                entry.order_id,
                entry.medicine_name,
                str(entry.schedule),
                entry.qty,
                entry.patient_name,
                entry.patient_phone,
                entry.prescriber_name or "",
                entry.prescriber_reg_no or "",
                entry.pharmacist_reg_no,
            ]
        )
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="h1-register-chemist-{chemist_id}.csv"'
        },
    )
