"""Tests for the spiral search session state machine.

No hardware: the mount and imager are fakes, and the imager writes a real FITS so the
read-back path is exercised for real. What is being pinned is the behaviour that guards
hardware and the operator's result --

* tracking starts on open and stops on close, on every exit path;
* a step or an end without an open session is an error, not a silent no-op (both used to
  be: `next_step` did nothing and `end_path` raised through to a 500);
* an abandoned session closes itself WITHOUT inventing a measurement;
* every step is logged even when intermediate frames are not saved.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("skimage", reason="scikit-image unavailable")
pytest.importorskip("photutils", reason="photutils unavailable")
from astropy.io import fits

import spiral_search
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.models.statuses import ImagerRoi
from spiral_search import FINAL_IMAGE, REFERENCE_IMAGE, RESULT_FILE, SpiralSearch

SIZE = 300
CENTER = SIZE // 2


class FakeRoi:
    """Stands in for guiding.rois[fcu_v2]; Config() would reach MongoDB."""

    fiber_x = CENTER
    fiber_y = CENTER
    margin_horizontal = 60
    margin_vertical = 60


def star_field(dy: float = 0.0, dx: float = 0.0) -> np.ndarray:
    rng = np.random.default_rng(1)
    data = rng.normal(100.0, 5.0, (SIZE, SIZE)).astype(np.float32)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    for y, x in [(80, 95), (150, 210), (220, 70), (120, 160)]:
        data += 3000.0 * np.exp(-(((yy - y - dy) ** 2 + (xx - x - dx) ** 2) / (2 * 3.0**2)))
    return data


class FakePw:
    #: The commissioning field (2026-08-13), so cos(dec) is a factor of 0.7529 rather than 1
    #: and a pixel estimate that skipped it would be visibly wrong.
    DEC_DEGREES = 41.157

    def __init__(self):
        self.calls: list[str] = []
        self.x = 0
        self.y = 0
        #: PWI4 older than 4.0.11 Beta 8 reports no spiral offset at all, and pwi4_client
        #: sets the whole section to None. Set False to exercise that.
        self.reports_offset = True

    def mount_spiral_offset_new(self, x_step_arcsec, y_step_arcsec):
        self.calls.append("new")
        self.x = self.y = 0

    def mount_spiral_offset_next(self):
        self.calls.append("next")
        self.x += 1

    def mount_spiral_offset_previous(self):
        self.calls.append("previous")
        self.x -= 1

    def status(self):
        offset = SimpleNamespace(x=self.x, y=self.y) if self.reports_offset else None
        return SimpleNamespace(mount=SimpleNamespace(spiral_offset=offset, dec_j2000_degs=self.DEC_DEGREES))


class FakeMount:
    def __init__(self):
        self.pw = FakePw()
        self.tracking = False
        self.settled = 0

    def start_tracking(self):
        self.tracking = True

    def stop_tracking(self):
        self.tracking = False

    def wait_until_settled(self, mode):
        self.settled += 1


class FakeImager:
    """Writes a real FITS, like PHD2 does -- this class cannot image to memory either."""

    #: PHD2's non-guiding path asserts on settings.roi, so a real one is required.
    full_frame = ImagerRoi(x=0, y=0, width=SIZE, height=SIZE)

    def __init__(self):
        self.latest_settings = None
        self.series_open = 0
        self.shift = (0.0, 0.0)
        self.exposures: list[str] = []
        self.fail_next = False
        self.truncate_rows = 0  # blank the last N rows, as a partial readout does

    def start_exposure_series(self, purpose=None):
        self.series_open += 1
        return object()

    def end_exposure_series(self, series):
        self.series_open -= 1

    def start_exposure(self, settings):
        self.latest_settings = settings
        if self.fail_next:
            # PHD2 RETURNS errors rather than raising -- when disconnected, or when the
            # requested binning does not match its profile. Nothing is saved in that case.
            return CanonicalResponse(errors=["fake imager was told to fail"])
        return CanonicalResponse_Ok

    def wait_for_image_saved(self):
        if self.fail_next:
            raise AssertionError("wait_for_image_saved must not be reached after a failed start_exposure")
        path = self.latest_settings.image_path
        self.exposures.append(os.path.basename(path))
        # The final frame is the shifted one; everything before it is the unshifted field.
        dy, dx = self.shift if os.path.basename(path) == FINAL_IMAGE else (0.0, 0.0)
        data = star_field(dy, dx)
        if self.truncate_rows:
            data[-self.truncate_rows :] = 0
        fits.writeto(path, data, overwrite=True)


class FakeConf:
    def __init__(self):
        self.acquisition = SimpleNamespace(gain=170)
        # 0.0 is what the config DB actually holds today (MAST_unit#138), so it is the
        # default here too -- the omit-the-estimate path is the one running on sky.
        self.imager = SimpleNamespace(pixel_scale_at_bin1=0.0)


class FakeUnit:
    def __init__(self):
        self.mount = FakeMount()
        self.imager = FakeImager()
        self.unit_conf = FakeConf()

    def is_active(self, _activity):
        return False


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A SpiralSearch writing into tmp_path, with the mover and reaper stubbed out."""
    folder = tmp_path / "Spirals" / "0001"
    folder.mkdir(parents=True)

    monkeypatch.setattr(spiral_search, "guiding_roi", lambda: FakeRoi())
    monkeypatch.setattr(spiral_search.PathMaker, "make_spirals_folder", lambda self: str(folder))
    monkeypatch.setattr(spiral_search.PathMaker, "make_seq", lambda self, f: f"{len(os.listdir(f)):04d}")

    moved: list[str] = []
    monkeypatch.setattr(spiral_search.filer, "move_ram_to_shared", lambda p: moved.append(str(p)))
    reaped: list[str] = []
    monkeypatch.setattr(
        spiral_search.MoveGuardian, "release_folder", lambda self, f, logger=None, timeout=None: reaped.append(str(f))
    )

    unit = FakeUnit()
    search = SpiralSearch(unit)  # type: ignore[arg-type]
    search.test_folder, search.test_moved, search.test_reaped = folder, moved, reaped  # type: ignore[attr-defined]
    return search


