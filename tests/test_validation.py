"""GuideValidator: from a fresh solve of the current pointing, decide whether the fibre is still on
target, and turn a correctable error into a reference shift (binned px) for the loop to drive out. A
synthetic TAN solution with a known scale; no astrometry.net, no hardware. Design §3.3."""

from __future__ import annotations

import math

import pytest

from mast_guider.pixel_to_sky import PixelToSky
from mast_guider.validation import (
    VALID_ALERT,
    VALID_OK,
    VALID_SHIFT,
    VALID_SOLVE_FAILED,
    GuideValidator,
)

PIXSCALE_BINNED = 0.2616 * 2.0  # arcsec per BINNED pixel
SCALE_DEG = PIXSCALE_BINNED / 3600.0
LOCK = (180.0, 10.0)  # CRVAL of the reference solve = the fibre's target pointing
CRPIX = (1000.0, 1000.0)
UNROTATED = (-SCALE_DEG, 0.0, 0.0, SCALE_DEG)  # RA decreases with +x, Dec increases with +y
TOL = 2.0  # guiding tolerance, arcsec


class _Solution:
    """The `SolvingSolution` fields PixelToSky.from_solution reads."""

    def __init__(self, cd=UNROTATED, ra_deg=LOCK[0], dec_deg=LOCK[1]):
        self.ra_rads = math.radians(ra_deg)
        self.dec_rads = math.radians(dec_deg)
        self.cd1_1, self.cd1_2, self.cd2_1, self.cd2_2 = cd


def _validator(**kw) -> GuideValidator:
    reference = PixelToSky.from_solution(_Solution(), crpix=CRPIX)
    return GuideValidator(reference, tolerance_arcsec=TOL, **kw)


def _dec_offset_solution(arcsec: float) -> _Solution:
    """A solve whose fibre points ``arcsec`` higher in Dec than the lock (a pure-Dec walk-off)."""
    return _Solution(dec_deg=LOCK[1] + arcsec / 3600.0)


def test_on_target_is_ok():
    r = _validator().validate(_Solution())
    assert r.decision == VALID_OK
    assert r.error_arcsec == pytest.approx(0.0, abs=1e-3)
    assert r.shift_px is None


def test_small_error_within_tolerance_is_ok():
    r = _validator().validate(_dec_offset_solution(1.0))  # 1" < 2" tol
    assert r.decision == VALID_OK
    assert r.error_arcsec == pytest.approx(1.0, abs=0.02)


def test_correctable_error_returns_a_reference_shift():
    r = _validator().validate(_dec_offset_solution(2.5))  # tol < 2.5" < 1.5*tol (3")
    assert r.decision == VALID_SHIFT
    assert r.error_arcsec == pytest.approx(2.5, abs=0.02)
    assert r.shift_px is not None
    dx, dy = r.shift_px
    assert abs(dx) < 0.05  # a pure-Dec walk-off is a pure-y shift
    assert dy > 0  # fibre points above the target -> target is below the fibre -> shift the ref +y
    assert math.hypot(dx, dy) == pytest.approx(2.5 / PIXSCALE_BINNED, rel=0.02)


def test_shift_applied_to_reference_would_null_the_error():
    # The shift is what the loop adds to the guide-star reference; driving that drift to zero moves the
    # field by the pixel-equivalent of the 2.5" error, bringing the target back onto the fibre.
    r = _validator().validate(_dec_offset_solution(2.5))
    assert math.hypot(*r.shift_px) * PIXSCALE_BINNED == pytest.approx(2.5, rel=0.02)


def test_ra_walkoff_is_a_pure_x_shift():
    sol = _Solution(ra_deg=LOCK[0] + 2.5 / 3600.0 / math.cos(math.radians(LOCK[1])))
    r = _validator().validate(sol)
    assert r.decision == VALID_SHIFT
    dx, dy = r.shift_px
    assert abs(dy) < 0.05
    assert abs(dx) > 1.0


def test_too_large_is_an_alert_not_a_chase():
    r = _validator().validate(_dec_offset_solution(5.0))  # > 1.5*tol = 3"
    assert r.decision == VALID_ALERT
    assert r.error_arcsec == pytest.approx(5.0, abs=0.05)
    assert r.shift_px is None  # do not chase a possibly-bad solve


def test_single_solve_failure_is_skipped():
    v = _validator()
    r = v.validate(None)
    assert r.decision == VALID_SOLVE_FAILED
    assert v.consecutive_failures == 1


def test_repeated_solve_failures_raise_an_alert():
    v = _validator()
    assert v.validate(None).decision == VALID_SOLVE_FAILED
    assert v.validate(None).decision == VALID_SOLVE_FAILED
    assert v.validate(None).decision == VALID_ALERT  # third in a row (default max 3)


def test_a_success_resets_the_failure_count():
    v = _validator()
    v.validate(None)
    v.validate(None)
    assert v.validate(_Solution()).decision == VALID_OK
    assert v.consecutive_failures == 0
    # it now takes three fresh failures again, not one, to alert
    assert v.validate(None).decision == VALID_SOLVE_FAILED


def test_a_solution_without_a_cd_matrix_is_treated_as_a_failure():
    v = _validator()
    r = v.validate(_Solution(cd=(None, None, None, None)))
    assert r.decision == VALID_SOLVE_FAILED
    assert v.consecutive_failures == 1
