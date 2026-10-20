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

from medic.domain.schedules import DrugSchedule


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


class GateState(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    CLEARED = "CLEARED"
    FAILED = "FAILED"


class OrderStatus(StrEnum):
    # Live states
    CREATED = "CREATED"
    AWAITING_FULFILMENT = "AWAITING_FULFILMENT"
    READY_FOR_PICKUP = "READY_FOR_PICKUP"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    # Terminal failures, kept distinct because they have different root causes
    # and the pilot's exit criteria are stated per cause.
    BLOCKED_NOT_DISPENSABLE = "BLOCKED_NOT_DISPENSABLE"
    CANCELLED_NO_CHEMIST = "CANCELLED_NO_CHEMIST"
    CANCELLED_STOCK_OUT = "CANCELLED_STOCK_OUT"
    REJECTED_PRESCRIPTION = "REJECTED_PRESCRIPTION"
    CANCELLED_BY_CUSTOMER = "CANCELLED_BY_CUSTOMER"


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
    qty_on_hand: Mapped[int] = mapped_column(Integer, default=0)
    # "declared" is a chemist's own statement of stock; "synced" comes from their
    # billing software. Stock accuracy is the core risk to the 60-minute promise,
    # so we keep the provenance of every number.
    source: Mapped[str] = mapped_column(String(16), default="declared")
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow
    )

    chemist: Mapped[Chemist] = relationship(back_populates="stock")


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

    promised_minutes: Mapped[int] = mapped_column(Integer)
    promise_honoured: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    eta_minutes_at_checkout: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
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

    id: Mapped[int] = mapped_column(primary_key=True)
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