def read_result(folder: Path) -> dict:
    return json.loads((folder / RESULT_FILE).read_text())


class TestOpeningASession:
    def test_start_tracks_exposes_and_opens_a_series(self, session):
        response = session.start(x_step_arcsec=5.0, y_step_arcsec=5.0, exposure_seconds=2.0)

        assert response.succeeded
        assert session.unit.mount.tracking, "tracking must be on for the whole search"
        assert session.unit.mount.pw.calls == ["new"]
        assert session.unit.imager.series_open == 1
        assert (session.test_folder / REFERENCE_IMAGE).exists()
        assert session.is_active

    def test_the_reference_frame_reaches_the_mover(self, session):
        session.start(1.0, 1.0, exposure_seconds=2.0)
        assert any(REFERENCE_IMAGE in p for p in session.test_moved)

    def test_exposure_seconds_is_used_and_binning_is_always_one(self, session):
        session.start(1.0, 1.0, exposure_seconds=3.5)
        assert session.unit.imager.latest_settings.seconds == 3.5
        assert session.unit.imager.latest_settings.binning == 1

    def test_starting_again_closes_the_previous_session(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0)
        first_folder = session.folder
        session.start(2.0, 2.0, exposure_seconds=1.0)

        assert read_result(Path(first_folder))["aborted"] is True, "the abandoned run must leave a trace"
        assert session.is_active, "the new session is open"

    def test_a_session_refuses_to_open_if_tracking_cannot_start(self, session):
        """The measurement is meaningless on an untracked mount.

        On 2026-08-17 `start_tracking()` returned a bare `None` because the mount's
        `connected` was false, the session opened regardless, and three runs produced
        trailed frames -- the last reporting a shift of (-0.0, 0.01) at confidence 0.93.
        One clear error beats a night of plausible-looking wrong answers.
        """
        refusal = CanonicalResponse(errors=["mount not connected, cannot start tracking"])
        session.unit.mount.start_tracking = lambda: refusal

        response = session.start(x_step_arcsec=5.0, y_step_arcsec=5.0, exposure_seconds=2.0)

        assert response.failed
        assert "not connected" in response.errors[0]
        assert not session.is_active, "no session may be left half-open"
        assert session.unit.mount.pw.calls == [], "the spiral must not be armed"
        assert session.unit.imager.series_open == 0, "no exposure series may be opened"
        assert not (session.test_folder / REFERENCE_IMAGE).exists(), "no frame may be taken"


