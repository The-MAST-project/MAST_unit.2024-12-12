"""The narrow field focus_on_star measures in: a box of the frame about the focus center.

Pure geometry, no hardware.  Every shape and center here is in full-detector
**bin-1** pixels, ``shape`` is ``(ny, nx)`` and a box is ``(x0, x1, y0, y1)``
with exclusive upper bounds -- the convention of ``imaging.hfd``.  Only
:func:`fov_box` converts to the sweep's binning, at the very end.
"""

from __future__ import annotations

import math

from common import asi


def frame_shape_bin1(imager) -> tuple[int, int]:
    """``(ny, nx)`` of the full sensor; the ASI294MM's size if the imager cannot say."""
    nx = getattr(imager, "camera_x_size", None) if imager is not None else None
    ny = getattr(imager, "camera_y_size", None) if imager is not None else None
    if not nx or not ny:
        return asi.ASI_294MM_HEIGHT, asi.ASI_294MM_WIDTH
    return int(ny), int(nx)


def focus_center_bin1(unit_conf, fcu_version) -> tuple[float, float] | None:
    """Where focus_on_star puts its star: the fibre pixel ``(fiber_x, fiber_y)``.

    The same point the ``spec`` acquisition phase aims at, so the star lands
    exactly where the solve put it.  It stands in for the optical axis while the
    optical-center calibration is still being proven; switching to
    ``calibration.products.optical_center`` is a change to this function only
    (the acquisition would then need a solve that aims there too).

    ``None`` when this unit has no guiding ROI for its FCU version.
    """
    rois = getattr(getattr(unit_conf, "guiding", None), "rois", None) or {}
    cfg = rois.get(fcu_version)
    if cfg is None or not hasattr(cfg, "fiber_x"):
        return None
    return float(cfg.fiber_x), float(cfg.fiber_y)


def fov_box_size(shape_bin1: tuple[int, int], fov_percent: float) -> tuple[int, int]:
    """``(width, height)``: ``fov_percent`` of the frame's width and of its height (LINEAR)."""
    ny, nx = shape_bin1
    return max(1, round(nx * fov_percent / 100.0)), max(1, round(ny * fov_percent / 100.0))


def fov_box(
    shape_bin1: tuple[int, int],
    center_bin1: tuple[float, float],
    fov_percent: float,
    binning: int = 1,
) -> tuple[int, int, int, int]:
    """The measurement box about ``center_bin1``, in the sweep's BINNED pixels.

    A center too near an edge SHIFTS the box inward rather than shrinking it, so
    the field measured is always the size asked for; the star then sits off the
    box center, which costs nothing because the box only filters which stars count.
    """
    ny, nx = shape_bin1
    w, h = fov_box_size(shape_bin1, fov_percent)
    w, h = min(w, nx), min(h, ny)
    cx, cy = center_bin1
    x0 = min(max(0, round(cx - w / 2.0)), nx - w)
    y0 = min(max(0, round(cy - h / 2.0)), ny - h)
    x1, y1 = x0 + w, y0 + h
    b = int(binning)
    return x0 // b, math.ceil(x1 / b), y0 // b, math.ceil(y1 / b)
