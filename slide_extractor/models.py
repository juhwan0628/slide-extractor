from dataclasses import dataclass

@dataclass(frozen=True)
class VideoMetadata:
    width: int
    height: int
    duration_s: float
    fps: float

from pathlib import Path

@dataclass(frozen=True)
class ROI:
    x: int
    y: int
    width: int
    height: int

@dataclass(frozen=True)
class SampleFrame:
    path: Path
    timestamp_s: float

@dataclass(frozen=True)
class PipelineResult:
    output_path: Path
    timeline_path: Path
    roi: ROI
    final_page_count: int
    candidate_transition_count: int
    accepted_transition_count: int
    runtime_s: float
    workdir: Path | None = None
    frame_dir: Path | None = None
    diagnostics_path: Path | None = None