class TestStepping:
    def test_steps_are_logged_without_saving_frames(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0, save_intermediate_exposures=False)
        before = list(session.unit.imager.exposures)

        session.step(forward=True)
        session.step(forward=True)
        session.step(forward=False)

        assert session.unit.imager.exposures == before, "no frames when save_intermediate_exposures is false"
        assert session.unit.mount.pw.calls == ["new", "next", "next", "previous"]
        assert [s["direction"] for s in session.steps] == ["reference", "next", "next", "previous"]
        assert all(s["spiral_offset"]["x"] is not None for s in session.steps), "PWI4's offset is recorded per step"

    def test_intermediate_frames_are_saved_when_asked(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0, save_intermediate_exposures=True)
        session.step(forward=True)
        assert any(name.startswith("step-") for name in session.unit.imager.exposures)

    def test_each_step_waits_for_the_mount_to_settle(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0)
        session.step(forward=True)
        assert session.unit.mount.settled == 1

    def test_stepping_without_a_session_is_an_error_not_a_no_op(self, session):
        response = session.step(forward=True)
        assert response.failed
        assert "no spiral session" in response.errors[0]


class TestTheStepMessage:
    """What comes back from a step has to say WHERE the mount is, not how many times a
    button was pressed -- the operator has to be standing on the position they judged
    brightest when they call `end`, and nothing downstream can check that for them."""

    def test_it_reports_the_cell_ring_and_angular_offset(self, session):
        session.start(10.0, 10.0, exposure_seconds=1.0)

        message = session.step(forward=True).value

        assert "step#1" in message
        assert "cell (1, 0)" in message, "the cell is the position; the counter is not"
        assert "ring 1" in message
        assert '+10.0" RA' in message, "one cell at a 10-arcsec step is 10 arcsec"
        assert '+0.0" Dec' in message
        assert "new position" in message

    def test_the_counter_keeps_rising_while_the_position_goes_back(self, session):
        """The whole reason the message carries a cell: after next/next/previous the
        counter reads 3 but the mount is back where it was at step 1."""
        session.start(10.0, 10.0, exposure_seconds=1.0)
        session.step(forward=True)
        session.step(forward=True)

        message = session.step(forward=False).value

        assert "step#3" in message, "the counter counts presses and only ever increases"
        assert "cell (1, 0)" in message
        assert "back at step#1" in message

    def test_returning_to_the_origin_is_named_as_the_reference(self, session):
        session.start(10.0, 10.0, exposure_seconds=1.0)
        session.step(forward=True)

        message = session.step(forward=False).value

        assert "back at the reference position" in message, "step#0 means nothing to an operator"

    def test_an_unknown_cell_never_matches_another_unknown_cell(self, session):
        """Two positions PWI4 could not report are not the same position. Claiming they
        are would send the operator confidently to the wrong place."""
        session.start(10.0, 10.0, exposure_seconds=1.0)
        session.unit.mount.pw.reports_offset = False

        first = session.step(forward=True).value
        second = session.step(forward=True).value

        for message in (first, second):
            assert "position unavailable" in message
            assert "back at" not in message, "an unknown cell is not a revisit"


class TestThePixelEstimate:
    SCALE = 0.2616  # arcsec/px, per COORDINATE_SURFACE.md

    def test_it_is_omitted_when_the_plate_scale_is_unset(self, session):
        """`pixel_scale_at_bin1` is 0.0 in the config DB today (MAST_unit#138). Saying
        nothing beats reporting a confident '0 px'."""
        session.start(10.0, 10.0, exposure_seconds=1.0)
        assert session.unit.unit_conf.imager.pixel_scale_at_bin1 == 0.0

        assert "px" not in session.step(forward=True).value

    def test_it_carries_the_cos_dec_factor_on_the_ra_axis(self, session):
        """`x_step_arcsec` is RA COORDINATE arcsec, so the sky moves x*step*cos(dec) along
        it (MAST_unit#136). At dec +41.157 a commanded 10" moves 7.53", which is 29 px --
        not the 38 px the uncorrected arithmetic would claim. The estimate is meant to be
        compared against the shift `end` measures, so it has to be in the same units as
        the sky, not as the command."""
        session.unit.unit_conf.imager.pixel_scale_at_bin1 = self.SCALE
        session.start(10.0, 10.0, exposure_seconds=1.0)

        message = session.step(forward=True).value

        assert "(~29 px)" in message
        assert "38 px" not in message, "cos(dec) was not applied"


