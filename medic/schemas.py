"""Request and response bodies."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from medic.domain.money import to_rupee_string


class MedicineOut(BaseModel):
    id: int
    name: str
    composition: str
    manufacturer: str
    pack: str
    form: str
    schedule: str
    mrp: str
    requires_prescription: bool
    requires_pharmacist_verification: bool
    is_chronic_care: bool


class LineIn(BaseModel):
    medicine_id: int
    qty: int = Field(gt=0, le=20)


class QuoteIn(BaseModel):
    customer_id: int
    lines: list[LineIn] = Field(min_length=1, max_length=25)
    delivery_lat: float
    delivery_lng: float


class QuoteLineOut(BaseModel):
    medicine_id: int
    medicine_name: str
    qty: int
    unit_price: str
    mrp: str
    line_total: str


class QuoteOut(BaseModel):
    lines: list[QuoteLineOut]
    subtotal: str
    chemist_funded_discount: str
    delivery_fee: str
    total: str
    chemist_id: int
    chemist_name: str
    eta_minutes: int
    promise_minutes: int
    requires_prescription: bool
    requires_pharmacist_verification: bool


class PlaceOrderIn(QuoteIn):
    delivery_zone: str
    prescription_id: int | None = None


class OrderItemOut(BaseModel):
    medicine_name: str
    schedule: str
    qty: int
    line_total: str


class OrderOut(BaseModel):
    id: int
    status: str
    chemist_gate: str
    prescription_gate: str
    chemist_id: int | None
    chemist_name: str | None
    items: list[OrderItemOut]
    total: str
    promise_minutes: int
    minutes_remaining: int
    sla_breached: bool
    promise_honoured: bool | None
    created_at: datetime
    customer_message: str
    closed_reason: str | None


class PrescriptionOut(BaseModel):
    id: int
    customer_id: int
    status: str
    prescriber_name: str | None
    prescriber_reg_no: str | None
    issued_on: datetime | None
    uses_count: int
    max_uses: int
    duplicate_of_id: int | None
    created_at: datetime
    rejection_reason: str | None


class AuthorisationIn(BaseModel):
    medicine_id: int
    authorised_qty: int = Field(gt=0, le=100)


class VerifyIn(BaseModel):
    authorisations: list[AuthorisationIn] = Field(min_length=1)
    pharmacist: str
    prescriber_name: str | None = None
    prescriber_reg_no: str | None = None
    issued_on: datetime | None = None


class RejectIn(BaseModel):
    reason: str = Field(min_length=3)


class ChemistRejectIn(RejectIn):
    stock_out: bool = True


class StockIn(BaseModel):
    medicine_id: int
    qty_on_hand: int = Field(ge=0)
    source: str = "declared"


class DispatchIn(BaseModel):
    rider: str = Field(min_length=1)


class EconomicsOut(BaseModel):
    delivered_orders: int
    mean_order_value: str
    mean_commission: str
    mean_delivery_fee: str
    mean_contribution: str
    on_time_rate: float | None
    break_even_orders_per_month: int | None
    assumptions_note: str


class SlaRowOut(BaseModel):
    order_id: int
    status: str
    zone: str
    minutes_remaining: int
    at_risk: bool
    breached: bool
    chemist_name: str | None
    waiting_on: str


def rupees_out(paise: int) -> str:
    return to_rupee_string(paise)
