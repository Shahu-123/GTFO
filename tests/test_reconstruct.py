from pathlib import Path
from types import SimpleNamespace

import pytest

from firesim.config import Settings
from firesim.reconstruct import ReconstructionError, build_input, reconstruct_room
from firesim.schema import Room

FIXTURE = Path(__file__).parent / "fixtures" / "sample_room.json"


class FakeResponses:
    def __init__(self, parsed: Room | None) -> None:
        self.parsed = parsed
        self.kwargs: dict = {}

    async def parse(self, **kwargs: object) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(output_parsed=self.parsed, usage=None)


@pytest.fixture
def frames(tmp_path: Path) -> list[Path]:
    paths = [tmp_path / f"frame_{i:03d}.jpg" for i in range(3)]
    for path in paths:
        path.write_bytes(b"\xff\xd8fake")
    return paths


def test_build_input_orders_frames_and_includes_hints(frames: list[Path]) -> None:
    [message] = build_input(frames, "ceiling 2.6 m")
    text, *images = message["content"]
    assert "ceiling 2.6 m" in text["text"]
    assert [i["type"] for i in images] == ["input_image"] * 3
    assert images[0]["image_url"].startswith("data:image/jpeg;base64,")


async def test_reconstruct_requests_structured_room(frames: list[Path]) -> None:
    room = Room.model_validate_json(FIXTURE.read_text())
    responses = FakeResponses(room)
    client = SimpleNamespace(responses=responses)

    result = await reconstruct_room(client, frames, "", Settings(_env_file=None))

    assert result is room
    assert responses.kwargs["model"] == "gpt-6.1-sol"
    assert responses.kwargs["text_format"] is Room
    assert responses.kwargs["reasoning"] == {"effort": "medium"}


async def test_reconstruct_raises_when_model_returns_nothing(frames: list[Path]) -> None:
    client = SimpleNamespace(responses=FakeResponses(None))
    with pytest.raises(ReconstructionError):
        await reconstruct_room(client, frames, "", Settings(_env_file=None))
