"""One occupant escaping the room during a fire.

The person becomes aware of the fire, takes a moment to react, then heads for a door over a
floor grid, staying under the smoke if they know to. With their head in thick smoke they
cannot see the way: they drift off course, walk into furniture and feel along it. Along the
way they accumulate a dose from toxic gas and from heat; a dose of 1 incapacitates them.
The behavioural numbers here are stated assumptions, not measurements of a real person.
"""

import math
import random
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from firesim.escape.dose import INCAPACITATION, asphyxiant_rate, heat_rate
from firesim.escape.grid import (
    Grid,
    build_grid,
    exit_cells,
    opening_segment,
    path_from,
    travel_costs,
)
from firesim.fire.fuels import FUELS
from firesim.fire.physics import (
    SIGMA,
    T_AMB,
    layer_emissivity,
    layer_view_factor,
    point_source_flux,
)
from firesim.fire.sim import FireEvent, FireRun
from firesim.schema import Opening, OpeningKind, Point2, Room

DT = 0.25  # s, while moving; also the spacing of the recorded samples
WAITING_DT = 1.0  # s, while the person is not moving (a multiple of DT)
REPLAN_EVERY = 1.0  # s

BODY_RADIUS = 0.2  # m, walking normally
SQUEEZE_RADIUS = 0.12  # m, turned sideways through a gap
SQUEEZE_SPEED_FACTOR = 0.6

Posture = Literal["lying", "upright", "crouch", "crawl", "collapsed"]
SPEED = {"upright": 1.2, "crouch": 0.9, "crawl": 0.7}  # m/s
HEAD_HEIGHT = {"upright": 1.6, "crouch": 1.0, "crawl": 0.4, "collapsed": 0.15}  # m
UPRIGHT_CLEARANCE = 1.7  # smoke must be above this to walk upright
CROUCH_CLEARANCE = 1.1
LYING_HEAD_ABOVE_SURFACE = 0.15
LYING_SURFACE_MAX = 0.75  # m; a bed's bounding box includes its headboard
MIN_SPEED = 0.2  # m/s, groping through thick smoke

ALARM_VISIBILITY = 12.0  # m; smoke at the ceiling dense enough to trigger a smoke alarm
NOTICE_FIRE_KW = 10.0  # an awake person in the room sees flames this size

# What wakes a sleeper when there is no alarm. The fire's own faint signals come first:
# each person either responds to a signal or sleeps through it, decided once per run from
# the seed, and wakes some seconds after it starts. Figures are for unimpaired adults in
# light sleep, from Bruck and Brennan (2001): crackling at 42 to 48 dBA woke 91%, mostly
# within 30 s; a faint flickering light woke 49%; smoke odour woke 59% after 45 to 205 s
# (other studies find under half, and almost none in deep sleep). Children, older people
# and anyone who has had alcohol or sleeping tablets are harder to wake than this.
# Each entry: chance of waking to it, shortest and longest delay in seconds.
WAKE_CUES = {
    "sound": (0.9, 5.0, 30.0),
    "light": (0.5, 10.0, 90.0),
    "smell": (0.5, 45.0, 205.0),
}
WAKE_TEXT = {
    "sound": "is woken by the sound of the fire",
    "light": "is woken by the flickering light of the fire",
    "smell": "is woken by the smell of smoke",
}
# When each signal starts. These thresholds are assumptions: no measurements relate the
# noise or light of a room fire to its size.
SOUND_FIRE_KW = 20.0  # flames in the room loud enough to hear from the bed
LIGHT_FIRE_KW = 5.0  # flames bright enough to flicker on the ceiling of a dark room
SMELL_VISIBILITY = 25.0  # m; thin smoke in the room, well before it is visible
# Failing those, smoke in the face or painful heat wakes anyone.
WAKE_SMOKE_VISIBILITY = 10.0  # m; smoke at the face thick enough to choke on
WAKE_FLUX = 2.5  # kW/m² on the skin: the threshold of pain

RESPONSE_AWAKE = 10.0  # s between noticing and moving
RESPONSE_ASLEEP = 30.0  # s to wake to an alarm or a faint signal, understand and get up
RESPONSE_ASLEEP_URGENT = 15.0  # s when woken by smoke in the face or by pain
DOOR_OPENING_TIME = 3.0  # s to open a shut door and go through

