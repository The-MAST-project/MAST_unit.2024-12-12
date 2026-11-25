"""Stages 0 to 2 of the solve pipeline (`docs/mastrometry_stages.md`, section 9): describe the input, bring it to the 2x2
solve grid, crop it to the region about the fibre -- and select the stars on that grid.

A PROTOTYPE. Nothing in `MastrometryDotNet` calls it yet. It exists to answer whether selecting on the binned grid finds the
same stars and solves the same as selecting on the full-resolution frame, which is four times the pixels.

Two kinds of input reach it, and both end up on the same grid:

* **binning 1** (the PHD2 imager, today: a full frame at binning one): the hot pixels are filled at full resolution, then
  the frame is block-averaged 2x2 in software. A block that holds a saturated pixel is set to the saturation level, so
  the selection's saturation test still sees it (a mean would hide it).
* **binning 2** (the ZWO imager, later: the camera bins in hardware): the array is used as delivered. The hot pixels cannot
  be filled before the binning; a binned pixel that contains one is filled from its binned neighbours.

Pixel conventions are `solvers/pixel_grid.py`'s: grid pixel ``g`` (0-based) covers the unbinned pixels ``2g`` and
``2g + 1`` of the grid's own origin, so its centre is at unbinned ``2g + 0.5``. The blocks are aligned to the sensor's own
pixels, so a region whose origin is odd loses its first row or column rather than straddle two blocks.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import numpy as np

from imaging.hot_pixels import HotPixels, fill_hot_pixels

from .source_selection import Selection, SelectionParams, select_sources

SOLVE_FACTOR = 2


@dataclass(frozen=True)
class FrameInfo:
    """Stage 0: what the array is."""

    binning: int  # 1 (the PHD2 imager, today) or 2 (the ZWO imager's hardware binning, later)
    full_shape: tuple[int, int]  # (ny, nx) of the unbinned sensor
    #: the array's first pixel, in unbinned full-frame pixels, 0-based (x, y); (0, 0) for a whole frame
    origin: tuple[int, int] = (0, 0)
    #: the level at which a pixel *of the array as delivered* is saturated; None: not tested
    saturation_adu: float | None = None


@dataclass
class SolveGrid:
    """Stage 1 and 2: the frame on the 2x2 solve grid, cropped to the region."""

    data: np.ndarray
    #: the unbinned full-frame pixel at the corner of the grid's first pixel's block (x, y)
    origin: tuple[int, int]
    factor: int = SOLVE_FACTOR
    hot_filled: int = 0
    saturated_blocks: int = 0

    @property
    def shape(self) -> tuple[int, int]:
        return self.data.shape  # type: ignore[return-value]

    def to_full(self, gx, gy):
        """Grid pixels (0-based, array coordinates) to unbinned full-frame pixels (0-based pixel centres)."""
        f = self.factor
        return self.origin[0] + f * np.asarray(gx, float) + (f - 1) / 2.0, self.origin[1] + f * np.asarray(gy, float) + (
            f - 1
        ) / 2.0

    def to_grid(self, x, y):
        """Unbinned full-frame pixels to grid pixels (0-based, array coordinates), the inverse of `to_full`."""
        f = self.factor
        return (np.asarray(x, float) - self.origin[0] - (f - 1) / 2.0) / f, (
            np.asarray(y, float) - self.origin[1] - (f - 1) / 2.0
        ) / f

    def fibre_fits(self, fibre_xy: tuple[float, float]) -> tuple[float, float]:
        """The fibre pixel as ``solve-field``'s ``--crpix-x/--crpix-y``: FITS 1-based, on this array."""
        gx, gy = self.to_grid(*fibre_xy)
        return float(gx) + 1.0, float(gy) + 1.0


def _region_blocks(
    region: tuple[float, float, float, float], origin: tuple[int, int], blocks: tuple[int, int], f: int
) -> tuple[int, int, int, int]:
    """``region`` (centre x, centre y, half width, half height; unbinned full-frame pixels) as blocks ``gx0:gx1, gy0:gy1``
    of an array of ``blocks`` (rows, columns) blocks that starts at ``origin``."""
    cx, cy, hw, hh = region
    gx0 = max(0, int(np.floor((cx - hw - origin[0]) / f)))
    gx1 = min(blocks[1], int(np.ceil((cx + hw - origin[0]) / f)))
    gy0 = max(0, int(np.floor((cy - hh - origin[1]) / f)))
    gy1 = min(blocks[0], int(np.ceil((cy + hh - origin[1]) / f)))
    return gx0, gx1, gy0, gy1


