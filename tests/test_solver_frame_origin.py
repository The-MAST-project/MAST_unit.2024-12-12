"""MastrometryDotNet solves a guide frame that is a limit-frame crop of the sensor.

The SPEC phase crops to a guiding ROI and puts the WCS reference pixel at the fiber, both in
full-sensor coordinates. A frame PHD2 delivered under a limit frame starts at the limit
frame's origin, so without that origin the crop lands in the wrong place or out of bounds
(#234). With it, the ROI is clipped to the frame and the fiber pixel is carried into the
frame's own coordinates, where it may lie outside the frame.
"""

from __future__ import annotations

import shlex
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.io import fits

import solvers.mastrometry as mastrometry_module
from common.config.rois import FcuVersion
from common.filer import Filer
from common.models.statuses import ImagerPixel, ImagerRoi
from solvers.astrometry_dot_net import cygwin_to_win
from solvers.mastrometry import DOWNSAMPLE_FACTOR, MastrometryDotNet
from solvers.pixel_grid import roi_center_to_crpix

#: The guiding ROI and its fiber center, in full-sensor pixels.
ROI_X, ROI_Y, ROI_WIDTH, ROI_HEIGHT = 100, 20, 600, 200
FIBER_X, FIBER_Y = 400, 120

#: A strip to the right of the fiber, as a fixed limit frame would deliver it.
STRIP_X, STRIP_Y, STRIP_WIDTH, STRIP_HEIGHT = 500, 0, 300, 250


def _roi() -> ImagerRoi:
    roi = ImagerRoi.verbatim(x=ROI_X, y=ROI_Y, width=ROI_WIDTH, height=ROI_HEIGHT)
    roi._center = ImagerPixel(x=FIBER_X, y=FIBER_Y)
    return roi


@pytest.fixture
def solver(tmp_path) -> MastrometryDotNet:
    backend = MastrometryDotNet.__new__(MastrometryDotNet)
    backend.index_dir = tmp_path / "mast-indexes"
    return backend


@pytest.fixture
def unit(monkeypatch):
    monkeypatch.setattr(mastrometry_module, "derived_guiding_roi", lambda *args: _roi())
    guiding = SimpleNamespace(rois={FcuVersion.v2: None}, binning=1)
    return SimpleNamespace(unit_conf=SimpleNamespace(guiding=guiding), fcu_version=FcuVersion.v2, acquirer=None)


@pytest.fixture(autouse=True)
def _artifacts_stay_put(monkeypatch, tmp_path):
    monkeypatch.setattr(Filer, "move_ram_to_shared", lambda self, paths: None)
    monkeypatch.setattr(mastrometry_module, "missing_indexes", lambda folder: None)

    def scratch_dir(name):
        path = tmp_path / name
        path.mkdir()
        return path

    monkeypatch.setattr(mastrometry_module, "scratch_dir", scratch_dir)


@pytest.fixture
def solve_field(monkeypatch) -> dict:
    """Record what solve-field was asked to solve, and report no solution."""
    seen: dict = {}

    def run(command, **_kwargs):
        tokens = shlex.split(command)
        seen["crpix"] = (float(tokens[tokens.index("--crpix-x") + 1]), float(tokens[tokens.index("--crpix-y") + 1]))
        # solve-field is given cygwin paths; on Windows, open the input as this process names it
        image = Path(cygwin_to_win(tokens[-1]))
        seen["image"] = fits.getdata(image)
        seen["shape"] = seen["image"].shape
        seen["header"] = fits.getheader(image)
        return subprocess.CompletedProcess(command, 1, b"", b"did not solve")

    monkeypatch.setattr(mastrometry_module.subprocess, "run", run)
    return seen


def _frame(tmp_path: Path) -> str:
    path = tmp_path / "savSTRIP.tmp"
    with path.open("wb") as file:
        fits.writeto(file, np.zeros((STRIP_HEIGHT, STRIP_WIDTH), dtype=np.uint16))
    return str(path)


def _join_cleanup() -> None:
    for thread in threading.enumerate():
        if thread.name == "mastrometry-cleanup":
            thread.join(timeout=10)


