"""Hex geo-indexing (Uber's H3): the retrieval prefilter and its outward rounding.

One property is the reason this suite exists. `supply.candidates_near` keeps a
chemist only when ``cell_for(chemist) in cells_within_km(delivery_point,
radius)``, so that set has to be a SUPERSET of what the exact haversine check
would admit. A chemist the prefilter drops is never scored, never logged as
rejected, and nothing downstream can recover it: a servable pharmacy simply
disappears from the market and nothing says so. The scatter tests below are the
guard against that, and every bound here is asserted in one direction only --
the prefilter may admit too much, never too little.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from itertools import islice

import h3
import pytest

from medic.config import Settings
from medic.domain import geocells
from medic.domain.geo import haversine_km

#: A fixed seed, so a scatter that finds a hole in the prefilter finds the same
#: hole on the next run. Nothing here may reach for the global `random` module.
SEED = 20261020
SCATTER_POINTS = 4_000

MALVIYA = (26.8505, 75.8060)
VAISHALI = (26.9124, 75.7400)
MANSAROVAR = (26.8520, 75.7640)
C_SCHEME = (26.9065, 75.7985)
RAJA_PARK = (26.8955, 75.8280)
JAIPUR_POINTS = (MALVIYA, VAISHALI, MANSAROVAR, C_SCHEME, RAJA_PARK)

CONFIG = Settings()
DISPATCH_RES = CONFIG.h3_dispatch_resolution
ZONE_RES = CONFIG.h3_zone_resolution
#: Radii a dispatcher actually asks for, up to the configured match radius.
RADII_KM = (0.5, 1.0, 2.5, CONFIG.max_match_radius_km)

_KM_PER_LAT_DEGREE = 111.32

#: A Jaipur point whose res-8 cell has a res-7 parent that is NOT the zone cell
#: its own coordinates fall in; found by sweeping the pilot area, and kept here
#: as a standing witness that the two resolutions do not nest.
OVERHANGING_POINT = (26.881341, 75.764672)

IMPOSSIBLE_POINTS = (
    (200.0, 75.8060),
    (-91.0, 75.8060),
    (26.8505, 181.0),
    (26.8505, -180.5),
)


def cell(point: tuple[float, float], *, resolution: int | None = None) -> str:
    return geocells.cell_for(point[0], point[1], resolution=resolution)


def scatter(
    origin: tuple[float, float],
    span_km: float,
    *,
    count: int = SCATTER_POINTS,
    seed: int = SEED,
) -> list[tuple[float, float]]:
    """`count` deterministic points in a box of +/- `span_km` around `origin`."""
    rng = random.Random(seed)
    lat_span = span_km / _KM_PER_LAT_DEGREE
    lng_span = span_km / (_KM_PER_LAT_DEGREE * math.cos(math.radians(origin[0])))
    return [
        (
            origin[0] + rng.uniform(-lat_span, lat_span),
            origin[1] + rng.uniform(-lng_span, lng_span),
        )
        for _ in range(count)
    ]


def prefilter_sweep(
    origin: tuple[float, float],
    radius_km: float,
    *,
    resolution: int,
    count: int = SCATTER_POINTS,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Scatter around `origin`, returning (points inside the radius, points dropped).

    The second list is what may never hold anything; the first is returned so a
    sweep that admits nothing cannot pass by being vacuous.
    """
    covered = geocells.cells_within_km(origin[0], origin[1], radius_km, resolution=resolution)
    inside: list[tuple[float, float]] = []
    missed: list[tuple[float, float]] = []
    for point in scatter(origin, radius_km * 1.3, count=count):
        if haversine_km(origin[0], origin[1], point[0], point[1]) > radius_km:
            continue
        inside.append(point)
        if cell(point, resolution=resolution) not in covered:
            missed.append(point)
    return inside, missed


