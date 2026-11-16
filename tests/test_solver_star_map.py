# ruff: noqa: F811, F401
# The fixtures are imported from test_solver_output_paths and then named as test parameters, which is
# what F811 objects to; F401 is the autouse cleanup fixture, which no test names.
"""The solver keeps a star map beside each solved frame, with `solve-field` stubbed.

`--corr` is what makes the used stars available at all: the solver used to pass `--corr none`.
The star map is made in the cleanup thread, so it must not delay the solve, and it must never
cost the artifacts that were kept before it existed.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from test_solver_output_paths import (
    SOLVED_STDOUT,
    _guide_frame,
    _join_cleanup,
    _no_scratch_left_from_other_tests,
    _scratch_leftovers,
    moved_to_shared,
    solver,
    unit,
)

import solvers.mastrometry as mastrometry_module
from common.filer import Filer, MoveGuardian

SOLVED_SIZE = 100  # the 200 x 200 sky ROI, binned 2 x 2
CD = 1.45e-4


def _path_arg(command: str, flag: str) -> Path:
    """A path argument back in the form this process can open: solve-field is given cygwin paths."""
    tokens = shlex.split(command)
    path = tokens[tokens.index(flag) + 1]
    match = re.match(r"^/cygdrive/([a-z])/", path)
    return Path(f"{match.group(1).upper()}:/{path[match.end() :]}") if match else Path(path)


def _table(path: Path, columns: dict[str, np.ndarray]) -> None:
    fits.BinTableHDU.from_columns([fits.Column(name=n, format="D", array=v) for n, v in columns.items()]).writeto(
        path, overwrite=True
    )


def _solve_field_that_matches(commands: list[str]):
    """A solve-field that solves, and leaves the corr file and the extraction list as the real one does."""

    def run(command, **_kwargs):
        commands.append(command)
        wcs = WCS(naxis=2)
        wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
        wcs.wcs.crpix = [50.5, 50.5]
        wcs.wcs.crval = [51.080486, 49.861198]
        wcs.wcs.cd = [[-CD, 0.0], [0.0, CD]]
        fits.writeto(
            _path_arg(command, "--new-fits"),
            np.zeros((SOLVED_SIZE, SOLVED_SIZE), dtype=np.uint16),
            wcs.to_header(),
            overwrite=True,
        )
        rng = np.random.default_rng(5)
        x, y = rng.uniform(5, 95, 14), rng.uniform(5, 95, 14)
        ra, dec = wcs.all_pix2world(x, y, 1)
        _table(
            _path_arg(command, "--corr"),
            {"field_x": x, "field_y": y, "index_ra": ra, "index_dec": dec, "match_weight": np.ones(14)},
        )
        _table(
            _path_arg(command, "--dir") / "downsampled_seq=0001,seconds=5.axy",
            {"X": np.append(x, [20.0, 80.0]), "Y": np.append(y, [20.0, 80.0])},
        )
        return subprocess.CompletedProcess(command, 0, SOLVED_STDOUT.encode(), b"")

    return run


def test_the_solver_asks_for_the_correspondences_not_none(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    commands: list[str] = []
    monkeypatch.setattr(mastrometry_module.subprocess, "run", _solve_field_that_matches(commands))
    frame = _guide_frame(tmp_path / "acquisition", "seq=0001,seconds=5.fits")

    solver.solve(unit=unit, phase="sky", full_frame_input_image_path=str(frame))
    _join_cleanup()

    tokens = shlex.split(commands[0])
    corr, new_fits = tokens[tokens.index("--corr") + 1], tokens[tokens.index("--new-fits") + 1]
    assert corr != "none"
    assert corr == new_fits.removesuffix(".fits") + ".corr"  # named from the solved FITS's stem (#314)
    # the other outputs are still not kept
    assert tokens[tokens.index("--rdls") + 1] == "none" and tokens[tokens.index("--match") + 1] == "none"


def test_a_solve_keeps_its_star_map_beside_the_frame(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    monkeypatch.setattr(mastrometry_module.subprocess, "run", _solve_field_that_matches([]))
    frame = _guide_frame(tmp_path / "acquisition", "seq=0001,seconds=5.fits")

    solver.solve(unit=unit, phase="sky", full_frame_input_image_path=str(frame))
    _join_cleanup()

    stem = "seq=0001,seconds=5,solver=mastrometry.net"
    kept = [frame.parent / f"{stem}{suffix}" for suffix in (".fits", "-result.txt", "-stars.jpg", "-stars.json")]
    assert all(path.exists() for path in kept)
    assert sorted(moved_to_shared) == sorted(str(path) for path in kept)  # the mover gets them with the others
    assert _scratch_leftovers() == []


def test_a_star_map_leaves_no_claim_behind(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    monkeypatch.setattr(mastrometry_module.subprocess, "run", _solve_field_that_matches([]))
    frame = _guide_frame(tmp_path / "acquisition", "seq=0001,seconds=5.fits")
    claims_before = set(MoveGuardian._folder_claims)

    solver.solve(unit=unit, phase="sky", full_frame_input_image_path=str(frame))
    _join_cleanup()

    tmp = os.path.realpath(Path(Filer().ram.root, "tmp"))  # type: ignore[union-attr]
    assert not [c for c in set(MoveGuardian._folder_claims) - claims_before if c.startswith(tmp)]
    assert not [p for p in MoveGuardian._products if p.startswith(tmp)]


def test_artifacts_not_kept_make_no_star_map(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    monkeypatch.setattr(mastrometry_module.subprocess, "run", _solve_field_that_matches([]))
    frame = _guide_frame(tmp_path / "phd2", "savF86B.tmp")

    solver.solve(unit=unit, phase="sky", full_frame_input_image_path=str(frame), keep_artifacts=False)
    _join_cleanup()

    assert sorted(frame.parent.iterdir()) == [frame]
    assert moved_to_shared == []
    assert _scratch_leftovers() == []


def test_a_star_map_that_fails_does_not_cost_the_other_artifacts(monkeypatch, solver, unit, tmp_path, moved_to_shared):
    monkeypatch.setattr(mastrometry_module.subprocess, "run", _solve_field_that_matches([]))

    def explode(**_kwargs):
        raise RuntimeError("the picture could not be drawn")

    monkeypatch.setattr(mastrometry_module, "write_star_map", explode)
    frame = _guide_frame(tmp_path / "acquisition", "seq=0001,seconds=5.fits")

    result = solver.solve(unit=unit, phase="sky", full_frame_input_image_path=str(frame))
    _join_cleanup()

    stem = "seq=0001,seconds=5,solver=mastrometry.net"
    assert result is not None and result.succeeded
    assert sorted(moved_to_shared) == sorted(str(frame.parent / f"{stem}{suffix}") for suffix in (".fits", "-result.txt"))
    assert _scratch_leftovers() == []
