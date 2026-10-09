"""Risk analysis for a room: many fires, several occupants, and what stands out.

For every likely ignition source it runs the fire, then sends occupants through it. From
those runs it produces:

- a heat map: the soonest each patch of floor becomes too hot to be in, over all fires
- a danger map: how badly someone who is at each spot when a fire starts fares, on average
- a ranking of where a fire starting does the most harm
- risk factors, found by comparing runs that differ in one thing or by reading the layout
"""

import math
import os
import re
import statistics
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from firesim.escape.dose import TOLERABLE_RADIANT_FLUX
from firesim.escape.grid import (
    OVERHEAD_CLEARANCE,
    Grid,
    blocks_movement,
    build_grid,
    exit_cells,
    is_clutter,
    path_from,
    travel_costs,
)
from firesim.escape.sim import (
    BODY_RADIUS,
    SQUEEZE_RADIUS,
    EscapeSummary,
    GridCache,
    PersonScenario,
    at,
    fire_flux,
    fire_sources,
    run_escape,
)
from firesim.fire.fuels import FUELS, burn_curve
from firesim.fire.physics import (
    SIGMA,
    T_AMB,
    distance_to_object,
    layer_emissivity,
    layer_view_factor,
)
from firesim.fire.sim import FireRun, FireScenario, run_fire
from firesim.schema import OpeningKind, Point2, Room, RoomObject, inside_floor

FIRE_DURATION = 300.0  # s; long enough to decide whether someone gets out of one room
# Repeats of each case. Chance decides which of a fire's signals wake a sleeper, and how
# well they find the way in smoke.
SEEDS = 8
MAX_SOURCES = 12
MAP_CELL = 0.2  # m
MAP_TIME_STEP = 5.0  # s
MAP_BODY_HEIGHT = 0.8  # m; mid-body of someone crouching
SPOT_STRIDE = 2  # danger map: simulate a person at every second map cell each way
# Danger map: how long after noticing the fire the person at the spot starts to leave.
# Prompt, slow (dressing, collecting things) and very slow (trying to fight the fire).
SPOT_DELAYS = (10.0, 45.0, 90.0)  # s
MAIN_ITEM_AREA = 0.15  # m²; floor-standing items at least this big are drawn on the plan
MAIN_ITEM_HEIGHT = 0.25  # m
NEAR_ROUTE = 0.8  # m; an ignition source this close to the way out is a problem
FAST_SPREAD = 60.0  # s; a second item catching sooner than this is "fast"
HARMFUL_DOSE = 0.3

SLEEPING_PLACE = re.compile(r"\b(bed|mattress|sofa|couch)\b", re.IGNORECASE)
SEAT = re.compile(r"\b(chair|seat|stool)\b", re.IGNORECASE)

PersonKind = Literal["asleep_alarm", "asleep_no_alarm", "awake"]


class RiskCase(BaseModel):
    """One ignition source and one occupant, repeated over SEEDS random seeds."""

    person: PersonKind
    scenario: PersonScenario  # replayable with the first seed
    opening_states: dict[str, bool]
    runs: int
    escaped: int
    worst_outcome: str
    aware_s: float | None
    escape_s: float | None  # median of the runs that got out
    mean_dose: float
    max_dose: float
    harm: float  # 0 unharmed to 1 did not get out, averaged over the runs


class SourceRisk(BaseModel):
    object_id: str
    label: str
    harm: float  # mean over this source's cases
    smoke_at_head_height_s: float | None
    first_spread_s: float | None
    first_spread_to: str | None
    cases: list[RiskCase]


class FloorMap(BaseModel):
    """One value per floor cell, row-major from (x0, y0); None where there is no open floor."""

    x0: float
    y0: float
    cell: float
    nx: int
    ny: int
    values: list[float | None]


class RestingPlace(BaseModel):
    """A bed or sofa, and how a sleeper there fares on average (0 unharmed, 1 does not get out)."""

    object_id: str
    label: str
    harm: float


class RiskFactor(BaseModel):
    title: str
    detail: str
    severity: float  # 0 to 1, for ordering
    object_ids: list[str]


