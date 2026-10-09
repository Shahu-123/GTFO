from pathlib import Path

import pytest

from firesim.risk import SEEDS, FloorMap, RiskReport, analyse
from firesim.schema import Room

FIXTURE = Path(__file__).parent / "fixtures" / "sample_room.json"


@pytest.fixture(scope="module")
def report() -> RiskReport:
    return analyse(Room.model_validate_json(FIXTURE.read_text()))


def test_every_flagged_ignition_source_is_simulated(report: RiskReport) -> None:
    assert {s.object_id for s in report.sources} == {"power-strip", "lamp"}
    assert [s.harm for s in report.sources] == sorted(
        (s.harm for s in report.sources), reverse=True
    )
    for source in report.sources:
        assert [c.person for c in source.cases] == ["asleep_alarm", "asleep_no_alarm", "awake"]
        assert all(c.runs == SEEDS and 0.0 <= c.harm <= 1.0 for c in source.cases)
    # Two door states per source.
    assert report.fires == 4


def test_cases_can_be_replayed_from_the_bed(report: RiskReport) -> None:
    case = report.sources[0].cases[0]
    assert case.scenario.start_object_id == "bed"
    assert case.scenario.asleep and case.scenario.smoke_alarm


def _cell(floor_map: FloorMap, x: float, y: float) -> float | None:
    i, j = int((x - floor_map.x0) / floor_map.cell), int((y - floor_map.y0) / floor_map.cell)
    return floor_map.values[j * floor_map.nx + i]


def test_heat_map_covers_open_floor_and_is_soonest_near_the_sources(report: RiskReport) -> None:
    heat = report.heat
    assert len(heat.values) == heat.nx * heat.ny
    assert _cell(heat, 2.85, 3.0) is None  # under the bed
    beside_lamp, by_door = _cell(heat, 1.5, 3.3), _cell(heat, 0.7, 0.3)
    assert beside_lamp is not None and by_door is not None
    assert beside_lamp < by_door <= report.heat_limit_s


def test_danger_map_scores_a_person_at_each_open_cell(report: RiskReport) -> None:
    danger, heat = report.danger, report.heat
    assert (danger.nx, danger.ny) == (heat.nx, heat.ny)
    # The same cells are open in both maps, and harm is a fraction.
    assert [v is None for v in danger.values] == [v is None for v in heat.values]
    scores = [v for v in danger.values if v is not None]
    assert scores and all(0.0 <= v <= 1.0 for v in scores)
    assert report.danger_delays_s == [10.0, 45.0, 90.0]
    # Standing right by the door is no worse than standing at the far side by the lamp.
    assert _cell(danger, 0.7, 0.3) <= _cell(danger, 1.5, 3.3)


def test_bed_is_reported_as_a_resting_place(report: RiskReport) -> None:
    [bed] = report.resting
    assert bed.object_id == "bed"
    assert 0.0 <= bed.harm <= 1.0


def test_plan_shows_main_furniture_and_risky_items(report: RiskReport) -> None:
    assert {"bed", "wardrobe", "desk"} <= set(report.main_items)
    assert "rug" not in report.main_items and "power-strip" not in report.main_items
    assert "lamp" in report.high_risk_items


def test_parallel_analysis_gives_the_same_report(report: RiskReport) -> None:
    room = Room.model_validate_json(FIXTURE.read_text())
    assert analyse(room, parallel=True) == report


def test_route_runs_from_bedside_to_door(report: RiskReport) -> None:
    assert len(report.route) > 5
    assert report.route[-1].y < 0.4  # the door is on the y = 0 wall


def test_missing_smoke_alarm_is_reported_as_a_factor(report: RiskReport) -> None:
    titles = [f.title for f in report.factors]
    assert "No smoke alarm" in titles
    assert any(f.object_ids == ["lamp"] for f in report.factors)
    assert [f.severity for f in report.factors] == sorted(
        (f.severity for f in report.factors), reverse=True
    )
