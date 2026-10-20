"""Pricing, price caps and order economics.

Two separate concerns live here:

1. Legal ceilings. We may price at or below MRP, and at or below the DPCO/NPPA
   ceiling where a formulation is price-capped. Breaching either is a
   regulatory matter, so these are hard errors rather than warnings.
2. Who funds a discount. The brief is explicit that discounts are funded mainly
   by chemists out of their own margin, because platform-funded discounts make
   most orders loss-making at this contribution level.
"""

from __future__ import annotations

from dataclasses import dataclass

from medic.config import Settings, settings
from medic.domain.money import apply_bps


class PriceCapBreach(Exception):
    pass


class CommissionOutOfBand(Exception):
    pass


@dataclass(frozen=True)
class PricedLine:
    medicine_id: int
    medicine_name: str
    qty: int
    mrp_paise: int
    # The highest price this line may lawfully be sold at: MRP, or the DPCO/NPPA
    # ceiling where that is lower.
    cap_paise: int
    unit_price_paise: int

    @property
    def line_total_paise(self) -> int:
        return self.unit_price_paise * self.qty

    @property
    def mrp_total_paise(self) -> int:
        return self.mrp_paise * self.qty

    @property
    def statutory_reduction_paise(self) -> int:
        """The MRP-to-cap gap. Nobody funds this; it is the price control."""
        return (self.mrp_paise - self.cap_paise) * self.qty

    @property
    def discount_paise(self) -> int:
        """Discount below the lawful cap, funded by whoever granted it."""
        return (self.cap_paise - self.unit_price_paise) * self.qty


def applicable_cap(mrp_paise: int, dpco_ceiling_paise: int | None) -> int:
    """The binding price ceiling for a line."""
    if dpco_ceiling_paise is None:
        return mrp_paise
    return min(mrp_paise, dpco_ceiling_paise)


def price_line(
    *,
    medicine_id: int,
    medicine_name: str,
    qty: int,
    mrp_paise: int,
    dpco_ceiling_paise: int | None,
    discount_bps: int = 0,
    unit_price_paise: int | None = None,
) -> PricedLine:
    """Price one line at or below its lawful ceiling.

    A price-capped formulation is sold at its ceiling by default rather than at
    MRP: the cap binds whether or not anyone remembers to apply it. Pass
    `unit_price_paise` to set a price explicitly, and it is checked against the
    cap -- that is the only way to trigger PriceCapBreach, which makes the
    exception a real signal rather than routine control flow.
    """
    if qty <= 0:
        raise ValueError("quantity must be positive")

    cap = applicable_cap(mrp_paise, dpco_ceiling_paise)
    if unit_price_paise is None:
        unit_price = cap - apply_bps(cap, discount_bps)
    else:
        unit_price = unit_price_paise

    if unit_price > cap:
        raise PriceCapBreach(
            f"{medicine_name}: unit price {unit_price} paise exceeds the applicable "
            f"ceiling of {cap} paise"
        )
    if unit_price < 0:
        raise ValueError("unit price cannot be negative")

    return PricedLine(
        medicine_id=medicine_id,
        medicine_name=medicine_name,
        qty=qty,
        mrp_paise=mrp_paise,
        cap_paise=cap,
        unit_price_paise=unit_price,
    )


@dataclass(frozen=True)
class OrderTotals:
    mrp_total_paise: int
    statutory_reduction_paise: int
    subtotal_paise: int
    chemist_funded_discount_paise: int
    platform_funded_discount_paise: int
    delivery_fee_paise: int
    total_paise: int
    commission_paise: int

    @property
    def customer_saving_paise(self) -> int:
        """What the customer pays below MRP, however it arose."""
        return self.mrp_total_paise - self.subtotal_paise


def total_order(
    lines: list[PricedLine],
    *,
    commission_bps: int,
    platform_funded_discount_paise: int = 0,
    config: Settings = settings,
) -> OrderTotals:
    if not lines:
        raise ValueError("an order needs at least one line")
    if not config.commission_bps_min <= commission_bps <= config.commission_bps_max:
        raise CommissionOutOfBand(
            f"commission of {commission_bps} bps sits outside the agreed "
            f"{config.commission_bps_min}-{config.commission_bps_max} bps band"
        )

    subtotal = sum(line.line_total_paise for line in lines)
    chemist_discount = sum(line.discount_paise for line in lines)
    mrp_total = sum(line.mrp_total_paise for line in lines)
    statutory = sum(line.statutory_reduction_paise for line in lines)
    fee = 0 if subtotal >= config.free_delivery_threshold_paise else config.delivery_fee_paise
    total = subtotal + fee - platform_funded_discount_paise
    if total < 0:
        raise ValueError("platform discount cannot exceed the order value")

    return OrderTotals(
        mrp_total_paise=mrp_total,
        statutory_reduction_paise=statutory,
        subtotal_paise=subtotal,
        chemist_funded_discount_paise=chemist_discount,
        platform_funded_discount_paise=platform_funded_discount_paise,
        delivery_fee_paise=fee,
        total_paise=total,
        # Commission is charged on the value the chemist actually bills, so a
        # chemist-funded discount reduces our commission with it.
        commission_paise=apply_bps(subtotal, commission_bps),
    )


@dataclass(frozen=True)
class Contribution:
    """Per-order contribution, before marketing and fixed costs."""

    revenue_commission_paise: int
    revenue_delivery_fee_paise: int
    cost_rider_paise: int
    cost_payment_gateway_paise: int
    cost_pharmacist_paise: int
    cost_platform_discount_paise: int

    @property
    def revenue_paise(self) -> int:
        return self.revenue_commission_paise + self.revenue_delivery_fee_paise

    @property
    def cost_paise(self) -> int:
        return (
            self.cost_rider_paise
            + self.cost_payment_gateway_paise
            + self.cost_pharmacist_paise
            + self.cost_platform_discount_paise
        )

    @property
    def contribution_paise(self) -> int:
        return self.revenue_paise - self.cost_paise


def contribution_for(totals: OrderTotals, *, config: Settings = settings) -> Contribution:
    return Contribution(
        revenue_commission_paise=totals.commission_paise,
        revenue_delivery_fee_paise=totals.delivery_fee_paise,
        cost_rider_paise=config.rider_cost_paise,
        cost_payment_gateway_paise=apply_bps(totals.total_paise, config.payment_gateway_bps),
        cost_pharmacist_paise=config.pharmacist_cost_per_order_paise,
        cost_platform_discount_paise=totals.platform_funded_discount_paise,
    )


def break_even_orders_per_month(
    mean_contribution_paise: float, *, config: Settings = settings
) -> int | None:
    """Orders a month needed to cover fixed costs. None when contribution is negative."""
    if mean_contribution_paise <= 0:
        return None
    return -(-config.monthly_fixed_cost_paise // int(mean_contribution_paise))