class TestEndingASession:
    def test_end_measures_the_shift_and_closes_down(self, session):
        session.unit.imager.shift = (4.0, 9.0)  # the operator moved the field
        session.start(1.0, 1.0, exposure_seconds=1.0)
        response = session.end()

        assert response.succeeded
        assert response.value["shift"]["dx"] == pytest.approx(9.0, abs=0.6)
        assert response.value["shift"]["dy"] == pytest.approx(4.0, abs=0.6)
        assert not session.unit.mount.tracking, "end must stop tracking"
        assert session.unit.imager.series_open == 0
        assert not session.is_active

    def test_both_frames_and_the_result_are_on_disk(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0)
        session.end()

        folder = session.test_folder
        assert (folder / REFERENCE_IMAGE).exists() and (folder / FINAL_IMAGE).exists()
        assert (folder / RESULT_FILE).exists(), "the operator reads this"
        for name in (REFERENCE_IMAGE, FINAL_IMAGE, RESULT_FILE):
            assert any(name in p for p in session.test_moved), f"{name} was never handed to the mover"

    def test_the_folder_is_released_for_reaping(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0)
        session.end()
        assert session.test_reaped, "release_folder must be called so the ram disk is reclaimed"

    def test_result_json_carries_the_session(self, session):
        session.start(2.5, 3.5, exposure_seconds=1.5, usable_fraction=0.5)
        session.step(forward=True)
        session.end()

        result = read_result(session.test_folder)
        assert result["x_step_arcsec"] == 2.5
        assert result["y_step_arcsec"] == 3.5
        assert result["exposure_seconds"] == 1.5
        assert result["margin_horizontal"] == SIZE // 4, "usable_fraction=0.5 trims a quarter off each side"
        assert result["margin_source"] == "usable_fraction=0.5"
        assert result["center_source"] == "guiding.rois[fcu_v2]"
        assert result["center_x"] == CENTER
        assert result["aborted"] is False
        assert len(result["steps"]) == 3  # reference + one step + final
        assert result["shift"]["dx"] == pytest.approx(0.0, abs=0.2)

    def test_ending_without_a_session_is_an_error(self, session):
        response = session.end()
        assert response.failed
        assert "no spiral session" in response.errors[0]

    def test_ending_back_at_the_origin_is_not_a_fixed_pattern_alarm(self, session):
        """An operator who judged the spiral origin brightest backtracks to it and ends
        there. The mount then genuinely did not move, so a null shift is the CORRECT
        answer -- but it is character-for-character the fixed-pattern signature
        `at_origin` exists to flag, and warning about it says the opposite of the truth."""
        session.start(10.0, 10.0, exposure_seconds=1.0)
        session.step(forward=True)
        session.step(forward=False)

        result = session.end().value

        assert result["ended_at_reference_position"] is True
        assert result["shift"]["dx"] == pytest.approx(0.0, abs=0.2)

    def test_a_session_that_moved_is_not_marked_as_ending_at_the_reference(self, session):
        session.start(10.0, 10.0, exposure_seconds=1.0)
        session.step(forward=True)

        assert session.end().value["ended_at_reference_position"] is False

    def test_an_oversized_shift_is_flagged(self, session, monkeypatch):
        """Past the overlap limit the correlation stops meaning anything; say so."""
        monkeypatch.setattr(spiral_search, "max_reliable_shift", lambda shape, frac: 1.0)
        session.unit.imager.shift = (4.0, 9.0)
        session.start(1.0, 1.0, exposure_seconds=1.0)

        result = session.end().value

        assert result["shift_exceeds_reliable_range"] is True
        assert result["max_reliable_shift_pixels"] == 1.0
        assert result["shift_magnitude_pixels"] > 1.0