def test_a_strip_is_cropped_to_its_overlap_with_the_roi(solver, unit, tmp_path, solve_field):
    solver.solve(unit=unit, phase="spec", full_frame_input_image_path=_frame(tmp_path), frame_origin=(STRIP_X, STRIP_Y))
    _join_cleanup()

    # overlap of x 100..700 with the strip's 500..800, and of y 20..220 with 0..250
    overlap_width, overlap_height = ROI_X + ROI_WIDTH - STRIP_X, ROI_HEIGHT
    assert solve_field["shape"] == (overlap_height // DOWNSAMPLE_FACTOR, overlap_width // DOWNSAMPLE_FACTOR)
    assert (solve_field["header"]["ROI_X"], solve_field["header"]["ROI_Y"]) == (STRIP_X, ROI_Y)


def test_the_reference_pixel_is_the_fiber_even_off_the_strip(solver, unit, tmp_path, solve_field):
    solver.solve(unit=unit, phase="spec", full_frame_input_image_path=_frame(tmp_path), frame_origin=(STRIP_X, STRIP_Y))
    _join_cleanup()

    # the crop starts at sensor (STRIP_X, ROI_Y); the fiber is 100 px left of it
    assert solve_field["crpix"] == (
        roi_center_to_crpix(FIBER_X, STRIP_X, DOWNSAMPLE_FACTOR),
        roi_center_to_crpix(FIBER_Y, ROI_Y, DOWNSAMPLE_FACTOR),
    )
    assert solve_field["crpix"][0] < 0


def test_without_an_origin_a_strip_is_still_refused(solver, unit, tmp_path, solve_field):
    """The full-frame contract is unchanged: a frame that cannot hold the ROI raises (#234)."""
    with pytest.raises(Exception, match="out of bounds"):
        solver.solve(unit=unit, phase="spec", full_frame_input_image_path=_frame(tmp_path))
    _join_cleanup()


def test_a_strip_that_misses_the_roi_is_refused(solver, unit, tmp_path, solve_field):
    with pytest.raises(Exception, match="does not overlap"):
        solver.solve(unit=unit, phase="spec", full_frame_input_image_path=_frame(tmp_path), frame_origin=(900, 0))
    _join_cleanup()


#: Sensor columns blanked by the guide-frame mask, inside the strip's overlap with the ROI.
MASK_FROM, MASK_TO = 600, 640


def _noisy_frame(tmp_path: Path) -> str:
    path = tmp_path / "savNOISY.tmp"
    data = np.random.default_rng(1).integers(100, 200, size=(STRIP_HEIGHT, STRIP_WIDTH), dtype=np.uint16)
    with path.open("wb") as file:
        fits.writeto(file, data)
    return str(path)


def test_the_masked_columns_reach_solve_field_blank(solver, unit, tmp_path, solve_field):
    solver.solve(
        unit=unit,
        phase="spec",
        full_frame_input_image_path=_noisy_frame(tmp_path),
        frame_origin=(STRIP_X, STRIP_Y),
        exclude_columns=(MASK_FROM, MASK_TO),
    )
    _join_cleanup()

    # the crop starts at sensor x STRIP_X; downsampled, the mask is these columns
    first, last = (MASK_FROM - STRIP_X) // DOWNSAMPLE_FACTOR, (MASK_TO - STRIP_X) // DOWNSAMPLE_FACTOR
    image = solve_field["image"]
    assert np.ptp(image[:, first:last]) == 0
    assert np.ptp(image[:, :first]) > 0 and np.ptp(image[:, last:]) > 0
    assert (solve_field["header"]["MASK_X0"], solve_field["header"]["MASK_X1"]) == (MASK_FROM, MASK_TO)


def test_without_a_mask_nothing_is_blanked(solver, unit, tmp_path, solve_field):
    solver.solve(
        unit=unit, phase="spec", full_frame_input_image_path=_noisy_frame(tmp_path), frame_origin=(STRIP_X, STRIP_Y)
    )
    _join_cleanup()

    assert np.ptp(solve_field["image"][:, 40:60]) > 0
    assert "MASK_X0" not in solve_field["header"]
