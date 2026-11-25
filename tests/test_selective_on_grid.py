# ruff: noqa: F811, F401 -- the fixtures are imported from test_solver_output_paths and used by name
"""The selective stage of `MastrometryDotNet` selects on the 2x2 solve grid: the frame is binned first, the xylist is in the
grid's pixels, CRPIX is the fibre on the grid, and the image that is solved (or kept in the solved FITS) is the grid.

`solve-field` is stubbed (as in `test_solver_output_paths.py`); what is checked is what it was given.
"""

from __future__ import annotations

import math
import re
import shlex
import subprocess
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from test_solver_output_paths import (  # noqa: F401 -- fixtures
    CD_DEGS_PER_PIXEL,
    SOLVED_DEC_DEGS,
    SOLVED_RA_DEGS,
    SOLVED_STDOUT,
    _join_cleanup,
    _no_scratch_left_from_other_tests,
    moved_to_shared,
    solver,
    unit,
)
from test_source_selection import NOISE, SKY, _star

import solvers.mastrometry as mastrometry_module
from imaging.mirror_shadow import ShadowModel
from solvers.pixel_grid import orig_to_grid_fits, roi_center_to_crpix
from solvers.selection_stage import SourceSelectionSettings
from solvers.source_selection import SelectionParams

NX, NY = 800, 600
FIBRE = (400.0, 300.0)


def _local(path: str) -> Path:
    match = re.match(r"^/cygdrive/([a-z])/", path)
    return Path(f"{match.group(1).upper()}:/{path[match.end() :]}") if match else Path(path)


def _stars() -> list[tuple[float, float]]:
    out = []
    for radius, count, phase in ((110.0, 8, 0.1), (190.0, 8, 0.4)):
        for k in range(count):
            angle = phase + 2 * math.pi * k / count
            out.append((FIBRE[0] + radius * math.cos(angle), FIBRE[1] + radius * math.sin(angle)))
    return out


@pytest.fixture
def frame_path(tmp_path) -> Path:
    frame = SKY + np.random.default_rng(3).normal(0, NOISE, (NY, NX))
    for x, y in _stars():
        _star(frame, x, y, 2e5)
    path = tmp_path / "acq" / "seq=0001,seconds=5.fits"
    path.parent.mkdir()
    fits.writeto(path, np.clip(frame, 0, 65000).astype(np.uint16))
    return path


class StubbedSolveField:
    """Records the command; for an xylist solve reads the xylist and writes the WCS the solved FITS is made from."""

    def __init__(self):
        self.commands: list[str] = []
        self.xylist: tuple[np.ndarray, np.ndarray] | None = None
        self.image: np.ndarray | None = None  # what an image solve was given

    def __call__(self, command, **_kwargs):
        self.commands.append(command)
        tokens = shlex.split(command)
        header = fits.Header()
        header["CRVAL1"], header["CRVAL2"] = SOLVED_RA_DEGS, SOLVED_DEC_DEGS
        header["CD1_1"], header["CD1_2"], header["CD2_1"], header["CD2_2"] = CD_DEGS_PER_PIXEL, 0.0, 0.0, CD_DEGS_PER_PIXEL
        if "--wcs" in tokens:
            table = fits.getdata(_local(tokens[-1]))
            self.xylist = (np.array(table["X"]), np.array(table["Y"]))
            fits.writeto(
                _local(tokens[tokens.index("--wcs") + 1]), np.zeros((2, 2), dtype=np.uint16), header, overwrite=True
            )
        else:
            self.image = fits.getdata(_local(tokens[-1]))
            fits.writeto(
                _local(tokens[tokens.index("--new-fits") + 1]), np.zeros((2, 2), dtype=np.uint16), header, overwrite=True
            )
        return subprocess.CompletedProcess(command, 0, SOLVED_STDOUT.encode(), b"")


def _solve(solver, unit, monkeypatch, frame_path):
    stub = StubbedSolveField()
    monkeypatch.setattr(mastrometry_module.subprocess, "run", stub)
    settings = SourceSelectionSettings(
        mode="selective", params=SelectionParams(seeing_scale=1.0, n_target=10, n_max=40, min_snr=25.0, grid=(2, 2))
    )
    result = solver.solve(
        unit=unit,
        phase="full",  # type: ignore[arg-type]
        full_frame_input_image_path=str(frame_path),
        source_selection=settings,
        fibre_xy=(int(FIBRE[0]), int(FIBRE[1])),
        shadow=ShadowModel(present=False, image_shape=(NY, NX)),
    )
    _join_cleanup()
    return result, stub


