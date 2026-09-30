from __future__ import annotations
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
