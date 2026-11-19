"""Validated layout controls shared by saved clips and timeline renders."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Region(BaseModel):
    """Coordinates on the displayed, autorotated source frame."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(ge=.02, le=1)
    h: float = Field(ge=.02, le=1)

    @model_validator(mode="after")
    def inside_frame(self):
        if self.x + self.w > 1 + 1e-12 or self.y + self.h > 1 + 1e-12:
            raise ValueError("O recorte deve ficar dentro do quadro.")
        return self


class LayoutConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    mode: Literal["standard", "split", "split_caption", "pip"] = "standard"
    aspect: Literal["9:16", "16:9"] = "9:16"
    face: Region | None = None
    gameplay: Region | None = None
    split_ratio: float = Field(default=.34, ge=.2, le=.65)
    pip_position: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = "top-right"
    pip_scale: float = Field(default=.28, ge=.18, le=.45)
    game_fit: Literal["contain", "cover"] = "contain"
    caption_position: Literal["bottom", "middle", "top"] = "bottom"


def normalize_layout_config(value: dict | LayoutConfig | None = None) -> dict:
    if isinstance(value, LayoutConfig):
        return value.model_dump()
    return LayoutConfig.model_validate({} if value is None else value).model_dump()


def require_face(config: dict) -> None:
    if config["mode"] != "standard" and config["face"] is None:
        raise ValueError("Defina o recorte do rosto para usar este layout.")


def output_dimensions(quality: str, aspect: str) -> tuple[int, int]:
    if quality not in {"preview", "final"} or aspect not in {"9:16", "16:9"}:
        raise ValueError("Perfil de render inválido.")
    short, long = (720, 1280) if quality == "preview" else (1080, 1920)
    return (short, long) if aspect == "9:16" else (long, short)


def _even(value: float) -> int:
    return max(2, 2 * round(value / 2))


def _split_geometry(config: dict, height: int) -> tuple[int, int]:
    face_height = _even(height * config["split_ratio"])
    band_height = _even(height * .1) if config["mode"] == "split_caption" else 0
    return face_height, band_height


def caption_anchor(config: dict, width: int, height: int) -> tuple[int, int] | None:
    """Return the center anchor. Top/bottom styles use ASS alignment and margins."""
    if config["mode"] in {"split", "split_caption"}:
        face_height, band_height = _split_geometry(config, height)
        return width // 2, face_height + band_height // 2
    if config["caption_position"] == "middle":
        return width // 2, height // 2
    return None


def _region_crop(region: dict | None) -> str:
    if region is None:
        return ""
    # Expressions use FFmpeg's post-autorotation frame dimensions. Round inward
    # to chroma sample boundaries and clamp sub-two-pixel crops on small sources.
    x, y, w, h = (region[key] for key in ("x", "y", "w", "h"))
    return (f"crop=w='max(2,2*trunc(iw*{w:.12f}/2))':h='max(2,2*trunc(ih*{h:.12f}/2))':"
            f"x='2*trunc(iw*{x:.12f}/2)':y='2*trunc(ih*{y:.12f}/2)',")


def _panel(region: dict | None, width: int, height: int, fit: str) -> str:
    crop = _region_crop(region)
    # Some containers omit SAR; use square pixels for those streams.
    sar = "if(gt(sar,0),sar,1)"
    if fit == "cover":
        # Crop the excess before scaling to avoid large intermediate frames.
        return (crop + f"crop=w='max(2,2*trunc(min(iw,ih*{width}/{height}/({sar}))/2))':"
                f"h='max(2,2*trunc(min(ih,iw*({sar})*{height}/{width})/2))',"
                f"scale={width}:{height},setsar=1")
    dar = f"(iw*({sar})/ih)"
    return (crop + f"scale=w='max(2,2*trunc(min({width},{height}*{dar})/2))':"
            f"h='max(2,2*trunc(min({height},{width}/{dar})/2))',setsar=1,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")


def build_composition_graph(layout: str, config: dict, width: int, height: int) -> str:
    """Feed one decoded stream into the output pad [base]."""
    if layout not in {"blur", "fit", "crop"}:
        raise ValueError("Enquadramento inválido.")
    mode = config["mode"]
    require_face(config)
    if mode == "standard":
        if layout == "crop":
            return (f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height},setsar=1,format=yuv420p[base]")
        if layout == "fit":
            return (f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[base]")
        return (f"[0:v]split=2[original_bg][original_fg];"
                f"[original_bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},boxblur=12:2[bg];"
                f"[original_fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[base]")

    split = "[0:v]split=2[face_src][game_src];"
    if mode == "pip":
        face_w = _even(width * config["pip_scale"])
        face_h = _even(face_w * .75)
        margin = _even(min(width, height) * .03)
        position = config["pip_position"]
        x = margin if position.endswith("left") else width - face_w - margin
        y = margin if position.startswith("top") else height - face_h - margin
        game = _panel(config["gameplay"], width, height, config["game_fit"])
        face = _panel(config["face"], face_w, face_h, "contain")
        return (split + f"[game_src]{game}[game];[face_src]{face}[face];"
                f"[game][face]overlay={x}:{y},setsar=1,format=yuv420p[base]")

    face_h, band_h = _split_geometry(config, height)
    game = _panel(config["gameplay"], width, height - face_h - band_h, config["game_fit"])
    face = _panel(config["face"], width, face_h, "contain")
    if band_h:
        face += f",pad={width}:{face_h+band_h}:0:0:color=0x121820"
    return (split + f"[face_src]{face}[face];[game_src]{game}[game];"
            "[face][game]vstack=inputs=2,setsar=1,format=yuv420p[base]")