# Finding the way with the head in smoke. Below SEE_RANGE of visibility the person cannot
# see where they are heading, so their direction wanders; touching furniture re-orients
# someone who knows the room. People in smoke are observed to weave and follow walls, but
# the size of the error here is an assumption.
SEE_RANGE = 3.0  # m
HEADING_ERROR_KNOWN_ROOM = 25.0  # degrees of wander at zero visibility
HEADING_ERROR_UNKNOWN_ROOM = 50.0
HEADING_ERROR_MEMORY = 2.0  # s over which the wander changes direction
LOOKAHEAD_CELLS = 2  # how far along the route the person aims

HAZARD_FLUX = 2.5  # kW/m²; routes through more radiant heat than this cost more
HAZARD_SOURCE_KW = 5.0  # ignore smaller fires when planning the route
SMOKE_SLOWDOWN = 0.34  # m/s lost per metre of visibility below 3 m (after Fridolf et al.)


class PersonScenario(BaseModel):
    start: Point2
    start_object_id: str | None = None  # the bed or seat they are on, if any
    asleep: bool = False
    smoke_alarm: bool = True
    stays_low: bool = True  # gets under the smoke instead of walking through it
    knows_room: bool = True  # their own room, so they can find the door by memory and touch
    seed: int = 0  # for the random wander in smoke; the same seed repeats the same run
    response_delay_s: float | None = None  # overrides the default reaction time


class EscapeSummary(BaseModel):
    outcome: Literal["escaped", "incapacitated", "trapped", "unaware", "inside"]
    alarm_s: float | None
    aware_s: float | None
    moving_s: float | None
    escaped_s: float | None
    incapacitated_s: float | None
    incapacitated_by: Literal["toxic gas", "heat"] | None
    distance_m: float
    bumps: int  # times they walked into something they could not see
    blind_s: float  # seconds spent moving with the way out hidden by smoke
    fed_gas: float
    fed_heat: float


class EscapeRun(BaseModel):
    """The occupant's timeline, sampled every dt seconds."""

    dt: float
    x: list[float]
    y: list[float]
    z: list[float]
    heading_deg: list[float]
    posture: list[Posture]
    fed_gas: list[float]
    fed_heat: list[float]
    events: list[FireEvent]
    summary: EscapeSummary


@dataclass
class Source:
    x: float
    y: float
    top: float  # height of the top of the burning item
    diameter: float  # of a circle with the item's footprint area
    radiative_fraction: float
    hrr: list[float]


def at(series: list[float], t: float) -> float:
    i = min(int(t), len(series) - 1)
    j = min(i + 1, len(series) - 1)
    return series[i] + (series[j] - series[i]) * min(max(t - i, 0.0), 1.0)


def fire_flux(x: float, y: float, z: float, sources: list[Source], t: float) -> float:
    total = 0.0
    for s in sources:
        hrr = at(s.hrr, t)
        if hrr > 1.0:
            # Radiate from the middle of the flame (Heskestad flame height).
            flame = max(0.235 * hrr**0.4 - 1.02 * s.diameter, 0.0)
            centre = s.top + flame / 2.0
            distance = math.sqrt((x - s.x) ** 2 + (y - s.y) ** 2 + (z - centre) ** 2)
            total += point_source_flux(hrr, s.radiative_fraction, distance)
    return total


def fire_sources(room: Room, fire: FireRun) -> list[Source]:
    """Every item that burns in this fire, as a radiating source."""
    objects = {o.id: o for o in room.objects}
    sources = []
    for key, burn in fire.objects.items():
        obj = objects[key]
        sources.append(
            Source(
                x=obj.position.x,
                y=obj.position.y,
                top=obj.position.z + obj.size.height,
                diameter=math.sqrt(4.0 * obj.size.width * obj.size.depth / math.pi),
                radiative_fraction=FUELS[obj.fuel].radiative_fraction,
                hrr=burn.hrr_kw,
            )
        )
    return sources


def _route_weights(grid: Grid, sources: list[Source], t: float) -> list[float]:
    """Cost multiplier per cell: slower over clutter, and much dearer close to a fire."""
    active = [s for s in sources if at(s.hrr, t) > HAZARD_SOURCE_KW]
    weights = []
    for k, ok in enumerate(grid.walkable):
        if not ok:
            weights.append(math.inf)
            continue
        x, y = grid.centre(k)
        flux = fire_flux(x, y, 1.0, active, t)
        weights.append((1.0 + (flux / HAZARD_FLUX) ** 2) / grid.speed_factor[k])
    return weights


GridCache = dict[float, tuple[Grid, list[int]]]


