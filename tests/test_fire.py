import math
from pathlib import Path

import pytest

from firesim.fire.fuels import FUELS, MIN_BURN_TIME, burn_curve
from firesim.fire.physics import (
    RHO_AMB,
    T_AMB,
    Vent,
    layer_view_factor,
    plume_entrainment,
    solve_vents,
)
from firesim.fire.sim import FireScenario, run_fire
from firesim.fire.zone import WALL_THERMAL_INERTIA, SmokeLayer
from firesim.schema import FuelClass, Room

FIXTURE = Path(__file__).parent / "fixtures" / "sample_room.json"


@pytest.fixture
def room() -> Room:
    return Room.model_validate_json(FIXTURE.read_text())


def test_burn_curve_releases_the_items_energy(room: Room) -> None:
    bed = next(o for o in room.objects if o.id == "bed")
    curve = burn_curve(bed, FUELS[bed.fuel])
    step = 0.5
    released = sum(curve.demand(i * step) * step for i in range(int(curve.decay_end / step) + 2))
    assert released == pytest.approx(curve.energy_kj, rel=0.01)
    assert curve.demand(curve.growth_end / 2) == pytest.approx(curve.peak_kw / 4, rel=0.01)


def test_thin_item_peak_is_limited_by_its_fuel(room: Room) -> None:
    rug = next(o for o in room.objects if o.id == "rug")
    props = FUELS[rug.fuel]
    curve = burn_curve(rug, props)
    w, d, h = rug.size.width, rug.size.depth, rug.size.height
    assert curve.peak_kw < props.hrrpua * (w * d + 2 * (w + d) * h)
    assert curve.peak_kw == pytest.approx(curve.energy_kj / MIN_BURN_TIME)


def test_plume_regimes_join_up() -> None:
    q = 500.0
    for boundary in (0.08, 0.20):
        z = boundary * q**0.4
        below, above = plume_entrainment(q, z * 0.999), plume_entrainment(q, z * 1.001)
        assert below == pytest.approx(above, rel=0.05)


def test_view_factor_matches_known_values() -> None:
    # Point under the centre of a very large layer sees nothing else.
    assert layer_view_factor((-500, -500, 500, 500), 0, 0, 1.0) == pytest.approx(1.0, abs=0.01)
    # Point under the corner of a unit square at unit distance: 0.1385 (standard tables).
    assert layer_view_factor((0, 0, 1, 1), 0, 0, 1.0) == pytest.approx(0.1385, abs=0.001)


def test_vent_flow_is_zero_without_heat_and_balanced_with_it() -> None:
    strips = Vent(0.85, 0.0, 2.05).strips()
    cold = solve_vents(strips, 1.0, RHO_AMB, 0.0)
    assert cold.out_upper + cold.out_lower + cold.inflow == pytest.approx(0.0, abs=2e-3)

    expansion = 0.5
    hot = solve_vents(strips, 1.0, RHO_AMB * T_AMB / 600.0, expansion)
    assert hot.out_upper > 0 and hot.inflow > 0
    assert hot.out_upper + hot.out_lower - hot.inflow == pytest.approx(expansion, abs=1e-3)


def test_layer_temperature_agrees_with_mqh_correlation() -> None:
    """A steady 500 kW fire with the door open should land near the McCaffrey, Quintiere
    and Harkleroad correlation for upper layer temperature rise."""
    q, dt, end = 500.0, 0.25, 300.0
    area, height, perimeter = 3.6 * 4.2, 2.6, 2 * (3.6 + 4.2)
    door_w, door_h = 0.85, 2.05
    strips = Vent(door_w, 0.0, door_h).strips()
    layer = SmokeLayer(area, height, perimeter)
    for step in range(int(end / dt)):
        entrained = min(plume_entrainment(q, layer.interface), layer.lower_air_supply(dt))
        layer.advance(step * dt, dt, strips, entrained=entrained, q_total=q, q_convective=0.65 * q)

    h_k = math.sqrt(WALL_THERMAL_INERTIA / end)
    surfaces = 2 * area + perimeter * height - door_w * door_h
    vent_factor = door_w * door_h * math.sqrt(door_h)
    mqh_rise = 6.85 * (q * q / (vent_factor * h_k * surfaces)) ** (1 / 3)
    assert layer.temperature - T_AMB == pytest.approx(mqh_rise, rel=0.2)
    assert 0.0 < layer.interface < door_h


def test_open_door_bed_fire_spreads_and_reaches_flashover(room: Room) -> None:
    run = run_fire(room, FireScenario(ignition_object_id="bed"))
    assert run.summary.flashover_s is not None
    assert 120 <= run.summary.flashover_s <= 480
    assert run.summary.items_burned > 5
    assert run.summary.smoke_at_head_height_s < run.summary.flashover_s
    assert run.objects["curtains"].cause.startswith("radiant heat from the burning queen bed")
    assert len(run.t) == len(run.hrr_kw) == len(run.objects["curtains"].hrr_kw) == 601


def test_closed_door_starves_the_fire_of_oxygen(room: Room) -> None:
    opened = run_fire(room, FireScenario(ignition_object_id="bed"))
    closed = run_fire(
        room, FireScenario(ignition_object_id="bed", opening_states={"door-1": False})
    )
    assert closed.summary.flashover_s is None
    assert closed.summary.peak_hrr_kw < 0.5 * opened.summary.peak_hrr_kw
    assert min(closed.o2_pct) < 15.0
    assert any(e.kind == "oxygen" for e in closed.events)


def test_small_fire_away_from_fuel_burns_out(room: Room) -> None:
    run = run_fire(room, FireScenario(ignition_object_id="power-strip"))
    assert run.summary.items_burned == 1
    assert run.hrr_kw[-1] == 0.0
    assert any(e.kind == "burnout" for e in run.events)


def test_rejects_unknown_or_non_combustible_start(room: Room) -> None:
    with pytest.raises(ValueError, match="No object"):
        run_fire(room, FireScenario(ignition_object_id="nope"))
    room.objects[0].fuel = FuelClass.NON_COMBUSTIBLE
    with pytest.raises(ValueError, match="not combustible"):
        run_fire(room, FireScenario(ignition_object_id=room.objects[0].id))
