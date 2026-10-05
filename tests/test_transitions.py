import numpy as np
import pytest
import slide_extractor.transitions as tr
from slide_extractor.config import DetectorConfig


def p(t, v):
    return tr.PreparedSample(t, np.full((80,120), v, np.uint8))


def test_transient_change_is_debounced():
    assert hasattr(tr, "detect_transitions")
    samples=[p(0,0),p(1,255),p(2,0),p(3,0)]
    d=tr.detect_transitions(samples, DetectorConfig(diff_threshold=.2, ambiguous_low=.1, debounce_samples=2))
    assert d.accepted_transition_timestamps == []


def test_persistent_change_is_accepted():
    assert hasattr(tr, "detect_transitions")
    samples=[p(0,0),p(1,255),p(2,255),p(3,255)]
    d=tr.detect_transitions(samples, DetectorConfig(diff_threshold=.2, ambiguous_low=.1, debounce_samples=2))
    assert d.accepted_transition_timestamps == [1]

from pathlib import Path
from slide_extractor.models import SampleFrame


def sf(t):
    return SampleFrame(Path(f"{t}.png"), float(t))


def test_build_segments_uses_transition_boundaries():
    samples=[sf(0),sf(1),sf(2),sf(3),sf(4)]
    segments=tr.build_segments(samples,[2.0])
    assert [(s.start_s,s.end_s) for s in segments] == [(0.0,2.0),(2.0,None)]


def test_representative_is_latest_sample_before_next_transition():
    samples=[sf(0),sf(1),sf(2),sf(3),sf(4)]
    segment=tr.SlideSegment(0.0,2.0)
    rep=tr.select_representative_sample(segment,samples)
    assert rep.timestamp_s == 1.0


def test_ambiguous_ssim_change_that_reverts_is_not_persistent():
    base = np.zeros((80, 120), np.uint8)
    changed = base.copy()
    changed[:, :12] = 128  # ~0.05 mean absolute difference: ambiguous band
    samples = [
        tr.PreparedSample(0.0, base),
        tr.PreparedSample(1.0, changed),
        tr.PreparedSample(2.0, base),
        tr.PreparedSample(3.0, base),
    ]
    cfg = DetectorConfig(diff_threshold=0.12, ambiguous_low=0.04, debounce_samples=2)
    d = tr.detect_transitions(samples, cfg)
    assert d.accepted_transition_timestamps == []


def test_representative_skips_unconfirmed_transient_at_segment_end():
    base = np.zeros((80, 120), np.uint8)
    transient = np.full((80, 120), 255, np.uint8)
    samples = [sf(0), sf(1), sf(2), sf(3)]
    prepared = [
        tr.PreparedSample(0.0, base),
        tr.PreparedSample(1.0, base),
        tr.PreparedSample(2.0, base),
        tr.PreparedSample(3.0, transient),
    ]
    rep = tr.select_representative_sample(
        tr.SlideSegment(0.0, None), samples, prepared, DetectorConfig()
    )
    assert rep.timestamp_s == 2.0


def test_tiny_persistent_bullet_is_detected():
    base = np.zeros((240, 320), np.uint8)
    bullet = base.copy()
    bullet[45:48, 55:65] = 255
    samples = [tr.PreparedSample(i, im) for i, im in enumerate([base, bullet, bullet])]
    result = tr.detect_transitions(samples, DetectorConfig())
    assert result.accepted_transition_timestamps == [1]
    score = next(s for s in result.candidate_scores if s.timestamp_s == 1)
    assert score.difference < .04
    assert "adjacent.max_tile_mad" in score.triggers
    assert score.persistence == 2
    assert score.reason == "accept_persistent"


def test_tiny_one_sample_change_reverts_without_false_states():
    base = np.zeros((240, 320), np.uint8)
    bullet = base.copy()
    bullet[45:48, 55:65] = 255
    samples = [tr.PreparedSample(i, im) for i, im in enumerate([base, bullet, base, base])]
    result = tr.detect_transitions(samples, DetectorConfig())
    assert result.accepted_transition_timestamps == []
    assert any(s.reason == "reject_transient_return" for s in result.candidate_scores)


def test_eof_candidate_retained_by_default():
    result = tr.detect_transitions([p(0, 0), p(1, 255)], DetectorConfig())
    assert result.accepted_transition_timestamps == [1]
    assert result.candidate_scores[-1].reason == "pending_eof_retained"
    strict = tr.detect_transitions([p(0, 0), p(1, 255)], DetectorConfig(keep_unconfirmed=False))
    assert strict.accepted_transition_timestamps == []
    assert strict.candidate_scores[-1].reason == "pending_eof"


def test_unstable_candidate_rejected_and_next_sample_checked():
    result = tr.detect_transitions([p(0, 0), p(1, 128), p(2, 255), p(3, 255)], DetectorConfig())
    assert result.accepted_transition_timestamps == [2]
    assert result.candidate_scores[1].reason == "reject_unstable"
    assert result.candidate_scores[1].persistence == 2


def test_anchor_detects_accumulated_subthreshold_drift():
    cfg = DetectorConfig(candidate_mad=.1, candidate_tile_mad=.1, candidate_changed_fraction=.9)
    result = tr.detect_transitions([p(i, v) for i, v in enumerate([0, 10, 20, 30, 30])], cfg)
    assert result.accepted_transition_timestamps == [3]
    score = next(s for s in result.candidate_scores if s.timestamp_s == 3)
    assert "anchor.global_mad" in score.triggers
    assert not any(t.startswith("adjacent") for t in score.triggers)