class RiskReport(BaseModel):
    fires: int
    runs: int
    harmful_cases: int
    total_cases: int
    # Seconds until each cell is too hot to be in, in whichever fire gets there soonest.
    heat: FloorMap
    heat_limit_s: float  # cells that stay tolerable are reported as this
    # Mean harm (0 to 1) to an awake person who is at that cell when a fire starts,
    # averaged over all the fires and over the reaction delays in danger_delays_s.
    danger: FloorMap
    danger_delays_s: list[float]
    resting: list[RestingPlace]
    route: list[Point2]  # the sleeper's way out with no fire in the way
    main_items: list[str]  # object ids worth drawing on a plan
    high_risk_items: list[str]
    sources: list[SourceRisk]
    factors: list[RiskFactor]


def _name(obj: RoomObject) -> str:
    return obj.label[:1].lower() + obj.label[1:]


def _ignition_sources(room: Room) -> list[RoomObject]:
    burnable = [o for o in room.objects if FUELS[o.fuel] is not None]
    flagged = [o for o in burnable if o.is_ignition_source]
    if flagged:
        return flagged[:MAX_SOURCES]
    # Nothing flagged: fall back to the items that would burn hardest.
    return sorted(burnable, key=lambda o: -burn_curve(o, FUELS[o.fuel]).peak_kw)[:6]


def _first_matching(room: Room, pattern: re.Pattern[str]) -> RoomObject | None:
    on_floor = [o for o in room.objects if o.position.z < 0.9 and pattern.search(o.category)]
    # A mattress sits on its bed frame: prefer the mattress, since that is what is lain on.
    return max(
        on_floor,
        key=lambda o: ("mattress" in o.category.lower(), o.size.width * o.size.depth),
        default=None,
    )


def _people(room: Room) -> dict[PersonKind, PersonScenario]:
    xs, ys = [p.x for p in room.floor], [p.y for p in room.floor]
    middle = Point2(x=(min(xs) + max(xs)) / 2, y=(min(ys) + max(ys)) / 2)
    bed = _first_matching(room, SLEEPING_PLACE)
    seat = _first_matching(room, SEAT)

    def at_object(obj: RoomObject | None, **kwargs: bool) -> PersonScenario:
        start = Point2(x=obj.position.x, y=obj.position.y) if obj else middle
        return PersonScenario(start=start, start_object_id=obj.id if obj else None, **kwargs)

    return {
        "asleep_alarm": at_object(bed, asleep=True, smoke_alarm=True),
        "asleep_no_alarm": at_object(bed, asleep=True, smoke_alarm=False),
        "awake": at_object(seat, asleep=False, smoke_alarm=True),
    }


def _harm(summary: EscapeSummary) -> float:
    if summary.outcome in ("incapacitated", "trapped"):
        return 1.0
    return min(max(summary.fed_gas, summary.fed_heat), 1.0)


def _run_case(
    room: Room, fire: FireRun, kind: PersonKind, person: PersonScenario, grids: GridCache
) -> RiskCase:
    runs = [
        run_escape(room, fire, person.model_copy(update={"seed": seed}), grids).summary
        for seed in range(SEEDS)
    ]
    out = [r.escaped_s for r in runs if r.escaped_s is not None]
    doses = [max(r.fed_gas, r.fed_heat) for r in runs]
    worst = max(runs, key=_harm)
    return RiskCase(
        person=kind,
        scenario=person,
        opening_states=fire.scenario.opening_states,
        runs=len(runs),
        escaped=len(out),
        worst_outcome=worst.outcome,
        aware_s=runs[0].aware_s,
        escape_s=statistics.median(out) if out else None,
        mean_dose=round(statistics.mean(doses), 3),
        max_dose=round(max(doses), 3),
        harm=round(statistics.mean(_harm(r) for r in runs), 3),
    )


