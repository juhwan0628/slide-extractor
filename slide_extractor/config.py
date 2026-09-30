from dataclasses import dataclass

@dataclass(frozen=True)
class DetectorConfig:
    diff_threshold: float = 0.12
    ambiguous_low: float = 0.04
    debounce_samples: int = 2
    duplicate_phash_distance: int = 6
