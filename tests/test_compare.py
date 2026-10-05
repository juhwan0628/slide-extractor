import numpy as np
import slide_extractor.compare as cmp


def test_difference_identical_is_zero():
    assert hasattr(cmp, "difference_score")
    a=np.zeros((100,100),np.uint8)
    assert cmp.difference_score(a,a) == 0.0


def test_difference_strong_change_is_large():
    assert hasattr(cmp, "difference_score")
    a=np.zeros((100,100),np.uint8); b=np.full((100,100),255,np.uint8)
    assert cmp.difference_score(a,b) > 0.9


def test_small_cursor_like_change_is_small():
    assert hasattr(cmp, "difference_score")
    a=np.zeros((100,100),np.uint8); b=a.copy(); b[40:45,40:45]=255
    assert cmp.difference_score(a,b) < 0.02


def test_overlapping_tile_catches_change_across_nonoverlapping_boundary():
    a = np.zeros((43, 57), np.uint8)
    b = a.copy()
    b[16:24, 16:24] = 255
    metrics = cmp.local_change_metrics(a, b)
    assert metrics.max_tile_mad == 64 / 400
    assert metrics.tile_location == (10, 10)
    assert metrics.changed_fraction == 64 / a.size


def test_local_metrics_small_dimensions_and_bottom_right_edge():
    a = np.zeros((7, 13), np.uint8)
    b = a.copy()
    b[-1, -1] = 255
    metrics = cmp.local_change_metrics(a, b)
    assert np.isclose(metrics.global_mad, 1 / a.size)
    assert np.isclose(metrics.max_tile_mad, 1 / a.size)
    assert metrics.tile_location == (0, 0)
    a = np.zeros((43, 57), np.uint8)
    b = a.copy()
    b[-2:, -2:] = 255
    assert cmp.local_change_metrics(a, b).tile_location == (37, 23)
