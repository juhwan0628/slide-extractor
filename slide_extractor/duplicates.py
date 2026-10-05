from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from PIL import Image
import imagehash
import numpy as np

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


@dataclass(frozen=True)
class StatePageMapping:
    state_id: int
    pdf_page: int
    duplicate_of: int | None
    reason: str
    phash_distance: int | None = None


@dataclass(frozen=True)
class DedupResult:
    slides: list[SlideImage]
    mappings: list[StatePageMapping]


def deduplicate_slides(
    slides: Sequence[SlideImage], mode: str = "off", threshold: int = 6,
) -> DedupResult:
    """Map every temporal state to a 1-based PDF page and canonical state ID."""
    if mode not in {"off", "adjacent", "legacy-global"}:
        raise ValueError("unknown dedup mode")
    kept: list[SlideImage] = []
    mappings: list[StatePageMapping] = []
    hashes = []
    canonical_ids: list[int] = []
    previous_pixels = None
    for state_id, slide in enumerate(slides, 1):
        duplicate = None
        distance = None
        if mode == "adjacent":
            with Image.open(slide.path) as image:
                pixels = np.asarray(image.convert("RGB"))
            if previous_pixels is not None and np.array_equal(pixels, previous_pixels):
                duplicate = mappings[-1]
            previous_pixels = pixels
        elif mode == "legacy-global":
            with Image.open(slide.path) as image:
                h = imagehash.phash(image.convert("RGB"))
            for page, previous in enumerate(hashes, 1):
                d = h - previous
                if d <= threshold:
                    distance = int(d)
                    duplicate = mappings[canonical_ids[page - 1] - 1]
                    break
            if duplicate is None:
                hashes.append(h)
        if duplicate is None:
            kept.append(slide)
            canonical_ids.append(state_id)
            mappings.append(StatePageMapping(state_id, len(kept), None, "preserved"))
        else:
            mappings.append(StatePageMapping(
                state_id, duplicate.pdf_page, duplicate.duplicate_of or duplicate.state_id,
                "adjacent_exact_pixels" if mode == "adjacent" else "legacy_global_phash", distance,
            ))
    return DedupResult(kept, mappings)
