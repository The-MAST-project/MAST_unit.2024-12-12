"""`spiral_correlate_steps`: correlating two steps of a run that has already finished.

Everything here builds a synthetic run folder on disk -- two FITS frames with a known
injected shift and a `result.json` describing them -- and points `correlate` at it. No
hardware, no share, and no dependence on a real run's products.

The refusal tests are as load-bearing as the measuring one. The decision was that a
re-correlation is faithful to the run it describes or it is refused, so each way a run can
fail to describe itself has a test that pins the refusal AND the reason, because the reason
is what a caller acts on.
"""

import json

import numpy as np
import pytest
from astropy.io import fits

from flux_metering import correlate

SHAPE = (128, 128)
#: Where the star sits in the reference frame. Off-centre so a transposed dx/dy cannot pass.
STAR_X, STAR_Y = 50, 70


def _frame(path, dx=0, dy=0):
    """A frame with one Gaussian star, displaced by (dx, dy) from the reference position."""
    y, x = np.mgrid[0 : SHAPE[0], 0 : SHAPE[1]]
    data = 100.0 + 3000.0 * np.exp(-(((x - (STAR_X + dx)) ** 2 + (y - (STAR_Y + dy)) ** 2) / (2 * 2.5**2)))
    fits.writeto(path, data.astype(np.float32), overwrite=True)


def _run(
    tmp_path,
    monkeypatch,
    *,
    shifts=((0, 0), (4, -3)),
    cells=((0, 0), (0, 1)),
    offsets=((0.0, 0.0), (0.0, 0.5)),
    terminal_state="max_rings",
    pixel_scale=0.2616,
    dec=49.3,
    result_json=True,
    write_frames=True,
    date="2026-09-01",
    seq="0004",
):
    """Build a synthetic run on disk and make `correlate` look at it."""
    folder = tmp_path / date / "FluxMetering" / seq
    folder.mkdir(parents=True)
    monkeypatch.setattr(correlate, "_run_root", lambda: tmp_path)

    steps = []
    # strict: a test that passed two shifts and three offsets would otherwise build a
    # shorter run than it meant to and still pass.
    for index, ((dx, dy), cell, offset) in enumerate(zip(shifts, cells, offsets, strict=True)):
        name = f"step-{index:05d}-00.fits"
        if write_frames:
            _frame(folder / name, dx, dy)
        steps.append(
            {
                "index": index,
                "cell": list(cell),
                "ring": 0 if cell == (0, 0) else 1,
                "offset_arcsec": list(offset),
                "flux": 1000.0 + index,
                "imager_frame": name,
                "saturated": False,
            }
        )

    if result_json:
        document = {
            "terminal_state": terminal_state,
            "hostname": "mast02",
            "best_index": len(steps) - 1,
            "steps": steps,
            "params": {"usable_fraction": 0.66, "x_step_arcsec": 0.5, "y_step_arcsec": 0.5},
            "result": {"fiber_x": 64, "fiber_y": 64, "fiber_source": "guiding.rois[fcu_v2]"},
        }
        if pixel_scale is not None:
            document["pixel_scale_at_bin1"] = pixel_scale
        if dec is not None:
            document["dec_degrees"] = dec
        (folder / "result.json").write_text(json.dumps(document), encoding="utf-8")
    return folder, date, seq


# --------------------------------------------------------------------- measuring --


def test_it_recovers_an_injected_shift(tmp_path, monkeypatch):
    _, date, seq = _run(tmp_path, monkeypatch, shifts=((0, 0), (4, -3)))
    out = correlate.correlate_steps(date, seq, 0, 1)
    assert out.dx == pytest.approx(4, abs=0.5)
    assert out.dy == pytest.approx(-3, abs=0.5)
    assert out.magnitude_px == pytest.approx(5, abs=0.5)


