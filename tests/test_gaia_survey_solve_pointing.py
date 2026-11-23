"""The Gaia survey's ``solve`` and ``pointing`` steps: both modes scored on one acquisition's last frame.

Synthetic: a true WCS, the target star at a known pixel, one solved WCS per mode that is the true one shifted by a known
amount (so the error each mode is scored with is known in advance); the solver itself is a recording fake.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from solvers.pixel_grid import orig_to_grid_fits

_SPEC = importlib.util.spec_from_file_location(
    "gaia_accuracy_survey_pointing", Path(__file__).resolve().parent.parent / "tools" / "gaia_accuracy_survey.py"
)
survey = importlib.util.module_from_spec(_SPEC)
sys.modules["gaia_accuracy_survey_pointing"] = survey
_SPEC.loader.exec_module(survey)  # type: ignore[union-attr]

SCALE = 0.5232 / 3600.0
NX, NY = 4144, 2822
FIBRE = (4144.0, 2822.0)  # full-frame, 0-based
MODES = ("original", "selective")


def _wcs(east=0.0, north=0.0) -> WCS:
    theta = np.radians(20.0)
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crpix = [NX / 2 + 0.5, NY / 2 + 0.5]
    w.wcs.crval = [60.0 + east / 3600.0 / np.cos(np.radians(50.0)), 50.0 + north / 3600.0]
    w.wcs.cd = SCALE * np.array([[-np.cos(theta), np.sin(theta)], [np.sin(theta), np.cos(theta)]])
    return w


def _grid(x_full, y_full):
    return orig_to_grid_fits(x_full + 1.0, 2), orig_to_grid_fits(y_full + 1.0, 2)


def _target_row(name="T01", at=(FIBRE[0] + 3.0, FIBRE[1] - 2.0)):  # where _write_solved puts the star
    ra, dec = _wcs().all_pix2world(*_grid(*at), 1)
    return {"name": name, "ra_deg": str(float(ra)), "dec_deg": str(float(dec)), "g": "9.5"}


def _write_solved(frame_dir: Path, shifts: dict, *, star=FIBRE, others=((3000.0, 2000.0), (5500.0, 3500.0)), fitted=()):
    """``frame_dir`` as a survey run leaves it: sources.csv (the star and others), a solved FITS and .corr per mode."""
    frame_dir.mkdir(parents=True)
    with open(frame_dir / "abc-sources.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["index", "fate", "x_full", "y_full", "flux"])
        w.writerow(
            [0, "saturated", star[0] + 3.0, star[1] - 2.0, 5.0e6]
        )  # the target star, rejected as a source: still found
        w.writerow([1, "kept", star[0] + 8.0, star[1] + 6.0, 1.0e3])  # fainter, near
        for i, (x, y) in enumerate(others, 2):
            w.writerow([i, "kept", x, y, 9.0e8])  # brighter but far: not the target
    star_xy = (star[0] + 3.0, star[1] - 2.0)
    for mode, (east, north) in shifts.items():
        sub = frame_dir / f"abc-{mode}"
        sub.mkdir()
        header = _wcs(east, north).to_header()
        header["DATE-OBS"] = "2026-11-24T20:40:13.906"
        header["EXPOSURE"] = 5.0
        solved = sub / "frame,solver=mastrometry.net.fits"
        fits.PrimaryHDU(np.zeros((4, 4), np.float32), header).writeto(solved)
        used_x = [0.0, 100.0]
        used_y = [0.0, 100.0]
        if mode in fitted:
            used_x.append(float(_grid(*star_xy)[0]))
            used_y.append(float(_grid(*star_xy)[1]))
        cols = [fits.Column("field_x", "D", array=np.array(used_x)), fits.Column("field_y", "D", array=np.array(used_y))]
        fits.HDUList([fits.PrimaryHDU(), fits.BinTableHDU.from_columns(cols)]).writeto(solved.with_suffix(".corr"))


def _acquired(out: Path, name: str, shifts: dict, *, target=None, **kw):
    folder = out / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "acquire.json").write_text(json.dumps({"target": target or _target_row(name), "outcome": "succeeded"}))
    _write_solved(folder / "solve", shifts, **kw)


def _targets_csv(out: Path, rows):
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "targets.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def _params(**kw):
    return survey.Params(fibre_xy=FIBRE, **kw)


# ------------------------------------------------------------------------------------------------ pointing


def test_each_modes_known_error_is_recovered(tmp_path):
    _targets_csv(tmp_path, [_target_row("T01")])
    _acquired(tmp_path, "T01", {"original": (0.0, 0.5), "selective": (0.2, 0.1)})
    report = survey.pointing(tmp_path, _params(), MODES, 25.0)
    with open(tmp_path / "pointing.csv", newline="") as f:
        rows = {r["mode"]: r for r in csv.DictReader(f)}
    assert float(rows["original"]["err_e"]) == pytest.approx(0.0, abs=0.02)
    assert float(rows["original"]["err_n"]) == pytest.approx(0.5, abs=0.02)
    assert float(rows["selective"]["err_e"]) == pytest.approx(0.2, abs=0.02)
    assert float(rows["selective"]["err_n"]) == pytest.approx(0.1, abs=0.02)
    assert "1 targets scored" in report


def test_the_target_star_is_the_brightest_source_near_the_fibre_not_the_brightest_in_the_frame(tmp_path):
    star = survey.find_target_star
    _write_solved(tmp_path / "f", {"original": (0.0, 0.0)})
    x, y, n = star(next((tmp_path / "f").glob("*sources.csv")), FIBRE, 25.0)
    assert (x, y) == pytest.approx((FIBRE[0] + 3.0, FIBRE[1] - 2.0))
    assert n == 2  # the bright one far away is outside the window
    assert star(next((tmp_path / "f").glob("*sources.csv")), (100.0, 100.0), 25.0) is None


def test_a_mode_that_fitted_the_star_is_flagged(tmp_path):
    _targets_csv(tmp_path, [_target_row("T01")])
    _acquired(tmp_path, "T01", {"original": (0.0, 0.0), "selective": (0.0, 0.0)}, fitted=("selective",))
    survey.pointing(tmp_path, _params(), MODES, 25.0)
    with open(tmp_path / "pointing.csv", newline="") as f:
        rows = {r["mode"]: r for r in csv.DictReader(f)}
    assert (rows["original"]["fitted"], rows["selective"]["fitted"]) == ("False", "True")


def test_the_better_mode_meets_the_criterion_over_targets(tmp_path):
    rng = np.random.default_rng(1)
    rows = [_target_row(f"T{k:02d}") for k in range(1, 9)]
    _targets_csv(tmp_path, rows)
    for r in rows:
        jitter = rng.normal(0, 0.03, 2)
        _acquired(
            tmp_path,
            r["name"],
            {"original": (0.0 + jitter[0], 0.5 + jitter[1]), "selective": (0.1, 0.1)},
            target=r,
        )
    report = survey.pointing(tmp_path, _params(), MODES, 25.0)
    assert "selective minus original" in report
    assert "8 of 8 targets closer to Gaia" in report
    assert "meets the plan's criterion" in report and "does not meet" not in report


def test_equal_modes_do_not_meet_the_criterion(tmp_path):
    rows = [_target_row(f"T{k:02d}") for k in range(1, 9)]
    _targets_csv(tmp_path, rows)
    for k, r in enumerate(rows):
        a, b = (0.2, 0.4) if k % 2 else (0.4, 0.2)  # each mode is better on half the targets: no difference on average
        _acquired(tmp_path, r["name"], {"original": (a, 0.0), "selective": (b, 0.0)}, target=r)
    assert "does not meet" in survey.pointing(tmp_path, _params(), MODES, 25.0)


def test_an_unsolved_or_unscorable_target_is_reported_not_fatal(tmp_path):
    rows = [_target_row("T01"), _target_row("T02")]
    _targets_csv(tmp_path, rows)
    _acquired(tmp_path, "T01", {"original": (0.0, 0.0), "selective": (0.0, 0.0)}, target=rows[0])
    (tmp_path / "T02").mkdir()
    (tmp_path / "T02" / "acquire.json").write_text(json.dumps({"target": rows[1], "outcome": "succeeded"}))
    report = survey.pointing(tmp_path, _params(), MODES, 25.0)
    assert "1 targets scored" in report
    assert "not scored: T02: no sources.csv" in report


def test_a_star_outside_the_search_window_is_not_scored(tmp_path):
    _targets_csv(tmp_path, [_target_row("T01")])
    _acquired(tmp_path, "T01", {"original": (0, 0), "selective": (0, 0)}, star=(FIBRE[0] + 200.0, FIBRE[1]))
    assert "no source within 25 px" in survey.pointing(tmp_path, _params(), MODES, 25.0)


# ------------------------------------------------------------------------------------------------ solve


def _acquire_record(out: Path, name: str, products: str, outcome="succeeded"):
    folder = out / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "acquire.json").write_text(
        json.dumps({"target": {"name": name}, "outcome": outcome, "shared_products_path": products})
    )


def _acq_products(root: Path, phases=("spec",), n=3):
    for phase in phases:
        d = root / phase
        d.mkdir(parents=True)
        for k in range(2, 2 + n):
            stem = f"seq={k:04d},time=21-12-45_328,seconds=5.0,binning=1x1"
            (d / f"{stem}.fits").write_text("")
            (d / f"{stem},solver=mastrometry.net.fits").write_text("")


def test_the_last_raw_frame_of_the_mirror_out_phase_is_chosen(tmp_path):
    _acq_products(tmp_path / "a", phases=("spec",), n=3)
    assert survey.final_frame(tmp_path / "a").name.startswith("seq=0004")
    assert ",solver=" not in survey.final_frame(tmp_path / "a").name
    _acq_products(tmp_path / "b", phases=("spec", "spec-mirror-out"), n=2)
    assert survey.final_frame(tmp_path / "b").parent.name == "spec-mirror-out"
    assert survey.final_frame(tmp_path / "empty") is None


def test_the_share_path_is_mapped_to_this_machine():
    root = Path("/Storage/mast-share")
    assert (
        survey.map_share("//mast-ns-control/mast-share/MAST/mast02/2026-11-21/Acq", root)
        == root / "MAST/mast02/2026-11-21/Acq"
    )
    assert survey.map_share("\\\\mast-ns-control\\mast-share\\MAST\\mast02\\x", root) == root / "MAST/mast02/x"
    assert survey.map_share("D:/MAST/x", root) == Path("D:/MAST/x")
    assert survey.map_share("//h/mast-share/MAST/x", None) == Path("//h/mast-share/MAST/x")


class Recorder:
    def __init__(self, returncode=0):
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.returncode, stdout="out", stderr="")


def _solve(out, run, tmp_path, **kw):
    tool = tmp_path / "source_survey.py"
    tool.write_text("")
    return survey.solve(
        out, unit="mast02", fibre_xy=FIBRE, modes=MODES, source_survey=tool, run=run, log=lambda m: None, **kw
    )


def test_solve_runs_the_survey_tool_on_the_last_frame_in_both_modes(tmp_path):
    products = tmp_path / "share" / "MAST" / "mast02" / "acq"
    _acq_products(products)
    out = tmp_path / "out"
    _acquire_record(out, "T01", "//mast-ns-control/mast-share/MAST/mast02/acq")
    run = Recorder()
    assert _solve(out, run, tmp_path, share_root=tmp_path / "share") == {"T01": "solved"}
    (cmd,) = run.calls
    assert cmd[2].endswith("seq=0004,time=21-12-45_328,seconds=5.0,binning=1x1.fits")
    assert cmd[cmd.index("--out") + 1] == str(out / "T01" / "solve")
    assert "--via-solver" in cmd and cmd[cmd.index("--solver-modes") + 1] == "original,selective"
    assert cmd[cmd.index("--fibre-xy") + 1 : cmd.index("--fibre-xy") + 3] == ["4144", "2822"]
    assert cmd[cmd.index("--hot-pixels") + 1] == "mast02"
    assert (out / "T01" / "solve.log").read_text() == "out"


def test_solve_skips_what_was_not_acquired_or_is_already_solved(tmp_path):
    products = tmp_path / "share" / "MAST" / "mast02" / "acq"
    _acq_products(products)
    out = tmp_path / "out"
    _acquire_record(out, "T01", "//h/mast-share/MAST/mast02/acq", outcome="failed")
    _acquire_record(out, "T02", "//h/mast-share/MAST/mast02/acq")
    _acquire_record(out, "T03", "//h/mast-share/MAST/mast02/none")
    (out / "T02" / "solve").mkdir()
    (out / "T02" / "solve" / "survey.csv").write_text("")
    run = Recorder()
    result = _solve(out, run, tmp_path, share_root=tmp_path / "share")
    assert result["T01"].startswith("skipped: acquisition failed")
    assert result["T02"] == "already solved"
    assert result["T03"].startswith("skipped: no mirror-out frame")
    assert run.calls == []
    assert _solve(out, run, tmp_path, share_root=tmp_path / "share", redo=True)["T02"] == "solved"


def test_a_failing_tool_is_recorded(tmp_path):
    _acq_products(tmp_path / "share" / "MAST" / "mast02" / "acq")
    out = tmp_path / "out"
    _acquire_record(out, "T01", "//h/mast-share/MAST/mast02/acq")
    assert _solve(out, Recorder(returncode=3), tmp_path, share_root=tmp_path / "share")["T01"].startswith("failed (exit 3)")


def test_a_missing_survey_tool_is_explained(tmp_path):
    with pytest.raises(SystemExit, match="solver-source-selection"):
        survey.solve(tmp_path, unit="m", fibre_xy=FIBRE, modes=MODES, source_survey=tmp_path / "nope.py")
