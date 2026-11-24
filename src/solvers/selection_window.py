"""The window the source selection searches, and how it grows: pure geometry, no images and no solver.

The selection starts at the low-coma region about the fibre pixel and widens it until it has enough stars. Detecting over
the whole frame first and widening afterwards pays for all of it even when the first window would do, so the window
decides what is *detected*, not only what is chosen.

**The window** is a square about the centre, ``half_size`` from it to a side, clipped to the frame. The half-sizes
with no shadow are the selection's own: the low-coma radius, widened by ``growth`` each step, until the window is the frame.

**A shadow displaces the window outward.** Sources in the shadow are rejected, so detecting there is wasted. A coarse mask
(`GridMask`) says which cells a star could be chosen in; the window of each step is the smallest, at least the plain one,
whose *allowed* area is that of the plain window. With no shadow every cell is allowed and the window is the plain one,
so there is one rule and no special case. A window is the last one (``exhausted``) when it is the frame, or when it holds all
the allowed area there is.

Nothing here knows about stars. `solvers/source_selection.py` builds the mask from a shadow model and runs the loop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

#: ``(x0, y0, x1, y1)``, pixels, ``x1`` and ``y1`` exclusive.
Box = tuple[int, int, int, int]


@dataclass(frozen=True)
class GridMask:
    """Which cells may hold a chosen star: ``cells[j, i]`` covers x in ``[i*cell, (i+1)*cell)`` and likewise y in ``j``."""

    cells: np.ndarray
    cell: int
    shape: tuple[int, int]  # (ny, nx) of the frame

    @classmethod
    def open(cls, shape: tuple[int, int], cell: int = 16) -> GridMask:
        """Every cell allowed: no shadow."""
        ny, nx = shape
        return cls(np.ones((math.ceil(ny / cell), math.ceil(nx / cell)), dtype=bool), cell, shape)

    @property
    def prefix(self) -> np.ndarray:
        """Summed-area table of the cells, one row and column larger, so a box's cell count is four lookups."""
        cached = self.__dict__.get("_prefix")
        if cached is None:
            cached = np.zeros((self.cells.shape[0] + 1, self.cells.shape[1] + 1), dtype=np.int64)
            cached[1:, 1:] = self.cells.astype(np.int64).cumsum(axis=0).cumsum(axis=1)
            object.__setattr__(self, "_prefix", cached)
        return cached

    @property
    def total_area(self) -> float:
        return float(self.prefix[-1, -1]) * self.cell**2

    def area(self, box: Box) -> float:
        """Allowed area in ``box``, pixels squared; a cell counts whole, so one cut by the frame's edge is over-counted."""
        c = self.cell
        i0, j0 = box[0] // c, box[1] // c
        i1, j1 = math.ceil(box[2] / c), math.ceil(box[3] / c)
        p = self.prefix
        return float(p[j1, i1] - p[j0, i1] - p[j1, i0] + p[j0, i0]) * c**2


@dataclass(frozen=True)
class Window:
    half_size: float  # the reach: every pixel within this of the centre is inside the box
    box: Box
    allowed_area: float
    exhausted: bool  # the last window: the frame, or all the allowed area


def square_box(centre: tuple[float, float], half_size: float, shape: tuple[int, int], cell: int) -> Box:
    """The square about ``centre``, snapped outward to the cell grid and clipped to the frame."""
    ny, nx = shape
    x0 = max(0, int(math.floor((centre[0] - half_size) / cell)) * cell)
    y0 = max(0, int(math.floor((centre[1] - half_size) / cell)) * cell)
    x1 = min(nx, int(math.ceil((centre[0] + half_size + 1) / cell)) * cell)
    y1 = min(ny, int(math.ceil((centre[1] + half_size + 1) / cell)) * cell)
    return x0, y0, x1, y1


def box_area(box: Box) -> float:
    return float((box[2] - box[0]) * (box[3] - box[1]))