@dataclass(frozen=True)
class _MapCells:
    x0: float
    y0: float
    nx: int
    ny: int
    bounds: tuple[float, float, float, float]
    centres: list[tuple[float, float]]
    open_floor: list[bool]

    def floor_map(self, values: list[float | None]) -> FloorMap:
        return FloorMap(
            x0=self.x0, y0=self.y0, cell=MAP_CELL, nx=self.nx, ny=self.ny, values=values
        )

    def spots(self) -> list[int]:
        """Cells where a person is placed for the danger map: a coarse lattice of open
        floor, plus any open cell the lattice leaves more than one cell away."""
        chosen = [
            j * self.nx + i
            for j in range(0, self.ny, SPOT_STRIDE)
            for i in range(0, self.nx, SPOT_STRIDE)
            if self.open_floor[j * self.nx + i]
        ]
        for k, ok in enumerate(self.open_floor):
            if ok and self._distance_to_nearest(k, chosen) > 1.5 * MAP_CELL:
                chosen.append(k)
        return chosen

    def _distance_to_nearest(self, k: int, among: list[int]) -> float:
        x, y = self.centres[k]
        return min(
            (math.hypot(x - self.centres[n][0], y - self.centres[n][1]) for n in among),
            default=math.inf,
        )

    def spread(self, spots: list[int], values: list[float]) -> list[float | None]:
        """Give every open cell a value blended from its three nearest spots."""
        out: list[float | None] = []
        for k, ok in enumerate(self.open_floor):
            if not ok:
                out.append(None)
                continue
            x, y = self.centres[k]
            near = sorted(
                ((x - self.centres[s][0]) ** 2 + (y - self.centres[s][1]) ** 2, n)
                for n, s in enumerate(spots)
            )[:3]
            weights = [1.0 / (d2 + 1e-4) for d2, _ in near]
            blended = sum(w * values[n] for w, (_, n) in zip(weights, near, strict=True))
            out.append(round(blended / sum(weights), 3))
        return out


def _map_cells(room: Room) -> _MapCells:
    xs, ys = [p.x for p in room.floor], [p.y for p in room.floor]
    x0, y0 = min(xs), min(ys)
    nx = math.ceil((max(xs) - x0) / MAP_CELL)
    ny = math.ceil((max(ys) - y0) / MAP_CELL)
    blockers = [o for o in room.objects if blocks_movement(o)]
    centres = [
        (x0 + (i + 0.5) * MAP_CELL, y0 + (j + 0.5) * MAP_CELL) for j in range(ny) for i in range(nx)
    ]
    open_floor = [
        inside_floor(room, x, y)
        and all(distance_to_object(x, y, o.position.z, o) > 0.01 for o in blockers)
        for x, y in centres
    ]
    return _MapCells(x0, y0, nx, ny, (x0, y0, max(xs), max(ys)), centres, open_floor)


def _heat_times(room: Room, fire: FireRun, cells: _MapCells) -> list[float | None]:
    """Seconds until each open cell receives more radiant heat than a person can tolerate."""
    sources = fire_sources(room, fire)
    times: list[float | None] = [FIRE_DURATION if ok else None for ok in cells.open_floor]
    pending = {k for k, ok in enumerate(cells.open_floor) if ok}
    t = 0.0
    while pending and t <= fire.t[-1]:
        interface = at(fire.layer_height_m, t)
        layer_k = at(fire.upper_temp_c, t) + 273.15
        emissive = (
            SIGMA * (layer_k**4 - T_AMB**4) * layer_emissivity(room.ceiling_height - interface)
        )
        active = [s for s in sources if at(s.hrr, t) > 1.0]
        gap = interface - MAP_BODY_HEIGHT
        for k in list(pending):
            x, y = cells.centres[k]
            heat = emissive * (layer_view_factor(cells.bounds, x, y, gap) if gap > 0 else 1.0)
            heat += fire_flux(x, y, MAP_BODY_HEIGHT, active, t)
            if heat >= TOLERABLE_RADIANT_FLUX:
                times[k] = t
                pending.discard(k)
        t += MAP_TIME_STEP
    return times


def _spot_harm(
    room: Room, fire: FireRun, cells: _MapCells, spots: list[int], grids: GridCache
) -> list[float]:
    """Harm to an awake person starting at each spot, averaged over the reaction delays."""
    harms = []
    for k in spots:
        x, y = cells.centres[k]
        runs = [
            run_escape(
                room,
                fire,
                PersonScenario(start=Point2(x=x, y=y), response_delay_s=delay),
                grids,
            ).summary
            for delay in SPOT_DELAYS
        ]
        harms.append(statistics.mean(_harm(r) for r in runs))
    return harms