def test_the_measurement_is_signed_from_a_to_b(tmp_path, monkeypatch):
    """Swapping the steps negates the shift. A correlation that reported |dx| would pass
    every other test here and be useless, since the sign is what says which way to move."""
    _, date, seq = _run(tmp_path, monkeypatch, shifts=((0, 0), (4, -3)))
    forward = correlate.correlate_steps(date, seq, 0, 1)
    backward = correlate.correlate_steps(date, seq, 1, 0)
    assert backward.dx == pytest.approx(-forward.dx, abs=0.5)
    assert backward.dy == pytest.approx(-forward.dy, abs=0.5)


def test_two_steps_in_the_same_cell_expect_no_motion(tmp_path, monkeypatch):
    """Generalises the session's `best.cell == (0, 0)` test.

    Two steps at one cell are two exposures at one pointing, so a zero shift is the correct
    answer -- but it is also what `measure_shift` flags as fixed-pattern capture unless it
    is told to expect it.
    """
    _, date, seq = _run(tmp_path, monkeypatch, shifts=((0, 0), (0, 0)), cells=((0, 0), (0, 0)))
    out = correlate.correlate_steps(date, seq, 0, 1)
    assert out.dx == pytest.approx(0, abs=0.5)
    assert out.dy == pytest.approx(0, abs=0.5)
    assert out.at_origin is True


# ------------------------------------------------------- the commanded offset check --


def test_the_commanded_offset_is_the_difference_between_the_two_steps(tmp_path, monkeypatch):
    """Not either step's own offset. The difference is what (dx, dy) should equal, and
    reporting one step's own would look like a check while only being one when the other
    step happened to be the origin."""
    _, date, seq = _run(
        tmp_path,
        monkeypatch,
        offsets=((1.0, 2.0), (1.0, 4.0)),
        pixel_scale=0.5,
        dec=0.0,  # cos(dec) == 1, so the arithmetic is visible by eye
    )
    out = correlate.correlate_steps(date, seq, 0, 1)
    # dec offset 4.0 - 2.0 = 2.0 arcsec at 0.5 arcsec/px = 4 px; RA unchanged.
    assert out.commanded_offset_px == pytest.approx((0.0, 4.0))


def test_cos_dec_is_applied_to_the_ra_axis_only(tmp_path, monkeypatch):
    _, date, seq = _run(
        tmp_path,
        monkeypatch,
        offsets=((0.0, 0.0), (2.0, 2.0)),
        pixel_scale=0.5,
        dec=60.0,  # cos(60) = 0.5
    )
    out = correlate.correlate_steps(date, seq, 0, 1)
    assert out.commanded_offset_px[0] == pytest.approx(2.0)  # 2.0 * 0.5 / 0.5
    assert out.commanded_offset_px[1] == pytest.approx(4.0)  # 2.0 / 0.5, no cos(dec)


def test_a_run_without_a_recorded_plate_scale_reports_no_commanded_offset(tmp_path, monkeypatch):
    """The runs already on the share (0001-0004) predate recording it.

    `None` and a stated reason, never a number: the live plate scale belongs to a possibly
    different configuration and the live declination to unrelated sky, so a value computed
    from them would be a check that is quietly wrong -- which is worse than no check, and
    is the failure mode MIN_CONFIDENCE taught this repo twice.
    """
    _, date, seq = _run(tmp_path, monkeypatch, pixel_scale=None)
    out = correlate.correlate_steps(date, seq, 0, 1)
    assert out.commanded_offset_px is None
    assert "plate scale" in out.commanded_offset_source
    assert out.dx is not None  # the shift itself is still measured


def test_a_missing_declination_is_reported_not_silently_assumed(tmp_path, monkeypatch):
    _, date, seq = _run(tmp_path, monkeypatch, dec=None, offsets=((0.0, 0.0), (2.0, 0.0)), pixel_scale=0.5)
    out = correlate.correlate_steps(date, seq, 0, 1)
    assert out.commanded_offset_px == pytest.approx((4.0, 0.0))  # cos(dec) taken as 1
    assert "declination" in out.commanded_offset_source


# --------------------------------------------------------------------- refusals --


def test_an_aborted_run_is_refused(tmp_path, monkeypatch):
    _, date, seq = _run(tmp_path, monkeypatch, terminal_state="aborted")
    with pytest.raises(correlate.CorrelationError, match="aborted"):
        correlate.correlate_steps(date, seq, 0, 1)


