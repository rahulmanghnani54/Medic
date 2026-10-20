"""Chemist matching.

"Customers don't browse chemists. They want a specific drug" -- so matching is
the product, not a directory. Given a basket and a delivery point, find the
nearest active chemist that can legally and actually fill all of it.

Stock accuracy is the core risk to the 60-minute promise, so a chemist's
declared stock only makes them a candidate; the order still has to be accepted
and stock-confirmed before we commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from medic.config import Settings, settings
from medic.domain.geo import EtaBreakdown, estimate_eta, haversine_km, promise_is_feasible
from medic.domain.schedules import DrugSchedule, policy_for


@dataclass(frozen=True)
class BasketLine:
    medicine_id: int
    medicine_name: str
    qty: int
    schedule: DrugSchedule
    requires_cold_chain: bool = False


@dataclass(frozen=True)
class ChemistCandidate:
    """A chemist as matching sees them, decoupled from the ORM for testability."""

    chemist_id: int
    name: str
    zone: str
    lat: float
    lng: float
    is_active: bool
    is_24x7: bool
    opens_hour_ist: int
    closes_hour_ist: int
    can_cold_chain: bool
    stock: dict[int, int]
    acceptance_rate: float = 1.0
    on_time_rate: float = 1.0
    licence_expires_on: datetime | None = None


@dataclass(frozen=True)
class MatchResult:
    candidate: ChemistCandidate
    eta: EtaBreakdown
    fill_ratio: float
    missing: tuple[str, ...]

    @property
    def is_full_fill(self) -> bool:
        return not self.missing


@dataclass(frozen=True)
class Matching:
    """Ranked full fills, plus the best partial fills when nothing fills fully."""

    full_fills: tuple[MatchResult, ...]
    partial_fills: tuple[MatchResult, ...]
    rejected: tuple[tuple[str, str], ...]  # (chemist name, reason)

    @property
    def best(self) -> MatchResult | None:
        return self.full_fills[0] if self.full_fills else None


IST_OFFSET_HOURS = 5.5


def _ist_hour(at: datetime) -> float:
    from datetime import UTC

    utc = at.astimezone(UTC)
    return (utc.hour + utc.minute / 60 + IST_OFFSET_HOURS) % 24


def is_open_at(candidate: ChemistCandidate, at: datetime) -> bool:
    if candidate.is_24x7:
        return True
    hour = _ist_hour(at)
    if candidate.opens_hour_ist <= candidate.closes_hour_ist:
        return candidate.opens_hour_ist <= hour < candidate.closes_hour_ist
    # Shop closes after midnight.
    return hour >= candidate.opens_hour_ist or hour < candidate.closes_hour_ist


@dataclass(frozen=True)
class Admission:
    """Whether one chemist may serve one basket right now, and why not."""

    ok: bool
    reason: str = ""
    eta: EtaBreakdown | None = None
    distance_km: float | None = None


def admits(
    candidate: ChemistCandidate,
    *,
    delivery_lat: float,
    delivery_lng: float,
    needs_cold_chain: bool,
    needs_verification: bool,
    at: datetime,
    budget_minutes: int | None = None,
    config: Settings = settings,
) -> Admission:
    """The hard-filter chain, in order, as one reusable predicate.

    Extracted so that the matcher and the offer-accept path cannot drift. An
    offer may sit open for minutes before a chemist answers, and in that time
    a licence can lapse, the shop can close, or the delivery budget can run
    down -- so accept has to re-run exactly these checks, in exactly this
    order, rather than a hand-copied subset of them.

    `budget_minutes` is what remains of the 60 minutes, not the whole promise.
    Passing None keeps the original behaviour of checking against the full
    window, which is only correct for the first attempt.
    """
    if not candidate.is_active:
        return Admission(False, "inactive")
    if candidate.licence_expires_on is not None and candidate.licence_expires_on <= at:
        return Admission(False, "drug licence expired")
    if not is_open_at(candidate, at):
        return Admission(False, "closed at this hour")
    if needs_cold_chain and not candidate.can_cold_chain:
        return Admission(False, "no cold chain")

    distance = haversine_km(delivery_lat, delivery_lng, candidate.lat, candidate.lng)
    if distance > config.max_match_radius_km:
        return Admission(False, f"{distance:.1f} km away, outside radius")

    eta = estimate_eta(distance, requires_verification=needs_verification, config=config)
    if budget_minutes is None:
        if not promise_is_feasible(eta, config):
            return Admission(False, f"ETA {eta.total_minutes} min leaves no margin")
    elif eta.total_minutes + config.sla_feasibility_margin_minutes > budget_minutes:
        return Admission(
            False,
            f"ETA {eta.total_minutes} min does not fit the {budget_minutes} min left",
        )
    return Admission(True, eta=eta, distance_km=distance)


def find_matches(
    basket: list[BasketLine],
    *,
    delivery_lat: float,
    delivery_lng: float,
    candidates: list[ChemistCandidate],
    at: datetime,
    exclude_chemist_ids: frozenset[int] | set[int] | None = None,
    budget_minutes: int | None = None,
    config: Settings = settings,
) -> Matching:
    if not basket:
        raise ValueError("cannot match an empty basket")

    # Fail closed on schedules we must not dispense at all, before we look at
    # any chemist. policy_for raises NotDispensableOnline for Schedule X and
    # for anything unclassified.
    for line in basket:
        policy = policy_for(line.schedule)
        if not policy.dispensable_online:
            from medic.domain.schedules import NotDispensableOnline

            raise NotDispensableOnline(line.medicine_name, policy.schedule)

    needs_verification = any(
        policy_for(line.schedule).requires_pharmacist_verification for line in basket
    )
    needs_cold_chain = any(line.requires_cold_chain for line in basket)

    full: list[MatchResult] = []
    partial: list[MatchResult] = []
    rejected: list[tuple[str, str]] = []
    excluded = exclude_chemist_ids or frozenset()

    for candidate in candidates:
        if candidate.chemist_id in excluded:
            rejected.append((candidate.name, "already tried for this order"))
            continue

        verdict = admits(
            candidate,
            delivery_lat=delivery_lat,
            delivery_lng=delivery_lng,
            needs_cold_chain=needs_cold_chain,
            needs_verification=needs_verification,
            at=at,
            budget_minutes=budget_minutes,
            config=config,
        )
        if not verdict.ok:
            rejected.append((candidate.name, verdict.reason))
            continue
        eta = verdict.eta
        assert eta is not None

        missing = tuple(
            line.medicine_name
            for line in basket
            if candidate.stock.get(line.medicine_id, 0) < line.qty
        )
        result = MatchResult(
            candidate=candidate,
            eta=eta,
            fill_ratio=(len(basket) - len(missing)) / len(basket),
            missing=missing,
        )
        (full if result.is_full_fill else partial).append(result)

    # Rank: soonest ETA first, then the chemist likelier to accept and deliver
    # on time. Distance is already inside the ETA.
    def rank(result: MatchResult) -> tuple:
        return (
            result.eta.total_minutes,
            -result.candidate.acceptance_rate,
            -result.candidate.on_time_rate,
        )

    full.sort(key=rank)
    partial.sort(key=lambda r: (-r.fill_ratio, *rank(r)))
    return Matching(
        full_fills=tuple(full), partial_fills=tuple(partial), rejected=tuple(rejected)
    )