def corner_sweep(
    origin: tuple[float, float],
    radius_km: float,
    *,
    resolution: int,
) -> tuple[list[str], list[str]]:
    """Cells with a corner inside the radius, and those of them the prefilter drops.

    The tight form of the guarantee: a cell with a corner inside the radius holds
    points the exact check would admit, so the prefilter has to hold the cell. The
    search runs a few rings wider than the prefilter itself, so the cells just
    outside it are the ones examined.
    """
    covered = geocells.cells_within_km(origin[0], origin[1], radius_km, resolution=resolution)
    rings = geocells.safe_ring_for_km(radius_km, resolution=resolution) + 3
    touching: list[str] = []
    missed: list[str] = []
    for candidate in h3.grid_disk(cell(origin, resolution=resolution), rings):
        corners = h3.cell_to_boundary(candidate)
        if all(haversine_km(origin[0], origin[1], lat, lng) > radius_km for lat, lng in corners):
            continue
        touching.append(candidate)
        if candidate not in covered:
            missed.append(candidate)
    return touching, missed


def nominal_rings(radius_km: float, *, resolution: int) -> int:
    """Rings a naive reading of the grid would ask for: the radius over nominal
    centre-to-centre spacing, with no cell offsets and no margin."""
    edge_km = h3.average_hexagon_edge_length(resolution, unit="km")
    return math.ceil(radius_km / (math.sqrt(3) * edge_km))


# --- Cell identity and resolution -----------------------------------------


def test_a_dispatch_cell_is_at_the_configured_dispatch_resolution():
    assert h3.get_resolution(cell(MALVIYA)) == DISPATCH_RES


def test_a_zone_cell_is_at_the_coarser_configured_zone_resolution():
    assert h3.get_resolution(geocells.zone_cell_for(*MALVIYA)) == ZONE_RES
    assert ZONE_RES < DISPATCH_RES


def test_an_explicit_resolution_overrides_the_configured_dispatch_grain():
    assert h3.get_resolution(cell(MALVIYA, resolution=9)) == 9


def test_both_resolutions_are_read_from_the_settings_passed_in():
    config = Settings(h3_dispatch_resolution=9, h3_zone_resolution=6)
    assert h3.get_resolution(geocells.cell_for(*MALVIYA, config=config)) == 9
    assert h3.get_resolution(geocells.zone_cell_for(*MALVIYA, config=config)) == 6


def test_a_zone_cell_comes_from_the_coordinates_not_from_the_dispatch_cells_parent():
    """Cells do not nest: this point's res-8 cell has a res-7 parent that is not the
    zone its own coordinates fall in, so a parent lookup would file it next door."""
    zone = geocells.zone_cell_for(*OVERHANGING_POINT)
    assert h3.cell_to_parent(cell(OVERHANGING_POINT), ZONE_RES) != zone
    assert zone == cell(OVERHANGING_POINT, resolution=ZONE_RES)


def test_the_dispatch_grain_separates_neighbouring_jaipur_landmarks():
    assert len({cell(point) for point in JAIPUR_POINTS}) == len(JAIPUR_POINTS)


# --- Coordinate guards ----------------------------------------------------


def test_an_out_of_range_coordinate_is_rejected_rather_than_wrapped():
    for point in IMPOSSIBLE_POINTS:
        with pytest.raises(ValueError, match="not a valid coordinate"):
            cell(point)


def test_an_out_of_range_coordinate_is_rejected_for_a_zone_cell_too():
    with pytest.raises(ValueError, match="not a valid coordinate"):
        geocells.zone_cell_for(200.0, 75.8060)


def test_the_prefilter_rejects_an_out_of_range_delivery_point():
    with pytest.raises(ValueError, match="not a valid coordinate"):
        geocells.cells_within_km(200.0, 75.8060, 2.0)


def test_h3_itself_would_have_wrapped_an_impossible_latitude_around_the_planet():
    """Why the guard cannot be left to the library: h3 takes a latitude of 200
    without complaint and answers with a perfectly valid cell in another ocean."""
    wrapped = h3.latlng_to_cell(200.0, 75.8060, DISPATCH_RES)
    assert h3.is_valid_cell(wrapped)
    assert haversine_km(*MALVIYA, *h3.cell_to_latlng(wrapped)) > 10_000


def test_the_poles_and_the_antimeridian_are_inside_the_accepted_range():
    for point in ((90.0, 180.0), (-90.0, -180.0)):
        assert h3.is_valid_cell(cell(point))


# --- The prefilter superset: the property this module exists for ----------