def test_legacy_mode_preserves_historical_skipping_and_eof_policy():
    result = tr.detect_transitions([p(0, 0), p(1, 255), p(2, 255), p(3, 0)],
                                   DetectorConfig(detector_mode="legacy"))
    assert result.accepted_transition_timestamps == [1]
    assert [s.timestamp_s for s in result.candidate_scores] == [1, 3]


def test_diagnostics_cover_every_local_sample():
    samples = [p(0, 0), p(1, 255), p(2, 255), p(3, 0), p(4, 0)]
    result = tr.detect_transitions(samples, DetectorConfig())
    assert [s.timestamp_s for s in result.candidate_scores] == [0, 1, 2, 3, 4]
    assert result.candidate_scores[0].reason == "initial_anchor"
    assert result.accepted_transition_timestamps == [1, 3]


def test_direct_legacy_detector_uses_legacy_persistence_threshold():
    samples = [p(0, 0), p(1, 255), p(2, 240)]
    assert tr.detect_legacy_transitions(samples, DetectorConfig()).accepted_transition_timestamps == [1]


def test_representatives_stay_inside_each_detected_state():
    samples = [sf(i) for i in range(5)]
    prepared = [p(i, v) for i, v in enumerate([0, 0, 128, 128, 255])]
    segments = tr.build_segments(samples, [2, 4])
    representatives = [tr.select_representative_sample(s, samples, prepared, DetectorConfig())
                       for s in segments]
    assert [s.timestamp_s for s in representatives] == [1, 3, 4]


@pytest.mark.parametrize("keep_unconfirmed", [False, True])
def test_unstable_truncated_window_is_rejected_even_at_eof(keep_unconfirmed):
    cfg = DetectorConfig(debounce_samples=4, keep_unconfirmed=keep_unconfirmed)
    result = tr.detect_transitions([p(0, 0), p(1, 128), p(2, 255)], cfg)
    assert result.accepted_transition_timestamps == ([2] if keep_unconfirmed else [])
    assert result.candidate_scores[1].reason == "reject_unstable"
    assert result.candidate_scores[1].persistence == 2


@pytest.mark.parametrize("keep_unconfirmed", [False, True])
def test_stable_truncated_window_is_pending_at_eof(keep_unconfirmed):
    cfg = DetectorConfig(debounce_samples=4, keep_unconfirmed=keep_unconfirmed)
    result = tr.detect_transitions([p(0, 0), p(1, 128), p(2, 128)], cfg)
    assert result.accepted_transition_timestamps == ([1] if keep_unconfirmed else [])
    assert result.candidate_scores[1].persistence == 2
    assert result.candidate_scores[1].reason == (
        "pending_eof_retained" if keep_unconfirmed else "pending_eof"
    )


def test_small_sub_pixel_threshold_drift_is_stable():
    samples = [p(i, v) for i, v in enumerate([0, 128, 129, 130, 130])]
    result = tr.detect_transitions(samples, DetectorConfig(debounce_samples=3))
    assert result.accepted_transition_timestamps == [1]
    assert result.candidate_scores[1].reason == "accept_persistent"
    assert result.candidate_scores[1].persistence == 3


@pytest.mark.parametrize("kind", ["tile_only", "fraction_only", "global"])
def test_local_gate_requires_meaningful_evidence(kind):
    base = np.zeros((240, 320), np.uint8)
    changed = base.copy()
    if kind == "tile_only":
        changed[40:60, 50:70] = 10  # tile MAD > .025, no changed pixels
    elif kind == "fraction_only":
        changed[::40, ::40] = 255  # scattered pixels, tile MAD < .025
    else:
        changed[:] = 2  # global MAD > .005, but no >20 changed pixels: noise-like
    samples = [tr.PreparedSample(i, im) for i, im in enumerate([base, changed, changed])]
    result = tr.detect_transitions(samples, DetectorConfig())
    assert result.accepted_transition_timestamps == []
    assert not result.candidate_scores[1].candidate


def test_chapter5_11s_like_local_change_persists():
    base = np.full((240, 320), 100, np.uint8)
    changed = base.copy()
    changed[45, 55:67] = 242
    changed[46, 55:66] = 242  # 23 pixels: tile MAD .0320, fraction .000299
    images = [base] * 11 + [changed, changed]
    result = tr.detect_transitions(
        [tr.PreparedSample(i, im) for i, im in enumerate(images)], DetectorConfig()
    )
    assert result.accepted_transition_timestamps == [11]
    score = result.candidate_scores[11]
    assert score.adjacent.max_tile_mad == pytest.approx(.0320196, abs=1e-7)
    assert score.adjacent.changed_fraction == pytest.approx(.000299479, abs=1e-9)
    assert score.persistence == 2
    assert score.reason == "accept_persistent"


@pytest.mark.parametrize("add_local_change", [False, True])
def test_long_random_local_noise_does_not_explode_states(add_local_change):
    rng = np.random.default_rng(42)
    base = np.full((240, 320), 100, np.uint8)
    images = [base]
    for index in range(300):
        noisy = base.astype(np.int16) + rng.integers(-1, 2, base.shape)
        if index % 2 == 0:
            noisy[40:42, 50:63] += rng.integers(40, 51, (2, 13))
        # Both repeated compression patterns and changing patterns must be ignored.
        images.extend([noisy.astype(np.uint8)] * 2)
    if add_local_change:
        changed = base.copy()
        changed[45, 55:67] = 242
        changed[46, 55:66] = 242
        images.extend([changed, changed, changed])
    result = tr.detect_transitions(
        [tr.PreparedSample(i, im) for i, im in enumerate(images)], DetectorConfig()
    )
    assert result.accepted_transition_timestamps == ([601] if add_local_change else [])
    assert all(not s.candidate for s in result.candidate_scores[1:601])
