from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence
import numpy as np
from .compare import difference_score, ssim_score, local_change_metrics, LocalChangeMetrics
from .config import DetectorConfig
from .models import SampleFrame

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
    candidate: bool = False
    adjacent: LocalChangeMetrics | None = None
    anchor: LocalChangeMetrics | None = None
    triggers: tuple[str, ...] = ()
    persistence: int = 0
    reason: str = "legacy"

@dataclass(frozen=True)
class TransitionDiagnostics:
    accepted_transition_timestamps: list[float]
    candidate_scores: list[CandidateScore]


def _similar(a: np.ndarray, b: np.ndarray, config: DetectorConfig) -> bool:
    if config.detector_mode == "local":
        return not _gates(_metrics(a, b, config), config)
    return difference_score(a, b) < config.diff_threshold


def detect_legacy_transitions(samples: Sequence[PreparedSample], config: DetectorConfig) -> TransitionDiagnostics:
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
            persistence = end - i == needed and all(difference_score(samples[i].image, samples[j].image) < config.diff_threshold for j in range(i + 1, end))
            if persistence:
                accepted.append(samples[i].timestamp_s)
                anchor = samples[i].image
                accepted_here = True
                i = end
                diagnostics.append(CandidateScore(samples[i-needed].timestamp_s, diff, ssim, True, True))
                continue
        diagnostics.append(CandidateScore(samples[i].timestamp_s, diff, ssim, accepted_here, candidate))
        i += 1
    return TransitionDiagnostics(accepted, diagnostics)



def _metrics(a: np.ndarray, b: np.ndarray, config: DetectorConfig) -> LocalChangeMetrics:
    return local_change_metrics(a, b, tile_size=config.tile_size,
                                tile_stride=config.tile_stride,
                                pixel_delta=config.candidate_pixel_delta)


def _gates(metrics: LocalChangeMetrics, config: DetectorConfig) -> tuple[str, ...]:
    changed = metrics.changed_fraction >= config.candidate_changed_fraction
    triggers: list[str] = []
    if changed and metrics.global_mad >= config.candidate_mad:
        triggers.extend(("global_mad", "changed_fraction"))
    if changed and metrics.max_tile_mad >= config.candidate_tile_mad:
        triggers.extend(("max_tile_mad", "changed_fraction"))
    return tuple(dict.fromkeys(triggers))


def detect_transitions(samples: Sequence[PreparedSample], config: DetectorConfig) -> TransitionDiagnostics:
    if config.detector_mode == "legacy":
        return detect_legacy_transitions(samples, config)
    accepted: list[float] = []
    scores: list[CandidateScore] = []
    if not samples:
        return TransitionDiagnostics(accepted, scores)
    anchor = samples[0].image
    scores.append(CandidateScore(samples[0].timestamp_s, 0.0, None, False,
                                 reason="initial_anchor"))
    # Look ahead without advancing the scan: every sample can start a state.
    # Returning to the anchor rejects a transient; other uncertain runs are
    # retained by default, including candidates at EOF.
    for i in range(1, len(samples)):
        current = samples[i].image
        adjacent_metrics = _metrics(samples[i - 1].image, current, config)
        anchor_metrics = _metrics(anchor, current, config)
        adjacent_gates, anchor_gates = _gates(adjacent_metrics, config), _gates(anchor_metrics, config)
        triggers = tuple(f"{kind}.{gate}" for kind, gates in (
            ("adjacent", adjacent_gates), ("anchor", anchor_gates)
        ) for gate in gates)
        candidate = bool(triggers)
        accept = False
        persistence = 0
        reason = "below_gates"
        if candidate and not anchor_gates:
            reason = "return_to_anchor"
        elif candidate:
            persistence = 1
            stable = True
            reverted = False
            end = min(len(samples), i + config.debounce_samples)
            for j in range(i + 1, end):
                future = samples[j].image
                if not _gates(_metrics(anchor, future, config), config):
                    reverted = True
                    break
                persistence += 1
                stable = stable and not _gates(_metrics(current, future, config), config)
            if reverted:
                reason = "reject_transient_return"
            elif not stable:
                reason = "reject_unstable"
            elif persistence == config.debounce_samples:
                accept, reason = True, "accept_persistent"
            elif end == len(samples) and persistence < config.debounce_samples:
                if config.keep_unconfirmed:
                    accept, reason = True, "pending_eof_retained"
                else:
                    reason = "pending_eof"
            else:
                reason = "reject_unconfirmed"
            if accept:
                accepted.append(samples[i].timestamp_s)
                anchor = current
        scores.append(CandidateScore(samples[i].timestamp_s, anchor_metrics.global_mad,
                                     None, accept, candidate, adjacent_metrics,
                                     anchor_metrics, triggers, persistence, reason))
    return TransitionDiagnostics(accepted, scores)

@dataclass(frozen=True)
class SlideSegment:
    start_s: float
    end_s: float | None


def build_segments(
    samples: Sequence[SampleFrame] | Sequence[PreparedSample], transitions: Sequence[float],
) -> list[SlideSegment]:
    if not samples:
        return []
    starts=[samples[0].timestamp_s, *transitions]
    ends=[*transitions, None]
    return [SlideSegment(float(s), None if e is None else float(e)) for s,e in zip(starts, ends)]


def select_representative_sample(
    segment: SlideSegment,
    samples: Sequence[SampleFrame],
    prepared: Sequence[PreparedSample] | None = None,
    config: DetectorConfig | None = None,
) -> SampleFrame:
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