def test_the_xylist_is_in_the_grids_pixels_and_the_solve_is_told_the_grids_size(
    monkeypatch, solver, unit, frame_path, moved_to_shared
):
    result, stub = _solve(solver, unit, monkeypatch, frame_path)

    assert result is not None and result.succeeded
    assert len(stub.commands) == 1 and "--x-column" in stub.commands[0]  # the xylist solve; no image fallback was needed
    tokens = shlex.split(stub.commands[0])
    assert tokens[tokens.index("--width") + 1] == str(NX // 2) and tokens[tokens.index("--height") + 1] == str(NY // 2)
    assert stub.xylist is not None
    gx, gy = stub.xylist
    found = 0
    for x, y in _stars():
        expected = (orig_to_grid_fits(x + 1.0, 2), orig_to_grid_fits(y + 1.0, 2))  # FITS 1-based, on the grid
        found += int(np.hypot(gx - expected[0], gy - expected[1]).min() < 0.2)
    assert found >= 8  # the stars inside the candidate region, in the grid's pixels (not the frame's, twice as large)
    assert gx.max() < NX // 2 + 1 and gy.max() < NY // 2 + 1


def test_crpix_is_the_fibre_on_the_grid(monkeypatch, solver, unit, frame_path, moved_to_shared):
    _result, stub = _solve(solver, unit, monkeypatch, frame_path)

    tokens = shlex.split(stub.commands[0])
    assert float(tokens[tokens.index("--crpix-x") + 1]) == pytest.approx(roi_center_to_crpix(FIBRE[0], 0, 2))
    assert float(tokens[tokens.index("--crpix-y") + 1]) == pytest.approx(roi_center_to_crpix(FIBRE[1], 0, 2))


def test_the_solved_fits_is_the_grid_with_the_wcs_and_the_result_says_the_selection_ran_on_it(
    monkeypatch, solver, unit, frame_path, moved_to_shared
):
    result, _stub = _solve(solver, unit, monkeypatch, frame_path)

    solved = fits.open(frame_path.parent / "seq=0001,seconds=5,solver=mastrometry.net.fits")
    assert solved[0].data.shape == (NY // 2, NX // 2)  # type: ignore[union-attr]
    assert solved[0].header["XBINNING"] == 2 and solved[0].header["CRVAL2"] == pytest.approx(SOLVED_DEC_DEGS)  # type: ignore[union-attr]
    assert result is not None and result.selection is not None
    assert result.selection["grid_factor"] == 2 and result.selection["solved_from"] == "xylist"
    assert result.selection["counts"]["kept"] >= 8


def test_the_image_is_the_block_mean_of_the_frame(monkeypatch, solver, unit, frame_path, moved_to_shared):
    _solve(solver, unit, monkeypatch, frame_path)

    frame = fits.getdata(frame_path).astype(float)
    mean = frame.reshape(NY // 2, 2, NX // 2, 2).mean(axis=(1, 3)).astype(np.uint16)
    solved = fits.getdata(frame_path.parent / "seq=0001,seconds=5,solver=mastrometry.net.fits")
    np.testing.assert_array_equal(solved, mean)


def test_the_original_stage_downsamples_as_it_always_did(monkeypatch, solver, unit, frame_path, moved_to_shared):
    stub = StubbedSolveField()
    monkeypatch.setattr(mastrometry_module.subprocess, "run", stub)

    result = solver.solve(unit=unit, phase="full", full_frame_input_image_path=str(frame_path))  # type: ignore[arg-type]
    _join_cleanup()

    assert result is not None and result.succeeded and "--x-column" not in stub.commands[0]
    frame = fits.getdata(frame_path).astype(float)
    mean = frame.reshape(NY // 2, 2, NX // 2, 2).mean(axis=(1, 3)).astype(np.uint16)
    np.testing.assert_array_equal(stub.image, mean)  # the strided block mean gives the values the reshaped one did
