"""How far a solved pointing is from its target, and whether that is within tolerance.

Offsets are **plain RA**: the difference of the RA coordinates in arcseconds, with no
``cos(dec)`` factor, because that is what ``mount_offset`` takes and what
``corrections.json`` records. Tolerances (``ToleranceConfig``) are **on the sky**, so the
RA side is applied in plain RA as ``ra_arcsec / cos(dec)``.

There is no cap toward the pole: the plain-RA tolerance grows as ``1/cos(dec)`` all the
way, because that is what spans the on-sky tolerance there. For a target at the pole itself
the solved RA can take any value and the Dec test alone bounds the distance, which the
unbounded tolerance gives; ``cos(radians(90))`` is about 6e-17 in floating point, not zero.
"""

import math

from common.interfaces.solving import SolvingTolerance
from common.utils import Coord

ARCSEC_PER_DEGREE = 3600
DEGREES_PER_RA_HOUR = 15
DEGREES_PER_TURN = 360
HALF_TURN_DEGREES = 180


def target_offset_plain_arcsec(target: Coord, ra_hours: float, dec_degs: float) -> tuple[float, float]:
    """``(d_ra, d_dec)`` in arcsec, each target minus solved, RA wrapped the short way round."""
    d_ra_deg = (target.ra.deg - ra_hours * DEGREES_PER_RA_HOUR) % DEGREES_PER_TURN  # type: ignore
    if d_ra_deg > HALF_TURN_DEGREES:
        d_ra_deg -= DEGREES_PER_TURN
    return d_ra_deg * ARCSEC_PER_DEGREE, (target.dec.deg - dec_degs) * ARCSEC_PER_DEGREE  # type: ignore


def plain_ra_tolerance_arcsec(on_sky_arcsec: float, dec_degs: float) -> float:
    """The plain-RA difference that spans `on_sky_arcsec` on the sky at `dec_degs`."""
    return on_sky_arcsec / math.cos(math.radians(dec_degs))


def within_tolerance(d_ra_plain_arcsec: float, d_dec_arcsec: float, tolerance: SolvingTolerance, dec_degs: float) -> bool:
    """Whether a plain-RA / Dec offset at `dec_degs` is within the on-sky `tolerance`."""
    return (
        abs(d_ra_plain_arcsec) <= plain_ra_tolerance_arcsec(tolerance.ra.arcsecond, dec_degs)  # type: ignore
        and abs(d_dec_arcsec) <= tolerance.dec.arcsecond  # type: ignore
    )
