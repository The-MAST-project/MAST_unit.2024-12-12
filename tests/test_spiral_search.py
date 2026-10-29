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
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("skimage", reason="scikit-image unavailable")
pytest.importorskip("photutils", reason="photutils unavailable")
from astropy.io import fits

import spiral_search
from common.activities import UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.filer import Filer, FilingArea
from common.models.latest import LATEST_FILE
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

    @property
    def is_tracking(self):
        return self.tracking

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
        # What the config DB holds for every unit (0.2616 arcsec/px, COORDINATE_SURFACE.md).
        self.imager = SimpleNamespace(pixel_scale_at_bin1=0.2616)


class FakeUnit:
    def __init__(self):
        self.mount = FakeMount()
        self.imager = FakeImager()
        self.unit_conf = FakeConf()
        self.active: set = set()

    def is_active(self, activity):
        return activity in self.active


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A SpiralSearch writing into tmp_path, with the mover and reaper stubbed out."""
    # Under the suite's RAM area (common.testing.filer), so the record derives its share
    # paths through a real Filer.
    folder = Path(spiral_search.filer.ram.root) / tmp_path.name / "Spirals" / "0001"
    folder.mkdir(parents=True)

    monkeypatch.setattr(spiral_search, "guiding_roi", lambda: FakeRoi())
    monkeypatch.setattr(spiral_search.PathMaker, "make_spirals_folder", lambda self: str(folder))
    monkeypatch.setattr(spiral_search.PathMaker, "make_seq", lambda self, f: f"{len(os.listdir(f)):04d}")

    moved: list[str] = []
    # On the class: the latest record hands its file to the mover through a Filer of its own.
    monkeypatch.setattr(Filer, "move_ram_to_shared", lambda self, p: moved.append(str(p)))
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

        assert message["step#"] == 1
        assert message["cell"] == (1, 0), "the cell is the position; the counter is not"
        assert message["ring"] == 1
        # With the plate scale every unit has, the pixel estimate follows (TestThePixelEstimate).
        assert message["offset"].startswith("+10.0arcsec RA, +0.0arcsec Dec"), "one cell at a 10-arcsec step is 10 arcsec"
        assert message["revisit"] == "new position"

    def test_the_cell_is_a_pair_of_numbers_not_a_pair_of_sets(self, session):
        """`{x}` is a one-element SET. The braces meant interpolation while this was an
        f-string and had to stop meaning that when it became a dict, or the operator gets
        `[[1], [0]]` back as their position."""
        session.start(10.0, 10.0, exposure_seconds=1.0)

        cell = session.step(forward=True).value["cell"]

        assert cell == (1, 0)
        assert all(isinstance(c, int) for c in cell), f"cell holds {[type(c).__name__ for c in cell]}"

    def test_the_counter_keeps_rising_while_the_position_goes_back(self, session):
        """The whole reason the response carries a cell: after next/next/previous the
        counter reads 3 but the mount is back where it was at step 1."""
        session.start(10.0, 10.0, exposure_seconds=1.0)
        session.step(forward=True)
        session.step(forward=True)

        message = session.step(forward=False).value

        assert message["step#"] == 3, "the counter counts presses and only ever increases"
        assert message["cell"] == (1, 0)
        assert message["revisit"] == "back at step#1"

    def test_returning_to_the_origin_is_named_as_the_reference(self, session):
        session.start(10.0, 10.0, exposure_seconds=1.0)
        session.step(forward=True)

        message = session.step(forward=False).value

        assert message["revisit"] == "back at the reference position", "step#0 means nothing to an operator"

    def test_an_unknown_cell_never_matches_another_unknown_cell(self, session):
        """Two positions PWI4 could not report are not the same position. Claiming they
        are would send the operator confidently to the wrong place."""
        session.start(10.0, 10.0, exposure_seconds=1.0)
        session.unit.mount.pw.reports_offset = False

        first = session.step(forward=True).value
        second = session.step(forward=True).value

        for message in (first, second):
            assert "no spiral offset" in message["error"]
            assert "revisit" not in message, "an unknown cell is not a revisit"


class TestThePixelEstimate:
    SCALE = 0.2616  # arcsec/px, per COORDINATE_SURFACE.md

    def test_it_is_omitted_without_a_configuration(self, session):
        """No configuration, no plate scale: saying nothing beats a confident '0 px'."""
        session.start(10.0, 10.0, exposure_seconds=1.0)
        session.unit.unit_conf = None

        assert "px" not in session.step(forward=True).value["offset"]

    def test_it_is_omitted_when_the_declination_is_unknown(self, session, monkeypatch):
        # Read from PWI4 on every step: PWI4 reporting none is what makes it unknown.
        monkeypatch.setattr(FakePw, "DEC_DEGREES", None)
        session.start(10.0, 10.0, exposure_seconds=1.0)

        assert "px" not in session.step(forward=True).value["offset"]

    def test_it_carries_the_cos_dec_factor_on_the_ra_axis(self, session):
        """`x_step_arcsec` is RA COORDINATE arcsec, so the sky moves x*step*cos(dec) along
        it (MAST_unit#136). At dec +41.157 a commanded 10" moves 7.53", which is 29 px --
        not the 38 px the uncorrected arithmetic would claim. The estimate is meant to be
        compared against the shift `end` measures, so it has to be in the same units as
        the sky, not as the command."""
        session.unit.unit_conf.imager.pixel_scale_at_bin1 = self.SCALE
        session.start(10.0, 10.0, exposure_seconds=1.0)

        offset = session.step(forward=True).value["offset"]

        assert "(~29 px)" in offset
        assert "38 px" not in offset, "cos(dec) was not applied"


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


def share_path(session, name: str) -> str:
    return Filer().to_top(FilingArea.SHARE, os.path.join(session.test_folder, name))


class TestTheLatestRecord:
    """`status.latest.spiral`: a caller off the unit follows the session there and reads its
    frames from the share, rather than guessing `<night>/Spirals/<NNNN>` -- a guess
    MAST_common#137 makes wrong after a reboot, when the folder numbers restart."""

    def test_an_open_session_is_running_with_its_share_folder(self, session):
        session.start(2.0, 3.0, exposure_seconds=4.0, save_intermediate_exposures=True)

        record = session.latest
        assert record.running and record.succeeded is None
        assert (record.x_step_arcsec, record.y_step_arcsec, record.exposure_seconds) == (2.0, 3.0, 4.0)
        assert record.shared_products_path == Filer().to_top(FilingArea.SHARE, str(session.test_folder))

    def test_a_saved_step_frame_is_reported_by_its_share_path(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0, save_intermediate_exposures=True)

        value = session.step(forward=True).value

        name = session.steps[-1]["image"]
        assert value["image"] == share_path(session, name)
        assert "image_error" not in value
        assert session.latest.steps == 1 and session.latest.last_frame.endswith(f"/{name}")

    def test_while_the_share_is_demoted_no_share_path_is_reported(self, session, monkeypatch):
        """Products go to the unit's share fallback then. A caller told the share path would
        wait for a file that never arrives -- the frames lost on 2026-07-14."""
        monkeypatch.setitem(Filer._demoted, FilingArea.SHARE, "unreachable")
        session.start(1.0, 1.0, exposure_seconds=1.0, save_intermediate_exposures=True)

        step = session.step(forward=True).value

        assert session.latest.shared_products_path is None
        assert step["image"] is None
        assert "not on the share" in step["image_error"]
        assert session.is_active, "the frames are still taken, on the unit"

    def test_no_image_is_reported_when_frames_are_not_saved(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0, save_intermediate_exposures=False)

        value = session.step(forward=True).value

        assert value["image"] is None
        assert "image_error" not in value, "not saving a frame is not an error"
        assert session.latest.steps == 1 and session.latest.last_frame is None

    def test_a_failed_step_frame_is_reported_as_failed(self, session):
        """Otherwise a lost frame and a slow mover look the same from outside: a file that
        never arrives."""
        session.start(1.0, 1.0, exposure_seconds=1.0, save_intermediate_exposures=True)
        session.unit.imager.fail_next = True

        value = session.step(forward=True).value

        assert value["image"] is None
        assert "fake imager was told to fail" in value["image_error"]

    def test_the_image_is_reported_even_when_pwi4_reports_no_cell(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0, save_intermediate_exposures=True)
        session.unit.mount.pw.reports_offset = False

        value = session.step(forward=True).value

        assert "no spiral offset" in value["error"]
        assert value["image"] == share_path(session, session.steps[-1]["image"])

    def test_a_measured_session_succeeds_with_its_shift_and_stays_reported(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0)

        result = session.end().value

        record = session.latest
        assert not session.is_active
        assert record.succeeded is True and not record.running
        assert record.shift == result["shift"]
        assert any(p.endswith(LATEST_FILE) for p in session.test_moved), "written into the folder, for the share"

    def test_an_abandoned_session_ends_failed_with_the_reason(self, session):
        """The abandonment timer closes a session with no request in flight: the record is
        the only place a caller can see it happened."""
        session.start(1.0, 1.0, exposure_seconds=1.0)

        session._on_timeout(session._timer_generation)

        assert session.latest.succeeded is False and not session.latest.running
        assert "no step or spiral_end_path" in session.latest.errors[0]

    def test_a_session_that_cannot_start_tracking_is_recorded_as_failed(self, session):
        session.unit.mount.start_tracking = lambda: CanonicalResponse(errors=["mount not connected"])

        session.start(1.0, 1.0, exposure_seconds=1.0)

        assert session.latest.succeeded is False
        assert session.latest.errors == ["mount not connected"]

    def test_a_new_session_replaces_the_record(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0)
        first = session.latest

        session.start(2.0, 2.0, exposure_seconds=1.0)

        assert first.succeeded is False and "superseded" in first.errors[0]
        assert session.latest is not first and session.latest.running


class TestAbandonedSession:
    def test_each_step_restarts_the_abandonment_timer(self, session):
        """The timeout is for a session nobody is driving. Counted from the start, it ended
        long walks partway -- an automated spiral of six rings takes over an hour -- with
        no measurement."""
        session.start(1.0, 1.0, exposure_seconds=1.0)
        first = session._timer

        session.step(forward=True)

        assert session._timer is not first
        assert not first.is_alive(), "the old timer must be cancelled, not left to fire"
        assert session._timer.is_alive()
        session.end()

    def test_a_timer_that_fired_during_a_step_does_not_abandon_the_session(self, session):
        """`cancel()` cannot stop a timer that has already fired. One that fires while a
        step holds the lock runs once the step releases it, after the step has re-armed --
        and must then leave the session alone."""
        session.start(1.0, 1.0, exposure_seconds=1.0)
        stale = session._timer_generation

        session.step(forward=True)
        session._on_timeout(stale)

        assert session.is_active, "a step re-armed the timer, so the old one is stale"
        session.end()

    def test_timeout_closes_without_inventing_a_measurement(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0)
        exposures_before = list(session.unit.imager.exposures)

        session._on_timeout(session._timer_generation)

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

        session._on_timeout(session._timer_generation)  # the timer fires late; end() already won

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

        monkeypatch.setattr(spiral_search, "Config", explode)
        assert spiral_search.resolve_center(None, None, self.SHAPE) == (1000, 500, "frame centre")


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


class TestAFramelessSession:
    """`frames=False`: PWI4's spiral moves the mount and the session takes no frame, so a
    caller's `/expose` can take them at each cell (MAST_unit#385)."""

    def test_it_opens_without_a_frame_or_a_series(self, session):
        response = session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)

        assert response.succeeded and session.is_active
        assert session.unit.imager.exposures == [] and session.unit.imager.series_open == 0
        assert session.unit.mount.tracking and session.unit.mount.pw.calls == ["new"]
        assert response.value["reference_image"] is None
        assert session.latest.frames is False and session.latest.exposure_seconds is None

    def test_it_is_refused_with_saved_step_frames(self, session):
        response = session.start(1.0, 1.0, exposure_seconds=1.0, frames=False, save_intermediate_exposures=True)

        assert response.failed and not session.is_active
        assert session.unit.mount.pw.calls == []

    def test_the_origin_is_logged_so_steps_count_and_revisit_as_usual(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)

        session.step(forward=True)
        back = session.step(forward=False).value

        assert session.steps[0]["direction"] == "origin" and session.steps[0]["image"] is None
        assert back["step#"] == 2
        assert back["revisit"] == "back at the reference position"

    def test_a_step_takes_no_frame(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)

        value = session.step(forward=True).value

        assert value["image"] is None and "image_error" not in value
        assert session.unit.imager.exposures == []

    @pytest.mark.parametrize("tracking_before", [True, False])
    def test_the_end_measures_nothing_and_leaves_tracking_as_it_found_it(self, session, tracking_before):
        session.unit.mount.tracking = tracking_before
        session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)
        session.step(forward=True)

        response = session.end()

        assert response.succeeded and not session.is_active
        assert session.unit.imager.exposures == [], "no final frame"
        assert session.unit.mount.tracking == tracking_before
        result = read_result(session.test_folder)
        assert result["shift"] is None and result["aborted"] is False and result["frames"] is False
        assert session.latest.succeeded is True and session.latest.shift is None

    def test_an_abandoned_frameless_session_has_not_succeeded(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)

        session._on_timeout(session._timer_generation)

        assert session.latest.succeeded is False
        assert read_result(session.test_folder)["aborted"] is True


