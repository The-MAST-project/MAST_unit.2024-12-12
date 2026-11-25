"""MastGuider on synthetic frames: the fixed set recovers a known shift, and the robust combine
clips outliers and weights by SNR. Pure -- no hardware, no share -- but it imports the unit's source
selection (and `sep`), so it runs where those are available (a unit / the dev unit env), not on a bare
checkout. Patterned after the `calibration` tests (synthetic data, no hardware); design §8.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from mast_guider import GuideStars, MastGuider, NoMeasurementError
from mast_guider.guider import _combine
from solvers.source_selection import SelectionParams

FWHM = 7.6
SIGMA = FWHM / 2.3548200450309493


def _add_star(img: np.ndarray, x: float, y: float, peak: float, sigma: float = SIGMA) -> None:
    ny, nx = img.shape
    h = int(math.ceil(5 * sigma))
    x0, x1 = max(0, int(x) - h), min(nx, int(x) + h + 1)
    y0, y1 = max(0, int(y) - h), min(ny, int(y) + h + 1)
    ys, xs = np.mgrid[y0:y1, x0:x1]
    img[y0:y1, x0:x1] += peak * np.exp(-(((xs - x) ** 2 + (ys - y) ** 2) / (2.0 * sigma**2)))


def _frame(shape, stars, *, seed: int, peak: float = 4000.0, sky: float = 200.0, noise: float = 8.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = rng.normal(sky, noise, shape).astype(np.float64)
    for x, y in stars:
        _add_star(img, x, y, peak)
    return img


def _grid(shape, n_side: int = 7, margin: int = 250) -> list[tuple[float, float]]:
    ny, nx = shape
    return [(float(x), float(y)) for x in np.linspace(margin, nx - margin, n_side) for y in np.linspace(margin, ny - margin, n_side)]


def _params() -> SelectionParams:
    # a defined saturation level (a float synthetic frame carries none) and sep as the detector
    return SelectionParams(full_scale=65535.0, detector="sep")


def _guider(**kw) -> MastGuider:
    return MastGuider(params=_params(), **kw)


SHAPE = (1600, 1600)
CENTER = (SHAPE[1] / 2.0, SHAPE[0] / 2.0)


def test_reference_keeps_a_usable_set():
    g = _guider()
    rs = g.reference(_frame(SHAPE, _grid(SHAPE), seed=1), optical_center=CENTER, low_coma_radius=2000.0)
    assert isinstance(rs, GuideStars)
    assert rs.n >= 8
    assert math.isfinite(rs.sigma_window) and rs.sigma_window > 0


@pytest.mark.parametrize("sx,sy", [(0.0, 0.0), (3.4, -2.1), (-5.0, 4.0), (0.3, 0.2)])
def test_measure_recovers_known_shift(sx, sy):
    stars = _grid(SHAPE)
    g = _guider()
    g.reference(_frame(SHAPE, stars, seed=1), optical_center=CENTER, low_coma_radius=2000.0)
    shifted = _frame(SHAPE, [(x + sx, y + sy) for x, y in stars], seed=2)
    m = g.measure(shifted)
    assert m.n_used >= 8
    assert abs(m.dx - sx) < 0.3, (m.dx, sx)
    assert abs(m.dy - sy) < 0.3, (m.dy, sy)
    assert m.scatter_px < 0.5  # a clean field: the stars agree


def test_measure_requires_reference():
    with pytest.raises(RuntimeError):
        _guider().measure(_frame(SHAPE, _grid(SHAPE), seed=3))


def test_blank_frame_raises_rather_than_reads_on_target():
    g = _guider()
    g.reference(_frame(SHAPE, _grid(SHAPE), seed=1), optical_center=CENTER, low_coma_radius=2000.0)
    blank = np.random.default_rng(9).normal(200.0, 8.0, SHAPE)  # no stars where the set expects them
    with pytest.raises(NoMeasurementError):
        g.measure(blank)


# --- the robust combine, on offsets directly (no detector needed) ---------------------------------


def test_combine_clips_an_outlier():
    offsets = np.array([[1.0, 1.0]] * 10 + [[40.0, -30.0]])  # ten agree, one wild
    weights = np.ones(len(offsets))
    m = _combine(offsets, weights, clip_sigmas=3.0, min_stars=1)
    assert abs(m.dx - 1.0) < 1e-6 and abs(m.dy - 1.0) < 1e-6
    assert not m.used[-1] and m.n_used == 10


def test_combine_weights_by_snr_squared():
    # two equal groups so neither is a clip outlier (median sits between them); the heavier group's
    # snr**2 weight then pulls the mean toward it. (A lone high-weight disagreer is still clipped --
    # robustness runs before weighting -- so weighting only decides among the stars that agree.)
    offsets = np.array([[0.0, 0.0], [0.0, 0.0], [2.0, 0.0], [2.0, 0.0]])
    weights = np.array([1.0, 1.0, 100.0, 100.0])  # the (2,0) group is far heavier
    m = _combine(offsets, weights, clip_sigmas=10.0, min_stars=1)  # loose clip so none dropped
    assert m.n_used == 4
    assert m.dx > 1.5


def test_combine_too_few_raises():
    offsets = np.full((5, 2), math.nan)
    offsets[0] = (1.0, 1.0)
    with pytest.raises(NoMeasurementError):
        _combine(offsets, np.ones(5), clip_sigmas=3.0, min_stars=3)
