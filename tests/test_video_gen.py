"""Slideshow tests — these actually run ffmpeg, so they're skipped if it's absent."""
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app import video_gen

pytestmark = pytest.mark.skipif(
    not video_gen.is_available(), reason="ffmpeg not installed"
)


def _images(tmp_path, count=2):
    paths = []
    for i in range(count):
        p = tmp_path / f"img_{i}.jpg"
        Image.new("RGB", (1280, 720), (40 * (i + 1) % 255, 80, 160)).save(p)
        paths.append(p)
    return paths


def _probe(path: Path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_type,width,height:format=duration",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True,
    ).stdout
    is_video = "codec_type=video" in out
    dur = next(
        (float(l.split("=")[1]) for l in out.splitlines() if l.startswith("duration=")), 0.0
    )
    return is_video, dur


async def test_slideshow_from_two_images(tmp_path):
    imgs = _images(tmp_path, 2)
    result = await video_gen.create_slideshow(imgs, seconds_per_image=1.5, ken_burns=False)
    out = Path(result["path"])
    assert out.exists() and out.stat().st_size > 0
    assert result["filename"].endswith(".mp4")
    is_video, dur = _probe(out)
    assert is_video
    # 2 images x 1.5s ≈ 3s — guards against the zoompan frame-explosion bug.
    assert 2.5 <= dur <= 4.0, f"unexpected duration {dur}"
    out.unlink(missing_ok=True)


async def test_slideshow_ken_burns_duration_correct(tmp_path):
    imgs = _images(tmp_path, 2)
    result = await video_gen.create_slideshow(imgs, seconds_per_image=1.5, ken_burns=True)
    out = Path(result["path"])
    is_video, dur = _probe(out)
    assert is_video
    assert 2.5 <= dur <= 4.0, f"ken burns clip exploded: {dur}s"
    out.unlink(missing_ok=True)


async def test_slideshow_requires_images():
    with pytest.raises((ValueError, RuntimeError)):
        await video_gen.create_slideshow([])
