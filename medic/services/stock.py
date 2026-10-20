"""Stock leases.

Offering an order to a chemist has to hold the units it is about. Uber can
broadcast one trip to several drivers because a driver is not consumed by
being offered; stock is. Without a hold, two chemists score against the same
declared units and both can accept.

The previous design decremented `qty_on_hand` on acceptance and incremented it
back on release. That is unsafe the moment a crash-prone worker can call
release twice, because the second increment INFLATES declared stock and
invents phantom units of a scheduled drug, which the matcher will then promise
to someone.

So this module keeps two things apart:

    qty_on_hand   what the chemist says is physically on the shelf
    qty_reserved  units currently held by open leases

and every lease is a row with a terminal state. Releasing a lease that is
already settled is a no-op rather than an arithmetic operation, which is what
makes the whole path safe to retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from medic.models import ChemistStock, Order, StockLease

LEASE_HELD = "HELD"
LEASE_RELEASED = "RELEASED"
LEASE_CONSUMED = "CONSUMED"


class InsufficientStock(Exception):
    """A chemist cannot cover this basket from unreserved units."""

    def __init__(self, medicine_name: str, wanted: int, available: int) -> None:
        self.medicine_name = medicine_name
        self.wanted = wanted
        self.available = available
        super().__init__(
            f"{medicine_name}: {wanted} needed but only {available} unreserved on hand"
        )


@dataclass(frozen=True)
class Shortfall:
    medicine_id: int
    medicine_name: str
    wanted: int
    available: int

    @property
    def short_by(self) -> int:
        return max(0, self.wanted - self.available)


def _row(session: Session, chemist_id: int, medicine_id: int) -> ChemistStock | None:
    return session.scalars(
        select(ChemistStock).where(
            ChemistStock.chemist_id == chemist_id,
            ChemistStock.medicine_id == medicine_id,
        )
    ).first()


def availability(session: Session, chemist_id: int, medicine_id: int) -> int:
    row = _row(session, chemist_id, medicine_id)
    return row.qty_available if row else 0


def shortfalls(session: Session, *, order: Order, chemist_id: int) -> list[Shortfall]:
    """What this chemist cannot cover, with quantities.

    Quantities matter: the previous match result carried medicine NAMES only,
    so the residual basket could not be reconstructed and a line short by one
    unit weighed the same as a line with no stock at all.
    """
    out: list[Shortfall] = []
    for item in order.items:
        have = availability(session, chemist_id, item.medicine_id)
        if have < item.qty:
            out.append(
                Shortfall(
                    medicine_id=item.medicine_id,
                    medicine_name=item.medicine_name,
                    wanted=item.qty,
                    available=have,
                )
            )
    return out


def hold(session: Session, *, order: Order, chemist_id: int) -> list[StockLease]:
    """Reserve every line of this order at this chemist, or reserve nothing.

    Called inside the same transaction as the offer accept, so two concurrent
    accepts cannot both pass the availability check.
    """
    existing = _leases(session, order_id=order.id, chemist_id=chemist_id, state=LEASE_HELD)
    if existing:
        # Already held by this order at this chemist. Idempotent by design:
        # the retry of an accept must not double-reserve.
        return existing

    short = shortfalls(session, order=order, chemist_id=chemist_id)
    if short:
        first = short[0]
        raise InsufficientStock(first.medicine_name, first.wanted, first.available)

    leases: list[StockLease] = []
    for item in order.items:
        row = _row(session, chemist_id, item.medicine_id)
        assert row is not None  # shortfalls() would have raised
        row.qty_reserved += item.qty
        lease = StockLease(
            order_id=order.id,
            chemist_id=chemist_id,
            medicine_id=item.medicine_id,
            qty=item.qty,
            state=LEASE_HELD,
        )
        session.add(lease)
        leases.append(lease)
    session.flush()
    return leases


def _leases(
    session: Session, *, order_id: int, chemist_id: int | None = None, state: str | None = None
) -> list[StockLease]:
    stmt = select(StockLease).where(StockLease.order_id == order_id)
    if chemist_id is not None:
        stmt = stmt.where(StockLease.chemist_id == chemist_id)
    if state is not None:
        stmt = stmt.where(StockLease.state == state)
    return list(session.scalars(stmt))


def release(session: Session, *, order_id: int, chemist_id: int | None = None) -> int:
    """Give back every held lease for this order. Safe to call any number of times.

    `chemist_id` is optional on purpose. Release is often called when an order
    is being handed back to sourcing, at which point `Order.chemist_id` is
    about to change or has already been cleared -- so resolving the stock row
    through the order would target the wrong chemist, or nothing at all, and
    the units would silently vanish from one shelf and be promised from
    another. The leases themselves record where the units are held.
    """
    released = 0
    for lease in _leases(session, order_id=order_id, chemist_id=chemist_id, state=LEASE_HELD):
        row = _row(session, lease.chemist_id, lease.medicine_id)
        if row is not None:
            row.qty_reserved = max(0, row.qty_reserved - lease.qty)
        lease.state = LEASE_RELEASED
        lease.settled_at = datetime.now(UTC)
        released += 1
    if released:
        session.flush()
    return released


def consume(session: Session, *, order_id: int, chemist_id: int) -> int:
    """Turn held leases into a dispense: the units leave the shelf for good.

    Only ever called from the dispensing event, in the same transaction as the
    register write.
    """
    consumed = 0
    for lease in _leases(session, order_id=order_id, chemist_id=chemist_id, state=LEASE_HELD):
        row = _row(session, lease.chemist_id, lease.medicine_id)
        if row is not None:
            row.qty_reserved = max(0, row.qty_reserved - lease.qty)
            row.qty_on_hand = max(0, row.qty_on_hand - lease.qty)
        lease.state = LEASE_CONSUMED
        lease.settled_at = datetime.now(UTC)
        consumed += 1
    if consumed:
        session.flush()
    return consumed


def held_chemist_id(session: Session, order_id: int) -> int | None:
    """Which chemist currently holds units for this order, if any."""
    leases = _leases(session, order_id=order_id, state=LEASE_HELD)
    return leases[0].chemist_id if leases else None