class _Navigator:
    """Knows the way to the exits, first at normal body width and, failing that, sideways."""

    def __init__(
        self, room: Room, exits: list[Opening], sources: list[Source], grids: GridCache
    ) -> None:
        self.room, self.exits, self.sources = room, exits, sources
        self.squeezing = False
        self._grids = grids
        self._cost: list[float] = []

    def grid(self) -> Grid:
        return self._load(SQUEEZE_RADIUS if self.squeezing else BODY_RADIUS)[0]

    def _load(self, radius: float) -> tuple[Grid, list[int]]:
        if radius not in self._grids:
            grid = build_grid(self.room, radius)
            self._grids[radius] = (grid, exit_cells(grid, self.room, self.exits))
        return self._grids[radius]

    def plan(self, x: float, y: float, t: float) -> bool:
        """Work out the cost of reaching an exit from everywhere. False if (x, y) cannot."""
        for radius in [SQUEEZE_RADIUS] if self.squeezing else [BODY_RADIUS, SQUEEZE_RADIUS]:
            grid, goals = self._load(radius)
            start = grid.cell_at(x, y)
            if start is None or not goals:
                continue
            cost = travel_costs(grid, goals, _route_weights(grid, self.sources, t))
            if math.isfinite(cost[start]):
                self.squeezing = radius == SQUEEZE_RADIUS
                self._cost = cost
                return True
        return False

    def aim(self, x: float, y: float) -> tuple[float, float] | None:
        """A point a short way along the cheapest route from (x, y)."""
        grid = self.grid()
        start = grid.cell_at(x, y)
        if start is None or not math.isfinite(self._cost[start]):
            return None
        path = path_from(grid, self._cost, start)
        return grid.centre(path[min(LOOKAHEAD_CELLS, len(path) - 1)])

    def at_exit(self, x: float, y: float) -> bool:
        k = self.grid().cell_index(x, y)
        return k is not None and self.grid().walkable[k] and self._cost[k] == 0.0


def _nearest_exit(room: Room, exits: list[Opening], x: float, y: float) -> Opening:
    def distance(o: Opening) -> float:
        x1, y1, x2, y2 = opening_segment(room, o)
        return math.hypot(x - (x1 + x2) / 2, y - (y1 + y2) / 2)

    return min(exits, key=distance)


