"""Single-room, two-zone fire simulation.

The room is treated as a hot smoke layer over a layer of clean air. Each burning item
follows a t² heat release curve, feeds a plume that carries air into the smoke layer, and
radiates onto its neighbours, which ignite once the heat flux on them passes their
threshold. Everything here is deterministic.
"""

from dataclasses import dataclass

from pydantic import BaseModel, Field

from firesim.fire.fuels import FUELS, BurnCurve, FuelProps, burn_curve
from firesim.fire.physics import (
    HEAT_PER_KG_O2,
    O2_AMB,
    SIGMA,
    T_AMB,
    Vent,
    distance_to_object,
    layer_emissivity,
    layer_view_factor,
    oxygen_limit_factor,
    plume_entrainment,
    point_source_flux,
)
from firesim.fire.zone import SmokeLayer
from firesim.schema import OpeningKind, Room, RoomObject, floor_area, wall_length

DT = 0.25  # s, integration step
STEPS_PER_OUTPUT = 4  # one timeline sample per second

HEAD_HEIGHT = 1.5  # m
TARGET_CONVECTION = 0.02  # kW/m²/K on an item immersed in smoke
FLASHOVER_K = 873.15
WINDOW_BREAK_K = 573.15
HOT_LAYER_K = 473.15  # about 2.5 kW/m² of radiant heat at floor level
UNDERVENTILATED_CO_YIELD = 0.2  # kg/kg
CLOSED_DOOR_GAP = 0.02  # m, undercut and frame gaps of a shut door


class FireScenario(BaseModel):
    ignition_object_id: str
    opening_states: dict[str, bool] = Field(
        default_factory=dict, description="Opening id to open (true) or closed (false)"
    )
    duration_s: float = Field(default=600.0, ge=30.0, le=1800.0)


class FireEvent(BaseModel):
    t: float
    kind: str
    text: str
    object_id: str | None = None


class ObjectBurn(BaseModel):
    ignited_at: float
    cause: str
    peak_kw: float
    energy_kj: float
    hrr_kw: list[float]


class FireSummary(BaseModel):
    peak_hrr_kw: float
    max_upper_temp_c: float
    min_layer_height_m: float
    items_burned: int
    smoke_at_head_height_s: float | None
    visibility_below_5m_s: float | None
    layer_above_200c_s: float | None
    flashover_s: float | None


class FireRun(BaseModel):
    """Timeline of one fire. Every list is sampled once per second and aligned with t."""

    scenario: FireScenario
    t: list[float]
    hrr_kw: list[float]
    layer_height_m: list[float]
    upper_temp_c: list[float]
    visibility_m: list[float]
    co_ppm: list[float]
    co2_pct: list[float]
    o2_pct: list[float]
    objects: dict[str, ObjectBurn]
    events: list[FireEvent]
    summary: FireSummary


@dataclass
class _Burning:
    obj: RoomObject
    props: FuelProps
    curve: BurnCurve
    base_z: float
    tau: float = 0.0
    hrr: float = 0.0
    entrained: float = 0.0


@dataclass
class _Combustion:
    q_total: float = 0.0
    q_demand: float = 0.0
    q_convective: float = 0.0
    entrained: float = 0.0
    fuel_rate: float = 0.0
    co_rate: float = 0.0
    soot_rate: float = 0.0


def _name(obj: RoomObject) -> str:
    """Label for use mid-sentence."""
    return obj.label[:1].lower() + obj.label[1:]


def _vent_strips(
    room: Room, states: dict[str, bool], broken: set[str]
) -> list[tuple[float, float]]:
    vents: list[Vent] = []
    for o in room.openings:
        is_open = states[o.id] or o.id in broken or o.kind is OpeningKind.OPEN_PASSAGE
        if is_open:
            vents.append(Vent(o.width, o.sill_height, o.sill_height + o.height))
        elif o.kind is OpeningKind.DOOR:
            vents.append(Vent(o.width, 0.0, CLOSED_DOOR_GAP))
    if not vents:
        vents.append(Vent(1.0, 0.0, 0.01))  # no room is airtight
    return [strip for vent in vents for strip in vent.strips()]


