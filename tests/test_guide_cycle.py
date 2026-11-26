"""GuideCycle: the loop's decision brain, composing measurement + pixel->sky + policy. Driven by a
fake guider / pixel-to-sky and a real CorrectionPolicy; the closed-loop test runs a fake sky + mount
to convergence (design §8, the mock-mount test). No camera, no real mount."""

from __future__ import annotations

import numpy as np
import pytest

from mast_guider.correction_policy import CorrectionPolicy
from mast_guider.guide_cycle import APPLIED, DEADBAND, HELD_SCATTER, STAR_LOST, GuideCycle
from mast_guider.core import Measurement, NoMeasurementError

ARCSEC_PER_PX = 0.5232  # binned pixel scale, for the fakes


def _measurement(dx, dy, *, scatter=0.2, n=12) -> Measurement:
    offsets = np.tile([float(dx), float(dy)], (n, 1))
    return Measurement(dx=float(dx), dy=float(dy), error_px=(0.05, 0.05), scatter_px=float(scatter), offsets=offsets, used=np.ones(n, bool))


class _FakeP2S:
    """A linear pixel->sky: the sky error is the drift times the pixel scale (identity parity)."""

    def correction(self, dx, dy):
        return (dx * ARCSEC_PER_PX, dy * ARCSEC_PER_PX)


class _ScriptedGuider:
    """Returns measurements from a list (or raises NoMeasurementError for a ``None`` entry), recording
    the predicted_offset it was seeded with each call."""

    def __init__(self, results):
        self._results = list(results)
        self.seeds: list[tuple[float, float]] = []
        self.i = 0

    def measure(self, frame, *, predicted_offset=(0.0, 0.0)):
        self.seeds.append(predicted_offset)
        r = self._results[self.i]
        self.i += 1
        if r is None:
            raise NoMeasurementError("scripted loss")
        return r


def _policy(**kw):
    kw.setdefault("max_correction_arcsec", 100.0)
    return CorrectionPolicy(**kw)


def test_applied_sends_a_same_sign_correction():
    g = _ScriptedGuider([_measurement(5.0, -3.0)])
    gc = GuideCycle(g, _FakeP2S(), _policy(aggressiveness_ra=0.7, aggressiveness_dec=0.7, hysteresis_ra=0.0, hysteresis_dec=0.0))
    r = gc.cycle(None)
    assert r.decision == APPLIED
    assert r.error_arcsec[0] == pytest.approx(5.0 * ARCSEC_PER_PX)
    assert r.correction_arcsec[0] == pytest.approx(0.7 * 5.0 * ARCSEC_PER_PX)
    assert r.correction_arcsec[1] < 0  # same sign as the -3 px dec drift


def test_small_drift_falls_in_the_dead_band():
    g = _ScriptedGuider([_measurement(0.1, 0.1)])
    gc = GuideCycle(g, _FakeP2S(), _policy(min_move_ra_arcsec=0.5, min_move_dec_arcsec=0.5, hysteresis_ra=0.0, hysteresis_dec=0.0))
    r = gc.cycle(None)
    assert r.decision == DEADBAND
    assert r.correction_arcsec is None
    assert r.error_arcsec is not None  # it was measured and trusted, just not worth moving for


def test_high_scatter_frame_is_held_and_does_not_move_the_seed():
    g = _ScriptedGuider([_measurement(5.0, 0.0, scatter=3.0)])
    gc = GuideCycle(g, _FakeP2S(), _policy(), max_scatter_px=1.0)
    r = gc.cycle(None)
    assert r.decision == HELD_SCATTER
    assert r.correction_arcsec is None
    assert gc.predicted_offset == (0.0, 0.0)  # an untrusted frame must not advance the search seed


def test_star_loss_is_reported_and_holds_the_seed():
    g = _ScriptedGuider([None])
    gc = GuideCycle(g, _FakeP2S(), _policy())
    r = gc.cycle(None)
    assert r.decision == STAR_LOST
    assert gc.predicted_offset == (0.0, 0.0)


def test_trusted_frame_advances_the_seed_to_the_measured_drift():
    g = _ScriptedGuider([_measurement(7.0, -4.0), _measurement(7.0, -4.0)])
    gc = GuideCycle(g, _FakeP2S(), _policy(hysteresis_ra=0.0, hysteresis_dec=0.0))
    gc.cycle(None)
    assert gc.predicted_offset == (7.0, -4.0)
    gc.cycle(None)
    assert g.seeds[1] == (7.0, -4.0)  # the second measure was seeded from the first's drift


class _FakeSky:
    """A drifting field a fake mount corrects: ``measure`` reports the current drift; a correction
    (plain RA, Dec arcsec) moves the field back by the pixel-equivalent; a steady leak is added each
    cycle to stand for real drift."""

    def __init__(self, drift, leak=(0.1, -0.05)):
        self.drift = list(map(float, drift))
        self.leak = leak

    def measure(self, frame, *, predicted_offset=(0.0, 0.0)):
        return _measurement(self.drift[0], self.drift[1], scatter=0.1)

    def apply(self, correction):
        self.drift[0] -= correction[0] / ARCSEC_PER_PX
        self.drift[1] -= correction[1] / ARCSEC_PER_PX

    def leak_a_cycle(self):
        self.drift[0] += self.leak[0]
        self.drift[1] += self.leak[1]


def test_closed_loop_converges():
    sky = _FakeSky([30.0, -20.0])
    gc = GuideCycle(sky, _FakeP2S(), _policy(aggressiveness_ra=0.7, aggressiveness_dec=0.7, hysteresis_ra=0.1, hysteresis_dec=0.1))
    for _ in range(60):
        result = gc.cycle(None)
        if result.decision == APPLIED:
            sky.apply(result.correction_arcsec)
        sky.leak_a_cycle()
    assert abs(sky.drift[0]) < 2.0  # started 30 px out, held to a small residual against the leak
    assert abs(sky.drift[1]) < 2.0
