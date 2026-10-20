"""Persistence model.

Money is stored in paise (integers). Timestamps are timezone-aware UTC; the
customer-facing apps render them in IST.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from medic.domain.couriers import CourierState, StopKind, StopState
from medic.domain.dispatch import OfferState
from medic.domain.orders import GateState, OrderStatus
from medic.domain.schedules import DrugSchedule
from medic.domain.triage import ClinicalUrgency

# These enums are defined in the domain layer and imported here, so that
# `from medic.models import OrderStatus` keeps working for every existing
# caller. No __all__: a partial one would silently narrow `import *`.


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator):
    """A timestamp that stays timezone-aware across a round trip.

    SQLite has no native timestamptz, so `UtcDateTime` stores the
    offset nowhere and hands back a NAIVE datetime on read. Comparing that with
    an aware `now` raises, and -- worse -- arithmetic against it would silently
    mis-compute the 60-minute SLA clock. So every timestamp in this system goes
    through here: converted to UTC on the way in, re-tagged as UTC on the way
    out.

    A naive value arriving from a client is assumed to be UTC. That assumption
    is wrong by 5.5 hours for an IST wall-clock time, which matters at the edge
    of the prescription validity window, so API clients should always send an
    explicit offset.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def enum_column(enum_type: type[StrEnum], length: int) -> SAEnum:
    """A string column that comes back as the enum member, not a bare string.

    `mapped_column(String(...))` with a `Mapped[SomeEnum]` annotation does NOT
    coerce on load: the attribute is a plain `str` after a database round trip,
    so every `value is SomeEnum.MEMBER` check silently evaluates False. That
    reads as "this order is waiting on nothing" rather than as an error, which
    is the worst possible failure mode here. Storing the member's value and
    letting SQLAlchemy coerce it back keeps identity comparisons honest.
    """
    return SAEnum(
        enum_type,
        native_enum=False,
        length=length,
        values_callable=lambda enum: [member.value for member in enum],
        validate_strings=True,
    )


class Base(DeclarativeBase):
    pass