def _clear_route(room: Room, start: Point2) -> tuple[Grid, list[tuple[float, float]], bool]:
    """The way from start to a door with no fire: grid, waypoints, and whether it squeezes."""
    exits = [o for o in room.openings if o.leads_outside_room and o.kind is not OpeningKind.WINDOW]
    grid = build_grid(room, BODY_RADIUS)
    for radius in (BODY_RADIUS, SQUEEZE_RADIUS):
        grid = build_grid(room, radius)
        goals = exit_cells(grid, room, exits)
        begin = grid.cell_at(start.x, start.y)
        if begin is None or not goals:
            continue
        cost = travel_costs(grid, goals, [1.0 / f for f in grid.speed_factor])
        if math.isfinite(cost[begin]):
            cells = path_from(grid, cost, begin)
            return grid, [grid.centre(k) for k in cells], radius == SQUEEZE_RADIUS
    return grid, [], False


def _distance_to_route(obj: RoomObject, route: list[tuple[float, float]]) -> float:
    """Horizontal distance from the object to the route. Overhead items are never near."""
    if obj.position.z >= OVERHEAD_CLEARANCE:
        return math.inf
    return min((distance_to_object(x, y, obj.position.z, obj) for x, y in route), default=math.inf)


_PERSON_TEXT = {
    "asleep_alarm": "a sleeper with a smoke alarm",
    "asleep_no_alarm": "a sleeper with no smoke alarm",
    "awake": "someone awake",
}


def _worst_case_text(source: SourceRisk) -> str:
    case = max(source.cases, key=lambda c: c.harm)
    who = _PERSON_TEXT[case.person]
    if case.harm < 0.05:
        return "Everyone simulated got out unharmed."
    if case.worst_outcome == "incapacitated":
        return f"Worst case simulated: {who} is overcome before reaching the door."
    if case.worst_outcome == "trapped":
        return f"Worst case simulated: {who} cannot reach the door."
    return f"Worst case simulated: {who} gets out with a dose of {case.max_dose:.2f}."


def _layout_factors(
    room: Room, sources: list[SourceRisk], route: list[tuple[float, float]], squeezes: bool
) -> list[RiskFactor]:
    objects = {o.id: o for o in room.objects}
    doors = [o for o in room.openings if o.leads_outside_room and o.kind is not OpeningKind.WINDOW]
    if not route:
        return [
            RiskFactor(
                title="No clear route to the door",
                detail="Furniture or clutter blocks every way from the bed to the door.",
                severity=1.0,
                object_ids=[],
            )
        ]

    factors: list[RiskFactor] = []
    for source in sources:
        obj = objects[source.object_id]
        distance = _distance_to_route(obj, route)
        near_route = distance <= NEAR_ROUTE
        fast = source.first_spread_s is not None and source.first_spread_s <= FAST_SPREAD
        if source.harm < 0.05 and not near_route and not (fast and source.first_spread_s <= 30):
            continue
        notes = []
        if near_route:
            only = "the only door" if len(doors) == 1 else "the door"
            notes.append(f"It is {distance:.1f} m from the way out to {only}.")
        if fast:
            notes.append(
                f"A fire here spreads to the {source.first_spread_to} "
                f"in {source.first_spread_s:.0f} s."
            )
        notes.append(_worst_case_text(source))
        severity = 0.2 + source.harm + (0.25 if near_route else 0.0) + (0.15 if fast else 0.0)
        factors.append(
            RiskFactor(
                title=f"Fire starting at the {_name(obj)}",
                detail=" ".join(notes),
                severity=round(min(1.0, severity), 2),
                object_ids=[obj.id],
            )
        )

    in_the_way = [o for o in room.objects if is_clutter(o) and _distance_to_route(o, route) < 0.05]
    if in_the_way:
        factors.append(
            RiskFactor(
                title="Clutter on the way out",
                detail=(
                    "The route to the door crosses the "
                    + ", the ".join(_name(o) for o in in_the_way)
                    + ". Stepping over things halves walking speed there and is harder in smoke."
                ),
                severity=0.35,
                object_ids=[o.id for o in in_the_way],
            )
        )
    if squeezes:
        factors.append(
            RiskFactor(
                title="The way out is too narrow to walk normally",
                detail="Part of the route is under 0.4 m wide, so the person has to turn sideways.",
                severity=0.5,
                object_ids=[],
            )
        )
    return factors


