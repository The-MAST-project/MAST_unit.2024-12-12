"""The star map of a solve: what it reads, the influence it computes, and the picture it draws.

Pure numpy / astropy / Pillow: no astrometry.net. The files `solve-field` writes are imitated
with the same column names the reader expects, so a hand-run on a unit has to confirm those
names (`src/solvers/tests/test_star_map_integration.py`).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS
from PIL import Image

from solvers import star_map
from solvers.star_map import Matched

CRPIX = (50.5, 50.5)
CD = 1.45e-4  # deg / pixel
SCALE_ARCSEC = CD * 3600.0
WIDTH, HEIGHT = 100, 80


def _wcs() -> WCS:
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.crpix = list(CRPIX)
    wcs.wcs.crval = [51.0, 49.8]
    wcs.wcs.cd = [[-CD, 0.0], [0.0, CD]]
    return wcs


def _solved_fits(path: Path) -> Path:
    fits.writeto(path, np.zeros((HEIGHT, WIDTH), dtype=np.uint16), _wcs().to_header(), overwrite=True)
    return path


def _stars(n: int, seed: int = 1) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    return rng.uniform(3, WIDTH - 3, n), rng.uniform(3, HEIGHT - 3, n)


def _table(path: Path, columns: dict[str, np.ndarray]) -> Path:
    cols = [fits.Column(name=name, format="D", array=values) for name, values in columns.items()]
    fits.BinTableHDU.from_columns(cols).writeto(path, overwrite=True)
    return path


def _corr(path: Path, x, y, ra, dec, **extra) -> Path:
    return _table(path, {"field_x": x, "field_y": y, "index_ra": ra, "index_dec": dec, **extra})


# ----- reading ---------------------------------------------------------------------------------


def test_extracted_stars_are_read_from_the_axy(tmp_path):
    _table(tmp_path / "frame.axy", {"X": np.array([1.0, 2.0]), "Y": np.array([3.0, 4.0]), "FLUX": np.array([9.0, 8.0])})

    assert star_map.read_extracted(tmp_path).tolist() == [[1.0, 3.0], [2.0, 4.0]]


def test_extracted_stars_fall_back_to_an_xyls(tmp_path):
    _table(tmp_path / "frame.xyls", {"x": np.array([5.0]), "y": np.array([6.0])})

    assert star_map.read_extracted(tmp_path).tolist() == [[5.0, 6.0]]


def test_no_kept_list_means_no_extracted_stars(tmp_path):
    assert star_map.read_extracted(tmp_path) is None


def test_matched_stars_are_read_with_their_weights(tmp_path):
    corr = _corr(
        tmp_path / "x.corr",
        np.array([1.0]),
        np.array([2.0]),
        np.array([51.0]),
        np.array([49.0]),
        MATCH_WEIGHT=np.array([0.9]),
    )

    matched = star_map.read_matched(corr)

    assert (matched.x[0], matched.y[0], matched.ra[0], matched.dec[0], matched.weight[0]) == (1.0, 2.0, 51.0, 49.0, 0.9)


def test_the_weight_is_optional(tmp_path):
    corr = _corr(tmp_path / "x.corr", np.array([1.0]), np.array([2.0]), np.array([51.0]), np.array([49.0]))

    assert star_map.read_matched(corr).weight is None


def test_a_corr_without_the_expected_columns_is_not_guessed_at(tmp_path, caplog):
    corr = _table(tmp_path / "x.corr", {"something": np.array([1.0]), "else": np.array([2.0])})

    with caplog.at_level("WARNING"):
        assert star_map.read_matched(corr) is None
    assert "something" in caplog.text  # the columns it did find, so a unit's log says what to fix


def test_a_missing_corr_is_none(tmp_path):
    assert star_map.read_matched(tmp_path / "absent.corr") is None


# ----- residuals -------------------------------------------------------------------------------


def test_residuals_are_measured_minus_catalogue_projected(tmp_path):
    x, y = _stars(12)
    ra, dec = _wcs().all_pix2world(x, y, 1)
    matched = Matched(x=x + 0.3, y=y - 0.2, ra=ra, dec=dec, weight=None)

    rx, ry, crpix, scale = star_map.residuals(matched, _solved_fits(tmp_path / "solved.fits"))

    assert rx == pytest.approx(0.3, abs=1e-4)
    assert ry == pytest.approx(-0.2, abs=1e-4)
    assert crpix == pytest.approx(CRPIX)
    assert scale == pytest.approx(SCALE_ARCSEC, rel=1e-4)


def test_a_solved_fits_without_a_wcs_gives_no_residuals(tmp_path):
    bare = tmp_path / "bare.fits"
    fits.writeto(bare, np.zeros((4, 4), dtype=np.uint16))

    assert star_map.residuals(Matched(np.ones(3), np.ones(3), np.ones(3), np.ones(3), None), bare) is None


# ----- influence -------------------------------------------------------------------------------


def _lsq_constant(x, y, values, skip=None) -> float:
    """The constant term of a least-squares order-2 polynomial, by plain lstsq (the brute force)."""
    keep = np.arange(len(x)) != skip
    u, v = (x[keep] - CRPIX[0]) / 50.0, (y[keep] - CRPIX[1]) / 50.0
    design = np.column_stack([np.ones_like(u), u, v, u**2, u * v, v**2])
    return float(np.linalg.lstsq(design, values[keep], rcond=None)[0][0])


def _lsq_residuals(x, y, values) -> np.ndarray:
    u, v = (x - CRPIX[0]) / 50.0, (y - CRPIX[1]) / 50.0
    design = np.column_stack([np.ones_like(u), u, v, u**2, u * v, v**2])
    return values - design @ np.linalg.lstsq(design, values, rcond=None)[0]


def test_fibre_shift_equals_a_leave_one_out_refit():
    """For a least-squares fit the closed form is exact: it must equal actually leaving each star out."""
    rng = np.random.default_rng(7)
    x, y = _stars(25)
    # data = a smooth field plus noise; residuals are those of the same polynomial fit
    data_x = 0.4 + 0.01 * (x - 50) + rng.normal(0, 0.15, x.size)
    data_y = -0.3 + 0.01 * (y - 40) + rng.normal(0, 0.15, y.size)
    rx, ry = _lsq_residuals(x, y, data_x), _lsq_residuals(x, y, data_y)

    measure = star_map.influence(x, y, rx, ry, CRPIX, SCALE_ARCSEC)

    for i in range(x.size):
        dx = _lsq_constant(x, y, data_x) - _lsq_constant(x, y, data_x, skip=i)
        dy = _lsq_constant(x, y, data_y) - _lsq_constant(x, y, data_y, skip=i)
        assert measure["fibre_shift_arcsec"][i] == pytest.approx(np.hypot(dx, dy) * SCALE_ARCSEC, rel=1e-6)


def test_a_distant_star_with_a_large_residual_is_the_most_influential():
    rng = np.random.default_rng(3)
    x, y = rng.uniform(35, 65, 20), rng.uniform(25, 55, 20)  # a tight cluster ...
    x[0], y[0] = 97.0, 77.0  # ... and one far corner star
    rx, ry = rng.normal(0, 0.05, 20), rng.normal(0, 0.05, 20)
    rx[0] = 0.8

    measure = star_map.influence(x, y, rx, ry, CRPIX, SCALE_ARCSEC)

    assert int(np.argmax(measure["leverage"])) == 0
    assert int(np.argmax(measure["cooks_distance"])) == 0
    assert int(np.argmax(measure["fibre_shift_arcsec"])) == 0


def test_too_few_stars_for_the_fit_give_no_influence():
    x, y = _stars(7)  # order 2 has 6 terms; it needs two more than that for a residual variance

    assert star_map.influence(x, y, np.ones(7), np.ones(7), CRPIX, SCALE_ARCSEC) is None


# ----- the picture -----------------------------------------------------------------------------


def _is_close(pixel, color, tolerance=40) -> bool:
    return all(abs(int(a) - int(b)) <= tolerance for a, b in zip(pixel, color, strict=True))


def test_the_picture_marks_extracted_stars_orange_and_used_stars_blue(tmp_path):
    image = np.zeros((HEIGHT, WIDTH))
    matched = Matched(x=np.array([30.5]), y=np.array([40.5]), ra=np.zeros(1), dec=np.zeros(1), weight=None)
    extracted = np.array([[70.5, 20.5]])
    out = tmp_path / "map.jpg"

    star_map.render(image, extracted, matched, np.array([0.1]), CRPIX, ["a caption"], out)

    picture = Image.open(out).convert("RGB")
    assert picture.size == (WIDTH, HEIGHT + star_map.CAPTION_HEIGHT)
    # the tick's arm, three pixels right of the extracted star's centre (centre at x = 70, y = 20).
    # JPEG washes out a thin line, so "clearly orange rather than blue or grey" is what is asked.
    red, _green, blue = picture.getpixel((73, 20 + star_map.CAPTION_HEIGHT))
    assert red >= 150 and red - blue >= 50
    # the used star's circle: radius is the largest, 30 pixels, since it is the only one
    assert _is_close(
        picture.getpixel((30 + star_map.MAX_USED_RADIUS, 40 + star_map.CAPTION_HEIGHT)), star_map.USED_COLOR, 90
    )


def test_a_large_frame_is_reduced_and_the_stars_still_land_on_their_pixels(tmp_path):
    image = np.zeros((2000, 4000))
    matched = Matched(x=np.array([2000.5]), y=np.array([1000.5]), ra=np.zeros(1), dec=np.zeros(1), weight=None)
    out = tmp_path / "map.jpg"

    star_map.render(image, None, matched, None, None, ["c"], out)

    picture = Image.open(out)
    assert picture.size == (2000, 1000 + star_map.CAPTION_HEIGHT)  # reduced by 2
    # no influence given: a fixed radius; the circle is centred on (1000, 500) in the reduced frame
    radius = round(star_map.MIN_USED_RADIUS + 0.3 * (star_map.MAX_USED_RADIUS - star_map.MIN_USED_RADIUS))
    assert _is_close(
        picture.convert("RGB").getpixel((1000 + radius, 500 + star_map.CAPTION_HEIGHT)), star_map.USED_COLOR, 110
    )


# ----- the whole -------------------------------------------------------------------------------


def _scene(tmp_path: Path, n_used: int = 12) -> dict:
    x, y = _stars(n_used)
    ra, dec = _wcs().all_pix2world(x, y, 1)
    fits.writeto(tmp_path / "downsampled_frame.fits", np.random.default_rng(0).normal(100, 5, (HEIGHT, WIDTH)))
    _table(tmp_path / "downsampled_frame.axy", {"X": np.append(x, [10.0, 90.0]), "Y": np.append(y, [10.0, 70.0])})
    return {
        "image_path": tmp_path / "downsampled_frame.fits",
        "corr_path": _corr(tmp_path / "solved.corr", x + 0.05, y - 0.05, ra, dec, MATCH_WEIGHT=np.ones(n_used)),
        "solved_fits": _solved_fits(tmp_path / "solved.fits"),
        "scratch": tmp_path,
        "jpeg_path": tmp_path / "solved-stars.jpg",
        "json_path": tmp_path / "solved-stars.json",
    }


def test_a_solve_gives_a_picture_and_a_table(tmp_path):
    scene = _scene(tmp_path)

    summary = star_map.write_star_map(**scene)

    assert summary["used"] == 12 and summary["extracted"] == 14
    assert summary["largest_fibre_shift_arcsec"] is not None
    assert Image.open(scene["jpeg_path"]).format == "JPEG"
    record = json.loads(scene["json_path"].read_text())
    assert record["used"] == 12 and record["extracted"] == 14
    stars = record["used_stars"]
    assert {"x", "y", "ra_deg", "dec_deg", "match_weight", "residual_arcsec", "leverage", "cooks_distance"} <= stars.keys()
    assert all(len(values) == 12 for values in stars.values())
    assert "proxy" in record["influence_model"]


def test_a_solve_with_too_few_used_stars_still_gets_a_picture_without_influence(tmp_path):
    scene = _scene(tmp_path, n_used=5)

    summary = star_map.write_star_map(**scene)

    assert summary["used"] == 5 and summary["largest_fibre_shift_arcsec"] is None
    assert scene["jpeg_path"].exists()
    assert "leverage" not in json.loads(scene["json_path"].read_text())["used_stars"]


def test_a_solve_without_correspondences_makes_nothing(tmp_path):
    scene = _scene(tmp_path)
    scene["corr_path"].unlink()

    assert star_map.write_star_map(**scene) is None
    assert not scene["jpeg_path"].exists() and not scene["json_path"].exists()


def test_without_a_kept_extraction_list_the_used_stars_are_still_drawn(tmp_path):
    scene = _scene(tmp_path)
    (tmp_path / "downsampled_frame.axy").unlink()

    summary = star_map.write_star_map(**scene)

    assert summary["extracted"] is None and scene["jpeg_path"].exists()
