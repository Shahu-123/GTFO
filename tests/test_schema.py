from pathlib import Path

import pytest

from firesim.schema import Room, check_room, floor_area, wall_length

FIXTURE = Path(__file__).parent / "fixtures" / "sample_room.json"


@pytest.fixture
def room() -> Room:
    return Room.model_validate_json(FIXTURE.read_text())


def test_sample_room_is_plausible(room: Room) -> None:
    assert check_room(room) == []
    assert floor_area(room) == pytest.approx(3.6 * 4.2)
    assert wall_length(room, 3) == pytest.approx(4.2)


def test_opening_wider_than_wall_is_reported(room: Room) -> None:
    room.openings[0].width = 5.0
    assert any("does not fit" in p for p in check_room(room))


def test_missing_exit_is_reported(room: Room) -> None:
    for opening in room.openings:
        opening.leads_outside_room = False
    assert any("No exit" in p for p in check_room(room))


def test_object_outside_floor_is_reported(room: Room) -> None:
    room.objects[0].position.x = 9.0
    assert any("outside the floor" in p for p in check_room(room))
