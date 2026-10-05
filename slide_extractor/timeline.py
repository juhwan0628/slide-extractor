from __future__ import annotations

import json
from dataclasses import asdict
from .config import DetectorConfig
from .duplicates import StatePageMapping
import os
import tempfile
from pathlib import Path
from typing import Sequence

from .models import ROI
from .transitions import SlideSegment


def default_timeline_path(pdf_output_path: Path) -> Path:
    return pdf_output_path.with_suffix('.json')


def build_timeline_payload(
    *,
    source: Path,
    duration_s: float,
    sample_fps: float,
    roi: ROI,
    segments: Sequence[SlideSegment],
    representative_timestamps: Sequence[float],
    frame_paths: Sequence[Path] | None = None,
    config: DetectorConfig | None = None,
    mappings: Sequence[StatePageMapping] | None = None,
) -> dict:
    if len(segments) != len(representative_timestamps):
        raise ValueError('segment and representative counts must match')
    if frame_paths is not None and len(frame_paths) != len(segments):
        raise ValueError('segment and frame counts must match')

    cfg = config or DetectorConfig()
    if mappings is None:
        mappings = [StatePageMapping(i, i, None, "preserved") for i in range(1, len(segments) + 1)]
    if len(mappings) != len(segments) or any(m.state_id != i for i, m in enumerate(mappings, 1)):
        raise ValueError("state mapping must match segments in order")
    pages = {m.pdf_page for m in mappings}
    if pages != set(range(1, len(pages) + 1)):
        raise ValueError("PDF pages must be contiguous and 1-based")
    slides = []
    for index, (segment, representative_timestamp_s) in enumerate(
        zip(segments, representative_timestamps), start=1
    ):
        end_s = duration_s if segment.end_s is None else segment.end_s
        slide = {
            'slide_id': index,
            'start_s': float(segment.start_s),
            'end_s': float(end_s),
            'representative_timestamp_s': float(representative_timestamp_s),
        }
        mapping = mappings[index - 1]
        slide.update(pdf_page=mapping.pdf_page, duplicate_of=mapping.duplicate_of,
                     dedup_reason=mapping.reason, phash_distance=mapping.phash_distance)
        if frame_paths is not None:
            slide['frame'] = str(frame_paths[index - 1])
        slides.append(slide)

    events = [
        {
            'type': 'slide_change',
            'timestamp_s': slide['start_s'],
            'from_slide': index,
            'to_slide': index + 1,
        }
        for index, slide in enumerate(slides[1:], start=1)
    ]

    return {
        'schema_version': 2,
        'detector_settings': asdict(cfg),
        'dedup_mode': cfg.dedup_mode,
        'dedup_settings': {'mode': cfg.dedup_mode, 'duplicate_phash_distance': cfg.duplicate_phash_distance},
        'pdf_page_count': len(pages),
        'source': source.name,
        'duration_s': float(duration_s),
        'sample_fps': float(sample_fps),
        'roi': {
            'x': roi.x,
            'y': roi.y,
            'width': roi.width,
            'height': roi.height,
        },
        'slides': slides,
        'events': events,
    }


def write_timeline_json(payload: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f'.{output_path.name}.', suffix='.tmp', dir=output_path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, output_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
