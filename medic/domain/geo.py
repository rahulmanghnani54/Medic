"""Distance and ETA estimation for the 60-minute promise."""

from __future__ import annotations

import math
from dataclasses import dataclass

from medic.config import Settings, settings

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = p2 - p1
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


# Straight-line distance understates road distance. Jaipur's older walled-city
# zones are worse than this; replace with routed distances once we have a maps
# provider, and re-measure the factor against real rider traces.
ROAD_DISTANCE_FACTOR = 1.4


@dataclass(frozen=True)
class EtaBreakdown:
    distance_km: float
    road_km: float
    prep_minutes: int
    travel_minutes: int
    verification_minutes: int

    @property
    def total_minutes(self) -> int:
        # Prep and pharmacist verification run in parallel, so the slower of the
        # two gates sets the pickup time rather than their sum.
        return max(self.prep_minutes, self.verification_minutes) + self.travel_minutes


def estimate_eta(
    distance_km: float,
    *,
    requires_verification: bool,
    config: Settings = settings,
) -> EtaBreakdown:
    road_km = distance_km * ROAD_DISTANCE_FACTOR
    travel = math.ceil(road_km / config.rider_speed_kmph * 60) + config.rider_pickup_buffer_minutes
    verification = config.rx_verification_target_minutes if requires_verification else 0
    return EtaBreakdown(
        distance_km=round(distance_km, 3),
        road_km=round(road_km, 3),
        prep_minutes=config.chemist_prep_minutes,
        travel_minutes=travel,
        verification_minutes=verification,
    )


def promise_is_feasible(eta: EtaBreakdown, config: Settings = settings) -> bool:
    """Only promise the window when the estimate leaves a safety margin."""
    return eta.total_minutes + config.sla_feasibility_margin_minutes <= (
        config.delivery_promise_minutes
    )
