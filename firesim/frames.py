"""Sample still frames from a walkthrough video. GPT-6.1 accepts images, not video."""

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class VideoError(Exception):
    pass


async def _run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise VideoError(stderr.decode(errors="replace").strip()[-500:])
    return stdout.decode()


async def video_duration(video: Path) -> float:
    out = await _run(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "format=duration", "-of", "csv=p=0", str(video),
    )  # fmt: skip
    try:
        duration = float(out.strip())
    except ValueError as exc:
        raise VideoError(f"Could not read a video stream from {video.name}") from exc
    if duration <= 0:
        raise VideoError(f"{video.name} has no duration")
    return duration


async def extract_frames(video: Path, out_dir: Path, max_frames: int, size: int) -> list[Path]:
    """Write up to max_frames JPEGs, evenly spaced in time, longest side at most size px."""
    duration = await video_duration(video)
    out_dir.mkdir(parents=True, exist_ok=True)
    fps = max_frames / duration
    await _run(
        "ffmpeg", "-v", "error", "-y", "-i", str(video),
        "-vf", f"fps={fps:.6f},scale={size}:{size}:force_original_aspect_ratio=decrease",
        "-frames:v", str(max_frames), "-q:v", "3",
        str(out_dir / "frame_%03d.jpg"),
    )  # fmt: skip
    frames = sorted(out_dir.glob("frame_*.jpg"))
    if not frames:
        raise VideoError(f"No frames could be extracted from {video.name}")
    log.info("Extracted %d frames from %s (%.1f s)", len(frames), video.name, duration)
    return frames