def test_a_run_with_no_result_json_is_refused(tmp_path, monkeypatch):
    _, date, seq = _run(tmp_path, monkeypatch, result_json=False)
    with pytest.raises(correlate.CorrelationError, match="result.json"):
        correlate.correlate_steps(date, seq, 0, 1)


def test_a_run_with_no_terminal_state_is_refused(tmp_path, monkeypatch):
    _, date, seq = _run(tmp_path, monkeypatch, terminal_state=None)
    with pytest.raises(correlate.CorrelationError, match="did not complete"):
        correlate.correlate_steps(date, seq, 0, 1)


def test_an_unknown_run_is_refused(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    with pytest.raises(correlate.CorrelationError, match="no such run"):
        correlate.correlate_steps("2026-01-01", "0099", 0, 1)


def test_a_step_outside_the_run_is_refused_and_says_what_is_available(tmp_path, monkeypatch):
    _, date, seq = _run(tmp_path, monkeypatch)
    with pytest.raises(correlate.CorrelationError, match=r"step_b=9 .*available: 0\.\.1"):
        correlate.correlate_steps(date, seq, 0, 9)


def test_a_frame_missing_from_the_folder_is_refused(tmp_path, monkeypatch):
    _, date, seq = _run(tmp_path, monkeypatch, write_frames=False)
    with pytest.raises(correlate.CorrelationError, match="not in the run folder"):
        correlate.correlate_steps(date, seq, 0, 1)


@pytest.mark.parametrize("date,seq", [("../etc", "0004"), ("2026-09-01", "../0004"), ("2026-9-1", "0004")])
def test_a_path_that_is_not_a_run_name_is_refused(tmp_path, monkeypatch, date, seq):
    """The names reach this module from a URL, so they are matched against an anchored
    pattern rather than sanitised afterwards."""
    _run(tmp_path, monkeypatch)
    with pytest.raises(correlate.CorrelationError):
        correlate.correlate_steps(date, seq, 0, 1)


# ---------------------------------------------------------------------- product --


def test_it_writes_the_result_beside_the_run_naming_both_steps(tmp_path, monkeypatch):
    folder, date, seq = _run(tmp_path, monkeypatch)
    out = correlate.correlate_steps(date, seq, 0, 1)
    path = correlate.write_correlation(out)

    assert (folder / "correlate-00000-00001.json").is_file()
    assert (folder / "result.json").is_file()  # written beside, not over
    written = json.loads((folder / "correlate-00000-00001.json").read_text(encoding="utf-8"))
    assert written["step_a"] == 0 and written["step_b"] == 1
    assert written["created_at"]
    assert path.endswith("correlate-00000-00001.json")


# -------------------------------------------------------------------- discovery --


def test_it_lists_the_runs_and_their_steps(tmp_path, monkeypatch):
    _, date, seq = _run(tmp_path, monkeypatch)
    runs = correlate.list_runs()
    assert [(r["date"], r["seq"], r["complete"]) for r in runs] == [(date, seq, True)]

    steps = correlate.list_run_steps(date, seq)
    assert [s["index"] for s in steps] == [0, 1]
    # The flux and the cell are the reason this exists: choosing between bare indices is
    # guesswork, and the arg-max is the step most callers actually want.
    assert steps[1]["argmax"] is True
    assert steps[1]["cell"] == [0, 1]
    assert steps[0]["flux"] is not None


def test_an_incomplete_run_is_listed_rather_than_hidden(tmp_path, monkeypatch):
    """Listed with complete=false, so a caller sees it and is told why it is refused
    instead of wondering where it went."""
    _, date, seq = _run(tmp_path, monkeypatch, result_json=False)
    runs = correlate.list_runs()
    assert len(runs) == 1
    assert runs[0]["complete"] is False


def test_listing_runs_on_a_machine_with_no_products_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(correlate, "_run_root", lambda: tmp_path / "nothing-here")
    assert correlate.list_runs() == []
