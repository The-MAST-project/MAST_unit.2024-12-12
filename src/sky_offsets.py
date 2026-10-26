"""Whether a solved pointing's offset from its target is within tolerance.

Offsets are **plain RA** (``common.interfaces.solving.target_offset_arcsec``): the difference
of the RA coordinates in arcseconds, with no ``cos(dec)`` factor, because that is what
``mount_offset`` takes and what ``corrections.json`` records. Tolerances (``ToleranceConfig``)
are **on the sky**, so the RA side is applied in plain RA as ``ra_arcsec / cos(dec)``.

There is no cap toward the pole: the plain-RA tolerance grows as ``1/cos(dec)`` all the
way, because that is what spans the on-sky tolerance there. For a target at the pole itself
the solved RA can take any value and the Dec test alone bounds the distance, which the
unbounded tolerance gives; ``cos(radians(90))`` is about 6e-17 in floating point, not zero.
"""

import math

from common.interfaces.solving import SolvingTolerance


def plain_ra_tolerance_arcsec(on_sky_arcsec: float, dec_degs: float) -> float:
    """The plain-RA difference that spans `on_sky_arcsec` on the sky at `dec_degs`."""
    return on_sky_arcsec / math.cos(math.radians(dec_degs))


def within_tolerance(d_ra_plain_arcsec: float, d_dec_arcsec: float, tolerance: SolvingTolerance, dec_degs: float) -> bool:
    """Whether a plain-RA / Dec offset at `dec_degs` is within the on-sky `tolerance`."""
    return (
        abs(d_ra_plain_arcsec) <= plain_ra_tolerance_arcsec(tolerance.ra.arcsecond, dec_degs)  # type: ignore
        and abs(d_dec_arcsec) <= tolerance.dec.arcsecond  # type: ignore
    )
