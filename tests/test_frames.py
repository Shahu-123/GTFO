import shutil
import subprocess
from pathlib import Path

import pytest

from firesim.frames import VideoError, extract_frames

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=1280x720:rate=30",
         "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )  # fmt: skip
    return path


async def test_extracts_evenly_spaced_downscaled_frames(video: Path, tmp_path: Path) -> None:
    frames = await extract_frames(video, tmp_path / "frames", max_frames=8, size=512)
    assert 7 <= len(frames) <= 8
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0",
         str(frames[0])],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    assert probe.stdout.strip() == "512,288"


async def test_rejects_non_video(tmp_path: Path) -> None:
    bogus = tmp_path / "notes.mp4"
    bogus.write_text("not a video")
    with pytest.raises(VideoError):
        await extract_frames(bogus, tmp_path / "frames", max_frames=8, size=512)