def window_schedule(mask: GridMask, centre: tuple[float, float], low_coma_radius: float, growth: float) -> list[Window]:
    """The windows, smallest first. Step ``k`` has the plain half-size ``low_coma_radius * growth**k``; the window is the
    smallest of at least that size whose allowed area is the plain (shadow-free) window's.

    The last has ``exhausted`` set. ``growth`` must exceed 1.
    """
    if growth <= 1.0:
        raise ValueError(f"growth must exceed 1, not {growth}")
    ny, nx = mask.shape
    frame: Box = (0, 0, nx, ny)
    reach_limit = float(max(nx, ny))
    total = mask.total_area
    windows: list[Window] = []
    plain = max(float(low_coma_radius), float(mask.cell))
    previous: Box | None = None
    while True:
        want = box_area(square_box(centre, plain, mask.shape, mask.cell))
        want = min(want, total)
        half = plain
        box = square_box(centre, half, mask.shape, mask.cell)
        while mask.area(box) < want and half < reach_limit:
            half = min(reach_limit, half + mask.cell)
            box = square_box(centre, half, mask.shape, mask.cell)
        exhausted = box == frame or mask.area(box) >= total
        if box != previous:
            windows.append(Window(half_size=half, box=box, allowed_area=mask.area(box), exhausted=exhausted))
            previous = box
        elif exhausted and windows and not windows[-1].exhausted:
            windows[-1] = Window(windows[-1].half_size, windows[-1].box, windows[-1].allowed_area, True)
        if exhausted:
            return windows
        plain *= growth


@dataclass(frozen=True)
class Tile:
    """A rectangle to detect in, and the part of it whose sources it owns (the rest is padding, for neighbours)."""

    core: Box
    box: Box  # core plus the padding, snapped outward to ``align`` and clipped to the frame


def _snap(box: Box, pad: int, align: int, shape: tuple[int, int]) -> Box:
    ny, nx = shape
    x0 = max(0, (box[0] - pad) // align * align)
    y0 = max(0, (box[1] - pad) // align * align)
    x1 = min(nx, math.ceil((box[2] + pad) / align) * align)
    y1 = min(ny, math.ceil((box[3] + pad) / align) * align)
    return x0, y0, x1, y1


def plan_tiles(rect: Box, mask: GridMask, pad: int, align: int) -> list[Tile]:
    """The tiles that cover the allowed cells of ``rect``: one per connected piece of allowed area, over its bounding box.

    Each tile's origin is a multiple of ``align`` (the background mesh's box size), so the mesh cells in the tile are the
    frame's own and the background inside the core agrees with a whole-frame estimate; ``pad`` is how far the tile reaches
    past the core on every side, enough for the mesh's filter, the blend distance and a centroid window.
    """
    c = mask.cell
    i0, j0 = rect[0] // c, rect[1] // c
    i1, j1 = math.ceil(rect[2] / c), math.ceil(rect[3] / c)
    sub = mask.cells[j0:j1, i0:i1]
    if not sub.any():
        return []
    labels, _count = ndimage.label(sub, structure=np.ones((3, 3), dtype=int))
    ny, nx = mask.shape
    tiles: list[Tile] = []
    for found in ndimage.find_objects(labels):
        if found is None:
            continue
        rows, cols = found
        core: Box = (
            max(rect[0], (i0 + cols.start) * c),
            max(rect[1], (j0 + rows.start) * c),
            min(rect[2], nx, (i0 + cols.stop) * c),
            min(rect[3], ny, (j0 + rows.stop) * c),
        )
        if core[2] > core[0] and core[3] > core[1]:
            tiles.append(Tile(core=core, box=_snap(core, pad, align, mask.shape)))
    return tiles


__all__ = [
    "Box",
    "GridMask",
    "Tile",
    "Window",
    "box_area",
    "plan_tiles",
    "square_box",
    "window_schedule",
]