def test_the_prefilter_admits_every_point_the_exact_check_admits_at_dispatch_grain():
    """The invariant `supply.candidates_near` rests on: every point the haversine
    check would admit has its cell in the prefilter's set, at every radius."""
    for radius_km in RADII_KM:
        inside, missed = prefilter_sweep(MALVIYA, radius_km, resolution=DISPATCH_RES)
        assert missed == []
        assert len(inside) > SCATTER_POINTS // 8


def test_the_prefilter_admits_every_point_the_exact_check_admits_at_zone_grain():
    for radius_km in RADII_KM:
        inside, missed = prefilter_sweep(MALVIYA, radius_km, resolution=ZONE_RES)
        assert missed == []
        assert len(inside) > SCATTER_POINTS // 8


def test_the_prefilter_holds_around_all_twelve_pentagons_where_the_grid_is_distorted():
    """The ratios behind `safe_ring_for_km` are measured, not proven, and the
    pentagon neighbourhoods are where the measurement is worst."""
    for resolution in (ZONE_RES, DISPATCH_RES):
        for pentagon in h3.get_pentagons(resolution):
            origin = h3.cell_to_latlng(pentagon)
            inside, missed = prefilter_sweep(origin, 2.0, resolution=resolution, count=400)
            assert missed == []
            assert inside


def test_the_prefilter_covers_every_cell_with_a_corner_inside_the_radius():
    """The scatter tests sample cell interiors; this one takes the extreme points of
    every cell near the boundary, where a bound that rounded inward would show."""
    for resolution in (ZONE_RES, DISPATCH_RES):
        for radius_km in RADII_KM:
            touching, missed = corner_sweep(MALVIYA, radius_km, resolution=resolution)
            assert missed == []
            assert touching


def test_the_prefilter_holds_from_every_reference_point_in_the_pilot_area():
    """Each landmark sits differently inside its own cell, and the bound has to
    allow for the delivery point's own offset as much as the chemist's."""
    for origin in JAIPUR_POINTS:
        for resolution in (ZONE_RES, DISPATCH_RES):
            touching, missed = corner_sweep(
                origin, CONFIG.max_match_radius_km, resolution=resolution
            )
            assert missed == []
            assert touching


def test_the_prefilter_admits_every_reference_chemist_inside_the_match_radius():
    """The call-site shape: `cell_for(chemist) in cells_within_km(delivery, radius)`."""
    radius = CONFIG.max_match_radius_km
    covered = geocells.cells_within_km(*MALVIYA, radius)
    admitted = [p for p in JAIPUR_POINTS if haversine_km(*MALVIYA, *p) <= radius]
    assert admitted
    for point in admitted:
        assert cell(point) in covered


def test_the_prefilter_is_over_inclusive_by_design():
    """It admits cells whose own centres are well outside the radius, which is the
    price of never dropping one that is inside it."""
    covered = geocells.cells_within_km(*MALVIYA, 1.0)
    reaches = [haversine_km(*MALVIYA, *h3.cell_to_latlng(c)) for c in covered]
    assert max(reaches) > 1.0


def test_a_wider_radius_covers_everything_a_narrower_one_covered():
    for resolution in (ZONE_RES, DISPATCH_RES):
        narrow = geocells.cells_within_km(*MALVIYA, 1.0, resolution=resolution)
        wide = geocells.cells_within_km(*MALVIYA, 5.0, resolution=resolution)
        assert narrow <= wide


def test_the_prefilter_contains_the_cell_of_the_point_it_is_centred_on():
    for point in JAIPUR_POINTS:
        assert cell(point) in geocells.cells_within_km(point[0], point[1], 0.0)


def test_the_prefilter_is_built_entirely_at_the_requested_resolution():
    covered = geocells.cells_within_km(*MALVIYA, 2.0, resolution=ZONE_RES)
    assert {h3.get_resolution(c) for c in covered} == {ZONE_RES}


def test_the_prefilter_covers_at_least_the_rings_a_sweep_would_visit():
    """The two paths through the module have to agree: the disk `cells_within_km`
    builds may be wider than a ring sweep of the same depth, never narrower."""
    radius = CONFIG.max_match_radius_km
    for resolution in (ZONE_RES, DISPATCH_RES):
        origin = cell(MALVIYA, resolution=resolution)
        rings = geocells.safe_ring_for_km(radius, resolution=resolution)
        covered = geocells.cells_within_km(*MALVIYA, radius, resolution=resolution)
        assert set(geocells.ring_cells(origin, rings)) <= covered


