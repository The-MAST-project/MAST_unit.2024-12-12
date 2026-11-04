"""focus_on_star's one star: `measure_star`, its sweep, its FWHM, and the peak fraction.

focus_on_star measures only the star it chose; every other HFD user keeps the median over
all the sources in its field. `analyze_focus_samples(star_xy=...)` is the opt-in.
"""

import numpy as np
import pytest

from calibration.analysis.vcurve import analyze_focus_samples
from imaging.hfd import half_max_fwhm, measure_star, measure_sweep_star, peak_fraction

SHAPE = (600, 800)  # (ny, nx)
FWHM_PER_SIGMA = 2.0 * np.sqrt(2.0 * np.log(2.0))


def _frame(stars, seed=0, ceiling=None):
    """Background 1000 +- 10 ADU plus Gaussian stars (x, y, sigma, peak); clipped at ``ceiling``."""
    rng = np.random.default_rng(seed)
    ny, nx = SHAPE
    yy, xx = np.mgrid[0:ny, 0:nx]
    img = 1000.0 + rng.normal(0, 10, SHAPE)
    for x, y, s, peak in stars:
        img += peak * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * s * s))
    return img if ceiling is None else np.minimum(img, ceiling)


class TestMeasureStar:
    def test_it_measures_the_star_near_the_pixel(self):
        m = measure_star(_frame([(400, 300, 3.0, 20000)]), (402, 298))
        assert m is not None and not m.saturated
        assert (m.x, m.y) == (pytest.approx(400, abs=0.3), pytest.approx(300, abs=0.3))
        assert m.fwhm == pytest.approx(3.0 * FWHM_PER_SIGMA, rel=0.1)
        assert m.hfd == pytest.approx(3.0 * FWHM_PER_SIGMA, rel=0.15)  # a Gaussian's HFD is its FWHM

    def test_the_brightest_in_the_radius_wins_not_the_nearest(self):
        """focus_on_star's star has nothing brighter near it, so after drift it is still the brightest."""
        m = measure_star(_frame([(400, 300, 3.0, 20000), (395, 295, 2.0, 3000)]), (395, 295))
        assert (m.x, m.y) == (pytest.approx(400, abs=0.5), pytest.approx(300, abs=0.5))

    def test_a_star_outside_the_radius_is_not_found(self):
        assert measure_star(_frame([(400, 300, 3.0, 20000)]), (460, 300), search_radius=30) is None

    def test_a_saturated_star_says_so_and_is_not_measured(self):
        m = measure_star(_frame([(400, 300, 3.0, 120000)], ceiling=65520), (400, 300))
        assert m.saturated and m.peak >= 63000
        assert np.isnan(m.hfd) and np.isnan(m.fwhm)
        assert m.flux > 0  # a lower bound, still usable to estimate a fainter limit

    def test_the_flux_is_the_star_s(self):
        sigma, peak = 3.0, 20000
        m = measure_star(_frame([(400, 300, sigma, peak)]), (400, 300))
        assert m.flux == pytest.approx(2 * np.pi * sigma * sigma * peak, rel=0.05)


def test_the_sweep_follows_a_drifting_star():
    """Unguided: 4 px a frame, 28 px over the sweep -- more than the search radius from the start."""
    frames = [_frame([(400 + 4 * i, 300, 3.0, 20000)], seed=i) for i in range(8)]
    measures = measure_sweep_star(frames, (400, 300), search_radius=10)
    assert all(m is not None for m in measures)
    assert measures[-1].x == pytest.approx(428, abs=0.5)


def test_a_frame_without_the_star_does_not_lose_it():
    frames = [_frame([(400, 300, 3.0, 20000)]), _frame([]), _frame([(402, 300, 3.0, 20000)])]
    measures = measure_sweep_star(frames, (400, 300))
    assert measures[1] is None and measures[2] is not None


class TestFwhm:
    def test_a_footprint_running_off_the_stamp_is_not_a_measurement(self):
        yy, xx = np.mgrid[0:21, 0:21]
        broad = 1000 * np.exp(-((xx - 10) ** 2 + (yy - 10) ** 2) / (2 * 20.0**2))
        assert np.isnan(half_max_fwhm(broad, 10, 10))

    def test_nothing_above_the_background_is_not_a_measurement(self):
        assert np.isnan(half_max_fwhm(np.zeros((21, 21)), 10, 10))


def test_the_peak_fraction_is_a_gaussian_s():
    """peak / flux of a Gaussian is 1 / (2 pi sigma^2), whatever its brightness."""
    img = _frame([(350, 250, 3.0, 20000), (450, 350, 3.0, 8000), (400, 300, 3.0, 120000)], ceiling=65520)
    fraction = peak_fraction(img, (300, 500, 200, 400))
    assert fraction == pytest.approx(1 / (2 * np.pi * 9.0), rel=0.1)  # the saturated star is left out


def test_no_unsaturated_star_gives_no_peak_fraction():
    img = _frame([(400, 300, 3.0, 120000)], ceiling=65520)
    assert peak_fraction(img, (300, 500, 200, 400)) is None


def test_the_v_curve_on_one_star_ignores_the_others():
    """With star_xy, a brighter, sharper neighbour in the field does not set the curve."""
    positions = [-300, -200, -100, 0, 100, 200, 300]
    samples = [
        (p, _frame([(400, 300, 2.0 + abs(p) / 60.0, 20000), (300, 250, 1.5, 15000)], seed=i))
        for i, p in enumerate(positions)
    ]
    status = analyze_focus_samples(samples, star_xy=(400, 300), min_edge_rise=None)
    result = status.analysis_result
    assert result.has_solution
    assert result.best_focus_position == pytest.approx(0, abs=40)
    assert all(s.num_stars == 1 for s in result.focus_samples)
    assert all(s.fwhm_pixels is not None for s in result.focus_samples)
