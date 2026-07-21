"""``solve_optical_center`` pools sources across frames into one fit.

These tests enter at the solver seam -- synthetic *extractions*, not synthetic
images -- so they run in milliseconds with no photutils detection.  Real-frame
behaviour of the full ``find_optical_center`` path is guarded separately by the
old-vs-new equivalence harness (identical to 9 decimals on two 47MP frames);
what is pinned here is the pooling logic that harness cannot see.

The synthetic field is built to satisfy the REAL gates, not bypassed versions:
orientations radial about a known centre (spin-2), centroid-vs-peak offsets
pointing outward with supra-pixel magnitude (spin-1, the gate that actually
fires when enough offsets resolve), ellipticity growing as k*r.
"""

import numpy as np
import pytest

from calibration.analysis.optical_center import fit_coma_slope, solve_optical_center

SHAPE = (3000, 4000)  # (ny, nx)
TRUE_CENTER = (2100.0, 1450.0)  # deliberately off the geometric centre (1999.5, 1499.5)
K_TRUE = 0.1 / 1000  # ellipticity per px of field radius


def synthetic_extraction(n, rng, theta_noise=0.03):
    """One frame's worth of coma-elongated sources about TRUE_CENTER."""
    ny, nx = SHAPE
    cx, cy = TRUE_CENTER
    # Sample the frame, keep sources outside the min_field_radius cut (which is
    # taken about the GEOMETRIC centre) so the default filter does not starve us.
    x = rng.uniform(0, nx - 1, n * 4)
    y = rng.uniform(0, ny - 1, n * 4)
    rr_geom = np.hypot(x - (nx - 1) / 2, y - (ny - 1) / 2)
    m = rr_geom >= 0.45 * np.hypot((nx - 1) / 2, (ny - 1) / 2)
    x, y = x[m][:n], y[m][:n]
    assert len(x) == n, "not enough margin sources sampled"

    dx, dy = x - cx, y - cy
    r = np.hypot(dx, dy)
    radial = np.arctan2(dy, dx)
    # orientation = radial direction, wrapped to (-pi/2, pi/2] like SourceCatalog
    theta = radial + rng.normal(0, theta_noise, len(x))
    theta = ((theta + np.pi / 2) % np.pi) - np.pi / 2
    # coma: centroid sits OUTWARD of the peak -> peak = centroid - offset*outward
    ux, uy = dx / r, dy / r
    return {
        "data_sub": np.zeros((2, 2)),  # plotting only; never touched here
        "shape": SHAPE,
        "x": x,
        "y": y,
        "theta": theta,
        "ellipticity": np.maximum(K_TRUE * r, 0.06),
        "area": np.full(len(x), 50.0),
        "flux": rng.uniform(1000, 5000, len(x)),
        "peak_x": x - 1.5 * ux,
        "peak_y": y - 1.5 * uy,
        "n_detected": len(x),
    }


def test_pooled_fit_recovers_the_center():
    rng = np.random.default_rng(42)
    frames = [synthetic_extraction(40, rng) for _ in range(3)]

    result = solve_optical_center(frames)

    assert result is not None
    assert result.center_x == pytest.approx(TRUE_CENTER[0], abs=15)
    assert result.center_y == pytest.approx(TRUE_CENTER[1], abs=15)
    assert result.n_detected == 120, "n_detected sums over frames"
    assert result.radiality > 0.9, "synthetic field is cleanly radial"


def test_pooling_rescues_frames_too_thin_to_fit_alone():
    """The reason pooling exists: per-frame fits fail or scatter; one pooled fit
    uses every source.  Each frame here has fewer sources than ``min_sources``,
    so alone it must be rejected -- together they solve."""
    rng = np.random.default_rng(7)
    thin = [synthetic_extraction(8, rng) for _ in range(3)]

    assert solve_optical_center([thin[0]]) is None, "8 < min_sources=12 must fail alone"
    pooled = solve_optical_center(thin)

    assert pooled is not None
    assert pooled.center_x == pytest.approx(TRUE_CENTER[0], abs=40)
    assert pooled.center_y == pytest.approx(TRUE_CENTER[1], abs=40)


def test_mixed_frame_shapes_are_refused():
    """Different shapes = different coordinate frames; pooling them would move
    the centre silently.  Refuse, never guess."""
    rng = np.random.default_rng(1)
    a = synthetic_extraction(20, rng)
    b = synthetic_extraction(20, rng)
    b["shape"] = (SHAPE[0], SHAPE[1] + 2)

    assert solve_optical_center([a, b]) is None


def test_empty_input_is_none():
    assert solve_optical_center([]) is None


def test_slope_from_pooled_sources_recovers_the_disk():
    """The downstream consumer: pooled x/y/ellipticity feed ``fit_coma_slope``
    with the fitted centre, yielding the low-coma radius.  End-to-end over the
    synthetic field: tolerance 0.1 at k=1e-4 -> radius 1000px."""
    rng = np.random.default_rng(3)
    frames = [synthetic_extraction(40, rng) for _ in range(3)]
    center_fit = solve_optical_center(frames)
    assert center_fit is not None

    x = np.concatenate([f["x"] for f in frames])
    y = np.concatenate([f["y"] for f in frames])
    e = np.concatenate([f["ellipticity"] for f in frames])
    flux = np.concatenate([f["flux"] for f in frames])

    slope = fit_coma_slope(x, y, e, flux, center_fit.center, coma_tolerance=0.1)

    assert slope is not None
    assert slope.low_coma_radius == pytest.approx(1000.0, rel=0.1)
