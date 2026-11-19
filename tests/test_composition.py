"""Exercise the saved-video compositor against decoded frames from actual exports."""
import math
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from autoclips import render_clip
from composition import normalize_layout_config
from subtitles import generate_srt


FACE = {"x": 0, "y": 0, "w": .5, "h": .5}
GAME = {"x": 0, "y": .5, "w": 1, "h": .5}


@pytest.mark.parametrize("change", [
    {"aspect": "4:3"}, {"pip_scale": .46}, {"split_ratio": .19},
    {"face": {**FACE, "w": .01}}, {"gameplay": {**GAME, "x": .01}},
    {"face": {**FACE, "x": math.nan}}, {"face": {**FACE, "x": True}},
    {"pip_scale": float("inf")}, {"game_fit": "stretch"}, {"extra": "unexpected"},
])
def test_reject_invalid_controls(change):
    with pytest.raises(ValidationError):
        normalize_layout_config(change)


@pytest.mark.parametrize("mode", ["split_caption", "split", "pip"])
def test_needs_face_before_opening_source(tmp_path, mode):
    with pytest.raises(ValueError, match="rosto"):
        render_clip(tmp_path / "absent.mp4", tmp_path / "out.mp4", {"start": 0, "end": 1},
                    quality="preview", layout="fit", burn=False, srt=None,
                    has_audio=False, layout_config={"mode": mode})


@pytest.fixture(scope="module")
def quadrants(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is required for composition renders.")
    video = tmp_path_factory.mktemp("layout") / "quadrants.mp4"
    pattern = ("color=red:s=320x240:r=5:d=0.4,"
               "drawbox=x=160:y=0:w=160:h=120:c=yellow:t=fill,"
               "drawbox=x=0:y=120:w=160:h=120:c=lime:t=fill,"
               "drawbox=x=160:y=120:w=160:h=120:c=blue:t=fill")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", pattern,
                    "-c:v", "libx264", "-an", str(video)], check=True, timeout=30)
    return video


class Frame:
    """Sample relative image positions from a downscaled RGB reference frame."""

    def __init__(self, path):
        self.width = 320
        self.raw = subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf",
            f"scale={self.width}:-2", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ], capture_output=True, check=True, timeout=30).stdout
        self.height = len(self.raw) // (3 * self.width)

    def pixel(self, x, y):
        pos = 3 * (int(y * self.height) * self.width + int(x * self.width))
        return tuple(self.raw[pos:pos + 3])

    def white_caption_rows(self):
        rows = []
        for y in range(self.height):
            row = self.raw[y*self.width*3:(y+1)*self.width*3]
            if any(min(r, g, b) > 185 for r, g, b in zip(row[::3], row[1::3], row[2::3])):
                rows.append(y / self.height)
        return rows


def render(source, tmp_path, config, *, quality="preview", captions=False):
    srt = tmp_path / "cues.srt"
    if captions:
        generate_srt([{"start": 0., "end": .4, "text": "GAMEPLAY"}], 0., .4, srt)
    output = tmp_path / f"{quality}.mp4"
    meta = render_clip(source, output, {"start": 0., "end": .4}, quality=quality,
                       layout="fit", layout_config=config, burn=captions,
                       srt=srt if captions else None, has_audio=False,
                       subtitle_style={"font_size": 72})
    assert meta["layout_config"] == normalize_layout_config(config)
    return meta, Frame(output)


def approx_rgb(pixel, expected):
    assert pixel == pytest.approx(expected, abs=22)


@pytest.mark.parametrize(("quality", "aspect", "size"), [
    ("preview", "9:16", (720, 1280)), ("final", "9:16", (1080, 1920)),
    ("preview", "16:9", (1280, 720)), ("final", "16:9", (1920, 1080)),
])
def test_split_caption_band_face_game_and_actual_white_text(quadrants, tmp_path, quality, aspect, size):
    config = {"mode": "split_caption", "aspect": aspect, "face": FACE, "gameplay": GAME,
              "split_ratio": .4}
    meta, frame = render(quadrants, tmp_path, config, quality=quality, captions=True)
    assert (meta["width"], meta["height"]) == size
    assert (meta["output_width"], meta["output_height"]) == size
    approx_rgb(frame.pixel(.5, .2), (255, 0, 0))
    approx_rgb(frame.pixel(.1, .45), (18, 24, 32))
    approx_rgb(frame.pixel(.4, .75), (0, 255, 0))
    approx_rgb(frame.pixel(.6, .75), (0, 0, 255))
    rows = frame.white_caption_rows()
    assert rows and min(rows) > .40 and max(rows) < .50
    assert (min(rows) + max(rows)) / 2 == pytest.approx(.45, abs=.014)


def test_split_without_caption_band_and_optional_seam_text(quadrants, tmp_path):
    config = {"mode": "split", "face": FACE, "gameplay": GAME, "game_fit": "cover",
              "split_ratio": .4}
    _, clear = render(quadrants, tmp_path, config)
    approx_rgb(clear.pixel(.48, .39), (255, 0, 0))
    approx_rgb(clear.pixel(.48, .41), (0, 255, 0))
    assert not clear.white_caption_rows()
    _, subtitled = render(quadrants, tmp_path, config, captions=True)
    rows = subtitled.white_caption_rows()
    assert rows and (min(rows) + max(rows)) / 2 == pytest.approx(.4, abs=.014)


@pytest.mark.parametrize("position", ["top-left", "top-right", "bottom-left", "bottom-right"])
def test_pip_over_game_at_selected_corner(quadrants, tmp_path, position):
    config = {"mode": "pip", "aspect": "16:9", "face": FACE,
              "gameplay": {"x": .5, "y": .5, "w": .5, "h": .5},
              "pip_position": position, "pip_scale": .3, "game_fit": "cover"}
    _, frame = render(quadrants, tmp_path, config)
    for corner, x, y in (("top-left", .16, .20), ("top-right", .84, .20),
                         ("bottom-left", .16, .80), ("bottom-right", .84, .80)):
        approx_rgb(frame.pixel(x, y), (255, 0, 0) if position == corner else (0, 0, 255))


def test_rotated_input_regions_follow_displayed_frame(quadrants, tmp_path):
    rotated = tmp_path / "phone.mp4"
    rotation = subprocess.run(["ffmpeg", "-v", "error", "-display_rotation", "90", "-i",
                               str(quadrants), "-c", "copy", str(rotated)], capture_output=True, timeout=30)
    if rotation.returncode and b"display_rotation" in rotation.stderr:
        # Older FFmpeg creates MP4 display matrices using stream metadata.
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(quadrants), "-c", "copy",
                        "-metadata:s:v:0", "rotate=90", str(rotated)], check=True, timeout=30)
    else:
        rotation.check_returncode()
    _, frame = render(rotated, tmp_path, {"mode": "split", "face": FACE,
                                          "gameplay": GAME, "split_ratio": .4})
    # A 90-degree display rotation moves the yellow upper-right tile to upper-left.
    approx_rgb(frame.pixel(.5, .2), (255, 255, 0))
