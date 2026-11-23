"""The unit's dances: fixed alt/az figures for the mount to walk, pure data and no hardware.

Alt/az, not RA/Dec: every step is inside the mount's allowed altitude and azimuth whatever the hour,
the figure is the same at any sidereal time, and an alt/az target stops tracking, so nothing drags the mount
off a step. Altitudes stay well above the mount's 15 degree observing floor. A figure never turns the mount
round the full azimuth circle except ``alto``, which does: the mount has no cable wrap, so a full turn is fine.
The foxtrot and waltz are drawn round a centre in the south; ``alto`` is drawn round the zenith.
"""

from __future__ import annotations

import math
from collections.abc import Callable

#: ``(altitude_degrees, azimuth_degrees)``
Step = tuple[float, float]

DEFAULT_STYLE = "foxtrot"


#: The centre of the circle the dances are drawn round, and its angular radius, degrees.
CENTRE_ALT, CENTRE_AZ = 60.0, 180.0
CIRCLE_RADIUS = 20.0


def circle_point(bearing_degrees: float, radius_degrees: float = CIRCLE_RADIUS) -> Step:
    """The point `radius_degrees` of arc from the centre, towards `bearing_degrees` (0 = up, 90 = towards larger azimuth)."""
    alt0, r, b = math.radians(CENTRE_ALT), math.radians(radius_degrees), math.radians(bearing_degrees)
    alt = math.asin(math.sin(alt0) * math.cos(r) + math.cos(alt0) * math.sin(r) * math.cos(b))
    daz = math.atan2(math.sin(b) * math.sin(r) * math.cos(alt0), math.cos(r) - math.sin(alt0) * math.sin(alt))
    return round(math.degrees(alt), 3), round((CENTRE_AZ + math.degrees(daz)) % 360.0, 3)


def _foxtrot() -> list[Step]:
    """A circle on the sky: twelve steps round the centre, and back to where it began."""
    return [circle_point(30.0 * k) for k in range(13)]


def _waltz() -> list[Step]:
    """Three steps to the bar: a triangle inscribed in the circle, danced twice."""
    return [circle_point(120.0 * k) for k in range(3)] * 2


#: ``alto`` is a circle round the zenith: 20 degrees of arc from it, i.e. a constant altitude of 70 degrees.
ALTO_ALT = 90.0 - CIRCLE_RADIUS


def _alto() -> list[Step]:
    """Twelve steps round the zenith at a constant altitude: a full turn of the azimuth, and back to where it began.

    The azimuth is 0, 30, ... 330 and back to 0 (the mount takes azimuths in [0, 360)); each step is the next
    30 degrees on, so the walk is one full turn.
    """
    return [(ALTO_ALT, (30.0 * k) % 360.0) for k in range(13)]


STYLES: dict[str, Callable[[], list[Step]]] = {"foxtrot": _foxtrot, "waltz": _waltz, "alto": _alto}


def dance_path(style: str = DEFAULT_STYLE) -> list[Step]:
    """The steps of ``style``. Raises ``ValueError`` naming the styles there are."""
    try:
        return STYLES[style.strip().lower()]()
    except KeyError:
        raise ValueError(f"unknown dance style {style!r}; the styles are: {', '.join(sorted(STYLES))}") from None
