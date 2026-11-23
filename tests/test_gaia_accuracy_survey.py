"""The Gaia survey's ``analyse`` step: held-out stars scored against Gaia, mode against mode.

Synthetic frames: a true WCS, Gaia stars at known sky positions, and one solved WCS per mode that is the true one
shifted by a known amount -- so the offset the step reports is known in advance. No solver, no unit, no catalog.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from solvers.pixel_grid import grid_to_orig_fits

_SPEC = importlib.util.spec_from_file_location(
    "gaia_accuracy_survey", Path(__file__).resolve().parent.parent / "tools" / "gaia_accuracy_survey.py"
)
survey = importlib.util.module_from_spec(_SPEC)
sys.modules["gaia_accuracy_survey"] = survey
_SPEC.loader.exec_module(survey)  # type: ignore[union-attr]

SCALE = 0.5232 / 3600.0  # degrees per binned pixel
NX, NY = 4144, 2822  # the binned grid
RA0, DEC0 = 60.0, 50.0
JYEAR_OBS = "2026-11-17T20:40:13.906"
#: What each mode's WCS is off by, (east, north) arcsec: its solved position minus the truth.
SHIFTS = {"original": (0.0, 0.5), "selective": (0.2, 0.1)}
FIBRE = (4144.0, 2822.0)  # full-frame pixel, the centre of the frame


def _wcs(shift_east=0.0, shift_north=0.0) -> WCS:
    theta = np.radians(20.0)
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crpix = [NX / 2 + 0.5, NY / 2 + 0.5]
    w.wcs.crval = [RA0 + shift_east / 3600.0 / np.cos(np.radians(DEC0)), DEC0 + shift_north / 3600.0]
    w.wcs.cd = SCALE * np.array([[-np.cos(theta), np.sin(theta)], [np.sin(theta), np.cos(theta)]])
    return w


def _sky():
    """200 Gaia stars on the sky, their solve-grid pixels, magnitudes."""
    rng = np.random.default_rng(3)
    gx = rng.uniform(200, NX - 200, 200)
    gy = rng.uniform(200, NY - 200, 200)
    ra, dec = _wcs().all_pix2world(gx, gy, 1)
    return gx, gy, ra, dec, rng.uniform(11.0, 15.5, 200)


def _write_frame(folder: Path, gx, gy, *, used=40, modes=("original", "selective"), junk=True):
    """One frame folder as a single-frame survey run leaves it."""
    folder.mkdir(parents=True)
    x = np.asarray(grid_to_orig_fits(gx, 2)) - 1.0  # full frame, 0-based
    y = np.asarray(grid_to_orig_fits(gy, 2)) - 1.0
    with open(folder / "abc-sources.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["index", "fate", "x_full", "y_full"])
        for i, (a, b) in enumerate(zip(x, y, strict=True)):
            w.writerow([i, "kept" if i % 2 else "spare", a, b])
        if junk:  # not clean: never scored
            w.writerow([len(x), "blended", 1000.0, 1000.0])
            w.writerow([len(x) + 1, "faint", 2000.0, 2000.0])
    for mode in modes:
        east, north = SHIFTS[mode]
        sub = folder / f"abc-{mode}"
        sub.mkdir()
        header = _wcs(east, north).to_header()
        header["DATE-OBS"] = JYEAR_OBS
        header["EXPOSURE"] = 5.0
        solved = sub / "frame,solver=mastrometry.net.fits"
        fits.PrimaryHDU(np.zeros((4, 4), np.float32), header).writeto(solved)
        columns = [
            fits.Column("field_x", "D", array=np.asarray(gx[:used], float)),
            fits.Column("field_y", "D", array=np.asarray(gy[:used], float)),
        ]
        fits.HDUList([fits.PrimaryHDU(), fits.BinTableHDU.from_columns(columns)]).writeto(solved.with_suffix(".corr"))


@pytest.fixture
def survey_dir(tmp_path):
    gx, gy, *_ = _sky()
    root = tmp_path / "run"
    for name in ("00", "01"):
        _write_frame(root / name, gx, gy)
    return root


def _reference(extra=None, calls=None):
    _, _, ra, dec, g = _sky()
    ra, dec, g = list(ra), list(dec), list(g)
    for e in extra or []:
        ra.append(e[0]), dec.append(e[1]), g.append(e[2])

    def reference(ra_c, dec_c, radius, years):
        if calls is not None:
            calls.append((ra_c, dec_c, radius, years))
        return np.array(ra), np.array(dec), np.array(g)

    return reference


def _params(**kw):
    return survey.Params(fibre_xy=FIBRE, **kw)


def _stars(out: Path):
    with open(out / "stars.csv", newline="") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------------------------------------ the helpers


class TestOffsets:
    def test_north_and_east(self):
        e, n = survey.offsets_arcsec(100.0, 30.0 + 10 / 3600, 100.0, 30.0)
        assert e == pytest.approx(0, abs=1e-6) and n == pytest.approx(10, rel=1e-4)
        e, _ = survey.offsets_arcsec(100.0 + 20 / 3600, 60.0, 100.0, 60.0)
        assert e == pytest.approx(10, rel=1e-3)  # cos 60 = 0.5

    def test_ra_wraps(self):
        e, _ = survey.offsets_arcsec(0.0005, 0.0, 359.9995, 0.0)
        assert e == pytest.approx(3.6, rel=1e-3)


class TestHeldOut:
    def test_a_star_a_fit_used_is_not_held_out(self):
        gx, gy = np.array([100.0, 200.0, 300.0]), np.array([100.0, 100.0, 100.0])
        free = survey.held_out(gx, gy, [np.array([[100.4, 100.0]]), np.array([[300.0, 101.0]])], 1.5)
        assert free.tolist() == [False, True, False]  # one star used by one mode, another by the other

    def test_no_fits_leaves_every_star(self):
        assert survey.held_out(np.array([1.0]), np.array([1.0]), [np.empty((0, 2))], 1.5).tolist() == [True]


class TestMatch:
    P = survey.Params(fibre_xy=(0.0, 0.0))

    def test_a_star_with_two_gaia_neighbours_is_not_matched(self):
        gaia_ra, gaia_dec = np.array([10.0, 10.01, 10.01]), np.array([20.0, 20.0, 20.0 + 2.0 / 3600])
        ra, dec = np.array([10.0, 10.01]), np.array([20.0001, 20.0])
        pairs = survey.match_gaia(ra, dec, gaia_ra, gaia_dec, np.array([12.0, 12.0, 12.0]), self.P)
        assert pairs == [(0, 0)]  # star 1 has two Gaia stars within 4"

    def test_a_faint_gaia_star_is_not_matched(self):
        pairs = survey.match_gaia(
            np.array([10.0]), np.array([20.0]), np.array([10.0]), np.array([20.0]), np.array([17.0]), self.P
        )
        assert pairs == []

    def test_too_far_is_not_matched(self):
        pairs = survey.match_gaia(
            np.array([10.0]), np.array([20.0 + 3.0 / 3600]), np.array([10.0]), np.array([20.0]), np.array([12.0]), self.P
        )
        assert pairs == []  # 3" is inside isolate (4") but outside match (2.5")


# ------------------------------------------------------------------------------------------------ the whole step


def test_each_modes_known_offset_is_recovered(survey_dir, tmp_path):
    out = tmp_path / "out"
    survey.analyse([survey_dir], out, _params(), _reference())
    rows = _stars(out)
    for mode, (east, north) in SHIFTS.items():
        e = np.array([float(r["err_e"]) for r in rows if r["mode"] == mode])
        n = np.array([float(r["err_n"]) for r in rows if r["mode"] == mode])
        assert len(e) > 100
        assert np.median(e) == pytest.approx(east, abs=0.02) and np.median(n) == pytest.approx(north, abs=0.02)


def test_the_stars_a_fit_used_are_not_scored(survey_dir, tmp_path):
    out = tmp_path / "out"
    survey.analyse([survey_dir], out, _params(), _reference())
    per_frame = {r["frame"] for r in _stars(out)}
    assert per_frame == {"00", "01"}
    # 200 clean stars, 40 of them used by both fits: 160 are held out, in each of 2 modes, in 2 frames
    assert len(_stars(out)) == 160 * 2 * 2
    used_x, used_y = _sky()[0][:40], _sky()[1][:40]
    scored = {(round(float(r["x"]), 3), round(float(r["y"]), 3)) for r in _stars(out)}
    for gx, gy in zip(used_x, used_y, strict=True):
        assert (
            round(float(grid_to_orig_fits(gx, 2)) - 1.0, 3),
            round(float(grid_to_orig_fits(gy, 2)) - 1.0, 3),
        ) not in scored


def test_a_gaia_neighbour_keeps_a_star_out(survey_dir, tmp_path):
    _, _, ra, dec, _ = _sky()
    crowded = (ra[100], dec[100] + 2.0 / 3600, 13.0)  # 2" north of star 100
    out = tmp_path / "out"
    survey.analyse([survey_dir], out, _params(), _reference(extra=[crowded]))
    gx, gy = _sky()[0][100], _sky()[1][100]
    x, y = float(grid_to_orig_fits(gx, 2)) - 1.0, float(grid_to_orig_fits(gy, 2)) - 1.0
    assert not any(abs(float(r["x"]) - x) < 0.01 and abs(float(r["y"]) - y) < 0.01 for r in _stars(out))


def test_the_reference_is_asked_for_the_frames_epoch(survey_dir, tmp_path):
    calls: list = []
    survey.analyse([survey_dir], tmp_path / "out", _params(), _reference(calls=calls))
    assert len(calls) == 2  # one per frame
    ra, dec, radius, years = calls[0]
    assert (ra, dec) == pytest.approx((RA0, DEC0), abs=0.2)
    assert years == pytest.approx(2026.88 - 2016.0, abs=0.01)  # J2016.0 to the exposure's middle
    assert 0.3 < radius < 1.5


def test_the_bins_and_the_frame_comparison(survey_dir, tmp_path):
    out = tmp_path / "out"
    text = survey.analyse([survey_dir], out, _params(), _reference(), near_px=4000.0)
    with open(out / "bins.csv", newline="") as f:
        bins = list(csv.DictReader(f))
    assert {b["mode"] for b in bins} == {"original", "selective"}
    assert all(float(b["offset_arcsec"]) >= 0 for b in bins)
    # the larger shift is 0.5", the smaller about 0.22"
    big = [float(b["offset_arcsec"]) for b in bins if b["mode"] == "original"]
    small = [float(b["offset_arcsec"]) for b in bins if b["mode"] == "selective"]
    assert np.median(big) == pytest.approx(0.5, abs=0.05) and np.median(small) == pytest.approx(np.hypot(0.2, 0.1), abs=0.05)
    assert "closer to Gaia than 'original' in 2 of 2 frames" in text


def test_a_frame_without_one_of_the_modes_is_skipped(tmp_path, capsys):
    gx, gy, *_ = _sky()
    root = tmp_path / "run"
    _write_frame(root / "00", gx, gy)
    _write_frame(root / "01", gx, gy, modes=("original",))
    survey.analyse([root], tmp_path / "out", _params(), _reference())
    assert {r["frame"] for r in _stars(tmp_path / "out")} == {"00"}
    assert "01: no solved FITS for 'selective'" in capsys.readouterr().err


def test_a_frame_folder_can_be_given_directly(survey_dir, tmp_path):
    survey.analyse([survey_dir / "00"], tmp_path / "out", _params(), _reference())
    assert {r["frame"] for r in _stars(tmp_path / "out")} == {"00"}


def test_the_command_line(survey_dir, tmp_path, capsys):
    out = tmp_path / "out"
    code = survey.main(
        ["analyse", str(survey_dir), "--out", str(out), "--fibre-xy", "4144", "2822", "--near-px", "4000"],
        reference=_reference(),
    )
    assert code == 0
    assert "closer to Gaia" in capsys.readouterr().out
    for name in ("stars.csv", "bins.csv", "frames.csv"):
        assert (out / name).exists()


def test_it_needs_two_modes(survey_dir, tmp_path, capsys):
    code = survey.main(
        ["analyse", str(survey_dir), "--out", str(tmp_path / "o"), "--fibre-xy", "1", "1", "--modes", "original"],
        reference=_reference(),
    )
    assert code == 2 and "at least two modes" in capsys.readouterr().err