class TestKeepingOutOfAnExposeRun:
    """While `/expose` runs, no session verb may move the mount, stop tracking or open a
    series under its frame (MAST_unit#232, #385)."""

    def test_a_session_cannot_open(self, session):
        session.unit.active.add(UnitActivities.Exposing)

        response = session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)

        assert response.failed and "exposure run" in response.errors[0]
        assert session.unit.mount.pw.calls == [] and not session.is_active

    @pytest.mark.parametrize("verb", ["next", "previous", "end"])
    def test_an_open_session_does_not_move_or_close(self, session, verb):
        session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)
        session.unit.active.add(UnitActivities.Exposing)
        calls = list(session.unit.mount.pw.calls)

        response = session.end() if verb == "end" else session.step(forward=verb == "next")

        assert response.failed and "exposure run" in response.errors[0]
        assert session.unit.mount.pw.calls == calls
        assert session.is_active and session.unit.mount.tracking

    def test_the_timeout_waits_for_the_run(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)
        session.unit.active.add(UnitActivities.Exposing)
        generation = session._timer_generation

        session._on_timeout(generation)

        assert session.is_active, "not closed under a frame"
        assert session._timer_generation == generation + 1, "re-armed"
        session.unit.active.clear()
        session.end()


class TestKeepingOutOfFluxMetering:
    def test_a_session_cannot_open_during_a_flux_metering_run(self, session):
        """Flux metering drives PWI4's spiral itself; the two would reset it under each other."""
        session.unit.active.add(UnitActivities.FluxMetering)

        response = session.start(1.0, 1.0, exposure_seconds=1.0)

        assert response.failed and "flux-metering" in response.errors[0]
        assert session.unit.mount.pw.calls == []