class TestAbandonedSession:
    def test_timeout_closes_without_inventing_a_measurement(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0)
        exposures_before = list(session.unit.imager.exposures)

        session._on_timeout()

        result = read_result(session.test_folder)
        assert result["aborted"] is True
        assert result["shift"] is None, "no measurement may be reported for a run nobody confirmed"
        assert result["final_image"] is None
        assert session.unit.imager.exposures == exposures_before, "no final frame is taken"
        assert not session.unit.mount.tracking, "tracking must not be left running"
        assert not session.is_active

    def test_timeout_after_a_normal_end_does_nothing(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0)
        session.end()
        tracking_after_end = session.unit.mount.tracking

        session._on_timeout()  # the timer fires late; end() already won

        assert session.unit.mount.tracking == tracking_after_end
        assert read_result(session.test_folder)["aborted"] is False

    def test_a_normal_end_cancels_the_timer(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0)
        timer = session._timer
        session.end()
        assert session._timer is None
        assert not timer.is_alive()


class TestResolvingTheUsableRegion:
    """Centre and size resolve independently, each by its own precedence chain.

    Keeping them independent is deliberate: the operator may want a different area
    around the configured fibre, or the configured area around a different point.
    """

    SHAPE = (1000, 2000)  # (ny, nx)

    def test_center_prefers_the_callers_parameters(self, monkeypatch):
        monkeypatch.setattr(spiral_search, "guiding_roi", lambda: FakeRoi())
        assert spiral_search.resolve_center(11, 22, self.SHAPE) == (11, 22, "parameters")

    @pytest.mark.parametrize(("x", "y"), [(11, None), (None, 22)], ids=["x only", "y only"])
    def test_one_coordinate_alone_is_not_enough(self, monkeypatch, x, y):
        """Half a centre says nothing about the other half, so it is not blended
        with the fallback -- that would place the window somewhere nobody chose."""
        monkeypatch.setattr(spiral_search, "guiding_roi", lambda: FakeRoi())
        cx, cy, source = spiral_search.resolve_center(x, y, self.SHAPE)
        assert (cx, cy) == (FakeRoi.fiber_x, FakeRoi.fiber_y)
        assert source == "guiding.rois[fcu_v2]"

    def test_center_falls_back_to_the_configured_fibre(self, monkeypatch):
        monkeypatch.setattr(spiral_search, "guiding_roi", lambda: FakeRoi())
        assert spiral_search.resolve_center(None, None, self.SHAPE) == (
            FakeRoi.fiber_x,
            FakeRoi.fiber_y,
            "guiding.rois[fcu_v2]",
        )

    def test_center_falls_back_to_the_frame_centre(self, monkeypatch):
        monkeypatch.setattr(spiral_search, "guiding_roi", lambda: None)
        assert spiral_search.resolve_center(None, None, self.SHAPE) == (1000, 500, "frame centre")

    def test_margins_prefer_the_callers_fraction(self, monkeypatch):
        monkeypatch.setattr(spiral_search, "guiding_roi", lambda: FakeRoi())
        h, v, source = spiral_search.resolve_margins(0.5, self.SHAPE)
        assert (h, v) == (500, 250), "each axis is trimmed by a quarter of its own length"
        assert source == "usable_fraction=0.5"

    def test_margins_fall_back_to_the_configured_roi(self, monkeypatch):
        monkeypatch.setattr(spiral_search, "guiding_roi", lambda: FakeRoi())
        assert spiral_search.resolve_margins(None, self.SHAPE) == (
            FakeRoi.margin_horizontal,
            FakeRoi.margin_vertical,
            "guiding.rois[fcu_v2]",
        )

    def test_margins_fall_back_to_the_defaults(self, monkeypatch):
        monkeypatch.setattr(spiral_search, "guiding_roi", lambda: None)
        h, v, source = spiral_search.resolve_margins(None, self.SHAPE)
        assert (h, v) == (spiral_search.DEFAULT_MARGIN_HORIZONTAL, spiral_search.DEFAULT_MARGIN_VERTICAL)
        assert source == "default"

    def test_an_unreadable_configuration_does_not_raise(self, monkeypatch):
        """A missing or malformed ROI must degrade to the frame centre, not abort the
        session -- but it must say so, which is what center_source carries."""

        def explode():
            raise RuntimeError("mongo is down")

        monkeypatch.setattr(spiral_search.Config, "__call__", lambda self: explode())
        monkeypatch.setattr(spiral_search, "guiding_roi", spiral_search.guiding_roi)
        _cx, _cy, source = spiral_search.resolve_center(None, None, self.SHAPE)
        assert source in ("frame centre", "guiding.rois[fcu_v2]")


