"""Hex geo-indexing (Uber's H3), used as an index and nothing more.

Candidate retrieval today loads every chemist row and loops. That is tolerable
for a Jaipur pilot and wrong for a city: the work grows with the size of the
catalogue instead of the size of the neighbourhood. H3 gives every point a hex
cell id, so a chemist's cell can be stored and indexed, and retrieval becomes a
lookup over a handful of cell ids, sweeping outward only until there are enough
candidates to rank. The same point at the coarser zone resolution gives a stable
key to count supply per area, which is what courier density is measured in.

THE CONTRACT: the grid is a RETRIEVAL OPTIMISATION and is never the authority on
serviceability. Exact haversine distance (``medic.domain.geo.haversine_km``) and
the ETA feasibility check (``promise_is_feasible``) remain the deciding
authority. A cell-based prefilter must therefore be a SUPERSET of what the exact
check would admit: it may admit too much, never too little. The asymmetry is not
aesthetic -- a candidate the prefilter drops is never scored, never logged as
rejected, and nothing downstream can recover it, whereas a candidate it admits
too many of merely costs a haversine call. So every bound here rounds outward
(see ``safe_ring_for_km``), and no radius-driven ring count is clamped to a
configured budget.

Two H3 facts the arithmetic has to respect:

* Twelve cells at every resolution are pentagons, not hexagons. A ring of index
  k therefore holds *at most* 6k cells and sometimes fewer, and the grid is
  distorted in their neighbourhood. Nothing here assumes a cell count, and ring
  distances use the worst case measured anywhere on the grid rather than the
  nominal hexagon geometry.
* Cells do not nest perfectly across resolutions: a res 7 cell is not the union
  of its res 8 children, and a child can overhang its parent's boundary. A
  resolution is thus part of a cell's identity -- cells from two resolutions are
  never compared, and a zone cell is derived from the coordinates, never from a
  dispatch cell.
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import h3

from medic.config import Settings, settings

#: Centre-to-centre distance between adjacent cells as a multiple of the edge
#: length. Exact for a regular hexagon, which no H3 cell quite is.
_NOMINAL_SPACING_PER_EDGE = math.sqrt(3)

#: Worst real-to-nominal ratios measured across resolutions 7 and 8, sampled
#: globally and around all twelve pentagons: the shortest step between adjacent
#: centres came out at 0.70 of nominal spacing, and the widest cell reached 1.10
#: average edge lengths from its own centre. Ring arithmetic assumes steps this
#: short and cells this wide, with headroom below and above, because
#: overestimating a step or understating a cell is what shrinks a prefilter
#: under the exact check's admission set.
_MIN_STEP_FRACTION = 0.65
_MAX_CIRCUMRADIUS_FACTOR = 1.15

#: One ring on top of whatever the arithmetic says. Those ratios are measured,
#: not proven, and a spare ring is the cheapest insurance available.
_MARGIN_RINGS = 1


def _resolve_resolution(resolution: int | None, config: Settings) -> int:
    return config.h3_dispatch_resolution if resolution is None else resolution


def _validate_latlng(lat: float, lng: float) -> None:
    # h3 takes a latitude of 200 without complaint and wraps it onto some cell
    # on the other side of the planet, which would silently prefilter against a
    # neighbourhood nobody asked about.
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        raise ValueError(f"({lat}, {lng}) is not a valid coordinate")


def _require_cell(cell: str) -> str:
    if not h3.is_valid_cell(cell):
        raise ValueError(f"{cell!r} is not a valid H3 cell")
    return cell


def cell_for(
    lat: float,
    lng: float,
    *,
    resolution: int | None = None,
    config: Settings = settings,
) -> str:
    """The dispatch-grain cell containing a point."""
    _validate_latlng(lat, lng)
    return h3.latlng_to_cell(lat, lng, _resolve_resolution(resolution, config))


def zone_cell_for(lat: float, lng: float, *, config: Settings = settings) -> str:
    """The coarse zone cell containing a point: the supply-density key.

    Taken from the coordinates rather than from the point's dispatch cell,
    because cells do not nest perfectly and a parent lookup would put some
    points in a neighbouring zone.
    """
    return cell_for(lat, lng, resolution=config.h3_zone_resolution, config=config)


def ring_cells(cell: str, k: int) -> list[str]:
    """The k-ring disk around `cell`, ordered by ring index so callers sweep outward.

    H3's own disk call makes no promise about the order of its output, so the
    order is built here instead of being inherited.
    """
    cells: list[str] = []
    for _, ring in expanding_search(cell, max_ring=k):
        cells.extend(ring)
    return cells


def expanding_search(
    cell: str,
    *,
    max_ring: int | None = None,
    config: Settings = settings,
) -> Iterator[tuple[int, list[str]]]:
    """Yield ``(ring_index, cells_at_that_ring)`` outward from `cell`.

    Lazy so a caller can stop as soon as it has enough candidates; that is the
    whole point of indexing by cell, since retrieval cost then tracks the
    density of supply near the delivery point rather than the size of the
    chemist table.

    `max_ring` is a retrieval BUDGET, not a radius. Stopping early -- here at
    the budget, or in the caller once it has enough -- can leave a servable
    chemist unexamined, so a sweep that has to cover a known radius must run to
    at least ``safe_ring_for_km(radius_km)``, and whatever it finds still has to
    pass the exact distance and ETA checks.
    """
    _require_cell(cell)
    limit = config.h3_max_ring if max_ring is None else max_ring
    if limit < 0:
        raise ValueError(f"max_ring must be non-negative, got {limit}")
    for ring_index in range(limit + 1):
        # grid_ring partitions the disk exactly, pentagon neighbourhoods
        # included, so a ring-by-ring sweep finds everything one grid_disk call
        # of the same radius would.
        yield ring_index, h3.grid_ring(cell, ring_index)


def grid_distance_cells(a: str, b: str) -> int:
    """Ring steps between two cells of the same resolution.

    Raises ``h3.H3FailedError`` for cells too far apart for H3 to walk between,
    which is left to propagate: a fabricated distance would be read as a real
    one.
    """
    _require_cell(a)
    _require_cell(b)
    if h3.get_resolution(a) != h3.get_resolution(b):
        raise ValueError(
            f"cells {a!r} (res {h3.get_resolution(a)}) and {b!r} "
            f"(res {h3.get_resolution(b)}) are not at the same resolution"
        )
    return h3.grid_distance(a, b)


def approx_km_between_cells(
    a: str,
    b: str,
    *,
    resolution: int | None = None,
    config: Settings = settings,
) -> float:
    """Crude km between two cells: ring steps times the average edge length.

    An APPROXIMATION, and a deliberately coarse one. It reads low, because
    adjacent cell centres sit about sqrt(3) edge lengths apart rather than one;
    it ignores where inside each cell the real coordinates are; and it averages
    over cells that vary by a third in width. It is for log lines, coarse
    buckets and ordering hints only. Anything the customer is told -- radius,
    ETA, delivery fee -- measures the real coordinates with
    ``geo.haversine_km``.
    """
    res = _resolve_resolution(resolution, config)
    for cell in (a, b):
        if h3.get_resolution(_require_cell(cell)) != res:
            raise ValueError(
                f"{cell!r} is at resolution {h3.get_resolution(cell)}, not {res}; "
                f"pass resolution= to measure cells at their own grain"
            )
    return grid_distance_cells(a, b) * h3.average_hexagon_edge_length(res, unit="km")


def safe_ring_for_km(
    radius_km: float,
    *,
    resolution: int | None = None,
    config: Settings = settings,
) -> int:
    """Rings certain to cover `radius_km` around any point in the origin cell.

    The guarantee the prefilter rests on, stated over POINTS rather than cell
    centres because that is how callers use it: if two points are within
    `radius_km` of each other, the cell of the second is within this many rings
    of the cell of the first. So ``cell_for(candidate) in cells_within_km(...)``
    cannot miss a candidate the exact check would have admitted.

    That costs two cell offsets, not one -- either point can sit a circumradius
    away from its own cell's centre, and the grid walk only sees the centres.
    On top of that the pessimism is deliberate at every step: a ring step is
    assumed to travel the shortest distance any step anywhere on the grid
    travels, both cells are assumed to be the widest on the grid, the division
    rounds up, and a margin ring is added, because H3 cells are not uniform in
    size and the grid is distorted near the pentagons.

    Returning too high costs a longer ``IN`` list. Returning too low drops a
    chemist the exact check would have accepted, invisibly. Note that the result
    is deliberately NOT clamped to ``config.h3_max_ring``: clamping a radius to
    a budget is exactly how a superset quietly becomes a subset.
    """
    if not math.isfinite(radius_km) or radius_km < 0:
        raise ValueError(f"radius_km must be a non-negative number of km, got {radius_km!r}")
    res = _resolve_resolution(resolution, config)
    edge_km = h3.average_hexagon_edge_length(res, unit="km")
    shortest_step_km = edge_km * _NOMINAL_SPACING_PER_EDGE * _MIN_STEP_FRACTION
    widest_cell_reach_km = edge_km * _MAX_CIRCUMRADIUS_FACTOR
    reach_km = radius_km + 2 * widest_cell_reach_km
    return math.ceil(reach_km / shortest_step_km) + _MARGIN_RINGS


def cells_within_km(
    lat: float,
    lng: float,
    radius_km: float,
    *,
    resolution: int | None = None,
    config: Settings = settings,
) -> set[str]:
    """Cells whose centres could be within `radius_km` of a point: a superset.

    Over-inclusive on purpose, and by a wide margin; see ``safe_ring_for_km``
    for why the margin is priced that way. It is wide enough that the set also
    holds the cell of any CANDIDATE within the radius, not merely the cells
    whose own centres fall inside it, which is what makes
    ``cell_for(candidate) in cells_within_km(...)`` a sound prefilter. The
    caller is expected to hand this to a query as a cell filter and then put
    every row it returns through the exact haversine and ETA checks, which are
    what actually decide serviceability.

    Built from H3's disk call rather than from a ring sweep because the disk is
    the single call that cannot come back short around a pentagon.
    """
    res = _resolve_resolution(resolution, config)
    origin = cell_for(lat, lng, resolution=res, config=config)
    rings = safe_ring_for_km(radius_km, resolution=res, config=config)
    return set(h3.grid_disk(origin, rings))
