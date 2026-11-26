"""PixelToSky: a pixel drift at the fibre -> plain-RA / Dec arcsec, through the reference solve's WCS.
The guider works in the solve's own 2x2-binned grid (guide frames captured at binning 2), so a drift
is in binned pixels and no binning transform is involved. A synthetic TAN WCS with a known scale and
parity; no astrometry.net. Design §8."""

from __future__ import annotations

import math

import pytest
from astropy.io import fits

from mast_guider.pixel_to_sky import PixelToSky

PIXSCALE_BINNED = 0.2616 * 2.0  # arcsec per BINNED pixel (0.2616"/orig px, binning 2)
SCALE_DEG = PIXSCALE_BINNED / 3600.0
CRVAL = (180.0, 10.0)
CRPIX = (1000.0, 1000.0)


def _header(cd):
    h = fits.Header()
    h["NAXIS"] = 2
    h["NAXIS1"], h["NAXIS2"] = 2000, 2000
    h["CTYPE1"], h["CTYPE2"] = "RA---TAN", "DEC--TAN"
    h["CRPIX1"], h["CRPIX2"] = CRPIX
    h["CRVAL1"], h["CRVAL2"] = CRVAL
    h["CD1_1"], h["CD1_2"], h["CD2_1"], h["CD2_2"] = cd
    return h


# parity: RA decreases with +x (CD1_1 < 0), Dec increases with +y (CD2_2 > 0), no rotation
UNROTATED = (-SCALE_DEG, 0.0, 0.0, SCALE_DEG)


def test_zero_drift_is_zero_correction():
    p = PixelToSky.from_solved(_header(UNROTATED))
    cr, cd = p.correction(0.0, 0.0)
    assert cr == pytest.approx(0.0, abs=1e-6)
    assert cd == pytest.approx(0.0, abs=1e-6)


def test_fibre_at_crpix_points_at_crval():
    p = PixelToSky.from_solved(_header(UNROTATED))
    ra, dec = p.pointing(0.0, 0.0)
    assert ra == pytest.approx(CRVAL[0], abs=1e-6)
    assert dec == pytest.approx(CRVAL[1], abs=1e-6)


def test_x_drift_is_pure_plain_ra():
    p = PixelToSky.from_solved(_header(UNROTATED))
    cr, cd = p.correction(10.0, 0.0)  # 10 binned px of +x drift
    assert abs(cd) < 0.01  # no Dec component
    # on-sky RA shift is 10 binned px * scale; plain RA divides by cos(dec); sign negative (parity)
    expected = -10.0 * PIXSCALE_BINNED / math.cos(math.radians(CRVAL[1]))
    assert cr == pytest.approx(expected, abs=0.02)


def test_y_drift_is_pure_dec():
    p = PixelToSky.from_solved(_header(UNROTATED))
    cr, cd = p.correction(0.0, 10.0)
    assert abs(cr) < 0.01
    assert cd == pytest.approx(10.0 * PIXSCALE_BINNED, abs=0.02)


def test_correction_is_linear_in_drift():
    p = PixelToSky.from_solved(_header(UNROTATED))
    small = p.correction(5.0, 0.0)[0]
    large = p.correction(20.0, 0.0)[0]
    assert large == pytest.approx(4.0 * small, rel=0.01)


def test_rotation_maps_x_drift_onto_dec():
    # CD rotated 90 deg: +x on the sensor now moves Dec, not RA
    p = PixelToSky.from_solved(_header((0.0, SCALE_DEG, SCALE_DEG, 0.0)))
    cr, cd = p.correction(10.0, 0.0)
    assert abs(cd) > 1.0  # the drift shows up in Dec now
    assert abs(cr) < 0.05


class _Solution:
    """The handful of `SolvingSolution` fields PixelToSky.from_solution reads."""

    def __init__(self, cd, ra_deg=CRVAL[0], dec_deg=CRVAL[1]):
        self.ra_rads = math.radians(ra_deg)
        self.dec_rads = math.radians(dec_deg)
        self.cd1_1, self.cd1_2, self.cd2_1, self.cd2_2 = cd


def test_from_solution_matches_from_solved():
    # The reused-solve path (CRVAL + CD, no FITS) must agree with the header path to the micro-arcsec.
    from_sol = PixelToSky.from_solution(_Solution(UNROTATED), crpix=CRPIX)
    from_hdr = PixelToSky.from_solved(_header(UNROTATED))
    for d in [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0), (7.0, -4.0)]:
        a = from_sol.correction(*d)
        b = from_hdr.correction(*d)
        assert a[0] == pytest.approx(b[0], abs=1e-6)
        assert a[1] == pytest.approx(b[1], abs=1e-6)


def test_from_solution_zero_drift_is_zero_whatever_the_crpix():
    # The absolute CRPIX must not introduce a constant bias: correction(0,0) is zero by construction.
    for crpix in [(1.0, 1.0), (1000.0, 1000.0), (2072.0, 1411.0)]:
        p = PixelToSky.from_solution(_Solution(UNROTATED), crpix=crpix)
        assert p.correction(0.0, 0.0) == pytest.approx((0.0, 0.0), abs=1e-6)


def test_from_solution_without_cd_matrix_raises():
    # A missing parity is the silent-sign bug the CD matrix prevents; refuse rather than guess.
    with pytest.raises(ValueError):
        PixelToSky.from_solution(_Solution((None, None, None, None)), crpix=CRPIX)


def test_extra_binning_and_roi_keys_are_ignored():
    # DOWNSAMP / ROI_* may ride along in the header; PixelToSky works in the WCS grid and ignores them
    h = _header(UNROTATED)
    h["DOWNSAMP"], h["ROI_X"], h["ROI_Y"] = 2, 640, 480
    p = PixelToSky.from_solved(h)
    assert p.correction(0.0, 0.0) == pytest.approx((0.0, 0.0), abs=1e-6)
    cr, _ = p.correction(10.0, 0.0)
    assert cr == pytest.approx(-10.0 * PIXSCALE_BINNED / math.cos(math.radians(CRVAL[1])), abs=0.02)
