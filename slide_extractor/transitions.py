from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence
import numpy as np
from .compare import difference_score, ssim_score
from .config import DetectorConfig

@dataclass(frozen=True)
class PreparedSample:
    timestamp_s: float
    image: np.ndarray

@dataclass(frozen=True)
class CandidateScore:
    timestamp_s: float
    difference: float
    ssim: float | None
    accepted: bool

@dataclass(frozen=True)
class TransitionDiagnostics:
    accepted_transition_timestamps: list[float]
    candidate_scores: list[CandidateScore]


def _similar(a: np.ndarray, b: np.ndarray, config: DetectorConfig) -> bool:
    return difference_score(a, b) < config.diff_threshold


def detect_transitions(samples: Sequence[PreparedSample], config: DetectorConfig) -> TransitionDiagnostics:
    if len(samples) < 2:
        return TransitionDiagnostics([], [])
    accepted: list[float] = []
    diagnostics: list[CandidateScore] = []
    anchor = samples[0].image
    i = 1
    while i < len(samples):
        diff = difference_score(anchor, samples[i].image)
        ssim = None
        candidate = diff >= config.diff_threshold
        if not candidate and diff >= config.ambiguous_low:
            ssim = ssim_score(anchor, samples[i].image)
            candidate = ssim < 0.85
        accepted_here = False
        if candidate:
            needed = max(1, config.debounce_samples)
            end = min(len(samples), i + needed)
            persistence = end - i == needed and all(_similar(samples[i].image, samples[j].image, config) for j in range(i + 1, end))
            if persistence:
                accepted.append(samples[i].timestamp_s)
                anchor = samples[i].image
                accepted_here = True
                i = end
                diagnostics.append(CandidateScore(samples[i-needed].timestamp_s, diff, ssim, True))
                continue
        diagnostics.append(CandidateScore(samples[i].timestamp_s, diff, ssim, accepted_here))
        i += 1
    return TransitionDiagnostics(accepted, diagnostics)

@dataclass(frozen=True)
class SlideSegment:
    start_s: float
    end_s: float | None


def build_segments(samples, transitions):
    if not samples:
        return []
    starts=[samples[0].timestamp_s, *transitions]
    ends=[*transitions, None]
    return [SlideSegment(float(s), None if e is None else float(e)) for s,e in zip(starts, ends)]


def select_representative_sample(
    segment: SlideSegment,
    samples,
    prepared: Sequence[PreparedSample] | None = None,
    config: DetectorConfig | None = None,
):
    candidates = [
        s for s in samples
        if s.timestamp_s >= segment.start_s
        and (segment.end_s is None or s.timestamp_s < segment.end_s)
    ]
    if not candidates:
        raise ValueError("segment has no samples")

    if prepared is None or len(candidates) < 2:
        return max(candidates, key=lambda s: s.timestamp_s)

    cfg = config or DetectorConfig()
    by_timestamp = {p.timestamp_s: p for p in prepared}
    prepared_candidates = [by_timestamp.get(s.timestamp_s) for s in candidates]

    # Prefer the end of the latest visual state that persisted for at least
    # debounce_samples consecutive samples. This prevents a one-sample
    # transition/animation at the end of a segment from becoming the PDF page.
    needed = max(2, cfg.debounce_samples)
    for end_idx in range(len(candidates) - 1, needed - 2, -1):
        end_prepared = prepared_candidates[end_idx]
        if end_prepared is None:
            continue
        start_idx = end_idx - needed + 1
        run = prepared_candidates[start_idx : end_idx + 1]
        if any(p is None for p in run):
            continue
        anchor = run[0].image
        if all(_similar(anchor, p.image, cfg) for p in run[1:]):
            return candidates[end_idx]

    return candidates[0]
