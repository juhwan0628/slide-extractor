from dataclasses import dataclass

@dataclass(frozen=True)
class DetectorConfig:
    diff_threshold: float = 0.12
    ambiguous_low: float = 0.04
    debounce_samples: int = 2
    duplicate_phash_distance: int = 6

    detector_mode: str = "local"
    dedup_mode: str = "off"
    candidate_mad: float = 0.005
    candidate_tile_mad: float = 0.025
    candidate_pixel_delta: int = 20
    candidate_changed_fraction: float = 0.0002
    tile_size: int = 20
    tile_stride: int = 10
    keep_unconfirmed: bool = True

    def __post_init__(self):
        if self.detector_mode not in {"local", "legacy"}:
            raise ValueError("unknown detector mode")
        if self.dedup_mode not in {"off", "adjacent", "legacy-global"}:
            raise ValueError("unknown dedup mode")
        if self.tile_size < 1 or self.tile_stride < 1 or self.debounce_samples < 1:
            raise ValueError("tile size, stride and debounce must be positive")
        if not 0 <= self.candidate_pixel_delta <= 255:
            raise ValueError("pixel delta must be between 0 and 255")
        if any(not 0 < value <= 1 for value in (
            self.candidate_mad, self.candidate_tile_mad, self.candidate_changed_fraction
        )):
            raise ValueError("candidate thresholds must be in (0, 1]")
