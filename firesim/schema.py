"""Room description shared by the reconstruction step, the viewer and (later) the simulation.

Coordinates are in metres, right-handed, z up. The floor is the z = 0 plane and the floor
outline is a polygon in x/y. Wall i runs from floor vertex i to vertex i + 1 (wrapping).

This model doubles as the structured-output schema sent to the LLM, so it avoids numeric
constraints and tuple types. Plausibility checks live in check_room instead.
"""

import math
from enum import StrEnum

from pydantic import BaseModel, Field


class OpeningKind(StrEnum):
    DOOR = "door"
    WINDOW = "window"
    OPEN_PASSAGE = "open_passage"


class FuelClass(StrEnum):
    UPHOLSTERED_FOAM = "upholstered_foam"
    MATTRESS = "mattress"
    WOOD = "wood"
    TEXTILE = "textile"
    PAPER = "paper"
    PLASTIC = "plastic"
    ELECTRONICS = "electronics"
    NON_COMBUSTIBLE = "non_combustible"
    OTHER = "other"


class PartShape(StrEnum):
    BOX = "box"
    CYLINDER = "cylinder"
    SPHERE = "sphere"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Point2(BaseModel):
    x: float
    y: float


class Point3(BaseModel):
    x: float
    y: float
    z: float


class Size3(BaseModel):
    width: float = Field(description="Extent along local x, metres")
    depth: float = Field(description="Extent along local y, metres")
    height: float = Field(description="Extent along z, metres")


class Opening(BaseModel):
    id: str
    kind: OpeningKind
    wall_index: int = Field(description="Wall i runs from floor vertex i to vertex i + 1")
    offset: float = Field(description="Distance along the wall from its start vertex, metres")
    width: float
    height: float
    sill_height: float = Field(description="Height of the bottom edge above the floor; 0 for doors")
    is_open: bool = Field(description="As seen in the video")
    leads_outside_room: bool = Field(description="True if a person could leave the room through it")


class Part(BaseModel):
    """One primitive of an object's visual model, in the object's local frame.

    The local origin is the centre of the object's footprint at its base.
    """

    shape: PartShape
    center: Point3
    size: Size3 = Field(description="Bounding size; cylinders stand along z, width is the diameter")
    rotation_deg: float = Field(description="Rotation about z, counter-clockwise from above")
    color: str = Field(description="Hex colour such as #8a6f4d")


class RoomObject(BaseModel):
    id: str
    label: str = Field(description="Short human-readable name, e.g. 'three-seat sofa'")
    category: str = Field(description="Lowercase noun, e.g. sofa, bed, wardrobe, curtain, tv")
    fuel: FuelClass
    is_ignition_source: bool = Field(
        description="Heaters, stoves, candles, chargers, power strips, lamps and similar"
    )
    position: Point3 = Field(description="Centre of the footprint; z is the base height")
    size: Size3
    rotation_deg: float = Field(description="Rotation about z, counter-clockwise from above")
    parts: list[Part]


class Room(BaseModel):
    name: str
    floor: list[Point2] = Field(description="Floor outline, counter-clockwise from above")
    ceiling_height: float
    wall_color: str
    floor_color: str
    openings: list[Opening]
    objects: list[RoomObject]
    scale_references: list[str] = Field(
        description="What was used to set the scale, e.g. 'door assumed 2.05 m tall'"
    )
    assumptions: list[str] = Field(description="Anything guessed because it was not visible")
    confidence: Confidence


def wall_length(room: Room, index: int) -> float:
    a = room.floor[index]
    b = room.floor[(index + 1) % len(room.floor)]
    return math.hypot(b.x - a.x, b.y - a.y)


def floor_area(room: Room) -> float:
    """Signed area: positive when the outline is counter-clockwise."""
    pts = room.floor
    return 0.5 * sum(p.x * q.y - q.x * p.y for p, q in zip(pts, pts[1:] + pts[:1], strict=True))


def inside_floor(room: Room, x: float, y: float) -> bool:
    inside = False
    pts = room.floor
    for p, q in zip(pts, pts[1:] + pts[:1], strict=True):
        if (p.y > y) != (q.y > y) and x < (q.x - p.x) * (y - p.y) / (q.y - p.y) + p.x:
            inside = not inside
    return inside


def check_room(room: Room) -> list[str]:
    """Return plausibility problems. The LLM estimates geometry, so the caller shows these
    to the user rather than rejecting the room outright."""
    problems: list[str] = []

    if len(room.floor) < 3:
        return ["Floor outline has fewer than three corners."]

    area = abs(floor_area(room))
    if not 2.0 <= area <= 150.0:
        problems.append(f"Floor area {area:.1f} m² is outside the expected 2 to 150 m².")
    if not 2.0 <= room.ceiling_height <= 5.0:
        problems.append(f"Ceiling height {room.ceiling_height:.2f} m is outside 2 to 5 m.")

    for opening in room.openings:
        if not 0 <= opening.wall_index < len(room.floor):
            problems.append(f"Opening '{opening.id}' refers to a wall that does not exist.")
            continue
        length = wall_length(room, opening.wall_index)
        if opening.offset < 0 or opening.offset + opening.width > length + 0.01:
            problems.append(f"Opening '{opening.id}' does not fit on its {length:.2f} m wall.")
        if opening.sill_height + opening.height > room.ceiling_height + 0.01:
            problems.append(f"Opening '{opening.id}' extends above the ceiling.")
        if opening.kind is OpeningKind.DOOR and not 0.6 <= opening.width <= 2.0:
            problems.append(f"Door '{opening.id}' width {opening.width:.2f} m looks wrong.")

    if not any(o.leads_outside_room for o in room.openings):
        problems.append("No exit found: no opening leads out of the room.")

    for obj in room.objects:
        if min(obj.size.width, obj.size.depth, obj.size.height) <= 0:
            problems.append(f"Object '{obj.label}' has a non-positive dimension.")
        if not inside_floor(room, obj.position.x, obj.position.y):
            problems.append(f"Object '{obj.label}' is centred outside the floor outline.")
        if obj.position.z + obj.size.height > room.ceiling_height + 0.01:
            problems.append(f"Object '{obj.label}' extends above the ceiling.")

    return problems
