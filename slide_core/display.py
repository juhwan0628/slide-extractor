"""Single canonical SDR display transform for preview, sample and export.

The caller must decode with -noautorotate and map the selected stream.
Output frames are packed RGB24 in square-pixel canonical display coordinates.
"""
from __future__ import annotations
from dataclasses import dataclass
from fractions import Fraction
from .models import DisplayTransform


class UnsupportedDisplayTransform(ValueError):
    code = "UnsupportedDisplayTransform"


@dataclass(frozen=True)
class DisplayPipeline:
    transform: DisplayTransform
    full_filter: str
    cache_filter: str
    cache_width: int
    cache_height: int
    color_warning: str | None = None


def build_display_pipeline(*, width: int, height: int, sar: Fraction = Fraction(1),
                           rotation: int = 0, color_matrix: str | None = None,
                           color_range: str | None = None, transfer: str | None = None,
                           pixel_format: str = "yuv420p", interlaced: bool = False,
                           dynamic_dimensions: bool = False) -> DisplayPipeline:
    """Prepare identical non-lossy display geometry and explicit SDR conversion policy.

    Cache resize follows full display geometry; no upscaling for small videos.
    Unsupported HDR, arbitrary rotations and unspecified transforms fail closed.
    """
    if (type(width) is not int or type(height) is not int or min(width, height) <= 0 or
            type(rotation) is not int or rotation not in (0, 90, 180, 270) or
            interlaced or dynamic_dimensions):
        raise UnsupportedDisplayTransform("Unsupported rotation, dimensions or interlace")
    try:
        sar = Fraction(sar)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise UnsupportedDisplayTransform("Invalid sample aspect ratio") from exc
    if sar <= 0:
        raise UnsupportedDisplayTransform("Invalid sample aspect ratio")
    transfer = (transfer or "unknown").lower()
    if transfer in ("smpte2084", "arib-std-b67", "pq", "hlg", "bt2020-10", "bt2020-12"):
        raise UnsupportedDisplayTransform("HDR transfer is not supported")
    if transfer not in ("unknown", "", "bt709", "bt470bg", "smpte170m", "iec61966-2-1", "gamma22", "gamma28"):
        raise UnsupportedDisplayTransform(f"Unsupported transfer: {transfer}")
    matrix = (color_matrix or "unknown").lower()
    rng = (color_range or "unknown").lower()
    rgb = pixel_format.lower().startswith(("rgb", "bgr", "gbr"))
    if rgb:
        if matrix not in ("unknown", "", "rgb", "gbr"):
            raise UnsupportedDisplayTransform("RGB source has conflicting color matrix")
        matrix = "rgb"
        rng = "full"
    else:
        if matrix in ("unknown", "", "unspecified"):
            matrix = "bt709" if height >= 720 else "bt601"
            warning = f"SDR matrix unspecified; assumed {matrix} limited range"
        else:
            warning = None
            matrix = {"bt470bg":"bt601", "smpte170m":"bt601", "bt709":"bt709"}.get(matrix, matrix)
        if matrix not in ("bt601", "bt709"):
            raise UnsupportedDisplayTransform(f"Unsupported SDR matrix: {matrix}")
        if rng in ("unknown", "", "unspecified", "tv", "limited", "mpeg"):
            rng = "limited"
        elif rng in ("pc", "full", "jpeg"):
            rng = "full"
        else:
            raise UnsupportedDisplayTransform(f"Unsupported color range: {rng}")
    warning = None if rgb else locals().get("warning")
    square_width = max(1, round(width * sar))
    display_width, display_height = ((square_width, height) if rotation in (0, 180)
                                    else (height, square_width))
    tr = DisplayTransform(width, height, sar, rotation, display_width, display_height)
    scale = f"scale={square_width}:{height}:flags=bicubic"
    if not rgb:
        scale += f":in_color_matrix={matrix}:in_range={'tv' if rng == 'limited' else 'pc'}"
    filters = [scale, "setsar=1"]
    if rotation == 90:
        filters.append("transpose=clock")
    elif rotation == 180:
        filters += ["hflip", "vflip"]
    elif rotation == 270:
        filters.append("transpose=cclock")
    full = ",".join(filters + ["format=rgb24"])
    cache_width = min(640, display_width)
    cache_height = max(1, round(display_height * cache_width / display_width))
    if (cache_width, cache_height) == (display_width, display_height):
        cache = full
    else:
        cache = ",".join(filters + [f"scale={cache_width}:{cache_height}:flags=area", "format=rgb24"])
    return DisplayPipeline(tr, full, cache, cache_width, cache_height, warning)
