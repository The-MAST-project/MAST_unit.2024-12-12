"""The survey tool: its outputs, its consistency checks and its refusal to write beside the frames.

Synthetic frames from the selection tests; no solver, no unit.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from PIL import Image
from test_source_selection import CX, NX, NY, PARAMS, _frame, _shadow

from solvers.source_selection import KEPT, SPARE, select_sources

_SPEC = importlib.util.spec_from_file_location(
    "source_survey", Path(__file__).resolve().parent.parent / "tools" / "source_survey.py"
)
survey = importlib.util.module_from_spec(_SPEC)
sys.modules["source_survey"] = survey
_SPEC.loader.exec_module(survey)  # type: ignore[union-attr]


def _write(path: Path, frame: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.writeto(path, frame, overwrite=True)
    return path


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="module")
def two_frames(tmp_path_factory):
    """Two exposures of one field: the second is the first shifted by (3, -2) pixels, with new noise."""
    base = tmp_path_factory.mktemp("frames")
    first, _ = _frame(planted=False, n_clean=20, seed=21)
    second = np.roll(first, shift=(-2, 3), axis=(0, 1)).astype(np.float64)
    second += np.random.default_rng(2).normal(0, 2.0, second.shape)
    second = np.clip(second, 0, 65535).astype(np.uint16)
    _write(base / "night" / "frame-1.fits", first)
    _write(base / "night" / "frame-2.fits", second)
    # the solver's own products must be skipped
    _write(base / "night" / "frame-1,solver=mastrometry.net.fits", first)
    _write(base / "night" / "downsampled_frame-1.fits", first)
    return base / "night"


#: these tests are about matching and exclusion across frames, wherever the stars are: no candidate-region cap
NO_CAP = ["--param", "reach_factor=none", "--param", "reach_factor_closed_shadow=none"]


def _run(argv):
    assert survey.main([str(a) for a in [*argv, *NO_CAP]]) == 0


def test_a_folder_is_surveyed_without_the_solvers_own_products(two_frames, tmp_path):
    out = tmp_path / "out"

    _run([two_frames, "--out", out, "--no-shadow", "--repeat", "2", "--same-field"])

    rows = _rows(out / "survey.csv")
    assert sorted(r["frame"] for r in rows) == ["frame-1.fits", "frame-2.fits"]
    assert all(r["repeat_identical"] == "True" for r in rows)
    assert all(int(r["kept"]) >= 1 for r in rows)
    for stem in ("frame-1", "frame-2"):
        assert Image.open(out / f"{stem}-survey.jpg").format == "JPEG"
        assert _rows(out / f"{stem}-sources.csv")


def test_every_source_row_says_what_became_of_it(two_frames, tmp_path):
    out = tmp_path / "out"
    _run([two_frames / "frame-1.fits", "--out", out, "--no-shadow", "--repeat", "1"])

    rows = _rows(out / "frame-1-sources.csv")

    assert {r["fate"] for r in rows} <= {KEPT, SPARE, *survey.REJECT_REASONS}
    assert all(r["x"] and r["y"] and r["snr"] for r in rows)


def test_frames_of_one_field_are_matched_across_exposures_despite_a_pointing_shift(two_frames, tmp_path):
    out = tmp_path / "out"

    _run([two_frames, "--out", out, "--no-shadow", "--repeat", "1", "--same-field"])

    rows = _rows(out / "consistency.csv")
    assert rows and all(r["frames"] == "2" for r in rows)
    # the field moved by (3, -2) px between exposures. Aligned first, every source that passes the
    # tests in either frame -- a real star, not a noise blob or an edge wrapped by the shift -- is found in both
    real = [r for r in rows if int(r["not_rejected"]) >= 1]
    assert len(real) >= 15
    assert all(r["detected"] == "2" for r in real)


def test_the_survey_refuses_to_write_beside_a_frame(two_frames):
    assert survey.main([str(two_frames / "frame-1.fits"), "--out", str(two_frames), "--no-shadow"]) == 2


def test_nothing_is_written_beside_the_frames(two_frames, tmp_path):
    before = sorted(p.name for p in two_frames.iterdir())

    _run([two_frames, "--out", tmp_path / "out", "--no-shadow", "--repeat", "1"])

    assert sorted(p.name for p in two_frames.iterdir()) == before


def test_a_missing_path_is_an_error_not_a_crash(tmp_path):
    assert survey.main([str(tmp_path / "absent"), "--out", str(tmp_path / "out")]) == 1


def test_a_roi_crop_reports_positions_in_both_frames(tmp_path):
    frame, _ = _frame(planted=False, n_clean=30, seed=4)
    path = _write(tmp_path / "in" / "frame.fits", frame)
    out = tmp_path / "out"

    _run([path, "--out", out, "--no-shadow", "--repeat", "1", "--roi", "100", "50", "700", "600"])

    for row in _rows(out / "frame-sources.csv"):
        assert float(row["x_full"]) == pytest.approx(float(row["x"]) + 100)
        assert float(row["y_full"]) == pytest.approx(float(row["y"]) + 50)


# ----- pieces ----------------------------------------------------------------------------------


def test_two_runs_that_differ_are_reported_as_different():
    frame, _ = _frame(planted=False, n_clean=15, seed=5)
    first = select_sources(frame, params=PARAMS)
    second = select_sources(frame, params=PARAMS)
    assert survey.runs_agree(first, second)

    second.flux = second.flux.copy()
    second.flux[0] += 1.0

    assert not survey.runs_agree(first, second)


def test_the_exclusion_sweep_counts_only_sources_that_pass_every_other_test():
    frame, positions = _frame()
    selection = select_sources(frame, shadow=_shadow(), params=PARAMS)

    rows = survey.sweep_counts(selection, [30.0, 130.0, 400.0])

    assert [r["excluded"] for r in rows] == sorted(r["excluded"] for r in rows)  # a wider band removes more
    assert all(r["remaining"] + r["excluded"] == r["usable_before_exclusion"] for r in rows)
    # the saturated star in the band's reach is not "usable", so it is not counted as removed
    assert rows[0]["usable_before_exclusion"] == rows[-1]["usable_before_exclusion"]


def test_a_shadow_found_on_the_full_frame_follows_a_crop():
    """The same star is the same distance across the band, measured on the frame or on its crop."""
    frame, positions = _frame()
    roi = (200, 100, 800, 600)
    model = _shadow()
    cropped_model = survey.shadow_for_roi(model, frame.shape, roi)

    full = select_sources(frame, shadow=model, params=PARAMS, windowed=False)
    crop = select_sources(frame[100:700, 200:1000], shadow=cropped_model, params=PARAMS, windowed=False)

    x, y = positions["in_penumbra"]
    i = int(np.argmin(np.hypot(full.x - x, full.y - y)))
    j = int(np.argmin(np.hypot(crop.x - (x - 200), crop.y - (y - 100))))
    assert crop.shadow_distance[j] == pytest.approx(full.shadow_distance[i], abs=1.0)
    assert crop.shadow_deficit[j] == pytest.approx(full.shadow_deficit[i], abs=1e-6)


def test_an_absent_shadow_stays_absent_on_a_crop():
    from imaging.mirror_shadow import ShadowModel

    cropped = survey.shadow_for_roi(ShadowModel(present=False, image_shape=(NY, NX)), (NY, NX), (0, 0, 100, 80))

    assert not cropped.present and cropped.image_shape == (80, 100)


def test_the_picture_masks_the_shadow_and_marks_the_sources(tmp_path):
    frame, positions = _frame()
    selection = select_sources(frame, shadow=_shadow(), params=PARAMS)
    out = tmp_path / "overlay.jpg"

    survey.draw_overlay(frame.astype(float), selection, _shadow(), [60.0, 120.0], ["a caption"], out)

    picture = Image.open(out).convert("RGB")
    assert picture.size == (NX, NY + survey.CAPTION_HEIGHT)
    # inside the umbra the frame is tinted toward the umbra colour; far from the band it is grey
    in_band = np.array(picture.getpixel((int(CX), survey.CAPTION_HEIGHT + 800)), dtype=int)
    away = np.array(picture.getpixel((100, survey.CAPTION_HEIGHT + 800)), dtype=int)
    assert abs(away[0] - away[2]) < 25  # grey
    assert in_band[0] - in_band[1] > 15  # reddish purple has red over green
    # a kept star has a green ring; a rejected one a vermillion cross
    kept = np.flatnonzero(selection.fate == KEPT)[0]
    kx, ky = selection.x[kept], selection.y[kept]
    ring = np.array(picture.getpixel((int(round(kx + 11)), int(round(ky + survey.CAPTION_HEIGHT)))), dtype=int)
    assert ring[1] > ring[0] + 20


def test_zscale_display_keeps_a_faint_point_source_that_the_block_mean_loses():
    """A star 6 sigma over the sky, one pixel wide, in a 5 x 5 block: the mean dilutes it, the maximum keeps it."""
    from source_survey import zscale_display

    rng = np.random.default_rng(1)
    image = rng.normal(1000.0, 10.0, (500, 10000))  # wide enough that the display is reduced by a factor > 1
    image[250, 5003] += 60.0
    gray, factor = zscale_display(image)
    assert factor > 1
    star = gray[250 // factor, 5003 // factor]
    assert star > np.percentile(gray, 99.9) or star > 200


def test_param_overrides_are_read_as_the_fields_own_types():
    from source_survey import parse_param_overrides

    got = parse_param_overrides(
        ["min_sep_fwhm=1.5", "n_target=40", "blend_ignores_hot=false", "grid=5,4", "full_scale=none"]
    )
    assert got == {"min_sep_fwhm": 1.5, "n_target": 40, "blend_ignores_hot": False, "grid": (5, 4), "full_scale": None}
    with pytest.raises(ValueError):
        parse_param_overrides(["no_such_field=1"])


def test_pictures_are_placed_beside_the_frame_with_the_stage_in_their_name(tmp_path):
    from source_survey import frame_folder_tag, place_beside_frame

    frame_dir, out = tmp_path / "share", tmp_path / "out"
    frame_dir.mkdir()
    frame = frame_dir / "seq=0002,seconds=5.0.fits"
    tag = frame_folder_tag(frame)
    survey = out / "seq=0002,seconds=5.0-survey.jpg"
    survey.parent.mkdir()
    survey.write_bytes(b"s")
    for mode in ("original", "selective"):
        folder = out / f"{tag}-{mode}"
        folder.mkdir()
        stem = "seq=0002,seconds=5.0,solver=mastrometry.net"
        for suffix in ("-stars.jpg", "-stars.json", "-result.txt", ".fits", ".corr"):
            (folder / f"{stem}{suffix}").write_bytes(b"x")
    (out / f"{tag}-selective" / "seq=0002,seconds=5.0,solver=mastrometry.net-shadow.jpg").write_bytes(b"x")

    placed = place_beside_frame(frame, out, ["original", "selective"], survey)

    names = sorted(p.name for p in placed)
    assert "seq=0002,seconds=5.0-survey.jpg" in names
    assert "seq=0002,seconds=5.0,solver=mastrometry.net,stage=original-stars.jpg" in names
    assert "seq=0002,seconds=5.0,solver=mastrometry.net,stage=selective-shadow.jpg" in names
    assert not any(n.endswith((".fits", ".corr")) for n in names)  # the big and the intermediate files stay in --out
    assert all((frame_dir / n).exists() for n in names) and len(names) == len(set(names))