def _comparison_factors(
    sources: list[SourceRisk], door_flipped: dict[str, list[RiskCase]], door_was_open: bool
) -> list[RiskFactor]:
    factors: list[RiskFactor] = []

    def cases(kind: PersonKind) -> list[RiskCase]:
        return [c for s in sources for c in s.cases if c.person == kind]

    with_alarm, without = cases("asleep_alarm"), cases("asleep_no_alarm")
    harmed_with = sum(c.harm >= HARMFUL_DOSE for c in with_alarm)
    harmed_without = sum(c.harm >= HARMFUL_DOSE for c in without)
    delays = [
        b.aware_s - a.aware_s
        for a, b in zip(with_alarm, without, strict=True)
        if a.aware_s is not None and b.aware_s is not None
    ]
    if delays:
        delay = statistics.mean(delays)
        extra = statistics.mean(c.harm for c in without) - statistics.mean(
            c.harm for c in with_alarm
        )
        factors.append(
            RiskFactor(
                title="No smoke alarm",
                detail=(
                    f"In the {len(delays)} fire(s) that wake a sleeper either way, they become "
                    f"aware {delay:.0f} s later on average without an alarm. Harmful outcomes: "
                    f"{harmed_without} of {len(without)} without an alarm, "
                    f"{harmed_with} of {len(with_alarm)} with one."
                ),
                severity=round(min(1.0, 0.3 + max(extra, 0.0) * 2 + delay / 120), 2),
                object_ids=[],
            )
        )

    if door_flipped:
        as_filmed = [c for s in sources for c in s.cases if c.person != "awake"]
        flipped = [c for cases_ in door_flipped.values() for c in cases_]
        base, other = (
            statistics.mean(c.harm for c in as_filmed),
            statistics.mean(c.harm for c in flipped),
        )
        if abs(base - other) >= 0.05:
            now, alt = ("open", "shut") if door_was_open else ("shut", "open")
            better = alt if other < base else now
            factors.append(
                RiskFactor(
                    title=f"Door {better} is safer for a sleeper in this room",
                    detail=(
                        f"Average harm to a sleeper is {base:.2f} with the door {now} and "
                        f"{other:.2f} with it {alt} (0 unharmed, 1 does not get out). "
                        "The rest of the home is not modelled."
                    ),
                    severity=round(min(1.0, 0.2 + abs(base - other)), 2),
                    object_ids=[],
                )
            )
    return factors


def _plan_items(
    room: Room, sources: list[SourceRisk], factors: list[RiskFactor]
) -> tuple[list[str], list[str]]:
    """Which objects a plan should show: the big floor-standing ones, and the risky ones."""
    main = [
        o.id
        for o in room.objects
        if o.position.z < 0.1
        and o.size.height >= MAIN_ITEM_HEIGHT
        and o.size.width * o.size.depth >= MAIN_ITEM_AREA
    ]
    risky = {s.object_id for s in sources if s.harm >= 0.05}
    risky |= {i for f in factors for i in f.object_ids}
    return main, [o.id for o in room.objects if o.id in risky]


@dataclass
class _SourceResult:
    source: SourceRisk
    door_flipped: list[RiskCase]
    heat: list[float | None]
    spot_harm: list[float]
    runs: int


