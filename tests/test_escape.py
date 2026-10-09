import math
from pathlib import Path

import pytest

from firesim.escape.dose import asphyxiant_rate, heat_rate
from firesim.escape.grid import build_grid, exit_cells, path_from, travel_costs
from firesim.escape.sim import BODY_RADIUS, PersonScenario, run_escape
from firesim.fire.sim import FireRun, FireScenario, run_fire
from firesim.schema import Point2, Room

FIXTURE = Path(__file__).parent / "fixtures" / "sample_room.json"
IN_BED = {"start": Point2(x=2.85, y=3.0), "start_object_id": "bed", "asleep": True}


@pytest.fixture
def room() -> Room:
    return Room.model_validate_json(FIXTURE.read_text())


@pytest.fixture
def curtain_fire(room: Room) -> FireRun:
    return run_fire(room, FireScenario(ignition_object_id="curtains", duration_s=300))


def test_dose_rates_match_reference_points() -> None:
    # 3,500 ppm of CO for ten minutes is one incapacitating dose (ISO 13571).
    assert asphyxiant_rate(3500, 0.0, 20.9) * 600 == pytest.approx(1.0)
    # CO2 at 5% multiplies uptake by e.
    assert asphyxiant_rate(3500, 5.0, 20.9) == pytest.approx(
        math.e * asphyxiant_rate(3500, 0, 20.9)
    )
    # 10 kW/m² burns in roughly eleven seconds; 2.5 kW/m² or less is tolerable.
    assert 1.0 / heat_rate(10.0, 20.0) == pytest.approx(11.4, abs=0.5)
    assert heat_rate(2.5, 20.0) == 0.0
    # Air at 200 °C incapacitates in under a minute.
    assert 30 < 1.0 / heat_rate(0.0, 200.0) < 60


def test_grid_blocks_furniture_and_finds_the_door(room: Room) -> None:
    grid = build_grid(room, BODY_RADIUS)
    exits = [o for o in room.openings if o.id == "door-1"]
    goals = exit_cells(grid, room, exits)
    assert goals
    assert grid.cell_at(2.85, 3.0) != int(3.0 / 0.1) * grid.nx + int(2.85 / 0.1)  # on the bed
    beside_bed = grid.cell_at(1.6, 2.5)
    cost = travel_costs(grid, goals, [1.0] * len(grid.walkable))
    path = path_from(grid, cost, beside_bed)
    assert path[-1] in goals
    assert all(grid.walkable[k] for k in path)
    # Straight-line distance to the door is about 2.7 m; the route cannot be shorter.
    assert 2.5 < cost[beside_bed] < 4.0


def test_sleeper_with_alarm_escapes_unharmed(room: Room, curtain_fire: FireRun) -> None:
    run = run_escape(room, curtain_fire, PersonScenario(**IN_BED))
    s = run.summary
    assert s.outcome == "escaped"
    assert s.alarm_s == s.aware_s < s.moving_s < s.escaped_s
    assert s.moving_s - s.aware_s == pytest.approx(30.0)
    assert max(s.fed_gas, s.fed_heat) < 0.3
    assert run.posture[0] == "lying" and run.z[0] == pytest.approx(0.75)
    assert run.z[-1] == 0.0
    assert len(run.x) == len(run.posture) == len(run.fed_heat)


def test_no_alarm_delays_the_sleeper(room: Room, curtain_fire: FireRun) -> None:
    with_alarm = run_escape(room, curtain_fire, PersonScenario(**IN_BED)).summary
    without = run_escape(room, curtain_fire, PersonScenario(**IN_BED, smoke_alarm=False)).summary
    assert without.alarm_s is None
    assert without.aware_s > with_alarm.aware_s
    assert without.fed_heat >= with_alarm.fed_heat


def test_closed_door_takes_time_to_open(room: Room) -> None:
    fire = run_fire(
        room,
        FireScenario(
            ignition_object_id="wardrobe", opening_states={"door-1": False}, duration_s=120
        ),
    )
    run = run_escape(room, fire, PersonScenario(start=Point2(x=2.65, y=0.9)))
    assert run.summary.outcome == "escaped"
    assert any("opens it" in e.text for e in run.events)


