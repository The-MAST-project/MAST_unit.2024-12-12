"""The survey's solve of the kept list, with `solve-field` stubbed.

The stub reads the xylist it is handed, "matches" the first few rows, and writes the `.corr` and
`.wcs` a real run would -- so what is under test is the xylist written, the command built, the
conversion through the solve grid and back, and what is done with every way a solve can end.
"""

from __future__ import annotations

import csv
import subprocess
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS
from PIL import Image
from test_source_selection import _frame
from test_source_survey import _write, survey

from solvers import pixel_grid, survey_solve
from solvers.survey_solve import SolveSettings, from_solve_grid, solve_filtered_list, to_solve_grid, write_xylist

CRVAL = (51.0, 49.8)


def _flag(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def _stub(matched: int = 12, returncode: int = 0, write_corr: bool = True, calls: list | None = None):
    """A solve-field that matches the first `matched` rows of the xylist it is given."""

    def run(command, **_kwargs):
        args = list(command)
        if calls is not None:
            calls.append(args)
        if returncode:
            return subprocess.CompletedProcess(args, returncode, b"", b"Did not solve.\n")
        width, height = int(_flag(args, "--width")), int(_flag(args, "--height"))
        scale = float(_flag(args, "--scale-low")) / 0.9
        wcs = WCS(naxis=2)
        wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
        wcs.wcs.crpix = [(width + 1) / 2.0, (height + 1) / 2.0]
        wcs.wcs.crval = list(CRVAL)
        wcs.wcs.cd = [[-scale / 3600.0, 0.0], [0.0, scale / 3600.0]]
        fits.writeto(Path(_flag(args, "--wcs")), np.zeros((2, 2), np.uint8), wcs.to_header(), overwrite=True)
        table = fits.getdata(Path(args[-1]))
        x, y = np.asarray(table["X"][:matched], float), np.asarray(table["Y"][:matched], float)
        ra, dec = wcs.all_pix2world(x, y, 1)
        if write_corr:
            columns = {"field_x": x, "field_y": y, "index_ra": ra, "index_dec": dec, "match_weight": np.full(len(x), 0.9)}
            fits.BinTableHDU.from_columns([fits.Column(name=n, format="D", array=v) for n, v in columns.items()]).writeto(
                Path(_flag(args, "--corr")), overwrite=True
            )
        return subprocess.CompletedProcess(args, 0, b"Field 1: solved with index index-5206-00.fits.\n", b"")

    return run


@pytest.fixture(autouse=True)
def _posix_command(monkeypatch):
    """The command is a list of arguments here; the Windows form is tested on its own."""
    monkeypatch.setattr(survey_solve, "ON_WINDOWS", False)


@pytest.fixture
def solve_field_binary(tmp_path) -> str:
    binary = tmp_path / "bin" / "solve-field"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return str(binary)


def _xylist(n=30, shape=(900, 1200), seed=2) -> np.ndarray:
    rng = np.random.default_rng(seed)
    flux = np.sort(rng.uniform(1000, 9000, n))[::-1]
    return np.column_stack([rng.uniform(20, shape[1] - 20, n), rng.uniform(20, shape[0] - 20, n), flux])


def _settings(binary, **overrides) -> SolveSettings:
    return SolveSettings(index_dir="/idx", solve_field=binary, **overrides)


def _solve(tmp_path, binary, monkeypatch, *, xylist=None, stub=None, **overrides):
    monkeypatch.setattr(survey_solve.subprocess, "run", stub or _stub())
    xylist = _xylist() if xylist is None else xylist
    outcome = solve_filtered_list(
        xylist, (900, 1200), 2, _settings(binary, **overrides), tmp_path / "work", tmp_path / "solve.txt", "frame"
    )
    return outcome, xylist


# ----- the grid --------------------------------------------------------------------------------


@pytest.mark.parametrize("factor", [1, 2])
@pytest.mark.parametrize("origin", [0, 1])
def test_a_position_goes_out_and_comes_back(factor, origin):
    x, y = np.array([0.0, 7.25, 1199.0]), np.array([0.0, 3.5, 899.0])

    gx, gy = to_solve_grid(x, y, factor, origin)
    bx, by = from_solve_grid(gx, gy, factor, origin)

    assert bx == pytest.approx(x, abs=1e-9) and by == pytest.approx(y, abs=1e-9)


def test_the_solve_grid_is_pixel_grids_convention_not_a_division():
    """0-based x = 0 is FITS 1; binned by 2 that is (1 + 0.5) / 2 = 0.75, not 0.5."""
    gx, _ = to_solve_grid(np.array([0.0, 10.0]), np.array([0.0, 10.0]), 2, origin=1)

    assert gx == pytest.approx([pixel_grid.orig_to_grid_fits(1.0, 2), pixel_grid.orig_to_grid_fits(11.0, 2)])
    assert gx[0] == pytest.approx(0.75)


def test_origin_zero_is_one_pixel_below_origin_one():
    one, _ = to_solve_grid(np.array([5.0]), np.array([5.0]), 2, origin=1)
    zero, _ = to_solve_grid(np.array([5.0]), np.array([5.0]), 2, origin=0)

    assert one - zero == pytest.approx(1.0)


def test_the_binned_grid_is_cropped_to_whole_blocks():
    assert survey_solve.grid_size((901, 1203), 2) == (601, 450)


# ----- the xylist and the command --------------------------------------------------------------


def test_the_xylist_keeps_the_order_and_the_columns(tmp_path):
    xylist = _xylist(10)
    write_xylist(tmp_path / "f.xyls", xylist, factor=2, origin=1)

    table = fits.getdata(tmp_path / "f.xyls")

    assert table.names == ["X", "Y", "FLUX"]
    assert np.array_equal(table["FLUX"], xylist[:, 2]) and np.all(np.diff(table["FLUX"]) <= 0)
    gx, gy = to_solve_grid(xylist[:, 0], xylist[:, 1], 2, 1)
    assert np.allclose(table["X"], gx) and np.allclose(table["Y"], gy)


def test_the_command_is_the_units_with_an_xylist_for_the_input(tmp_path):
    args = survey_solve.build_args(
        SolveSettings(index_dir="/idx", solve_field="solve-field"),
        tmp_path / "f.xyls",
        tmp_path / "f.wcs",
        tmp_path / "f.corr",
        tmp_path,
        (600, 450),
        tweak_order=2,
    )

    assert _flag(args, "--width") == "600" and _flag(args, "--height") == "450"
    assert _flag(args, "--uniformize") == "0"
    assert _flag(args, "--tweak-order") == "2" and "--no-tweak" not in args  # tweak stays on
    assert _flag(args, "--corr").endswith("f.corr") and _flag(args, "--corr") != "none"
    assert _flag(args, "--rdls") == "none" and _flag(args, "--match") == "none"
    assert "--crpix-center" in args and args[-1].endswith("f.xyls")
    assert float(_flag(args, "--scale-low")) == pytest.approx(0.9 * 0.2616 * 2)
    assert "--ra" not in args  # blind unless hinted


def test_a_position_hint_is_passed_only_when_given(tmp_path):
    args = survey_solve.build_args(
        SolveSettings(index_dir="/i", solve_field="s", ra_deg=51.0, dec_deg=49.8, radius_deg=1.5),
        tmp_path / "f.xyls",
        tmp_path / "f.wcs",
        tmp_path / "f.corr",
        tmp_path,
        (10, 10),
        1,
    )

    assert (_flag(args, "--ra"), _flag(args, "--dec"), _flag(args, "--radius")) == ("51.0", "49.8", "1.5")
    assert _flag(args, "--tweak-order") == "1"


# ----- the solve -------------------------------------------------------------------------------


@pytest.mark.parametrize("factor", [1, 2])
@pytest.mark.parametrize("origin", [0, 1])
def test_the_used_stars_come_back_as_the_array_pixels_that_were_fed(
    tmp_path, solve_field_binary, monkeypatch, factor, origin
):
    outcome, xylist = _solve(tmp_path, solve_field_binary, monkeypatch, factor=factor, origin=origin)

    assert outcome.status == "ok" and outcome.fed == 30 and outcome.used == 12
    assert outcome.used_x == pytest.approx(xylist[:12, 0], abs=1e-6)
    assert outcome.used_y == pytest.approx(xylist[:12, 1], abs=1e-6)


def test_residuals_and_influence_are_read_back(tmp_path, solve_field_binary, monkeypatch):
    outcome, _ = _solve(tmp_path, solve_field_binary, monkeypatch)

    assert outcome.residual_rms_arcsec == pytest.approx(0.0, abs=1e-3)  # the stub's index is the same WCS
    assert outcome.scale_arcsec == pytest.approx(0.2616 * 2, rel=1e-3)
    assert outcome.crval == pytest.approx(CRVAL)
    assert outcome.fibre_shift_arcsec is not None and len(outcome.fibre_shift_arcsec) == 12
    assert outcome.weight == pytest.approx(np.full(12, 0.9))


def test_too_few_used_stars_give_no_influence_but_still_a_result(tmp_path, solve_field_binary, monkeypatch):
    outcome, _ = _solve(tmp_path, solve_field_binary, monkeypatch, stub=_stub(matched=5))

    assert outcome.status == "ok" and outcome.used == 5 and outcome.fibre_shift_arcsec is None


def test_the_command_and_the_output_are_kept(tmp_path, solve_field_binary, monkeypatch):
    _solve(tmp_path, solve_field_binary, monkeypatch)

    log = (tmp_path / "solve.txt").read_text()
    assert "--uniformize 0" in log and "solved with index" in log and "--- status ---\nok" in log


def test_a_solve_field_that_is_not_there_is_a_result_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(survey_solve.subprocess, "run", _stub())
    outcome = solve_filtered_list(
        _xylist(),
        (900, 1200),
        2,
        _settings(str(tmp_path / "absent" / "solve-field")),
        tmp_path / "w",
        tmp_path / "s.txt",
        "f",
    )

    assert outcome.status.startswith("failed:") and "not found" in outcome.status
    assert "not found" in (tmp_path / "s.txt").read_text()


def test_a_solve_that_exits_nonzero_is_a_failure_with_its_output_kept(tmp_path, solve_field_binary, monkeypatch):
    outcome, _ = _solve(tmp_path, solve_field_binary, monkeypatch, stub=_stub(returncode=1))

    assert outcome.status == "failed: solve-field exited 1" and outcome.used == 0
    assert "Did not solve" in (tmp_path / "solve.txt").read_text()


def test_a_solve_with_no_correspondences_is_a_failure(tmp_path, solve_field_binary, monkeypatch):
    outcome, _ = _solve(tmp_path, solve_field_binary, monkeypatch, stub=_stub(write_corr=False))

    assert outcome.status.startswith("failed: no match")


def test_a_timeout_is_a_failure(tmp_path, solve_field_binary, monkeypatch):
    def slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    outcome, _ = _solve(tmp_path, solve_field_binary, monkeypatch, stub=slow)

    assert outcome.status == "failed: solve-field timed out"


def test_nothing_kept_is_not_solved(tmp_path, solve_field_binary, monkeypatch):
    calls: list = []
    monkeypatch.setattr(survey_solve.subprocess, "run", _stub(calls=calls))

    outcome = solve_filtered_list(
        np.empty((0, 3)), (900, 1200), 2, _settings(solve_field_binary), tmp_path / "w", tmp_path / "s.txt", "f"
    )

    assert outcome.status.startswith("skipped") and not calls


def test_used_stars_are_matched_back_to_the_sources_that_were_fed():
    x, y = np.array([10.0, 20.0, 30.0, 40.0]), np.array([5.0, 6.0, 7.0, 8.0])

    index = survey_solve.match_used_to_sources(np.array([30.0, 10.0]), np.array([7.0, 5.0]), x, y)

    assert index.tolist() == [1, -1, 0, -1]


# ----- through the survey tool -----------------------------------------------------------------


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def frame_path(tmp_path):
    frame, _ = _frame(planted=False, n_clean=30, seed=4)
    return _write(tmp_path / "in" / "frame.fits", frame)


def _survey(argv):
    return survey.main([str(a) for a in argv])


def test_solving_marks_the_used_stars_and_writes_the_solve_columns(tmp_path, frame_path, solve_field_binary, monkeypatch):
    calls: list = []
    monkeypatch.setattr(survey_solve.subprocess, "run", _stub(matched=12, calls=calls))
    out = tmp_path / "out"

    assert (
        _survey(
            [
                frame_path,
                "--out",
                out,
                "--no-shadow",
                "--repeat",
                "1",
                "--solve",
                "--index-dir",
                "/idx",
                "--solve-field",
                solve_field_binary,
            ]
        )
        == 0
    )

    row = _rows(out / "survey.csv")[0]
    assert row["solve_status"] == "ok" and row["used"] == "12" and int(row["fed"]) == int(row["kept"])
    sources = _rows(out / "frame-sources.csv")
    used = [r for r in sources if r["used_by_solver"] == "True"]
    assert len(used) == 12 and all(r["fate"] == "kept" for r in used)  # only fed stars can be used
    assert all(r["solver_weight"] for r in used)
    assert (out / "frame-solve.txt").exists()
    assert Image.open(out / "frame-survey.jpg").format == "JPEG"
    assert len(calls) == 1 and _flag(calls[0], "--tweak-order") in ("1", "2")


def test_without_solve_the_columns_are_empty_and_nothing_is_run(tmp_path, frame_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(survey_solve.subprocess, "run", _stub(calls=calls))
    out = tmp_path / "out"

    _survey([frame_path, "--out", out, "--no-shadow", "--repeat", "1"])

    assert not calls and "solve_status" not in _rows(out / "survey.csv")[0]
    assert all(r["used_by_solver"] == "" for r in _rows(out / "frame-sources.csv"))


def test_solving_needs_an_index_directory(tmp_path, frame_path):
    assert _survey([frame_path, "--out", tmp_path / "out", "--solve"]) == 2


def test_a_failed_solve_does_not_stop_the_survey(tmp_path, frame_path, monkeypatch):
    monkeypatch.setattr(survey_solve.subprocess, "run", _stub())
    out = tmp_path / "out"

    code = _survey(
        [
            frame_path,
            "--out",
            out,
            "--no-shadow",
            "--repeat",
            "1",
            "--solve",
            "--index-dir",
            "/idx",
            "--solve-field",
            str(tmp_path / "absent"),
        ]
    )

    assert code == 0
    assert "not found" in _rows(out / "survey.csv")[0]["solve_status"]
    assert Image.open(out / "frame-survey.jpg").format == "JPEG"


def test_a_frame_with_too_few_sources_is_not_solved(tmp_path, solve_field_binary, monkeypatch):
    calls: list = []
    monkeypatch.setattr(survey_solve.subprocess, "run", _stub(calls=calls))
    frame, _ = _frame(planted=False, n_clean=4, seed=6)
    path = _write(tmp_path / "in" / "sparse.fits", frame)
    out = tmp_path / "out"

    _survey(
        [
            path,
            "--out",
            out,
            "--no-shadow",
            "--repeat",
            "1",
            "--solve",
            "--index-dir",
            "/idx",
            "--solve-field",
            solve_field_binary,
        ]
    )

    assert not calls
    assert _rows(out / "survey.csv")[0]["solve_status"].startswith("skipped")


# ----- the Windows form ------------------------------------------------------------------------


def test_on_windows_the_paths_are_cygwin_paths(monkeypatch):
    monkeypatch.setattr(survey_solve, "ON_WINDOWS", True)

    args = survey_solve.build_args(
        SolveSettings(index_dir="D:\\mast-indexes"),
        Path("C:/survey/f.xyls"),
        Path("C:/survey/f.wcs"),
        Path("C:/survey/f.corr"),
        Path("C:/survey"),
        (10, 10),
        2,
    )

    assert _flag(args, "--index-dir") == "/cygdrive/d/mast-indexes"
    assert _flag(args, "--corr") == "/cygdrive/c/survey/f.corr"
    assert args[-1] == "/cygdrive/c/survey/f.xyls"
    assert survey_solve._environment()["PATH"].startswith("C:\\cygwin64\\bin")


def test_on_windows_solve_field_is_started_as_a_shell_string_like_the_unit_does(tmp_path, monkeypatch):
    monkeypatch.setattr(survey_solve, "ON_WINDOWS", True)
    seen: dict = {}

    def capture(command, **kwargs):
        seen.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(survey_solve.subprocess, "run", capture)

    survey_solve._run(["--width", "5"], "C:/cygwin64/usr/local/astrometry/bin/solve-field", timeout=10)

    assert seen["command"] == "C:/cygwin64/usr/local/astrometry/bin/solve-field --width 5"
    assert seen["shell"] is True and seen["env"]["PATH"].startswith("C:\\cygwin64\\bin")
