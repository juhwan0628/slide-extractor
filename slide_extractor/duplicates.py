from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from PIL import Image
import imagehash

@dataclass(frozen=True)
class SlideImage:
    path: Path
    timestamp_s: float


def filter_duplicate_slides(slides: Sequence[SlideImage], threshold: int) -> list[SlideImage]:
    kept: list[SlideImage] = []
    hashes = []
    for slide in slides:
        with Image.open(slide.path) as im:
            h = imagehash.phash(im.convert("RGB"))
        if any((h - previous) <= threshold for previous in hashes):
            continue
        kept.append(slide)
        hashes.append(h)
    return kept