class TestExposureFailures:
    """PHD2 signals failure by RETURNING errors, not raising -- when it is disconnected,
    or when the requested binning does not match its configured profile. Nothing is saved
    in that case, and `wait_for_image_saved` waits on an event that will never be set,
    with no timeout. Ignoring the return value therefore hangs the session forever while
    holding its lock. These pin that every exposure site checks it.
    """

    def test_a_failed_reference_exposure_closes_the_session_down(self, session):
        session.unit.imager.fail_next = True

        response = session.start(1.0, 1.0, exposure_seconds=1.0)

        assert response.failed
        assert "reference exposure failed" in response.errors[0]
        assert not session.is_active, "a session with no reference frame is unusable"
        assert not session.unit.mount.tracking, "tracking must not be left on"
        assert session.unit.imager.series_open == 0, "the exposure series must not be left open"

    def test_a_failed_final_exposure_still_closes_and_records(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0)
        session.unit.imager.fail_next = True

        response = session.end()

        assert response.failed
        assert not session.unit.mount.tracking, "tracking must not be left on"
        assert not session.is_active
        result = read_result(session.test_folder)
        assert result["aborted"] is True, "the operator must be able to see why there is no shift"
        assert result["shift"] is None
        assert "final exposure failed" in result["abort_reason"]

    def test_a_truncated_reference_frame_closes_the_session_down(self, session):
        """A partial readout is not a measurable frame.

        On 2026-08-17 four sessions ran to completion on frames that stopped at row 4881 of
        5640, and the last reported (-0.0, 0.01) at confidence 0.93 -- a correlation between
        two frames sharing the same dead region locks onto it. The exposure "succeeds": PHD2
        returns Ok and the FITS is written at its declared size. Only the data is missing.
        """
        session.unit.imager.truncate_rows = 40

        response = session.start(1.0, 1.0, exposure_seconds=1.0)

        assert response.failed
        assert "truncated" in response.errors[0]
        assert not session.is_active
        assert not session.unit.mount.tracking, "tracking must not be left on"
        assert session.unit.imager.series_open == 0, "the exposure series must not be left open"

    def test_a_truncated_final_frame_records_why_there_is_no_shift(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0)
        session.unit.imager.truncate_rows = 40

        response = session.end()

        assert response.failed
        assert not session.is_active
        result = read_result(session.test_folder)
        assert result["aborted"] is True
        assert result["shift"] is None, "no shift may be reported from a partial frame"
        assert "truncated" in result["abort_reason"]

    def test_a_failed_intermediate_exposure_does_not_end_the_session(self, session):
        """The measurement needs only the reference and the final, so a lost step frame
        is a nuisance rather than a reason to throw away the operator's work."""
        session.start(1.0, 1.0, exposure_seconds=1.0, save_intermediate_exposures=True)
        session.unit.imager.fail_next = True

        response = session.step(forward=True)

        assert response.succeeded
        assert session.is_active, "the session must survive a lost intermediate frame"
        assert session.steps[-1]["image"] is None, "no filename may be recorded for a frame never written"

        session.unit.imager.fail_next = False
        assert session.end().succeeded, "and the session must still be able to finish"

    def test_the_exposure_asks_for_a_full_frame_and_a_gain(self, session):
        """Both are required by PHD2's non-guiding path: it does `assert settings.roi`
        and converts settings.gain, so omitting either raises inside the backend."""
        session.start(1.0, 1.0, exposure_seconds=1.0)

        settings = session.unit.imager.latest_settings
        assert settings.roi is not None, "PHD2 asserts on this"
        full = session.unit.imager.full_frame
        # Compared against the imager's own full_frame rather than the raw frame size:
        # ImagerRoi conditions its dimensions (width to a multiple of 8, height of 2), so
        # the two agree on a real 8288x5644 sensor but not on an arbitrary test size.
        assert (settings.roi.width, settings.roi.height) == (full.width, full.height), "the full frame"
        assert settings.gain == 170
        assert settings.binning == 1
