"""The acquisition tolerance is on the sky; offsets stay in plain RA (MAST_unit#191).

The RA side of the tolerance is applied in plain RA as ``ra_arcsec / cos(Dec)``. On
2026-10-25 a strip arm on mast01 at Dec +79.9 used all 20 SPEC tries with RA offsets of
0.1-0.4" on the sky, because the plain-RA difference was compared with the on-sky 0.5".
"""

import math

import astropy.units as u
import pytest
from astropy.coordinates import Angle

from common.interfaces.solving import SolvingTolerance
from common.utils import Coord
from sky_offsets import plain_ra_tolerance_arcsec, target_offset_plain_arcsec, within_tolerance

TOLERANCE = SolvingTolerance(Angle(0.5 * u.arcsecond), Angle(0.5 * u.arcsecond))  # type: ignore
ARCSEC_PER_HOUR = 15 * 3600


def _target(ra_hours: float, dec_degs: float) -> Coord:
    return Coord(ra=Angle(ra_hours * u.hourangle), dec=Angle(dec_degs * u.deg))  # type: ignore


class TestPlainRaTolerance:
    @pytest.mark.parametrize(
        ("dec", "expected"),
        [(0.0, 0.5), (60.0, 1.0), (-60.0, 1.0), (79.9, 0.5 / math.cos(math.radians(79.9)))],
    )
    def test_scaled_by_one_over_cos_dec(self, dec, expected):
        assert plain_ra_tolerance_arcsec(0.5, dec) == pytest.approx(expected)

    @pytest.mark.parametrize("dec", [90.0, -90.0])
    def test_finite_at_the_pole(self, dec):
        tolerance = plain_ra_tolerance_arcsec(0.5, dec)
        assert math.isfinite(tolerance)
        assert tolerance > 0.5 * ARCSEC_PER_HOUR * 24


class TestTargetOffset:
    def test_plain_ra_and_dec_in_arcsec(self):
        d_ra, d_dec = target_offset_plain_arcsec(
            _target(6.0, 80.0), ra_hours=6.0 - 2.0 / ARCSEC_PER_HOUR, dec_degs=80.0 - 0.2 / 3600
        )
        assert d_ra == pytest.approx(2.0)
        assert d_dec == pytest.approx(0.2)

    def test_wraps_through_zero_hours(self):
        d_ra, _ = target_offset_plain_arcsec(_target(0.0, 0.0), ra_hours=24.0 - 1.0 / ARCSEC_PER_HOUR, dec_degs=0.0)
        assert d_ra == pytest.approx(1.0)


class TestWithinTolerance:
    def test_high_dec_offset_within_on_the_sky(self):
        """2" of plain RA at Dec +80 is 0.35" on the sky: within 0.5"."""
        assert within_tolerance(2.0, 0.2, TOLERANCE, dec_degs=80.0)

    def test_same_offset_outside_at_the_equator(self):
        assert not within_tolerance(2.0, 0.2, TOLERANCE, dec_degs=0.0)

    def test_dec_still_gates(self):
        assert not within_tolerance(0.0, 0.6, TOLERANCE, dec_degs=80.0)

    @pytest.mark.parametrize("dec", [90.0, -90.0])
    def test_at_the_pole_any_ra_passes_once_dec_is_within(self, dec):
        """At the pole the solved RA can be anything; the Dec test alone bounds the distance."""
        assert within_tolerance(180 * 3600.0, 0.3, TOLERANCE, dec_degs=dec)

    def test_three_arcmin_from_the_pole(self):
        """400" of plain RA at Dec 89.95 is 0.35" on the sky; half a turn is 6' away."""
        assert within_tolerance(400.0, 0.3, TOLERANCE, dec_degs=89.95)
        assert not within_tolerance(180 * 3600.0, 0.3, TOLERANCE, dec_degs=89.95)