def to_solve_grid(
    frame: np.ndarray,
    info: FrameInfo,
    hot: HotPixels | None = None,
    region: tuple[float, float, float, float] | None = None,
) -> SolveGrid:
    """Stages 1 and 2. ``region`` is ``(centre x, centre y, half width, half height)`` in unbinned full-frame pixels."""
    f = SOLVE_FACTOR
    if info.binning not in (1, 2):
        raise ValueError(f"binning must be 1 or 2, not {info.binning}")
    x0, y0 = info.origin
    data = np.asarray(frame, dtype=float)
    hot = hot if (hot is not None and hot.matches(info.full_shape)) else None
    hot_filled = 0

    if info.binning == 1:
        skip_x, skip_y = x0 % f, y0 % f  # the blocks are the sensor's, not the array's
        data = data[skip_y:, skip_x:]
        x0, y0 = x0 + skip_x, y0 + skip_y
        data = data[: (data.shape[0] // f) * f, : (data.shape[1] // f) * f]
        if region is not None:
            # crop before binning: the region is a fraction of the frame, and so is the work. The blocks of the crop are the
            # frame's own, so this is the same grid as binning the whole frame and cropping it.
            gx0, gx1, gy0, gy1 = _region_blocks(region, (x0, y0), (data.shape[0] // f, data.shape[1] // f), f)
            data = data[gy0 * f : gy1 * f, gx0 * f : gx1 * f]
            x0, y0 = x0 + f * gx0, y0 + f * gy0
            region = None
        if hot is not None:
            hot_filled = int(
                np.count_nonzero((hot.x >= x0) & (hot.x < x0 + data.shape[1]) & (hot.y >= y0) & (hot.y < y0 + data.shape[0]))
            )
            data = hot.fill(data, origin=(x0, y0))
        # the four pixels of each block as strided views and a sum: about 5x faster than `reshape(...).mean(axis=(1, 3))`,
        # which reduces over strided axes, and the same values
        a, b, c, d = data[0::2, 0::2], data[0::2, 1::2], data[1::2, 0::2], data[1::2, 1::2]
        binned = (a + b + c + d) * 0.25
        saturated = np.zeros(binned.shape, bool)
        if info.saturation_adu is not None:
            level = info.saturation_adu
            saturated = np.maximum(np.maximum(a, b), np.maximum(c, d)) >= level
            binned[saturated] = level
        grid = binned
    else:
        if x0 % f or y0 % f:
            raise ValueError(f"a binned array must start on a block of the sensor: origin {info.origin} is odd")
        grid = data
        if hot is not None:
            gx, gy = (hot.x - x0) // f, (hot.y - y0) // f
            inside = (gx >= 0) & (gx < grid.shape[1]) & (gy >= 0) & (gy < grid.shape[0])
            hot_filled = int(np.unique(np.column_stack([gx[inside], gy[inside]]), axis=0).shape[0])
            grid = fill_hot_pixels(grid, gx[inside], gy[inside])
        saturated = (grid >= info.saturation_adu) if info.saturation_adu is not None else np.zeros(grid.shape, bool)

    result = SolveGrid(grid, (x0, y0), f, hot_filled=hot_filled, saturated_blocks=int(np.count_nonzero(saturated)))
    if region is not None:
        gx0, gx1, gy0, gy1 = _region_blocks(region, (x0, y0), grid.shape, f)
        result = SolveGrid(
            grid[gy0:gy1, gx0:gx1],
            (x0 + f * gx0, y0 + f * gy0),
            f,
            hot_filled=hot_filled,
            saturated_blocks=result.saturated_blocks,
        )
    return result


def binned_params(params: SelectionParams, factor: int = SOLVE_FACTOR) -> SelectionParams:
    """``params`` with the fields that are in pixels (the selection's are in unbinned ones) brought to the binned grid.

    The background mesh, the default seeing, the edge margin and the coma scale shrink by the factor, and the minimum area by
    its square. **The number of connected pixels a detection needs (`npixels`) does not**: scaled down to 2 it let in some
    68,000 noise blobs a frame and made the selection three times slower (mast08, 2026-11-23), with the same stars found.
    The signal-to-noise thresholds are unchanged: a block mean has half the noise.
    """
    return dataclasses.replace(
        params,
        box_size=max(8, params.box_size // factor),
        default_fwhm_px=params.default_fwhm_px / factor,
        min_area=max(2, round(params.min_area / factor**2)),
        edge_margin_extra_px=params.edge_margin_extra_px / factor,
        coma_px=params.coma_px / factor,
    )


def select_on_grid(
    grid: SolveGrid,
    params: SelectionParams | None = None,
    *,
    fibre_xy: tuple[float, float],
    low_coma_radius_px: float,
    saturation_adu: float | None = None,
    shadow=None,
) -> Selection:
    """The source selection on the grid, searching outwards from the fibre pixel from the low-coma radius (unbinned pixels).

    The `Selection`'s x and y are the grid array's pixels (0-based); `grid.to_full` converts them, and
    `solve_filtered_list(..., factor=1)` takes them as they are.
    """
    p = binned_params(params or SelectionParams(), grid.factor)
    if saturation_adu is not None:
        p = dataclasses.replace(p, full_scale=saturation_adu)
    cx, cy = grid.to_grid(*fibre_xy)
    return select_sources(
        grid.data,
        shadow=shadow,
        optical_center=(float(cx), float(cy)),
        low_coma_radius=low_coma_radius_px / grid.factor,
        params=p,
    )