class PrescriptionStatus(StrEnum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    zone: Mapped[str] = mapped_column(String(80))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Medicine(Base):
    __tablename__ = "medicines"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    composition: Mapped[str] = mapped_column(String(300))
    manufacturer: Mapped[str] = mapped_column(String(160))
    pack: Mapped[str] = mapped_column(String(80))
    form: Mapped[str] = mapped_column(String(40))
    schedule: Mapped[DrugSchedule] = mapped_column(enum_column(DrugSchedule, 8), index=True)
    mrp_paise: Mapped[int] = mapped_column(Integer)
    # Set where the formulation is price-capped under DPCO/NPPA. Null means we
    # have no ceiling on record, which is not the same as "no ceiling exists".
    dpco_ceiling_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requires_cold_chain: Mapped[bool] = mapped_column(Boolean, default=False)
    is_chronic_care: Mapped[bool] = mapped_column(Boolean, default=False)
    # Clinical urgency is a pharmacist's judgement recorded per medicine. Drug
    # schedule cannot stand in for it: a rescue inhaler and a sleeping tablet
    # are both Schedule H.
    urgency: Mapped[ClinicalUrgency] = mapped_column(
        enum_column(ClinicalUrgency, 16), default=ClinicalUrgency.ROUTINE
    )


class Chemist(Base):
    __tablename__ = "chemists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    drug_licence_no: Mapped[str] = mapped_column(String(80), unique=True)
    licence_expires_on: Mapped[datetime] = mapped_column(UtcDateTime)
    pharmacist_name: Mapped[str] = mapped_column(String(120))
    pharmacist_reg_no: Mapped[str] = mapped_column(String(80))
    zone: Mapped[str] = mapped_column(String(80), index=True)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    phone: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_24x7: Mapped[bool] = mapped_column(Boolean, default=False)
    opens_hour_ist: Mapped[int] = mapped_column(Integer, default=8)
    closes_hour_ist: Mapped[int] = mapped_column(Integer, default=22)
    commission_bps: Mapped[int] = mapped_column(Integer)
    can_cold_chain: Mapped[bool] = mapped_column(Boolean, default=False)
    # Rolling SLA record, used to rank otherwise-equal candidates.
    orders_offered: Mapped[int] = mapped_column(Integer, default=0)
    orders_accepted: Mapped[int] = mapped_column(Integer, default=0)
    orders_on_time: Mapped[int] = mapped_column(Integer, default=0)
    orders_completed: Mapped[int] = mapped_column(Integer, default=0)

    stock: Mapped[list[ChemistStock]] = relationship(back_populates="chemist")

    @property
    def acceptance_rate(self) -> float:
        return self.orders_accepted / self.orders_offered if self.orders_offered else 1.0

    @property
    def on_time_rate(self) -> float:
        return self.orders_on_time / self.orders_completed if self.orders_completed else 1.0


class ChemistStock(Base):
    __tablename__ = "chemist_stock"
    __table_args__ = (UniqueConstraint("chemist_id", "medicine_id", name="uq_chemist_medicine"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chemist_id: Mapped[int] = mapped_column(ForeignKey("chemists.id"), index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id"), index=True)
    #: What the chemist says is physically on the shelf. Offers never change
    #: this; only a real count or a dispense does.
    qty_on_hand: Mapped[int] = mapped_column(Integer, default=0)
    #: Units held by open leases. Availability is qty_on_hand - qty_reserved.
    #: Keeping the two apart is what makes releasing a lease idempotent: a
    #: double release of a decrement would INFLATE declared stock and invent
    #: phantom units of a scheduled drug.
    qty_reserved: Mapped[int] = mapped_column(Integer, default=0)
    # "declared" is a chemist's own statement of stock; "synced" comes from their
    # billing software. Stock accuracy is the core risk to the 60-minute promise,
    # so we keep the provenance of every number.
    source: Mapped[str] = mapped_column(String(16), default="declared")
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow
    )

    chemist: Mapped[Chemist] = relationship(back_populates="stock")

    @property
    def qty_available(self) -> int:
        return max(0, self.qty_on_hand - self.qty_reserved)


class Prescription(Base):
    __tablename__ = "prescriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    image_sha256: Mapped[str] = mapped_column(String(64), index=True)
    stored_path: Mapped[str] = mapped_column(String(400))
    prescriber_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    prescriber_reg_no: Mapped[str | None] = mapped_column(String(80), nullable=True)
    issued_on: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    status: Mapped[PrescriptionStatus] = mapped_column(
        enum_column(PrescriptionStatus, 16), default=PrescriptionStatus.PENDING, index=True
    )
    max_uses: Mapped[int] = mapped_column(Integer, default=3)
    uses_count: Mapped[int] = mapped_column(Integer, default=0)
    verified_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when the same image hash has been seen before. Not an automatic
    # rejection: the pharmacist decides, but they are told.
    duplicate_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("prescriptions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    items: Mapped[list[PrescriptionItem]] = relationship(back_populates="prescription")


class PrescriptionItem(Base):
    """What a verified prescription authorises, per medicine."""

    __tablename__ = "prescription_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    prescription_id: Mapped[int] = mapped_column(ForeignKey("prescriptions.id"), index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id"))
    authorised_qty: Mapped[int] = mapped_column(Integer)
    dispensed_qty: Mapped[int] = mapped_column(Integer, default=0)

    prescription: Mapped[Prescription] = relationship(back_populates="items")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    chemist_id: Mapped[int | None] = mapped_column(ForeignKey("chemists.id"), nullable=True)
    prescription_id: Mapped[int | None] = mapped_column(
        ForeignKey("prescriptions.id"), nullable=True
    )
    status: Mapped[OrderStatus] = mapped_column(
        enum_column(OrderStatus, 32), default=OrderStatus.CREATED, index=True
    )
    chemist_gate: Mapped[GateState] = mapped_column(
        enum_column(GateState, 16), default=GateState.PENDING
    )
    rx_gate: Mapped[GateState] = mapped_column(
        enum_column(GateState, 16), default=GateState.NOT_REQUIRED
    )

    delivery_zone: Mapped[str] = mapped_column(String(80))
    delivery_lat: Mapped[float] = mapped_column(Float)
    delivery_lng: Mapped[float] = mapped_column(Float)

    subtotal_paise: Mapped[int] = mapped_column(Integer, default=0)
    chemist_discount_paise: Mapped[int] = mapped_column(Integer, default=0)
    platform_discount_paise: Mapped[int] = mapped_column(Integer, default=0)
    delivery_fee_paise: Mapped[int] = mapped_column(Integer, default=0)
    total_paise: Mapped[int] = mapped_column(Integer, default=0)
    commission_paise: Mapped[int] = mapped_column(Integer, default=0)

    #: Snapshot of the basket's clinical urgency, used to sequence dispatch.
    urgency: Mapped[ClinicalUrgency] = mapped_column(
        enum_column(ClinicalUrgency, 16), default=ClinicalUrgency.ROUTINE
    )
    #: How many chemists have been offered this order. Bounded by config and
    #: again by assert_sourcing_attempt, because the SEEKING <-> AWAITING
    #: cycle is deliberately cyclic and the transition table cannot bound it.
    attempt_seq: Mapped[int] = mapped_column(Integer, default=0)
    #: Predicted moment the chemist counter will be ready, used to time the
    #: courier leg. Null until a chemist accepts.
    ready_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: The firm ETA, committed only when a chemist accepts. Before that the
    #: customer is shown a band, never a promise we have no chemist to keep.
    committed_eta_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    promised_minutes: Mapped[int] = mapped_column(Integer)
    promise_honoured: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    eta_minutes_at_checkout: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: When the goods physically left the pharmacy. Set for EVERY order, not
    #: just scheduled ones: an OTC basket in a courier's bag is just as much
    #: out of the chemist's hands, and custody cannot be read from the
    #: Schedule H1 register because OTC lines never write one.
    dispensed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list[OrderItem]] = relationship(back_populates="order")

    @property
    def deadline(self) -> datetime:
        from datetime import timedelta

        return self.created_at + timedelta(minutes=self.promised_minutes)


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id"))
    qty: Mapped[int] = mapped_column(Integer)
    # Snapshot of name and schedule at order time: the catalogue can be
    # reclassified later, but the register must reflect what was dispensed.
    medicine_name: Mapped[str] = mapped_column(String(200))
    schedule: Mapped[DrugSchedule] = mapped_column(enum_column(DrugSchedule, 8))
    unit_price_paise: Mapped[int] = mapped_column(Integer)
    mrp_paise: Mapped[int] = mapped_column(Integer)
    line_total_paise: Mapped[int] = mapped_column(Integer)

    order: Mapped[Order] = relationship(back_populates="items")


class RegisterEntry(Base):
    """Schedule H1 dispensing register.

    One row per scheduled line item dispensed. Exported per chemist, because the
    legal obligation to maintain the register sits with the dispensing pharmacy.
    """

    __tablename__ = "register_entries"
    #: The dispensing event must be replay-safe. At-least-once workers make a
    #: duplicate call a question of when, not if, and a duplicated row here
    #: falsifies a statutory register and double-debits the patient's
    #: authorised quantity. The database refuses the second write.
    __table_args__ = (
        UniqueConstraint("order_id", "medicine_id", name="uq_register_order_medicine"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id"))
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    chemist_id: Mapped[int] = mapped_column(ForeignKey("chemists.id"), index=True)
    medicine_name: Mapped[str] = mapped_column(String(200))
    schedule: Mapped[DrugSchedule] = mapped_column(enum_column(DrugSchedule, 8))
    qty: Mapped[int] = mapped_column(Integer)
    patient_name: Mapped[str] = mapped_column(String(120))
    patient_phone: Mapped[str] = mapped_column(String(20))
    prescriber_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    prescriber_reg_no: Mapped[str | None] = mapped_column(String(80), nullable=True)
    pharmacist_reg_no: Mapped[str] = mapped_column(String(80))
    #: Who physically took the goods out of the pharmacy. The register records
    #: the dispensing pharmacist; custody is a separate fact and the row was
    #: previously silent on it.
    custody_courier_id: Mapped[int | None] = mapped_column(
        ForeignKey("couriers.id"), nullable=True
    )
    dispensed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class AuditEvent(Base):
    """Append-only record of every state change. Never updated or deleted."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(80), index=True)
    subject_type: Mapped[str] = mapped_column(String(40))
    subject_id: Mapped[int] = mapped_column(Integer, index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class Offer(Base):
    """One dispatch attempt: this order, offered to this chemist, once.

    Replaces encoding "offered" as `Order.chemist_gate == PENDING` plus
    `Order.chemist_id`. Keeping it as its own row is what makes a decline a
    routing event rather than the death of the order, and what gives each
    attempt somewhere to record its own outcome.
    """

    __tablename__ = "offers"
    __table_args__ = (
        # One live offer per order. A second open offer would mean two
        # chemists leasing units for the same basket.
        UniqueConstraint("order_id", "attempt_seq", name="uq_offer_order_attempt"),
        Index("ix_offer_open", "state", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    chemist_id: Mapped[int] = mapped_column(ForeignKey("chemists.id"), index=True)
    attempt_seq: Mapped[int] = mapped_column(Integer)
    state: Mapped[OfferState] = mapped_column(
        enum_column(OfferState, 16), default=OfferState.OFFERED, index=True
    )
    offered_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    #: Written in the same transaction as the offer, so a dispatcher crash
    #: cannot lose the timer -- the sweep rediscovers it from the row.
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    answered_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: ETA quoted to this chemist, recomputed per attempt against the
    #: remaining budget rather than reused from checkout.
    eta_minutes: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class StockLease(Base):
    """Units held for one order at one chemist while an offer is live.

    A lease moves `ChemistStock.qty_reserved`, never `qty_on_hand`, and it has
    its own state so releasing is idempotent. Uber can broadcast a trip to
    many drivers because a driver is not consumed by being offered; stock is.
    """

    __tablename__ = "stock_leases"
    __table_args__ = (
        UniqueConstraint("order_id", "chemist_id", "medicine_id", name="uq_lease_scope"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    chemist_id: Mapped[int] = mapped_column(ForeignKey("chemists.id"), index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id"))
    qty: Mapped[int] = mapped_column(Integer)
    #: HELD -> CONSUMED (dispensed) | RELEASED (offer lost or order closed).
    #: Terminal either way, which is what makes a double release a no-op
    #: rather than an inflation of declared stock.
    state: Mapped[str] = mapped_column(String(16), default="HELD", index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class Courier(Base):
    """A delivery partner. The one genuinely Uber-shaped supply side here."""

    __tablename__ = "couriers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    #: Per-person secret, stored hashed. A shared role token cannot answer
    #: "which courier took custody of this Schedule H1 package", which is
    #: exactly what the register has to record.
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    state: Mapped[CourierState] = mapped_column(
        enum_column(CourierState, 16), default=CourierState.OFFLINE, index=True
    )
    vehicle: Mapped[str] = mapped_column(String(40), default="two-wheeler")
    can_cold_chain: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: H3 cell of the last reported position, at the dispatch resolution.
    last_cell: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    offers_made: Mapped[int] = mapped_column(Integer, default=0)
    offers_accepted: Mapped[int] = mapped_column(Integer, default=0)
    deliveries_completed: Mapped[int] = mapped_column(Integer, default=0)
    deliveries_on_time: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    @property
    def acceptance_rate(self) -> float:
        return self.offers_accepted / self.offers_made if self.offers_made else 1.0


class CourierPosition(Base):
    """Position history. Append-only; the courier row holds the latest."""

    __tablename__ = "courier_positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    courier_id: Mapped[int] = mapped_column(ForeignKey("couriers.id"), index=True)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    cell: Mapped[str] = mapped_column(String(20), index=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)


class Stop(Base):
    """One waypoint in a courier's plan.

    Supply holds an ordered plan of stops rather than a current_trip_id, so a
    courier can carry one order while collecting another.
    """

    __tablename__ = "stops"
    __table_args__ = (
        UniqueConstraint("order_id", "kind", name="uq_stop_order_kind"),
        Index("ix_stop_plan", "courier_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    courier_id: Mapped[int] = mapped_column(ForeignKey("couriers.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    kind: Mapped[StopKind] = mapped_column(enum_column(StopKind, 16))
    state: Mapped[StopState] = mapped_column(
        enum_column(StopState, 16), default=StopState.PLANNED, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    #: True when completing this stop moves scheduled goods into or out of the
    #: courier's custody.
    carries_scheduled_goods: Mapped[bool] = mapped_column(Boolean, default=False)
    planned_for: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    arrived_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class Credential(Base):
    """A dated document that gates a party from working when it expires.

    Uber hard-gates a driver from going online on document expiry with no
    human in the loop. The same shape covers a chemist's drug licence, a
    pharmacist's registration, a courier's licence, and -- unlike the previous
    static `can_cold_chain` boolean -- a cold-chain equipment validation.
    """

    __tablename__ = "credentials"
    __table_args__ = (
        UniqueConstraint("holder_type", "holder_id", "kind", name="uq_credential_holder_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    holder_type: Mapped[str] = mapped_column(String(16), index=True)  # 'chemist' | 'courier'
    holder_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    reference: Mapped[str] = mapped_column(String(120))
    issued_on: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    expires_on: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    def is_valid_at(self, at: datetime) -> bool:
        return self.revoked_at is None and self.expires_on > at


class CourierOffer(Base):
    """A delivery offered to one courier.

    Kept separate from `Offer` rather than generalised, because the two legs
    are genuinely different: a chemist offer leases depletable stock and is
    strictly sequential, while a courier offer leases nothing and could in
    principle be broadcast. Collapsing them into one table would hide that.
    """

    __tablename__ = "courier_offers"
    __table_args__ = (
        UniqueConstraint("order_id", "attempt_seq", name="uq_courier_offer_order_attempt"),
        Index("ix_courier_offer_open", "state", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    courier_id: Mapped[int] = mapped_column(ForeignKey("couriers.id"), index=True)
    attempt_seq: Mapped[int] = mapped_column(Integer)
    state: Mapped[OfferState] = mapped_column(
        enum_column(OfferState, 16), default=OfferState.OFFERED, index=True
    )
    offered_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    answered_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    pickup_km: Mapped[float] = mapped_column(Float)
    #: When the courier is asked to be at the chemist: scheduled backwards
    #: from predicted ready_at so they arrive as the counter becomes ready,
    #: rather than waiting at it.
    summon_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    fee_paise: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
