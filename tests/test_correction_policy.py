"""CorrectionPolicy: the PHD2 port (hysteresis, min-move dead-band, clamp) as a pure policy.
Synthetic error sequences; no hardware. Design §8."""

from __future__ import annotations

import random

import pytest

from mast_guider.correction_policy import CorrectionPolicy


def test_correction_follows_the_error_sign_and_gain():
    # no hysteresis, no dead-band: the correction is just aggressiveness * error, same sign.
    p = CorrectionPolicy(aggressiveness_ra=0.5, aggressiveness_dec=0.5, hysteresis_ra=0.0, hysteresis_dec=0.0)
    out = p.update(4.0, -2.0)
    assert out is not None
    assert out[0] == pytest.approx(2.0)
    assert out[1] == pytest.approx(-1.0)


def test_dead_band_returns_none():
    p = CorrectionPolicy(min_move_ra_arcsec=0.5, min_move_dec_arcsec=0.5, hysteresis_ra=0.0, hysteresis_dec=0.0)
    # 0.7 * 0.1 = 0.07 arcsec on each axis, below the 0.5 dead-band -> nothing to move for
    assert p.update(0.1, 0.1) is None


def test_dead_band_is_per_axis():
    p = CorrectionPolicy(min_move_ra_arcsec=0.5, min_move_dec_arcsec=0.5, hysteresis_ra=0.0, hysteresis_dec=0.0)
    out = p.update(0.1, 10.0)  # RA in the dead-band, Dec well outside
    assert out is not None
    assert out[0] == 0.0
    assert out[1] != 0.0


def test_clamp_caps_a_single_correction():
    p = CorrectionPolicy(aggressiveness_ra=1.0, aggressiveness_dec=1.0, hysteresis_ra=0.0, hysteresis_dec=0.0, max_correction_arcsec=2.0)
    out = p.update(100.0, -100.0)
    assert out == (2.0, -2.0)


def test_hysteresis_lags_a_step():
    # aggressiveness 1, hysteresis 0.1: a step of 10 yields 9.0 the first cycle (0.9*10), ramping up
    p = CorrectionPolicy(aggressiveness_ra=1.0, hysteresis_ra=0.1, max_correction_arcsec=100.0)
    first = p.update(10.0, 0.0)[0]
    second = p.update(10.0, 0.0)[0]
    assert first == pytest.approx(9.0)
    assert first < second < 10.0  # converging toward the error, never past it


def test_never_exceeds_the_clamp():
    p = CorrectionPolicy(aggressiveness_ra=0.7, aggressiveness_dec=0.7, max_correction_arcsec=1.5)
    rng = random.Random(0)
    for _ in range(500):
        out = p.update(rng.uniform(-50, 50), rng.uniform(-50, 50))
        if out is not None:
            assert abs(out[0]) <= 1.5 + 1e-9 and abs(out[1]) <= 1.5 + 1e-9


def test_closed_loop_converges_without_sign_flip():
    # drive a fake closed loop: the mount applies the correction, shrinking the error each cycle
    p = CorrectionPolicy(aggressiveness_ra=0.7, hysteresis_ra=0.1, max_correction_arcsec=5.0)
    error = 10.0
    prev_sign = 1.0
    for _ in range(60):
        out = p.update(error, 0.0)
        corr = 0.0 if out is None else out[0]
        error -= corr  # the mount moved by corr, reducing the error
        assert error > -0.5  # never overshoots materially negative
        if abs(error) > 0.2:
            assert (error > 0) == (prev_sign > 0)  # same sign while still converging
    assert abs(error) < 0.2  # converged


def test_reset_forgets_history():
    p = CorrectionPolicy(aggressiveness_ra=1.0, hysteresis_ra=0.5, max_correction_arcsec=100.0)
    p.update(10.0, 0.0)
    p.reset()
    # after reset the history is zero again, so a fresh step behaves like the first ever
    assert p.update(10.0, 0.0)[0] == pytest.approx(5.0)  # 0.5*10 + 0.5*0
