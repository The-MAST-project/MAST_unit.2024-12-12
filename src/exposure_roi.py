"""ROI parameter handling for the `expose` endpoint.

Kept in its own module, free of imports, so it can be tested without dragging in the
unit's hardware stack -- importing `unit` costs ~1400 modules and pulls in PlaneWave,
PHD2 and the solver, which is a poor dependency for four lines of arithmetic and one
that would inherit any module-level side effect anything in that chain ever grows.
"""

from __future__ import annotations


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