class TestAbort:
    def test_an_imaging_session_closes_as_abandoned_without_a_final_frame(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0)
        exposures = list(session.unit.imager.exposures)

        session.abort()

        assert not session.is_active and not session.unit.mount.tracking
        assert session.unit.imager.exposures == exposures
        assert read_result(session.test_folder)["aborted"] is True
        assert session.latest.succeeded is False and session.latest.errors == ["aborted"]

    def test_a_frameless_session_closes_as_aborted(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)

        session.abort()

        assert not session.is_active and session.latest.succeeded is False

    def test_without_a_session_nothing(self, session):
        session.abort()
        assert session.latest is None

    def test_a_busy_session_is_closed_once_its_verb_lets_go_and_abort_does_not_wait(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)
        holding, release = threading.Event(), threading.Event()

        def hold():
            with session._lock:
                holding.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        holding.wait(5)
        began = time.monotonic()
        session.abort()
        assert time.monotonic() - began < 1.0, "abort answers at once"
        assert session.is_active, "the verb in flight still holds it"

        release.set()
        holder.join(5)
        session._abort_waiter.join(5)
        assert not session.is_active

    def test_a_session_opened_after_the_abort_is_left_alone(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)
        holding, release = threading.Event(), threading.Event()

        def hold():
            with session._lock:
                holding.set()
                release.wait(5)
                session.end()
                session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)

        holder = threading.Thread(target=hold)
        holder.start()
        holding.wait(5)
        session.abort()
        release.set()
        holder.join(5)
        session._abort_waiter.join(5)

        assert session.is_active, "the new session is not the one abort was asked to close"
        session.end()


