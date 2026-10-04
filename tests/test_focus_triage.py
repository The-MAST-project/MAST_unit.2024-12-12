"""Phase-0 triage must not call a frame without stars "near focus".

On mast00 2026-10-01 every run triaged "near", from 500 ticks out to 10000.  At
2000 and beyond the stars were large faint donuts, but a 3-sigma detection pass
still found hundreds of small sources, each filling the minimum HFD aperture, and
their median HFD (~12.7 px) sat comfortably under `near_hfd_max_px`.  So
far-from-focus runs went straight to a V-curve.  The fix first counts sources
with a bright core at their centroid -- which a donut, centred on its hole,
lacks.  See test_focus_replay_2026_10_01 for the same on the real frames.
"""

from __future__ import annotations

import numpy as np
import pytest

from calibration.phases.focuser import FocuserCalibrator
from common.config.calibration import FocuserCalibrationSettings
from imaging.hfd import assess_focus_regime, count_significant_sources, frame_hfd

SHAPE = (512, 512)
SKY, NOISE = 1000.0, 20.0


def _add(img, rng, n, peak_snr, sigma):
    yy, xx = np.mgrid[: SHAPE[0], : SHAPE[1]]
    for x, y in rng.uniform(40, SHAPE[0] - 40, size=(n, 2)):
        img += peak_snr * NOISE * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma**2))
    return img


def _noise(seed=1):
    """Sky noise plus faint lumps that extract at 3 sigma but are not stars.

    Plain white noise does not extract at all, which would make the old failure
    unreproducible here; the real frames clearly have structure that does
    (hundreds of detections at +-2000, centre visibly empty).
    """
    rng = np.random.default_rng(seed)
    return _add(rng.normal(SKY, NOISE, SHAPE), rng, 150, peak_snr=4.0, sigma=1.5)


def _stars(n=40, peak_snr=60.0, sigma=2.0, seed=2):
    return _add(_noise(seed), np.random.default_rng(seed + 100), n, peak_snr, sigma)


def test_noise_still_yields_a_plausible_hfd():
    """The failure itself: noise extracts, and its HFD looks like a near-focus star."""
    hfd, n = frame_hfd(_noise())
    assert n > 0 and np.isfinite(hfd) and hfd < 20.0
    assert assess_focus_regime(_noise(), near_hfd_max=20.0) == "near"  # the old verdict


def test_noise_has_no_significant_sources_and_stars_do():
    assert count_significant_sources(_noise()) == 0
    assert count_significant_sources(_stars(n=40)) >= 30


def _donuts(n=20, radius=25.0, width=3.0, ring_snr=40.0, seed=3):
    """Bright rings with empty centres -- what a star looks like far from focus."""
    rng = np.random.default_rng(seed)
    img = _noise(seed)
    yy, xx = np.mgrid[: SHAPE[0], : SHAPE[1]]
    for x, y in rng.uniform(60, SHAPE[0] - 60, size=(n, 2)):
        r = np.hypot(xx - x, yy - y)
        img += ring_snr * NOISE * np.exp(-((r - radius) ** 2) / (2 * width**2))
    return img


def test_donuts_have_no_bright_cores():
    """A ring's segment max is far above 10 sigma; its centroid sits in the hole.

    Not exactly zero: overlapping rings merge into one segment whose centroid can
    land on a ring -- the real +-2000 frames scored 0-2 for the same reason.
    """
    assert count_significant_sources(_donuts()) <= 2
    assert assess_focus_regime(_donuts(), near_hfd_max=20.0, near_min_stars=10) != "near"


def test_noise_is_not_near_focus():
    assert assess_focus_regime(_noise(), near_hfd_max=20.0, near_min_stars=10) != "near"


def test_a_star_field_is_near_focus():
    assert assess_focus_regime(_stars(), near_hfd_max=20.0, near_min_stars=10) == "near"


def test_too_few_stars_is_not_near_focus():
    assert assess_focus_regime(_stars(n=4), near_hfd_max=20.0, near_min_stars=10) != "near"


def test_cold_start_landing_near_focus_skips_the_donut_jump(monkeypatch):
    """`_cold_start` that finds STARS must not then be fed to the donut planner."""
    cal = FocuserCalibrator.__new__(FocuserCalibrator)
    cal.errors, cal.regime, cal._frame_seq = [], None, 0
    monkeypatch.setattr(cal, "_move_focuser", lambda *a, **k: None)
    monkeypatch.setattr(cal, "_expose", lambda *a, **k: object())
    monkeypatch.setattr(FocuserCalibrator, "_triage", staticmethod(lambda image, st: "empty"))

    def cold_start(seed, st, folder):
        cal.regime = "near"
        return seed + 1000

    monkeypatch.setattr(cal, "_cold_start", cold_start)
    monkeypatch.setattr(cal, "_donut_jump", lambda *a, **k: pytest.fail("donut jump on a near-focus frame"))
    assert cal._acquire_near_focus(5000, FocuserCalibrationSettings(), None) == 6000
