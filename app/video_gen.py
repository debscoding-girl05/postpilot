"""Local short-video creation: turn images into a vertical MP4 with ffmpeg.

No API key, no network — composes uploaded/generated images into a 1080x1920
(9:16) slideshow with fade transitions and an optional gentle Ken Burns zoom and
background audio track. Ideal for TikTok/Reels-style posts.

Requires the `ffmpeg` binary on PATH.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from app.database import DATA_DIR

MEDIA_DIR = DATA_DIR / "media"
WIDTH, HEIGHT = 1080, 1920
FPS = 30
FADE = 0.4  # seconds of fade in/out per image


def is_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _segment_filter(duration: float, ken_burns: bool) -> str:
    fade_out_start = max(0.0, duration - FADE)
    frames = max(1, int(duration * FPS))
    if ken_burns:
        # Scale up for pan room, slow centered zoom, then down to output size.
        pre = (
            f"scale={WIDTH * 3 // 2}:{HEIGHT * 3 // 2}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH * 3 // 2}:{HEIGHT * 3 // 2},"
            f"zoompan=z='min(zoom+0.0015,1.2)':d={frames}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={WIDTH}x{HEIGHT}:fps={FPS}"
        )
    else:
        pre = (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},fps={FPS}"
        )
    return (
        f"{pre},setsar=1,format=yuv420p,"
        f"fade=t=in:st=0:d={FADE},fade=t=out:st={fade_out_start:.3f}:d={FADE}"
    )


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-800:]
        raise RuntimeError(f"ffmpeg failed: {tail}")


def _build_slideshow(
    image_paths: list[Path],
    audio_path: Path | None,
    seconds_per_image: float,
    ken_burns: bool,
) -> Path:
    if not is_available():
        raise RuntimeError("ffmpeg is not installed")
    if not image_paths:
        raise ValueError("At least one image is required")

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    out_name = f"{uuid.uuid4().hex}.mp4"
    out_path = MEDIA_DIR / out_name

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        segments: list[Path] = []
        for i, img in enumerate(image_paths):
            seg = tmp_dir / f"seg_{i}.mp4"
            vf = _segment_filter(seconds_per_image, ken_burns)
            if ken_burns:
                # Feed zoompan a SINGLE still (no -loop): otherwise zoompan re-emits
                # its frame count for every looped input frame and the clip explodes.
                cmd = ["ffmpeg", "-y", "-i", str(img)]
            else:
                cmd = ["ffmpeg", "-y", "-loop", "1", "-t", f"{seconds_per_image}", "-i", str(img)]
            cmd += [
                "-vf", vf, "-t", f"{seconds_per_image}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS),
                "-preset", "veryfast", str(seg),
            ]
            _run(cmd)
            segments.append(seg)

        list_file = tmp_dir / "list.txt"
        list_file.write_text("".join(f"file '{s}'\n" for s in segments))

        if audio_path is not None and Path(audio_path).exists():
            _run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-i", str(audio_path),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-shortest", str(out_path),
            ])
        else:
            _run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-c", "copy", str(out_path),
            ])

    return out_path


async def create_slideshow(
    image_paths: list[Path],
    audio_path: Path | None = None,
    seconds_per_image: float = 3.0,
    ken_burns: bool = True,
) -> dict:
    """Render a slideshow MP4. Returns {path, filename, url}. Runs ffmpeg off-loop."""
    seconds_per_image = max(1.0, min(float(seconds_per_image), 15.0))
    out_path = await asyncio.to_thread(
        _build_slideshow, [Path(p) for p in image_paths], audio_path, seconds_per_image, ken_burns
    )
    fname = out_path.name
    return {"path": str(out_path), "filename": fname, "url": f"/api/media/file/{fname}"}


__all__ = ["create_slideshow", "is_available", "WIDTH", "HEIGHT"]