# --- Rings certain to cover a radius --------------------------------------


def test_the_safe_ring_count_grows_monotonically_with_the_radius():
    for resolution in (ZONE_RES, DISPATCH_RES):
        counts = [
            geocells.safe_ring_for_km(half_km / 2, resolution=resolution)
            for half_km in range(40)
        ]
        assert counts == sorted(counts)
        assert counts[-1] > counts[0]


def test_the_safe_ring_count_always_exceeds_the_nominal_hexagon_arithmetic():
    """Pessimism at every step: the shortest step measured anywhere on the grid, the
    widest cell at both ends, division rounded up, and a margin ring on top."""
    for resolution in (ZONE_RES, DISPATCH_RES):
        for radius_km in RADII_KM:
            safe = geocells.safe_ring_for_km(radius_km, resolution=resolution)
            assert safe > nominal_rings(radius_km, resolution=resolution)


def test_a_zero_radius_still_allows_for_both_points_sitting_off_centre():
    """Two cell offsets, not one: either point can sit a circumradius from its own
    cell's centre, and the grid walk only ever sees the centres."""
    for resolution in (ZONE_RES, DISPATCH_RES):
        assert geocells.safe_ring_for_km(0.0, resolution=resolution) >= 2


def test_the_safe_ring_count_is_not_clamped_to_the_configured_ring_budget():
    """`h3_max_ring` is a retrieval budget, not a radius; clamping a radius to it is
    exactly how a superset quietly becomes a subset."""
    assert geocells.safe_ring_for_km(20.0) > CONFIG.h3_max_ring


def test_a_coarser_resolution_needs_fewer_rings_to_cover_the_same_radius():
    radius = CONFIG.max_match_radius_km
    zone_rings = geocells.safe_ring_for_km(radius, resolution=ZONE_RES)
    dispatch_rings = geocells.safe_ring_for_km(radius, resolution=DISPATCH_RES)
    assert zone_rings < dispatch_rings


def test_a_negative_radius_is_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        geocells.safe_ring_for_km(-1.0)


def test_an_infinite_or_undefined_radius_is_rejected():
    for radius_km in (math.inf, -math.inf, math.nan):
        with pytest.raises(ValueError, match="non-negative"):
            geocells.safe_ring_for_km(radius_km)


# --- Sweeping outward -----------------------------------------------------


def test_ring_cells_begins_at_the_centre_and_never_steps_back_inward():
    centre = cell(MALVIYA)
    disk = geocells.ring_cells(centre, 3)
    steps = [h3.grid_distance(centre, c) for c in disk]
    assert disk[0] == centre
    assert steps == sorted(steps)
    assert max(steps) == 3


def test_ring_cells_covers_the_disk_h3_returns_without_repeating_a_cell():
    """The ordering is built here, but the coverage still has to match h3's disk."""
    centre = cell(MALVIYA)
    disk = geocells.ring_cells(centre, 4)
    assert set(disk) == set(h3.grid_disk(centre, 4))
    assert len(disk) == len(set(disk))


def test_ring_cells_sweeps_a_pentagon_without_repeating_or_dropping_a_cell():
    pentagon = h3.get_pentagons(DISPATCH_RES)[0]
    disk = geocells.ring_cells(pentagon, 3)
    assert set(disk) == set(h3.grid_disk(pentagon, 3))
    assert len(disk) == len(set(disk))


def test_a_ring_around_a_pentagon_holds_fewer_cells_than_a_hexagonal_one():
    """The fact the ring arithmetic has to respect: a ring of index k holds AT MOST
    6k cells, so nothing may assume a cell count."""
    pentagon = h3.get_pentagons(DISPATCH_RES)[0]
    for k in (1, 2, 3):
        assert len(h3.grid_ring(pentagon, k)) < 6 * k
        assert len(h3.grid_ring(cell(MALVIYA), k)) == 6 * k


def test_expanding_search_yields_ring_indexes_in_increasing_order():
    sweep = list(geocells.expanding_search(cell(MALVIYA), max_ring=4))
    assert [index for index, _ in sweep] == [0, 1, 2, 3, 4]


