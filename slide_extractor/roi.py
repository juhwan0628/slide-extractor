from __future__ import annotations
from pathlib import Path
from typing import Sequence
import cv2
import numpy as np
from .models import ROI

BLACK_THRESHOLD = 8


def _outer_black_extent(profile: np.ndarray, reverse: bool = False) -> int:
    values = profile[::-1] if reverse else profile
    count = 0
    for value in values:
        if value <= BLACK_THRESHOLD:
            count += 1
        else:
            break
    return count


def detect_slide_roi(image_paths: Sequence[Path]) -> ROI:
    if not image_paths:
        raise ValueError("no images provided for ROI detection")
    grays = []
    for path in image_paths[:8]:
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError(f"cannot read image: {path}")
        grays.append(image)
    shape = grays[0].shape
    if any(g.shape != shape for g in grays):
        raise ValueError("ROI samples have inconsistent dimensions")
    stack = np.stack(grays).astype(np.float32)
    stable = np.median(stack, axis=0)
    col_profile = np.median(stable, axis=0)
    row_profile = np.median(stable, axis=1)
    left = _outer_black_extent(col_profile)
    right = _outer_black_extent(col_profile, reverse=True)
    top = _outer_black_extent(row_profile)
    bottom = _outer_black_extent(row_profile, reverse=True)
    h, w = shape
    width = w - left - right
    height = h - top - bottom
    if width <= 0 or height <= 0 or width < w * 0.25 or height < h * 0.25:
        raise ValueError("could not detect a plausible slide ROI")
    return ROI(left, top, width, height)


def parse_roi(text: str, frame_width: int, frame_height: int) -> ROI:
    try:
        x, y, w, h = [int(v.strip()) for v in text.split(",")]
    except Exception as exc:
        raise ValueError("ROI must be x,y,w,h") from exc
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > frame_width or y + h > frame_height:
        raise ValueError("ROI is outside video bounds")
    return ROI(x, y, w, h)


def crop_to_roi(image: np.ndarray, roi: ROI) -> np.ndarray:
    return image[roi.y:roi.y + roi.height, roi.x:roi.x + roi.width]
