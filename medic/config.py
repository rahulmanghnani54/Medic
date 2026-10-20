"""Platform configuration.

Commercial defaults come from the illustrative unit economics in the venture brief
(docs/venture-brief.md, section 6). They are assumptions, not market data: replace
them with real Jaipur quotes before using any output for a decision.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    # --- Service promise -------------------------------------------------
    delivery_promise_minutes: int = 60
    # Pharmacist verification is shown to the customer as its own step.
    rx_verification_target_minutes: int = 10
    # Below this, we do not promise the 60-minute window at checkout.
    sla_feasibility_margin_minutes: int = 10

    # --- Matching --------------------------------------------------------
    max_match_radius_km: float = 6.0
    chemist_prep_minutes: int = 12
    rider_speed_kmph: float = 18.0
    rider_pickup_buffer_minutes: int = 8
    # How long a chemist has to answer an offer before it expires and the
    # order is re-offered to the next candidate.
    chemist_accept_window_minutes: int = 5
    # Hard bound on the re-offer cycle for one order.
    max_sourcing_attempts: int = 4
    # How long a courier has to answer a delivery offer.
    courier_offer_window_seconds: int = 45
    max_courier_offer_attempts: int = 5
    # A courier will not be sent further than this to reach a chemist. Uber's
    # "wild goose chase": under excess demand a matcher that keeps reaching
    # further turns pickup time into deadweight and throughput collapses.
    max_courier_pickup_km: float = 4.0

    # --- Geospatial index (Uber's H3) ---------------------------------
    # res 8 cells are ~0.75 km2 with a ~530 m edge: the right grain for
    # dispatch in Jaipur. res 7 (~5.2 km2) is the zoning/aggregation grain.
    h3_dispatch_resolution: int = 8
    h3_zone_resolution: int = 7
    # Rings of neighbours to sweep outward when looking for supply.
    h3_max_ring: int = 8

    # --- Readiness prediction -----------------------------------------
    # Fallback prep estimate before a chemist has any history.
    default_chemist_prep_minutes: int = 12
    # Per pharmacist, per order in the queue ahead of this one.
    verification_minutes_per_queued_order: int = 3
    # How long a courier is asked to arrive before predicted ready_at. Tuned
    # so the courier waits a little rather than the package waiting a lot.
    courier_early_arrival_minutes: int = 3

    # --- Commercials (all money in paise) --------------------------------
    commission_bps_min: int = 800   # 8%  -- brief: take rate band to test
    commission_bps_max: int = 1200  # 12%
    commission_bps_default: int = 1000
    delivery_fee_paise: int = 2500
    free_delivery_threshold_paise: int = 50000
    payment_gateway_bps: int = 200
    rider_cost_paise: int = 4000
    pharmacist_cost_per_order_paise: int = 500
    monthly_fixed_cost_paise: int = 7_00_00_000  # Rs 7 lakh, midpoint of the brief's range

    # --- Compliance ------------------------------------------------------
    # A single prescription may back at most this many dispensing events.
    default_prescription_max_uses: int = 3
    # Prescriptions older than this are not accepted for scheduled drugs.
    prescription_validity_days: int = 90

    # --- Dev auth (NOT production) ---------------------------------------
    role_tokens: dict[str, str] = field(
        default_factory=lambda: {
            "customer": os.environ.get("MEDIC_TOKEN_CUSTOMER", "dev-customer-token"),
            "chemist": os.environ.get("MEDIC_TOKEN_CHEMIST", "dev-chemist-token"),
            "pharmacist": os.environ.get("MEDIC_TOKEN_PHARMACIST", "dev-pharmacist-token"),
            "admin": os.environ.get("MEDIC_TOKEN_ADMIN", "dev-admin-token"),
        }
    )

    database_url: str = os.environ.get("MEDIC_DATABASE_URL", "sqlite:///./medic.sqlite3")
    upload_dir: str = os.environ.get("MEDIC_UPLOAD_DIR", "./uploads")

    def using_default_tokens(self) -> bool:
        return any(token.startswith("dev-") for token in self.role_tokens.values())


settings = Settings()