def test_each_ring_of_an_expanding_search_sits_exactly_that_many_steps_out():
    centre = cell(MALVIYA)
    for index, ring in geocells.expanding_search(centre, max_ring=4):
        assert {h3.grid_distance(centre, c) for c in ring} == {index}


def test_an_expanding_search_starts_with_the_origin_cell_alone():
    centre = cell(MALVIYA)
    index, ring = next(geocells.expanding_search(centre, max_ring=0))
    assert index == 0
    assert list(ring) == [centre]


def test_an_expanding_search_stops_at_the_configured_budget_when_given_none():
    indexes = [index for index, _ in geocells.expanding_search(cell(MALVIYA))]
    assert indexes[-1] == CONFIG.h3_max_ring


def test_an_expanding_search_does_not_build_rings_the_caller_never_asks_for():
    """Lazy on purpose: retrieval cost should track the density of supply near the
    delivery point, not the budget the sweep happened to be handed."""
    sweep = geocells.expanding_search(cell(MALVIYA), max_ring=2_000)
    assert isinstance(sweep, Iterator)
    assert [index for index, _ in islice(sweep, 2)] == [0, 1]


def test_a_negative_ring_budget_is_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        list(geocells.expanding_search(cell(MALVIYA), max_ring=-1))


def test_an_expanding_search_refuses_something_that_is_not_a_cell():
    with pytest.raises(ValueError, match="not a valid H3 cell"):
        list(geocells.expanding_search("not-a-cell"))


# --- Grid distance --------------------------------------------------------


def test_grid_distance_is_symmetric():
    for a in JAIPUR_POINTS:
        for b in JAIPUR_POINTS:
            forward = geocells.grid_distance_cells(cell(a), cell(b))
            backward = geocells.grid_distance_cells(cell(b), cell(a))
            assert forward == backward


def test_a_cell_is_zero_rings_from_itself():
    assert geocells.grid_distance_cells(cell(MALVIYA), cell(MALVIYA)) == 0


def test_grid_distance_counts_the_ring_a_cell_sits_on():
    centre = cell(MALVIYA)
    for k in (1, 3, 5):
        ring = h3.grid_ring(centre, k)
        assert {geocells.grid_distance_cells(centre, c) for c in ring} == {k}


def test_two_resolutions_are_never_compared():
    """A resolution is part of a cell's identity, and cells do not nest, so a res-7
    to res-8 step count would be meaningless rather than merely imprecise."""
    with pytest.raises(ValueError, match="not at the same resolution"):
        geocells.grid_distance_cells(cell(MALVIYA), geocells.zone_cell_for(*MALVIYA))


def test_cells_too_far_apart_to_walk_raise_rather_than_return_a_number():
    """A fabricated distance would be read downstream as a real one."""
    antipodal = geocells.cell_for(-26.8505, -104.1940)
    with pytest.raises(h3.H3FailedError):
        geocells.grid_distance_cells(cell(MALVIYA), antipodal)


def test_grid_distance_refuses_something_that_is_not_a_cell():
    with pytest.raises(ValueError, match="not a valid H3 cell"):
        geocells.grid_distance_cells(cell(MALVIYA), "not-a-cell")


# --- The coarse cell-to-cell distance -------------------------------------


def test_the_coarse_cell_distance_reads_low_against_the_real_distance():
    """Ring steps times an average edge length understates by around sqrt(3), so it
    is for log lines and ordering hints, never for anything a customer is told."""
    for point in (MANSAROVAR, C_SCHEME, RAJA_PARK):
        approx = geocells.approx_km_between_cells(cell(MALVIYA), cell(point))
        assert approx < haversine_km(*MALVIYA, *point)


def test_measuring_a_zone_cell_against_the_dispatch_edge_length_is_refused():
    """The other half of the non-nesting rule: a res-7 ring count multiplied by a
    res-8 edge length would still read as a number of km."""
    zone = geocells.zone_cell_for(*MALVIYA)
    with pytest.raises(ValueError, match="pass resolution="):
        geocells.approx_km_between_cells(zone, zone)


def test_a_zone_cell_can_be_measured_at_its_own_grain():
    zone = geocells.zone_cell_for(*MALVIYA)
    other = geocells.zone_cell_for(*RAJA_PARK)
    assert geocells.approx_km_between_cells(zone, other, resolution=ZONE_RES) > 0