def _burn(burning: list[_Burning], layer: SmokeLayer) -> _Combustion:
    """Burn each item for one step, as far as the oxygen its plume can reach allows."""
    interface, y_o2 = layer.interface, layer.o2_fraction
    demands = [b.curve.demand(b.tau) for b in burning]
    for b, demand in zip(burning, demands, strict=True):
        b.entrained = plume_entrainment(demand, max(interface - b.base_z, 0.0))
    wanted = sum(b.entrained for b in burning)
    share = min(1.0, layer.lower_air_supply(DT) / wanted) if wanted > 0 else 1.0

    total = _Combustion(q_demand=sum(demands))
    for b, demand in zip(burning, demands, strict=True):
        if demand <= 0:
            b.hrr = 0.0
            b.tau += DT
            continue
        b.entrained *= share
        q_low = min(demand, b.entrained * O2_AMB * HEAT_PER_KG_O2)
        q_high = 0.0
        if demand - q_low > 1e-9:
            # The rest of the flame is in the smoke layer and burns its leftover oxygen.
            reach = layer.height - max(interface, b.base_z)
            drawn = plume_entrainment(demand - q_low, reach)
            usable = drawn * y_o2 * HEAT_PER_KG_O2 * oxygen_limit_factor(y_o2)
            q_high = min(demand - q_low, usable)
        b.hrr = q_low + q_high
        fed = b.hrr / demand
        b.tau += DT * fed  # a starved fire burns through its fuel more slowly

        burnt = b.hrr / (b.props.heat_of_combustion * 1000.0)  # kg/s of fuel
        co_yield = b.props.co_yield + (UNDERVENTILATED_CO_YIELD - b.props.co_yield) * (1.0 - fed)
        total.q_total += b.hrr
        total.q_convective += (1.0 - b.props.radiative_fraction) * b.hrr
        total.entrained += b.entrained
        total.fuel_rate += burnt
        total.co_rate += co_yield * burnt
        total.soot_rate += b.props.soot_yield * burnt
    return total


def _heat_flux_on(
    obj: RoomObject,
    sources: list[_Burning],
    layer: SmokeLayer,
    bounds: tuple[float, float, float, float],
) -> tuple[float, str]:
    """Incident heat flux on an item (kW/m²) and what contributes most of it."""
    interface, temperature = layer.interface, layer.temperature
    emissive = SIGMA * (temperature**4 - T_AMB**4) * layer_emissivity(layer.height - interface)
    top = obj.position.z + obj.size.height
    if top >= interface:
        from_layer = emissive + TARGET_CONVECTION * (temperature - T_AMB)
    else:
        gap = interface - top
        from_layer = emissive * layer_view_factor(bounds, obj.position.x, obj.position.y, gap)

    flux, strongest, cause = from_layer, from_layer, "heat from the smoke layer"
    for b in sources:
        src = b.obj
        flame_z = src.position.z + src.size.height
        distance = distance_to_object(src.position.x, src.position.y, flame_z, obj)
        part = point_source_flux(b.hrr, b.props.radiative_fraction, distance)
        flux += part
        if part > strongest:
            strongest, cause = part, f"radiant heat from the burning {_name(src)}"
    return flux, cause


