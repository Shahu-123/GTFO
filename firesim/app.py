import logging
import re
import uuid
from pathlib import Path

import openai
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from firesim.config import get_settings
from firesim.escape.sim import EscapeRun, PersonScenario, run_escape
from firesim.fire.sim import FireRun, FireScenario, run_fire
from firesim.frames import VideoError, extract_frames
from firesim.reconstruct import ReconstructionError, reconstruct_room
from firesim.risk import RiskReport, analyse
from firesim.schema import Room, check_room

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_ROOM = ROOT / "tests" / "fixtures" / "sample_room.json"
ROOM_ID = re.compile(r"^[0-9a-f]{12}$")
CHUNK = 1024 * 1024

app = FastAPI(title="GTFO")


class RoomResult(BaseModel):
    id: str
    room: Room
    problems: list[str]


def _result(room_id: str, room: Room) -> RoomResult:
    return RoomResult(id=room_id, room=room, problems=check_room(room))


def _room_dir(room_id: str) -> Path:
    return get_settings().data_dir / "rooms" / room_id


async def _save_upload(video: UploadFile, dest: Path, max_bytes: int) -> None:
    written = 0
    with dest.open("wb") as out:
        while chunk := await video.read(CHUNK):
            written += len(chunk)
            if written > max_bytes:
                raise HTTPException(413, f"Video is larger than {max_bytes // CHUNK} MB.")
            out.write(chunk)


@app.post("/api/rooms")
async def create_room(video: UploadFile, hints: str = Form("")) -> RoomResult:
    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(503, "OPENAI_API_KEY is not set. Add it to .env and try again.")

    room_id = uuid.uuid4().hex[:12]
    room_dir = _room_dir(room_id)
    room_dir.mkdir(parents=True)
    suffix = Path(video.filename or "").suffix.lower() or ".mp4"
    video_path = room_dir / f"video{suffix}"
    await _save_upload(video, video_path, settings.max_upload_mb * CHUNK)

    try:
        frames = await extract_frames(
            video_path, room_dir / "frames", settings.max_frames, settings.frame_size
        )
    except VideoError as exc:
        log.warning("Frame extraction failed for %s: %s", room_id, exc)
        raise HTTPException(400, f"Could not read that video: {exc}") from exc

    # No retries: a build takes minutes, and a silent retry after a timeout bills twice.
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key, max_retries=0)
    try:
        room = await reconstruct_room(client, frames, hints, settings)
    except (openai.APIError, ReconstructionError) as exc:
        log.exception("Reconstruction failed for %s", room_id)
        raise HTTPException(502, f"Reconstruction failed: {exc}") from exc

    (room_dir / "room.json").write_text(room.model_dump_json(indent=2))
    return _result(room_id, room)


@app.get("/api/rooms")
async def list_rooms() -> list[str]:
    rooms_dir = get_settings().data_dir / "rooms"
    if not rooms_dir.exists():
        return []
    found = [p.parent for p in rooms_dir.glob("*/room.json")]
    return [p.name for p in sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)]


def _load_room(room_id: str) -> Room:
    if room_id == "sample":
        path = SAMPLE_ROOM
    elif ROOM_ID.match(room_id):
        path = _room_dir(room_id) / "room.json"
    else:
        raise HTTPException(404, "No such room.")
    if not path.exists():
        raise HTTPException(404, "No such room.")
    return Room.model_validate_json(path.read_text())


@app.get("/api/rooms/{room_id}")
async def get_room(room_id: str) -> RoomResult:
    return _result(room_id, _load_room(room_id))


# Plain def: the simulation is CPU-bound, so FastAPI runs it in a worker thread.
@app.post("/api/rooms/{room_id}/fire")
def simulate_fire(room_id: str, scenario: FireScenario) -> FireRun:
    room = _load_room(room_id)
    try:
        return run_fire(room, scenario)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class EscapeRequest(BaseModel):
    fire: FireScenario
    person: PersonScenario


class EscapeResult(BaseModel):
    fire: FireRun
    escape: EscapeRun


@app.post("/api/rooms/{room_id}/escape")
def simulate_escape(room_id: str, request: EscapeRequest) -> EscapeResult:
    room = _load_room(room_id)
    try:
        fire = run_fire(room, request.fire)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return EscapeResult(fire=fire, escape=run_escape(room, fire, request.person))


@app.post("/api/rooms/{room_id}/risk")
def analyse_risk(room_id: str) -> RiskReport:
    return analyse(_load_room(room_id), parallel=True)


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