def test_blocked_door_traps_the_person(room: Room, curtain_fire: FireRun) -> None:
    blocker = room.objects[1].model_copy(deep=True)  # the wardrobe
    blocker.id, blocker.label = "blocker", "wardrobe moved in front of the door"
    blocker.position.x, blocker.position.y, blocker.rotation_deg = 0.72, 0.4, 0
    blocker.size.width = 1.6
    room.objects.append(blocker)
    run = run_escape(room, curtain_fire, PersonScenario(**IN_BED))
    assert run.summary.outcome in ("trapped", "incapacitated")
    assert any("blocks every route" in e.text for e in run.events)


def test_person_who_never_reacts_is_overcome(room: Room) -> None:
    fire = run_fire(room, FireScenario(ignition_object_id="bed", duration_s=300))
    run = run_escape(room, fire, PersonScenario(**IN_BED, response_delay_s=10_000))
    assert run.summary.outcome == "incapacitated"
    assert run.summary.incapacitated_by == "heat"
    assert run.posture[-1] == "collapsed"


def _smoky(fire: FireRun, layer: float) -> FireRun:
    """The same fire with no flames or heat, and thick cool smoke down to the given height."""
    n = len(fire.t)
    return fire.model_copy(
        update={
            "objects": {},
            "hrr_kw": [0.0] * n,
            "upper_temp_c": [25.0] * n,
            "co_ppm": [0.0] * n,
            "visibility_m": [0.3] * n,
            "layer_height_m": [layer] * n,
        }
    )


def test_smoke_at_head_height_hides_the_way(room: Room, curtain_fire: FireRun) -> None:
    person = {"start": Point2(x=3.0, y=2.3), "knows_room": False}
    # Smoke down to the floor: even crawling, the head is in it.
    blind = [
        run_escape(room, _smoky(curtain_fire, 0.2), PersonScenario(**person, seed=seed)).summary
        for seed in range(6)
    ]
    # Smoke down to 1 m: a crawler sees along the floor.
    clear = run_escape(room, _smoky(curtain_fire, 1.0), PersonScenario(**person)).summary

    assert clear.outcome == "escaped" and clear.bumps == 0 and clear.blind_s == 0.0
    assert all(s.blind_s > 0 for s in blind)
    assert sum(s.bumps for s in blind) > 0
    assert len({s.distance_m for s in blind}) > 1  # chance plays a part
    escaped = [s for s in blind if s.outcome == "escaped"]
    assert escaped and min(s.distance_m for s in escaped) >= clear.distance_m - 0.3


def test_same_seed_repeats_the_same_escape(room: Room, curtain_fire: FireRun) -> None:
    smoke = _smoky(curtain_fire, 0.2)
    person = PersonScenario(start=Point2(x=3.0, y=2.3), knows_room=False, seed=4)
    assert run_escape(room, smoke, person) == run_escape(room, smoke, person)


def _wake_reasons(room: Room, fire: FireRun, seeds: range) -> list[str]:
    reasons = []
    for seed in seeds:
        run = run_escape(room, fire, PersonScenario(**IN_BED, smoke_alarm=False, seed=seed))
        woke = next((e.text for e in run.events if "woken" in e.text), "never woke")
        reasons.append(woke)
    return reasons


def test_most_sleepers_without_an_alarm_wake_to_the_sound_of_the_fire(
    room: Room, curtain_fire: FireRun
) -> None:
    reasons = _wake_reasons(room, curtain_fire, range(200))
    by_sound = sum("sound of the fire" in r for r in reasons)
    by_faint_signal = sum(any(k in r for k in ("sound", "light", "smell")) for r in reasons)
    # Bruck and Brennan: about nine in ten wake to crackling; light and smell catch more.
    assert 0.6 <= by_sound / 200 <= 0.95
    assert by_faint_signal / 200 >= 0.85
    # The few who sleep through all three are woken later, by smoke or by pain.
    assert any("smoke in their face" in r or "the heat" in r for r in reasons)


def test_sleeper_wakes_to_sound_before_smoke_reaches_them(
    room: Room, curtain_fire: FireRun
) -> None:
    for seed in range(30):
        scenario = PersonScenario(**IN_BED, smoke_alarm=False, seed=seed)
        run = run_escape(room, curtain_fire, scenario)
        if any("sound of the fire" in e.text for e in run.events):
            layer_when_aware = curtain_fire.layer_height_m[int(run.summary.aware_s)]
            assert layer_when_aware > 0.9  # smoke is still above the pillow
            return
    pytest.fail("no seed woke to the sound of the fire")
