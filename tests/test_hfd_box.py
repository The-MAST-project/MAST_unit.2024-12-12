"""`box=` on the HFD metric: only stars inside the box count, and the box never widens.

focus_on_star measures ONE star in a small box; everything else in the frame -- brighter
stars included -- must be invisible to the metric, or the V-curve is somebody else's.
"""

import numpy as np
import pytest

from calibration.analysis.vcurve import analyze_focus_samples
from imaging.hfd import frame_hfd, measure_sweep_hfd

SHAPE = (600, 800)  # (ny, nx)
BOX = (300, 500, 200, 400)  # (x0, x1, y0, y1)


def _frame(stars, seed=0):
    """Background 1000 +- 10 ADU plus Gaussian stars (x, y, sigma, peak)."""
    rng = np.random.default_rng(seed)
    ny, nx = SHAPE
    yy, xx = np.mgrid[0:ny, 0:nx]
    img = 1000.0 + rng.normal(0, 10, SHAPE)
    for x, y, s, peak in stars:
        img += peak * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * s * s))
    return img


def test_only_the_star_in_the_box_is_measured():
    img = _frame([(400, 300, 2.5, 20000), (100, 100, 2.5, 50000), (700, 500, 6.0, 30000)])
    hfd, n = frame_hfd(img, box=BOX)
    assert n == 1
    alone, _ = frame_hfd(_frame([(400, 300, 2.5, 20000)]), box=BOX)
    assert hfd == pytest.approx(alone, rel=0.05)


def test_an_empty_box_measures_nothing_even_with_stars_elsewhere():
    img = _frame([(100, 100, 2.5, 50000), (700, 500, 2.5, 50000)])
    hfd, n = frame_hfd(img, box=BOX)
    assert n == 0 and np.isnan(hfd)


def test_a_star_just_inside_the_edge_keeps_its_whole_stamp():
    # 4 px inside the box's left edge: the crop is padded, so its stamp is not clipped.
    edge, _ = frame_hfd(_frame([(304, 300, 3.0, 20000)]), box=BOX)
    centre, _ = frame_hfd(_frame([(400, 300, 3.0, 20000)]), box=BOX)
    assert edge == pytest.approx(centre, rel=0.05)


def test_a_small_box_is_not_widened_to_the_whole_frame():
    small = (390, 410, 290, 310)  # 20 x 20 px around the one star
    img = _frame([(400, 300, 2.0, 20000), (100, 100, 2.0, 50000), (700, 500, 2.0, 50000)])
    _, n = frame_hfd(img, box=small)
    assert n == 1


def test_a_sweep_through_focus_solves_on_the_star_in_the_box():
    best = 1000.0
    positions = [900.0, 950.0, 1000.0, 1050.0, 1100.0]
    frames = []
    for i, p in enumerate(positions):
        sigma = np.hypot(2.0, (p - best) / 25.0)  # a V in HFD about `best`
        peak = 40000.0 * (2.0 / sigma) ** 2  # same flux, spread wider
        # A brighter, always-sharp star outside the box: if it were counted, the curve would be flat.
        frames.append(_frame([(400, 300, sigma, peak), (120, 120, 2.0, 60000)], seed=i))
    per_frame, n = measure_sweep_hfd(frames, box=BOX)
    assert n == 1
    status = analyze_focus_samples(list(zip(positions, frames, strict=True)), min_edge_rise=1.1, box=BOX)
    result = status.analysis_result
    assert result is not None and result.has_solution, status.errors
    assert result.best_focus_position == pytest.approx(best, abs=15)