def run_fire(room: Room, scenario: FireScenario) -> FireRun:
    objects = {o.id: o for o in room.objects}
    first = objects.get(scenario.ignition_object_id)
    if first is None:
        raise ValueError(f"No object with id '{scenario.ignition_object_id}' in this room.")
    if FUELS[first.fuel] is None:
        raise ValueError(f"'{first.label}' is not combustible, so a fire cannot start there.")

    perimeter = sum(wall_length(room, i) for i in range(len(room.floor)))
    layer = SmokeLayer(abs(floor_area(room)), room.ceiling_height, perimeter)
    xs, ys = [p.x for p in room.floor], [p.y for p in room.floor]
    bounds = (min(xs), min(ys), max(xs), max(ys))

    states = {o.id: scenario.opening_states.get(o.id, o.is_open) for o in room.openings}
    broken: set[str] = set()
    strips = _vent_strips(room, states, broken)

    burning: dict[str, _Burning] = {}
    burns: dict[str, ObjectBurn] = {}
    events: list[FireEvent] = []
    reached: dict[str, float] = {}
    names = ("t", "hrr", "layer", "temp", "vis", "co", "co2", "o2")
    series: dict[str, list[float]] = {name: [] for name in names}

    def ignite(obj: RoomObject, t: float, cause: str) -> None:
        props = FUELS[obj.fuel]
        assert props is not None
        curve = burn_curve(obj, props)
        base_z = min(obj.position.z + 0.5 * obj.size.height, layer.height - 0.1)
        burning[obj.id] = _Burning(obj, props, curve, base_z)
        burns[obj.id] = ObjectBurn(
            ignited_at=t,
            cause=cause,
            peak_kw=round(curve.peak_kw, 1),
            energy_kj=round(curve.energy_kj),
            hrr_kw=[0.0] * len(series["t"]),
        )
        text = (
            f"Fire starts at the {_name(obj)}."
            if not events
            else f"Fire spreads to the {_name(obj)}: {cause}."
        )
        events.append(FireEvent(t=t, kind="ignition", text=text, object_id=obj.id))

    def mark(key: str, t: float, kind: str, text: str) -> None:
        if key not in reached:
            reached[key] = t
            events.append(FireEvent(t=t, kind=kind, text=text))

    ignite(first, 0.0, "starting point of the fire")

    for step in range(int(scenario.duration_s / DT) + 1):
        t = step * DT
        fire = _burn(list(burning.values()), layer)

        if step % STEPS_PER_OUTPUT == 0:
            interface, temperature = layer.interface, layer.temperature
            series["t"].append(round(t, 2))
            series["hrr"].append(round(fire.q_total, 1))
            series["layer"].append(round(interface, 3))
            series["temp"].append(round(temperature - 273.15, 1))
            series["vis"].append(round(layer.visibility, 2))
            series["co"].append(round(layer.co_ppm))
            series["co2"].append(round(layer.co2_pct, 2))
            series["o2"].append(round(layer.o2_pct, 2))
            for key, burn in burns.items():
                burn.hrr_kw.append(round(burning[key].hrr, 1) if key in burning else 0.0)

            if interface < HEAD_HEIGHT:
                mark("head", t, "smoke", "Smoke layer drops below head height (1.5 m).")
            if layer.visibility < 5.0:
                mark("visibility", t, "smoke", "Visibility inside the smoke falls below 5 m.")
            if temperature >= HOT_LAYER_K:
                text = "Smoke layer passes 200 °C: heat from above becomes painful."
                mark("hot", t, "heat", text)
            if fire.q_demand > 5.0 and fire.q_total < 0.8 * fire.q_demand:
                mark("oxygen", t, "oxygen", "The fire is now limited by lack of oxygen.")

            for o in room.openings:
                closed_window = (
                    o.kind is OpeningKind.WINDOW and not states[o.id] and o.id not in broken
                )
                in_smoke = interface < o.sill_height + o.height
                if closed_window and in_smoke and temperature >= WINDOW_BREAK_K:
                    broken.add(o.id)
                    strips = _vent_strips(room, states, broken)
                    text = "A window breaks from the heat."
                    events.append(FireEvent(t=t, kind="window", text=text))

            flashover = temperature >= FLASHOVER_K
            if flashover:
                mark("flashover", t, "flashover", "Flashover: the whole room is on fire.")
            sources = [b for b in burning.values() if b.hrr > 1.0]
            for obj in room.objects:
                props = FUELS[obj.fuel]
                if props is None or obj.id in burns:
                    continue
                flux, cause = _heat_flux_on(obj, sources, layer, bounds)
                if flashover:
                    ignite(obj, t, "flashover")
                elif flux >= props.ignition_flux:
                    ignite(obj, t, cause)

        layer.advance(
            t,
            DT,
            strips,
            entrained=fire.entrained,
            q_total=fire.q_total,
            q_convective=fire.q_convective,
            fuel_rate=fire.fuel_rate,
            co_rate=fire.co_rate,
            soot_rate=fire.soot_rate,
        )

        for key in [k for k, b in burning.items() if b.tau >= b.curve.decay_end]:
            del burning[key]
        if not burning:
            mark("out", t, "burnout", "The fire has burnt out.")

    return FireRun(
        scenario=scenario,
        t=series["t"],
        hrr_kw=series["hrr"],
        layer_height_m=series["layer"],
        upper_temp_c=series["temp"],
        visibility_m=series["vis"],
        co_ppm=series["co"],
        co2_pct=series["co2"],
        o2_pct=series["o2"],
        objects=burns,
        events=sorted(events, key=lambda e: e.t),
        summary=FireSummary(
            peak_hrr_kw=max(series["hrr"]),
            max_upper_temp_c=max(series["temp"]),
            min_layer_height_m=min(series["layer"]),
            items_burned=len(burns),
            smoke_at_head_height_s=reached.get("head"),
            visibility_below_5m_s=reached.get("visibility"),
            layer_above_200c_s=reached.get("hot"),
            flashover_s=reached.get("flashover"),
        ),
    )
