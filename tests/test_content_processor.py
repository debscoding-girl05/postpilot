import io
from pathlib import Path

from PIL import Image

from app.content_processor import (
    adapt_caption_for_platform,
    process_image_for_platform,
)


def _make_image(tmp_path: Path, size=(2000, 1500), mode="RGB") -> Path:
    p = tmp_path / "img.png"
    Image.new(mode, size, (124, 58, 237)).save(p)
    return p


def test_image_resized_within_platform_bounds(tmp_path):
    src = _make_image(tmp_path)
    data = process_image_for_platform(src, "instagram")  # max 1080x1080
    assert isinstance(data, bytes) and len(data) > 0
    out = Image.open(io.BytesIO(data))
    assert out.width <= 1080 and out.height <= 1080


def test_jpeg_flattens_alpha_channel(tmp_path):
    # instagram spec is JPEG; an RGBA source must not raise.
    src = tmp_path / "rgba.png"
    Image.new("RGBA", (1200, 1200), (255, 0, 0, 128)).save(src)
    data = process_image_for_platform(src, "instagram")
    out = Image.open(io.BytesIO(data))
    assert out.mode == "RGB"


def test_unknown_platform_uses_default_spec(tmp_path):
    src = _make_image(tmp_path, size=(3000, 3000))
    data = process_image_for_platform(src, "myspace")  # default 1200x1200
    out = Image.open(io.BytesIO(data))
    assert out.width <= 1200 and out.height <= 1200


def test_caption_within_limit_unchanged():
    assert adapt_caption_for_platform("hello", "twitter") == "hello"


def test_caption_truncated_to_limit():
    long = "x" * 400
    result = adapt_caption_for_platform(long, "twitter")  # 280
    assert len(result) <= 280
    assert result.endswith("...")


def test_caption_appends_hashtags():
    result = adapt_caption_for_platform("launch day", "bluesky", hashtags=["build", "#ship"])
    assert "#build" in result and "#ship" in result
    assert "##" not in result  # leading # is normalized, not doubled


def test_caption_keeps_hashtags_when_truncating():
    long = "y" * 400
    result = adapt_caption_for_platform(long, "twitter", hashtags=["news"])
    assert "#news" in result
    assert len(result) <= 280