def run_escape(
    room: Room, fire: FireRun, person: PersonScenario, grids: GridCache | None = None
) -> EscapeRun:
    """grids lets many runs in the same room share the walkable grids."""
    objects = {o.id: o for o in room.objects}
    sources = fire_sources(room, fire)
    xs, ys = [p.x for p in room.floor], [p.y for p in room.floor]
    bounds = (min(xs), min(ys), max(xs), max(ys))
    states = {o.id: fire.scenario.opening_states.get(o.id, o.is_open) for o in room.openings}
    exits = [o for o in room.openings if o.leads_outside_room and o.kind is not OpeningKind.WINDOW]

    # Starting pose: lying on the chosen bed or seat if asleep, otherwise standing beside it.
    x, y, z = person.start.x, person.start.y, 0.0
    heading = 0.0
    posture: Posture = "upright"
    on = objects.get(person.start_object_id) if person.start_object_id else None
    if person.asleep:
        posture = "lying"
        if on is not None:
            z = min(on.position.z + on.size.height, LYING_SURFACE_MAX)
            heading = on.rotation_deg + (90.0 if on.size.depth > on.size.width else 0.0)

    navigator = _Navigator(room, exits, sources, {} if grids is None else grids)
    rng = random.Random(person.seed)
    # Which of the fire's faint signals this sleeper responds to, and how slowly.
    cue_delay: dict[str, float] = {}
    for cue, (chance, shortest, longest) in WAKE_CUES.items():
        responds, delay = rng.random() < chance, rng.uniform(shortest, longest)
        if person.asleep and responds:
            cue_delay[cue] = delay
    cue_started: dict[str, float] = {}
    wander_limit = math.radians(
        HEADING_ERROR_KNOWN_ROOM if person.knows_room else HEADING_ERROR_UNKNOWN_ROOM
    )
    wander = 0.0  # current heading error, radians
    bumps = 0
    blind_s = 0.0
    if not person.asleep:
        # An awake person stands on clear floor next to the chosen spot, not inside furniture.
        clear = navigator.grid().nearest_walkable(x, y)
        if clear is not None:
            x, y = navigator.grid().centre(clear)
    next_replan = 0.0

    events: list[FireEvent] = []
    out: dict[str, list] = {k: [] for k in ("x", "y", "z", "heading", "posture", "gas", "heat")}
    fed_gas = fed_heat = distance = 0.0
    alarm_s = aware_s = moving_s = escaped_s = incapacitated_s = None
    incapacitated_by = None
    respond_at = door_done_at = math.inf
    state = "unaware"  # unaware, reacting, moving, at_door, trapped, escaped, collapsed

    def say(t: float, text: str) -> None:
        events.append(FireEvent(t=round(t, 2), kind="person", text=text))

    end = fire.t[-1]
    burning_until = max((i for i, kw in enumerate(fire.hrr_kw) if kw > 0.5), default=0)
    t = 0.0
    while True:
        dt = WAITING_DT if state in ("unaware", "reacting", "trapped") else DT
        interface = at(fire.layer_height_m, t)
        temp_c = at(fire.upper_temp_c, t)
        visibility = at(fire.visibility_m, t)

        if state == "moving" and person.stays_low:
            posture = (
                "upright"
                if interface >= UPRIGHT_CLEARANCE
                else "crouch"
                if interface >= CROUCH_CLEARANCE
                else "crawl"
            )
        head = z + (LYING_HEAD_ABOVE_SURFACE if posture == "lying" else HEAD_HEIGHT[posture])
        in_smoke = head >= interface

        # Exposure at the current position.
        layer_k = temp_c + 273.15
        emissive = (
            SIGMA * (layer_k**4 - T_AMB**4) * layer_emissivity(room.ceiling_height - interface)
        )
        if not in_smoke:
            emissive *= layer_view_factor(bounds, x, y, interface - head)
        flux = emissive + fire_flux(x, y, (z + head) / 2.0, sources, t)
        if state != "escaped":
            if in_smoke:
                fed_gas += dt * asphyxiant_rate(
                    at(fire.co_ppm, t), at(fire.co2_pct, t), at(fire.o2_pct, t)
                )
            fed_heat += dt * heat_rate(flux, temp_c if in_smoke else 20.0)

        for _ in range(round(dt / DT)):  # keep the recorded samples DT apart
            out["x"].append(round(x, 3))
            out["y"].append(round(y, 3))
            out["z"].append(round(z, 3))
            out["heading"].append(round(heading, 1))
            out["posture"].append(posture)
            out["gas"].append(round(fed_gas, 4))
            out["heat"].append(round(fed_heat, 4))

        if state in ("escaped", "collapsed") or t >= end:
            break
        if state == "unaware" and t > burning_until + 60 and temp_c < 40.0:
            break  # the fire burnt out without the person ever noticing; nothing more happens

        if max(fed_gas, fed_heat) >= INCAPACITATION:
            incapacitated_s = t
            incapacitated_by = "heat" if fed_heat >= fed_gas else "toxic gas"
            state, posture, z = "collapsed", "collapsed", 0.0 if moving_s is not None else z
            say(t, f"The person is overcome by {incapacitated_by} and collapses.")
            t += dt
            continue

        if person.smoke_alarm and alarm_s is None and visibility <= ALARM_VISIBILITY:
            alarm_s = t
            say(t, "The smoke alarm sounds.")

        if state == "unaware":
            hrr = at(fire.hrr_kw, t)
            present = {
                "sound": hrr >= SOUND_FIRE_KW,
                "light": hrr >= LIGHT_FIRE_KW,
                "smell": visibility <= SMELL_VISIBILITY,
            }
            for cue, on in present.items():
                if on:
                    cue_started.setdefault(cue, t)
            woken_by = next(
                (c for c, delay in cue_delay.items() if t >= cue_started.get(c, math.inf) + delay),
                None,
            )

            reason, default = None, RESPONSE_ASLEEP
            if alarm_s is not None:
                reason = "wakes to the smoke alarm" if person.asleep else "hears the smoke alarm"
            elif not person.asleep and hrr >= NOTICE_FIRE_KW:
                reason = "notices the fire"
            elif woken_by is not None:
                reason = WAKE_TEXT[woken_by]
            elif person.asleep and in_smoke and visibility < WAKE_SMOKE_VISIBILITY:
                reason, default = "is woken by smoke in their face", RESPONSE_ASLEEP_URGENT
            elif person.asleep and flux >= WAKE_FLUX:
                reason, default = "is woken by the heat", RESPONSE_ASLEEP_URGENT
            if reason:
                aware_s = t
                if not person.asleep:
                    default = RESPONSE_AWAKE
                delay = default if person.response_delay_s is None else person.response_delay_s
                respond_at = t + delay
                state = "reacting"
                say(t, f"The person {reason}.")

        if state == "reacting" and person.asleep and person.response_delay_s is None:
            # Smoke in the face or painful heat hurries up someone who is already awake.
            urgent = (in_smoke and visibility < WAKE_SMOKE_VISIBILITY) or flux >= WAKE_FLUX
            if urgent:
                respond_at = min(respond_at, max(t, aware_s + RESPONSE_ASLEEP_URGENT))

        if state == "reacting" and t >= respond_at:
            if not exits:
                state = "trapped"
                say(t, "There is no door to leave by.")
            elif navigator.plan(x, y, t):
                state, moving_s, next_replan = "moving", t, t + REPLAN_EVERY
                posture, z = "upright", 0.0
                say(t, "The person starts moving towards the door.")
                if navigator.squeezing:
                    say(t, "The way out is too narrow to walk normally; they squeeze through.")
            else:
                state = "trapped"
                say(t, "Furniture or clutter blocks every route to the door.")

        elif state == "moving":
            if t >= next_replan:
                # The fire has moved on, so the cheapest route may have changed.
                navigator.plan(x, y, t)
                next_replan = t + REPLAN_EVERY
            grid = navigator.grid()
            here = grid.cell_index(x, y)
            on_floor = here is not None and grid.walkable[here]  # not still on the bed
            speed = SPEED[posture] * (grid.speed_factor[here] if on_floor else 1.0)
            if navigator.squeezing:
                speed *= SQUEEZE_SPEED_FACTOR
            if in_smoke and visibility < 3.0:
                speed = max(MIN_SPEED, speed - SMOKE_SLOWDOWN * (3.0 - visibility))

            # How hidden the way is: 0 with a clear view, 1 in smoke too thick to see through.
            blind = min(max(1.0 - visibility / SEE_RANGE, 0.0), 1.0) if in_smoke else 0.0
            if blind > 0:
                pull = DT / HEADING_ERROR_MEMORY
                wander += -wander * pull + wander_limit * blind * math.sqrt(2 * pull) * rng.gauss(
                    0.0, 1.0
                )
                if blind >= 0.5:
                    blind_s += DT
                    if blind_s == DT:
                        say(t, "Smoke hides the way out; the person has to feel their way.")
            else:
                wander = 0.0

            target = navigator.aim(x, y)
            if target is not None:
                gap = math.hypot(target[0] - x, target[1] - y)
                direction = math.atan2(target[1] - y, target[0] - x) + wander
                stride = speed * DT if blind > 0 else min(speed * DT, gap)
                dx, dy = stride * math.cos(direction), stride * math.sin(direction)
                moves = [(dx, dy)]
                if on_floor:
                    # If the way is blocked, slide along the obstacle in whichever
                    # direction keeps more of the intended movement.
                    moves += sorted([(dx, 0.0), (0.0, dy)], key=lambda m: -abs(m[0] + m[1]))
                blocked = 0
                for mx, my in moves:
                    k = grid.cell_index(x + mx, y + my)
                    if not on_floor or (k is not None and grid.walkable[k]):
                        if mx or my:
                            heading = math.degrees(math.atan2(my, mx))
                        x, y, distance = x + mx, y + my, distance + math.hypot(mx, my)
                        break
                    blocked += 1
                if blocked and blind > 0:
                    # Walked into something unseen. Touching it tells someone who knows
                    # the room where they are.
                    bumps += 1
                    if person.knows_room:
                        wander = 0.0

            if navigator.at_exit(x, y):
                door = _nearest_exit(room, exits, x, y)
                if states[door.id] or door.kind is OpeningKind.OPEN_PASSAGE:
                    state, escaped_s = "escaped", t + DT
                    say(t + DT, "The person gets out of the room.")
                else:
                    state, door_done_at = "at_door", t + DOOR_OPENING_TIME
                    say(t, "The person reaches the closed door and opens it.")
        elif state == "at_door" and t >= door_done_at:
            state, escaped_s = "escaped", t
            say(t, "The person gets out of the room and shuts the door behind them.")

        t += dt

    outcome = {
        "escaped": "escaped",
        "collapsed": "incapacitated",
        "trapped": "trapped",
        "unaware": "unaware",
    }.get(state, "inside")
    return EscapeRun(
        dt=DT,
        x=out["x"],
        y=out["y"],
        z=out["z"],
        heading_deg=out["heading"],
        posture=out["posture"],
        fed_gas=out["gas"],
        fed_heat=out["heat"],
        events=events,
        summary=EscapeSummary(
            outcome=outcome,
            alarm_s=alarm_s,
            aware_s=aware_s,
            moving_s=moving_s,
            escaped_s=escaped_s,
            incapacitated_s=incapacitated_s,
            incapacitated_by=incapacitated_by,
            distance_m=round(distance, 2),
            bumps=bumps,
            blind_s=round(blind_s, 2),
            fed_gas=round(fed_gas, 3),
            fed_heat=round(fed_heat, 3),
        ),
    )
