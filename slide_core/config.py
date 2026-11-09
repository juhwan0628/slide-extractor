from dataclasses import dataclass,field
from .models import Rect,KEYFRAME_ALGORITHM

PROCESS_TIMEOUTS = {"version": 5.0, "probe": 15.0, "preview": 15.0,
                    "frame": 60.0, "progress": 60.0,
                    "extraction": 120.0, "qpdf": 120.0}
PROCESS_CANCEL_POLL_S = 0.05
PROCESS_TERMINATE_GRACE_S = 2.0
PROCESS_STDERR_TAIL_BYTES = 64 * 1024


def analysis_timeout(duration_s: float) -> float:
    return max(300.0, float(duration_s) * 2)


def export_timeout(page_count: int) -> float:
    return max(300.0, int(page_count) * PROCESS_TIMEOUTS["extraction"])


@dataclass(frozen=True)
class AnalysisSettings:
    roi_mode: str = "auto"
    effective_roi: Rect | None = None
    masks: tuple[Rect, ...] = ()
    detector: "DetectorConfig | None" = None
    settings_revision: int = 0

    def __post_init__(self):
        object.__setattr__(self, "masks", tuple(self.masks))
        if self.roi_mode not in ("auto", "manual", "full") or self.settings_revision < 0:
            raise ValueError("invalid analysis settings")
        if self.roi_mode == "manual" and self.effective_roi is None:
            raise ValueError("manual ROI is required")

@dataclass(frozen=True)
class DetectorConfig:
    debounce_samples: int = 2
    candidate_mad: float = 0.005
    candidate_tile_mad: float = 0.025
    candidate_pixel_delta: int = 20
    candidate_changed_fraction: float = 0.0002
    tile_size: int = 20
    tile_stride: int = 10
    keep_unconfirmed: bool = True
    algorithm: str = "masked-anchor-tile-v1"
    spatial: "SpatialConfig" = field(default_factory=lambda: SpatialConfig())

    def __post_init__(self):
        if self.algorithm not in ("masked-anchor-tile-v1","spatial-groups-v1") or not isinstance(self.spatial,SpatialConfig):
            raise ValueError("Invalid detector algorithm/settings")


@dataclass(frozen=True)
class SpatialConfig:
    pixel_delta: int = 20
    changed_fraction: float = .06
    cell_fraction: float = .30
    cell_changed_fraction: float = .02
    tile_size: int = 20
    support_samples: int = 2
    edge_delta: int = 25
    core_edge_fraction: float = .6
    overlay_fraction: float = .25
    overlay_missing_edges: int = 8
    transient_penalty: float = .25

    def __post_init__(self):
        import math
        for name in ('pixel_delta','edge_delta'):
            v=getattr(self,name)
            if type(v) is not int or not 0<=v<=255:raise ValueError('Invalid spatial delta')
        for name in ('tile_size','support_samples','overlay_missing_edges'):
            v=getattr(self,name)
            if type(v) is not int or v<1:raise ValueError('Invalid spatial geometry')
        for name in ('changed_fraction','cell_fraction','cell_changed_fraction','core_edge_fraction','overlay_fraction','transient_penalty'):
            v=getattr(self,name)
            if type(v) not in (int,float) or not math.isfinite(v) or not 0<v<=1:raise ValueError('Invalid spatial fraction')
