"""Parameter validation for the `expose` endpoint.

Kept in its own module, free of imports, so it can be tested without dragging in the
unit's hardware stack -- importing `unit` costs ~1400 modules and pulls in PlaneWave,
PHD2 and the solver, which is a poor dependency for four lines of arithmetic and one
that would inherit any module-level side effect anything in that chain ever grows.
"""

from __future__ import annotations

import math

#: Largest offset accepted between exposures. Offsets nudge a target within the field;
#: anything approaching this is a slew, which is what ra_j2000_hours/dec_j2000_degs are
#: for. The cap exists so a mistyped value cannot drive the mount somewhere unintended.
MAX_OFFSET_DEGREES = 10
MAX_OFFSET_ARCSEC = MAX_OFFSET_DEGREES * 3600


def resolve_exposure_roi(
    fiber_x: int | None,
    fiber_y: int | None,
    width: int | None,
    height: int | None,
    camera_x_size: int | None,
    camera_y_size: int | None,
) -> tuple[int, int, int, int]:
    """The four ROI parameters, all present, or a ValueError saying what is missing.

    All four omitted means "the whole sensor". Anything in between is refused rather
    than guessed: the four are not independent, so filling in the gaps would produce a
    region the caller never asked for.

    Refusing matters more than it looks. `expose` hands these straight to a thread, and
    a partial set used to arrive as None -- overriding `do_expose`'s own defaults, since
    they were passed positionally -- so `UnitRoi(None, None, 1000, None)` was built
    happily (it is a plain class with no validation) and `ImagerRoi.from_other` then died
    on `None - int`, inside a thread, after the endpoint had already returned "ok". No
    image, no error, nothing in the response.
    """
    supplied = {"fiber_x": fiber_x, "fiber_y": fiber_y, "width": width, "height": height}
    missing = [name for name, value in supplied.items() if value is None]

    if len(missing) == 4:
        if not camera_x_size or not camera_y_size:
            raise ValueError("cannot get width and height from the imager")
        return camera_x_size // 2, camera_y_size // 2, camera_x_size, camera_y_size

    if missing:
        given = [name for name in supplied if name not in missing]
        raise ValueError(
            f"incomplete ROI: {', '.join(missing)} not supplied (given: {', '.join(given)}). "
            "Supply all of fiber_x, fiber_y, width, height, or none of them for the full frame"
        )

    return fiber_x, fiber_y, width, height  # type: ignore[return-value]


def resolve_offsets(offsets: str | list[str] | list[float] | None, repeats: int, name: str) -> list[float] | None:
    """One offset per repeat, in arcseconds, or None for no offsetting.

    Offsets are plain arcsecond floats -- never sexagesimal -- so the only conversion
    needed is `float()`, and the only real work is refusing bad input in a way the
    caller can act on. `float()` was previously applied unguarded, so a value like
    "abc" left the endpoint as a ValueError and reached the client as HTTP 500 rather
    than a CanonicalResponse, inconsistent with the ra/dec parsing above it.

    Accepted: a whitespace-separated string, or a list. One value is broadcast to every
    repeat; otherwise there must be exactly `repeats` of them. Empty means no offsetting,
    the same as omitting the parameter -- it previously produced "must have N elements",
    which describes the wrong problem.

    Each value must be finite and within +/-`MAX_OFFSET_ARCSEC`. Every value that fails
    is reported together, so a list is fixed in one pass rather than one rejection at a
    time.
    """
    if offsets is None:
        return None

    values = offsets.split() if isinstance(offsets, str) else list(offsets)
    if not values:
        return None

    if len(values) not in (1, repeats):
        raise ValueError(
            f"{name} has {len(values)} values; supply exactly one (used for every repeat) "
            f"or exactly {repeats}, one per repeat"
        )

    # Every bad value is collected before reporting, not just the first: fixing a list
    # one rejection at a time is needless round trips when they are all visible here.
    converted: list[float] = []
    problems: list[str] = []
    for position, value in enumerate(values):
        try:
            number = float(value)
        except (TypeError, ValueError):
            problems.append(f"{name}[{position}]='{value}' is not a number")
            continue
        # float() also accepts "nan", "inf" and "-inf", and silently overflows "1e400"
        # to inf. Any of those would reach mount_offset() as an arcsecond count.
        if not math.isfinite(number):
            problems.append(f"{name}[{position}]='{value}' is not a finite number")
            continue
        if abs(number) > MAX_OFFSET_ARCSEC:
            problems.append(f"{name}[{position}]={number:g} exceeds the +/-{MAX_OFFSET_ARCSEC:g} arcsec limit")
            continue
        converted.append(number)

    if problems:
        raise ValueError(
            "; ".join(problems) + f" -- offsets are arcseconds, as plain decimals, within +/-{MAX_OFFSET_ARCSEC:g} "
            f"({MAX_OFFSET_DEGREES} degrees)"
        )

    return converted * repeats if len(converted) == 1 else converted
