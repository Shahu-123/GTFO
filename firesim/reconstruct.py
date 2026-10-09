"""Turn walkthrough frames into a Room using GPT-6.1 structured output."""

import base64
import logging
from pathlib import Path

from openai import AsyncOpenAI

from firesim.config import Settings
from firesim.schema import Room

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
You reconstruct a single room as a metric 3D scene from still frames of a phone walkthrough \
video. The frames are in time order. The result is drawn by a 3D renderer and later used for \
a fire escape simulation, so dimensions and positions matter more than decoration.

Coordinates
- Metres, right-handed, z up. The floor is z = 0.
- Put one floor corner at (0, 0) and keep the whole room in positive x and y.
- List floor corners counter-clockwise as seen from above. Wall i runs from corner i to \
corner i + 1, and the last wall returns to corner 0.
- Rotations are about z, counter-clockwise from above, in degrees.

Scale
- You cannot measure, so anchor the scale on standard items: interior doors about 2.0 to \
2.1 m tall and 0.8 to 0.9 m wide, kitchen counters about 0.9 m, single beds about 1.9 m long, \
light switches about 1.2 to 1.4 m above the floor, sockets about 0.3 m.
- If the user gives measurements, treat them as exact and fit everything else to them.
- Record each anchor you used in scale_references.

Openings
- Include every door, window and open passage. offset is measured along the wall from its \
start corner to the near edge of the opening.
- Set leads_outside_room for anything a person could walk or climb out through.

Objects
- Include every piece of furniture and every sizeable combustible item (curtains, rugs, \
bedding, bookshelves, piles of boxes or clothes), plus anything that could start a fire.
- position is the centre of the object's footprint; position.z is the height of its base \
(0 for items on the floor).
- size is the bounding box before rotation: width along local x, depth along local y.
- fuel is the dominant material that would burn.

Parts
- Model each object's appearance with 3 to 12 primitives so it is recognisable: a sofa has \
a base, a back, two arms and cushions; a table has a top and legs.
- Part coordinates are local to the object: origin at the centre of the footprint at the \
base, before the object's rotation. Keep every part inside the object's size.
- Cylinders stand along z; size.width is the diameter.
- Take colours from the frames.

Honesty
- List in assumptions anything you guessed because it was hidden or out of frame.
- Set confidence to low if large parts of the room were never visible.
"""


class ReconstructionError(Exception):
    pass


def _image_item(frame: Path) -> dict[str, str]:
    encoded = base64.b64encode(frame.read_bytes()).decode()
    return {
        "type": "input_image",
        "image_url": f"data:image/jpeg;base64,{encoded}",
        "detail": "auto",
    }


def build_input(frames: list[Path], hints: str) -> list[dict]:
    text = f"{len(frames)} frames from one walkthrough of a single room, in time order."
    if hints.strip():
        text += f"\n\nMeasurements and notes from the user:\n{hints.strip()}"
    content: list[dict] = [{"type": "input_text", "text": text}]
    content.extend(_image_item(frame) for frame in frames)
    return [{"role": "user", "content": content}]


async def reconstruct_room(
    client: AsyncOpenAI, frames: list[Path], hints: str, settings: Settings
) -> Room:
    log.info("Reconstructing room from %d frames with %s", len(frames), settings.model)
    response = await client.responses.parse(
        model=settings.model,
        instructions=INSTRUCTIONS,
        input=build_input(frames, hints),
        text_format=Room,
        reasoning={"effort": settings.reasoning_effort},
        store=False,
    )
    if response.usage:
        log.info("Tokens: %d in, %d out", response.usage.input_tokens, response.usage.output_tokens)
    room = response.output_parsed
    if room is None:
        raise ReconstructionError("The model returned no room (refused or truncated output).")
    return room