def _analyse_source(room: Room, object_id: str, grids: GridCache | None = None) -> _SourceResult:
    """Everything that depends on one ignition source. Runs in a worker process."""
    grids = {} if grids is None else grids
    obj = next(o for o in room.objects if o.id == object_id)
    people = _people(room)
    cells = _map_cells(room)
    spots = cells.spots()

    fire = run_fire(room, FireScenario(ignition_object_id=obj.id, duration_s=FIRE_DURATION))
    cases = [_run_case(room, fire, kind, person, grids) for kind, person in people.items()]
    spread = next((e for e in fire.events if e.kind == "ignition" and e.t > 0), None)
    spread_to = next((o for o in room.objects if spread and o.id == spread.object_id), None)
    source = SourceRisk(
        object_id=obj.id,
        label=obj.label,
        harm=round(statistics.mean(c.harm for c in cases), 3),
        smoke_at_head_height_s=fire.summary.smoke_at_head_height_s,
        first_spread_s=spread.t if spread else None,
        first_spread_to=_name(spread_to) if spread_to else None,
        cases=cases,
    )

    flipped: list[RiskCase] = []
    door_ids = [o.id for o in room.openings if o.kind is OpeningKind.DOOR]
    if door_ids:
        # The same fire with the doors the other way, for the sleeper only.
        was_open = any(o.is_open for o in room.openings if o.kind is OpeningKind.DOOR)
        states = dict.fromkeys(door_ids, not was_open)
        other = run_fire(
            room,
            FireScenario(
                ignition_object_id=obj.id, opening_states=states, duration_s=FIRE_DURATION
            ),
        )
        flipped = [
            _run_case(room, other, kind, people[kind], grids)
            for kind in ("asleep_alarm", "asleep_no_alarm")
        ]

    spot_harm = _spot_harm(room, fire, cells, spots, grids)
    runs = sum(c.runs for c in cases + flipped) + len(spots) * len(SPOT_DELAYS)
    return _SourceResult(source, flipped, _heat_times(room, fire, cells), spot_harm, runs)


_pool: ProcessPoolExecutor | None = None


def _run_sources(room: Room, ids: list[str], parallel: bool) -> list[_SourceResult]:
    """One result per ignition source, using worker processes when asked and available."""
    global _pool
    if parallel and len(ids) > 1:
        try:
            if _pool is None:
                _pool = ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1))
            return list(_pool.map(_analyse_source, [room] * len(ids), ids))
        except (BrokenProcessPool, OSError):
            _pool = None  # fall through to doing the work here
    grids: GridCache = {}
    return [_analyse_source(room, object_id, grids) for object_id in ids]


def analyse(room: Room, parallel: bool = False) -> RiskReport:
    results = _run_sources(room, [o.id for o in _ignition_sources(room)], parallel)
    sources = [r.source for r in results]
    door_flipped = {r.source.object_id: r.door_flipped for r in results if r.door_flipped}
    door_was_open = any(o.is_open for o in room.openings if o.kind is OpeningKind.DOOR)
    people = _people(room)

    cells = _map_cells(room)
    spots = cells.spots()
    earliest = [
        min(r.heat[k] for r in results) if ok else None for k, ok in enumerate(cells.open_floor)
    ]
    mean_harm = [
        round(statistics.mean(r.spot_harm[n] for r in results), 3) for n in range(len(spots))
    ]

    bed_id = people["asleep_alarm"].start_object_id
    bed = next((o for o in room.objects if o.id == bed_id), None)
    sleeper_cases = [c for s in sources for c in s.cases if c.person != "awake"]
    resting = (
        [
            RestingPlace(
                object_id=bed.id,
                label=bed.label,
                harm=round(statistics.mean(c.harm for c in sleeper_cases), 3),
            )
        ]
        if bed is not None
        else []
    )

    _, route, squeezes = _clear_route(room, people["asleep_alarm"].start)
    factors = _layout_factors(room, sources, route, squeezes)
    factors += _comparison_factors(sources, door_flipped, door_was_open)
    main_items, high_risk_items = _plan_items(room, sources, factors)

    all_cases = [c for s in sources for c in s.cases]
    return RiskReport(
        fires=len(results) + len(door_flipped),
        runs=sum(r.runs for r in results),
        harmful_cases=sum(c.harm >= HARMFUL_DOSE for c in all_cases),
        total_cases=len(all_cases),
        heat=cells.floor_map(earliest),
        heat_limit_s=FIRE_DURATION,
        danger=cells.floor_map(cells.spread(spots, mean_harm)),
        danger_delays_s=list(SPOT_DELAYS),
        resting=resting,
        route=[Point2(x=round(x, 2), y=round(y, 2)) for x, y in route],
        main_items=main_items,
        high_risk_items=high_risk_items,
        sources=sorted(sources, key=lambda s: -s.harm),
        factors=sorted(factors, key=lambda f: -f.severity),
    )
