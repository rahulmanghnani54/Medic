"""Chemist supply retrieval.

Turns database rows into the ORM-decoupled `ChemistCandidate` shapes the
matcher works on, and narrows the set before loading it.

The previous implementation loaded every chemist and every stock row on every
quote. That is fine for five partners and wrong by the time there are five
hundred, so retrieval now goes through Uber's H3 grid: take the delivery
point's cell, sweep outward ring by ring, and only consider chemists whose own
cell falls in the covered set.

The grid is a retrieval optimisation and never the authority. `matching.admits`
still decides serviceability with exact haversine distance and the ETA check,
and the cell prefilter is deliberately over-inclusive so it can never exclude
a chemist the exact check would have admitted.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from medic.config import Settings, settings
from medic.domain import geocells
from medic.domain.matching import ChemistCandidate
from medic.models import Chemist, ChemistStock


def _stock_by_chemist(session: Session, chemist_ids: list[int]) -> dict[int, dict[int, int]]:
    """Available (unreserved) units per chemist, per medicine.

    Availability rather than on-hand: units already leased to another live
    order are not free to promise again.
    """
    if not chemist_ids:
        return {}
    rows = session.scalars(
        select(ChemistStock).where(ChemistStock.chemist_id.in_(chemist_ids))
    ).all()
    out: dict[int, dict[int, int]] = {}
    for row in rows:
        out.setdefault(row.chemist_id, {})[row.medicine_id] = row.qty_available
    return out


def _to_candidate(chemist: Chemist, stock: dict[int, int]) -> ChemistCandidate:
    return ChemistCandidate(
        chemist_id=chemist.id,
        name=chemist.name,
        zone=chemist.zone,
        lat=chemist.lat,
        lng=chemist.lng,
        is_active=chemist.is_active,
        is_24x7=chemist.is_24x7,
        opens_hour_ist=chemist.opens_hour_ist,
        closes_hour_ist=chemist.closes_hour_ist,
        can_cold_chain=chemist.can_cold_chain,
        stock=stock,
        acceptance_rate=chemist.acceptance_rate,
        on_time_rate=chemist.on_time_rate,
        licence_expires_on=chemist.licence_expires_on,
    )


def candidates_near(
    session: Session,
    *,
    lat: float,
    lng: float,
    radius_km: float | None = None,
    config: Settings = settings,
) -> list[ChemistCandidate]:
    """Chemists whose H3 cell could fall inside the radius."""
    radius = config.max_match_radius_km if radius_km is None else radius_km
    covered = geocells.cells_within_km(lat, lng, radius, config=config)

    chemists = list(session.scalars(select(Chemist)))
    near = [
        c for c in chemists if geocells.cell_for(c.lat, c.lng, config=config) in covered
    ]
    stock = _stock_by_chemist(session, [c.id for c in near])
    return [_to_candidate(c, stock.get(c.id, {})) for c in near]


def all_candidates(session: Session) -> list[ChemistCandidate]:
    """Every partner chemist, unfiltered. Kept for tests and admin views."""
    chemists = list(session.scalars(select(Chemist)))
    stock = _stock_by_chemist(session, [c.id for c in chemists])
    return [_to_candidate(c, stock.get(c.id, {})) for c in chemists]