class TestExposeRefusal:
    """What `/expose` asks the session before it touches the mount (`refusal_for_expose`)."""

    def test_no_session_refuses_nothing(self, session):
        with session.held_for_expose(moves_mount=True) as refusal:
            assert refusal is None

    def test_an_imaging_session_refuses_and_says_how_to_end_it(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0)

        with session.held_for_expose(moves_mount=False) as refusal:
            assert refusal is not None and "spiral_end_path" in refusal and "unit/abort" in refusal
        session.end()

    def test_a_frameless_session_takes_an_expose_that_leaves_the_mount_alone(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)

        with session.held_for_expose(moves_mount=False) as refusal:
            assert refusal is None
        with session.held_for_expose(moves_mount=True) as refusal:
            assert refusal is not None and "outside PWI4's spiral" in refusal
        session.end()

    def test_a_step_in_progress_refuses(self, session):
        session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)
        holding, release = threading.Event(), threading.Event()

        def hold():
            with session._lock:
                holding.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        holding.wait(5)
        with session.held_for_expose(moves_mount=False) as refusal:
            assert refusal is not None and "moving the mount" in refusal
        release.set()
        holder.join(5)
        session.end()

    def test_the_session_is_held_while_the_expose_sets_up(self, session):
        """Held from the check until `Exposing` is set, so a step cannot slip in between."""
        session.start(1.0, 1.0, exposure_seconds=1.0, frames=False)
        acquired: list[bool] = []

        with session.held_for_expose(moves_mount=False):
            other = threading.Thread(target=lambda: acquired.append(session._lock.acquire(blocking=False)))
            other.start()
            other.join(5)
        assert acquired == [False]
        session.end()
