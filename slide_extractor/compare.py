from __future__ import annotations
from dataclasses import dataclass
import cv2
import numpy as np
from skimage.metrics import structural_similarity


def prepare_comparison_image(image: np.ndarray, max_width: int = 320) -> np.ndarray:
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    if gray.shape[1] > max_width:
        scale = max_width / gray.shape[1]
        gray = cv2.resize(gray, (max_width, max(1, round(gray.shape[0] * scale))), interpolation=cv2.INTER_AREA)
    return gray


def difference_score(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        raise ValueError("comparison images must have same shape")
    return float(np.mean(cv2.absdiff(a, b)) / 255.0)


def ssim_score(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        raise ValueError("comparison images must have same shape")
    return float(structural_similarity(a, b, data_range=255))


@dataclass(frozen=True)
class LocalChangeMetrics:
    global_mad: float
    max_tile_mad: float
    changed_fraction: float
    tile_location: tuple[int, int]  # x, y in comparison image


def local_change_metrics(
    a: np.ndarray, b: np.ndarray, *, tile_size: int = 20,
    tile_stride: int = 10, pixel_delta: int = 20,
) -> LocalChangeMetrics:
    if a.shape != b.shape or a.ndim != 2 or not a.size:
        raise ValueError("comparison images must be nonempty grayscale images of same shape")
    if tile_size < 1 or tile_stride < 1:
        raise ValueError("tile size and stride must be positive")
    delta = cv2.absdiff(a, b)
    height, width = delta.shape
    th, tw = min(tile_size, height), min(tile_size, width)
    ys = np.unique(np.append(np.arange(0, height - th + 1, tile_stride), height - th))
    xs = np.unique(np.append(np.arange(0, width - tw + 1, tile_stride), width - tw))
    integral = cv2.integral(delta)
    sums = (integral[(ys + th)[:, None], (xs + tw)[None, :]]
            - integral[ys[:, None], (xs + tw)[None, :]]
            - integral[(ys + th)[:, None], xs[None, :]]
            + integral[ys[:, None], xs[None, :]])
    y, x = np.unravel_index(sums.argmax(), sums.shape)
    return LocalChangeMetrics(
        float(delta.mean() / 255), float(sums[y, x] / (th * tw * 255)),
        float(np.count_nonzero(delta > pixel_delta) / delta.size),
        (int(xs[x]), int(ys[y])),
    )
