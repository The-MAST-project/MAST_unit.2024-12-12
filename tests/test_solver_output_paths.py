"""MastrometryDotNet's output paths and scratch cleanup, with solve-field stubbed (MAST_unit#314).

PHD2 hands guide frames over as `sav*.tmp`. The solver used to derive both output paths by
replacing ".fits" in the input name, so for such a frame the solved FITS and the result
text were one path, and the read-back found text where the WCS should be.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.io import fits

import solvers.mastrometry as mastrometry_module
from common.config.rois import FcuVersion, SkyRoiConfig
from common.filer import Filer
from solvers.mastrometry import MastrometryDotNet

SOLVED_RA_DEGS = 51.080486
SOLVED_DEC_DEGS = 49.861198
CD_DEGS_PER_PIXEL = 1.45e-4

FRAME_WIDTH = 400
FRAME_HEIGHT = 300
SKY_ROI = SkyRoiConfig(sky_x=200, sky_y=150, width=200, height=200)

SOLVED_STDOUT = (
    "Field 1: solved with index index-5206-00.fits.\n"
    f"Field center: (RA,Dec) = ({SOLVED_RA_DEGS}, {SOLVED_DEC_DEGS}) deg.\n"
    "Field rotation angle: up is 10.0 degrees E of N\n"
)


def _new_fits_arg(command: str) -> Path:
    """The --new-fits path, back in the form this process can open: solve-field is given cygwin paths."""
    tokens = shlex.split(command)
    path = tokens[tokens.index("--new-fits") + 1]
    match = re.match(r"^/cygdrive/([a-z])/", path)
    return Path(f"{match.group(1).upper()}:/{path[match.end() :]}") if match else Path(path)


def _solve_field_that_solves(command, **_kwargs):
    header = fits.Header()
    header["CRVAL1"] = SOLVED_RA_DEGS
    header["CRVAL2"] = SOLVED_DEC_DEGS
    header["CD1_1"] = CD_DEGS_PER_PIXEL
    header["CD1_2"] = 0.0
    header["CD2_1"] = 0.0
    header["CD2_2"] = CD_DEGS_PER_PIXEL
    fits.writeto(_new_fits_arg(command), np.zeros((2, 2), dtype=np.uint16), header, overwrite=True)
    return subprocess.CompletedProcess(command, 0, SOLVED_STDOUT.encode(), b"")


def _solve_field_that_crashes(command, **_kwargs):
    raise OSError("solve-field could not be started")


@pytest.fixture
def solver(tmp_path) -> MastrometryDotNet:
    # __init__ only checks that the RAM disk holds the index set, which a test has no use for.
    # The folder itself must exist: solve() checks for it before every run.
    backend = MastrometryDotNet.__new__(MastrometryDotNet)
    backend.index_dir = tmp_path / "mast-indexes"
    backend.index_dir.mkdir()
    return backend


@pytest.fixture
def unit():
    acquisition = SimpleNamespace(rois={FcuVersion.v2: SKY_ROI})
    return SimpleNamespace(
        unit_conf=SimpleNamespace(acquisition=acquisition),
        fcu_version=FcuVersion.v2,
        acquirer=None,
    )


@pytest.fixture
def moved_to_shared(monkeypatch) -> list[str]:
    moved: list[str] = []
    monkeypatch.setattr(Filer, "move_ram_to_shared", lambda self, paths: moved.append(paths))
    return moved


def _guide_frame(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    data = np.random.default_rng(0).integers(0, 1000, size=(FRAME_HEIGHT, FRAME_WIDTH), dtype=np.uint16)
    # astropy names the file after the argument, so a .tmp name stays a .tmp file, as PHD2's does
    with path.open("wb") as file:
        fits.writeto(file, data)
    return path


def _scratch_folders() -> list[Path]:
    ram = Filer().ram
    assert ram is not None
    tmp = Path(ram.root, "tmp")
    return sorted(tmp.glob("tmp_*")) if tmp.exists() else []


def _scratch_leftovers() -> list[str]:
    """Everything in the scratch parent, dot-files included: the claim's `.tmp_<id>.lock`
    is beside the folder, not in it, and outlived it once per solve (seen on mast00)."""
    ram = Filer().ram
    assert ram is not None
    tmp = Path(ram.root, "tmp")
    return sorted(p.name for p in tmp.iterdir()) if tmp.exists() else []


def _join_cleanup() -> None:
    for thread in threading.enumerate():
        if thread.name == "mastrometry-cleanup":
            thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _no_scratch_left_from_other_tests():
    _join_cleanup()
    for folder in _scratch_folders():
        for file in folder.iterdir():
            file.unlink()
        folder.rmdir()
    for lock in Path(Filer().ram.root, "tmp").glob(".tmp_*.lock"):  # type: ignore[union-attr]
        lock.unlink()


def test_a_non_fits_input_reads_back_its_solution(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    monkeypatch.setattr(mastrometry_module.subprocess, "run", _solve_field_that_solves)
    frame = _guide_frame(tmp_path / "phd2", "savF86B.tmp")

    result = solver.solve(unit=unit, phase="sky", full_frame_input_image_path=str(frame))
    _join_cleanup()

    assert result is not None and result.succeeded and result.solution is not None
    assert result.solution.dec_degs == pytest.approx(SOLVED_DEC_DEGS)
    assert result.solution.cd1_1 == pytest.approx(CD_DEGS_PER_PIXEL)


def test_kept_artifacts_are_two_distinct_files_beside_the_input(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    monkeypatch.setattr(mastrometry_module.subprocess, "run", _solve_field_that_solves)
    frame = _guide_frame(tmp_path / "phd2", "savF86B.tmp")

    solver.solve(unit=unit, phase="sky", full_frame_input_image_path=str(frame))
    _join_cleanup()

    solved = frame.parent / "savF86B,solver=mastrometry.net.fits"
    result_text = frame.parent / "savF86B,solver=mastrometry.net-result.txt"
    assert fits.getheader(solved)["CRVAL2"] == pytest.approx(SOLVED_DEC_DEGS)
    assert result_text.read_text().startswith("--- command ---")
    assert sorted(moved_to_shared) == sorted([str(solved), str(result_text)])


def test_a_fits_input_keeps_its_artifact_names(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    monkeypatch.setattr(mastrometry_module.subprocess, "run", _solve_field_that_solves)
    frame = _guide_frame(tmp_path / "acquisition", "seq=0001,seconds=5.fits")

    solver.solve(unit=unit, phase="sky", full_frame_input_image_path=str(frame))
    _join_cleanup()

    assert (frame.parent / "seq=0001,seconds=5,solver=mastrometry.net.fits").exists()
    assert (frame.parent / "seq=0001,seconds=5,solver=mastrometry.net-result.txt").exists()


def test_artifacts_not_kept_leave_nothing_behind(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    monkeypatch.setattr(mastrometry_module.subprocess, "run", _solve_field_that_solves)
    frame = _guide_frame(tmp_path / "phd2", "savF86B.tmp")

    result = solver.solve(unit=unit, phase="sky", full_frame_input_image_path=str(frame), keep_artifacts=False)
    _join_cleanup()

    assert result is not None and result.succeeded
    assert sorted(frame.parent.iterdir()) == [frame]
    assert moved_to_shared == []
    assert _scratch_leftovers() == []


def test_the_scratch_folder_goes_when_the_solve_raises(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    monkeypatch.setattr(mastrometry_module.subprocess, "run", _solve_field_that_crashes)
    frame = _guide_frame(tmp_path / "phd2", "savF86B.tmp")

    with pytest.raises(OSError, match="could not be started"):
        solver.solve(unit=unit, phase="sky", full_frame_input_image_path=str(frame), keep_artifacts=False)
    _join_cleanup()

    assert _scratch_leftovers() == []


def test_kept_artifacts_leave_no_claim_behind(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    """The result is written under protect() in the scratch folder, which claims it. Moving
    the artifacts out and removing the folder used to leave the claim: an open handle, a
    lock file beside the folder and the product records, once per solve."""
    from common.filer import MoveGuardian

    monkeypatch.setattr(mastrometry_module.subprocess, "run", _solve_field_that_solves)
    frame = _guide_frame(tmp_path / "phd2", "savF86B.tmp")
    claims_before = set(MoveGuardian._folder_claims)

    solver.solve(unit=unit, phase="sky", full_frame_input_image_path=str(frame))
    _join_cleanup()

    assert _scratch_leftovers() == []
    tmp = os.path.realpath(Path(Filer().ram.root, "tmp"))  # type: ignore[union-attr]
    assert not [c for c in set(MoveGuardian._folder_claims) - claims_before if c.startswith(tmp)]
    assert not [p for p in MoveGuardian._products if p.startswith(tmp)]


# -- the solve's meta-files: how good was the solve ---------------------------------------

#: solve-field's own record of a solve, for judging how good it was: the matched field and
#: index stars (`.corr`), the match itself with its log-odds (`.match`), the index stars in
#: the field (`-indx.xyls`) and the field's sources on the sky (`.rdls`).
METAFILE_OPTIONS = {"--corr": ".corr", "--match": ".match", "--index-xyls": "-indx.xyls", "--rdls": ".rdls"}


def _option_path(command: str, option: str) -> Path:
    tokens = shlex.split(command)
    path = tokens[tokens.index(option) + 1]
    match = re.match(r"^/cygdrive/([a-z])/", path)
    return Path(f"{match.group(1).upper()}:/{path[match.end() :]}") if match else Path(path)


def _solve_field_that_solves_and_writes_metafiles(command, **kwargs):
    for option in METAFILE_OPTIONS:
        _option_path(command, option).write_bytes(b"SIMPLE  =                    T")
    return _solve_field_that_solves(command, **kwargs)


def test_solve_field_is_asked_for_its_metafiles_in_the_scratch(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    commands: list[str] = []

    def record(command, **kwargs):
        commands.append(command)
        return _solve_field_that_solves(command, **kwargs)

    monkeypatch.setattr(mastrometry_module.subprocess, "run", record)
    frame = _guide_frame(tmp_path / "phd2", "savF86B.tmp")

    solver.solve(unit=unit, phase="sky", full_frame_input_image_path=str(frame))
    _join_cleanup()

    new_fits = _new_fits_arg(commands[0])
    for option, suffix in METAFILE_OPTIONS.items():
        path = _option_path(commands[0], option)
        assert path.parent == new_fits.parent  # the scratch
        assert path.name == f"savF86B,solver=mastrometry.net{suffix}"


def test_kept_artifacts_include_the_metafiles(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    monkeypatch.setattr(mastrometry_module.subprocess, "run", _solve_field_that_solves_and_writes_metafiles)
    frame = _guide_frame(tmp_path / "acquisition", "seq=0001,seconds=5.fits")

    solver.solve(unit=unit, phase="sky", full_frame_input_image_path=str(frame))
    _join_cleanup()

    for suffix in METAFILE_OPTIONS.values():
        kept = frame.parent / f"seq=0001,seconds=5,solver=mastrometry.net{suffix}"
        assert kept.exists()
        assert str(kept) in moved_to_shared
    assert _scratch_leftovers() == []


def test_a_caller_that_discards_its_frame_can_keep_the_metafiles(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    """The pointing check and the lock nudge drop the guide frame (94 MB at full size) and
    keep no solved FITS, but the solve's meta-files and result text are a few KB."""
    monkeypatch.setattr(mastrometry_module.subprocess, "run", _solve_field_that_solves_and_writes_metafiles)
    frame = _guide_frame(tmp_path / "phd2", "savF86B.tmp")
    solves = tmp_path / "share" / "guiding" / "solves"

    solver.solve(
        unit=unit, phase="sky", full_frame_input_image_path=str(frame), keep_artifacts=False, metafiles_folder=solves
    )
    _join_cleanup()

    stem = "savF86B,solver=mastrometry.net"
    assert sorted(p.name for p in solves.iterdir()) == sorted(
        [f"{stem}{suffix}" for suffix in METAFILE_OPTIONS.values()] + [f"{stem}-result.txt"]
    )
    assert sorted(frame.parent.iterdir()) == [frame]
    assert _scratch_leftovers() == []


def test_without_a_folder_nothing_of_a_discarded_solve_is_kept(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    monkeypatch.setattr(mastrometry_module.subprocess, "run", _solve_field_that_solves_and_writes_metafiles)
    frame = _guide_frame(tmp_path / "phd2", "savF86B.tmp")

    solver.solve(unit=unit, phase="sky", full_frame_input_image_path=str(frame), keep_artifacts=False)
    _join_cleanup()

    assert sorted(frame.parent.iterdir()) == [frame]
    assert moved_to_shared == []
    assert _scratch_leftovers() == []
