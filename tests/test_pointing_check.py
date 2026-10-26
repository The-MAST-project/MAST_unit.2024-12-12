"""Full-frame guide frames are plate solved while guiding continues (#283).

PHD2's own numbers say the star is holding still on the lock position. They cannot
say the lock position is still on the target: a lock on the wrong star, drift
between the guide camera and the fiber, or a handover that moved the field all
read as clean guiding. The monitor solves the frame PHD2 is already taking and
records how far the target sits from the fiber, without touching the guide loop.

What is pinned here:

- a limit-frame crop is solved with the crop origin read from PHD2, so the SPEC
  reference pixel stays at the fiber (#234); a frame that is not the held limit
  frame, or is binned, is reported **unavailable**, never solved;
- a failed solve is a recorded sample, not a dropped one;
- the frame (94 MB at full size) is deleted on every path;
- the cadence is read live, and zero means off.
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
from astropy.io import fits

import pointing_check
from common.asi import ASI_294MM_HEIGHT, ASI_294MM_WIDTH
from common.interfaces.solving import SolvingResult, SolvingSolution
from common.models.statuses import ImagerRoi, PointingCheckStatus, PointingSampleOutcome
from failure_streaks import FailureStreaks
from pointing_check import PointingMonitor

#: 15h 00m 00s, +30 deg -- an arbitrary target away from both RA wrap and the pole.
TARGET_RA_HOURS = 15.0
TARGET_DEC_DEGS = 30.0
ARCSEC_PER_HOUR_OF_RA = 15.0 * 3600.0

#: Short enough to keep the threaded tests fast, long enough to count ticks.
FAST_INTERVAL_SECONDS = 0.05
WAIT_FOR_TICKS_SECONDS = 5.0


def write_frame(path: Path, width: int, height: int) -> str:
    """A FITS file of the given size; only the header is ever read by the monitor."""
    hdu = fits.PrimaryHDU(np.zeros((height, width), dtype=np.uint8))
    hdu.writeto(path)
    return str(path)


class FakeBackend:
    def __init__(self, result: SolvingResult | None = None, raises: Exception | None = None):
        self.result = result
        self.raises = raises
        self.calls: list[dict] = []

    def solve(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.result


def solved(ra_hours: float = TARGET_RA_HOURS, dec_degs: float = TARGET_DEC_DEGS) -> SolvingResult:
    return SolvingResult(
        succeeded=True,
        solution=SolvingSolution(
            ra_hours=ra_hours,
            dec_degs=dec_degs,
            dec_rads=float(np.radians(dec_degs)),
            matched_stars=42,
        ),
    )


class FakeConnector:
    """What the monitor reads off the PHD2 connector, and nothing else."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        backend: FakeBackend,
        frame_size: tuple[int, int] = (ASI_294MM_WIDTH, ASI_294MM_HEIGHT),
        interval: float = 0,
        acquisition: bool = True,
        mirror_in: bool = True,
        limit_frame: ImagerRoi | None = None,
        profile_binning: int = 1,
    ):
        self.tmp_path = tmp_path
        self.frame_size = frame_size
        self.limit_frame = limit_frame
        self.profile_binning = profile_binning
        self.app_state = "Guiding"
        self.avg_dist = 0.4
        self.saved: list[str] = []
        self.lock_validity = SimpleNamespace(state=SimpleNamespace(validity="OnStar"))
        self.parent = SimpleNamespace(
            unit=SimpleNamespace(
                unit_conf=SimpleNamespace(phd2=SimpleNamespace(validation_interval=interval)),
                solver=SimpleNamespace(_backend=backend),
                stage=SimpleNamespace(at_preset=lambda preset: mirror_in),
                acquirer=SimpleNamespace(
                    latest_acquisition=(
                        SimpleNamespace(
                            target_ra=TARGET_RA_HOURS,
                            target_dec=TARGET_DEC_DEGS,
                            folder=str(tmp_path / "acquisition"),
                        )
                        if acquisition
                        else None
                    )
                ),
            )
        )

    @property
    def conf(self):
        return self.parent.unit.unit_conf.phd2

    def save_image(self) -> str:
        path = self.tmp_path / f"sav{len(self.saved):04d}.tmp"
        self.saved.append(write_frame(path, *self.frame_size))
        return str(path)

    def get_lock_position(self):
        return (4144.0, 2822.0)

    def get_limit_frame(self) -> ImagerRoi | None:
        return self.limit_frame


@pytest.fixture(autouse=True)
def sessions_end(monkeypatch):
    """Every session a test starts is stopped, and an idle one re-reads its cadence quickly."""
    monkeypatch.setattr(pointing_check, "IDLE_POLL_SECONDS", FAST_INTERVAL_SECONDS)
    started: list[PointingMonitor] = []
    start = PointingMonitor.start_session

    def tracked(self):
        started.append(self)
        start(self)

    monkeypatch.setattr(PointingMonitor, "start_session", tracked)
    yield
    for monitor in started:
        monitor.stop_session()


@pytest.fixture
def journal_dir(tmp_path, monkeypatch):
    """Point the journal at a temp folder instead of the share."""
    folder = tmp_path / "shared"
    monkeypatch.setattr(PointingMonitor, "_journal_folder", lambda self, acquisition_folder: folder)
    return folder


class TestASample:
    def test_a_solved_frame_records_the_offset_from_the_target(self, tmp_path, journal_dir):
        backend = FakeBackend(solved(ra_hours=TARGET_RA_HOURS - 1.0 / 3600.0, dec_degs=TARGET_DEC_DEGS - 2.0 / 3600.0))
        monitor = PointingMonitor(FakeConnector(tmp_path, backend=backend))
        monitor.start_session()

        sample = monitor.sample_once()

        assert sample is not None and sample.outcome is PointingSampleOutcome.Solved
        # target minus solved: one second of RA, two arcseconds of dec
        assert sample.d_ra_arcsec == pytest.approx(ARCSEC_PER_HOUR_OF_RA / 3600.0, abs=1e-6)
        assert sample.d_dec_arcsec == pytest.approx(2.0, abs=1e-6)
        on_sky_ra = 15.0 * np.cos(np.radians(TARGET_DEC_DEGS))
        assert sample.offset_arcsec == pytest.approx(np.hypot(on_sky_ra, 2.0), abs=1e-3)
        assert sample.matched_stars == 42
        assert sample.mirror_in is True
        assert sample.lock_position == (4144.0, 2822.0)
        assert sample.lock_validity == "OnStar"

    def test_it_solves_on_the_fiber_reference(self, tmp_path, journal_dir):
        """The SPEC phase puts the solved position at the fiber, which is what the target must stay on."""
        backend = FakeBackend(solved())
        connector = FakeConnector(tmp_path, backend=backend)
        monitor = PointingMonitor(connector)
        monitor.start_session()
        monitor.sample_once()

        (call,) = backend.calls
        assert call["phase"] == "spec"
        assert call["full_frame_input_image_path"] == connector.saved[0]

    def test_the_frame_is_deleted_after_solving(self, tmp_path, journal_dir):
        connector = FakeConnector(tmp_path, backend=FakeBackend(solved()))
        monitor = PointingMonitor(connector)
        monitor.start_session()
        monitor.sample_once()
        assert not os.path.exists(connector.saved[0])

    def test_a_failed_solve_is_recorded(self, tmp_path, journal_dir):
        monitor = PointingMonitor(FakeConnector(tmp_path, backend=FakeBackend(SolvingResult(succeeded=False))))
        monitor.start_session()
        sample = monitor.sample_once()
        assert sample is not None and sample.outcome is PointingSampleOutcome.NotSolved
        assert monitor.status().not_solved == 1

    def test_a_solver_that_raises_is_recorded_and_the_frame_still_deleted(self, tmp_path, journal_dir):
        connector = FakeConnector(tmp_path, backend=FakeBackend(raises=RuntimeError("solve-field died")))
        monitor = PointingMonitor(connector)
        monitor.start_session()
        sample = monitor.sample_once()
        assert sample is not None and sample.outcome is PointingSampleOutcome.NotSolved
        assert "solve-field died" in (sample.reason or "")
        assert not os.path.exists(connector.saved[0])


#: The strip limit frame on mast01, x 6363..8280: a fixed limit frame PHD2 delivers verbatim.
STRIP = ImagerRoi.verbatim(x=6363, y=0, width=1917, height=4812)


class TestALimitFrame:
    def test_a_strip_is_solved_from_its_origin(self, tmp_path, journal_dir):
        backend = FakeBackend(solved(ra_hours=TARGET_RA_HOURS, dec_degs=TARGET_DEC_DEGS - 3.0 / 3600.0))
        connector = FakeConnector(tmp_path, backend=backend, frame_size=(STRIP.width, STRIP.height), limit_frame=STRIP)
        monitor = PointingMonitor(connector)
        monitor.start_session()

        sample = monitor.sample_once()

        assert sample is not None and sample.outcome is PointingSampleOutcome.Solved
        assert sample.d_dec_arcsec == pytest.approx(3.0, abs=1e-6)
        assert sample.frame_crop == (STRIP.x, STRIP.y, STRIP.width, STRIP.height)
        (call,) = backend.calls
        assert call["frame_origin"] == (STRIP.x, STRIP.y)

    def test_a_full_frame_passes_no_origin(self, tmp_path, journal_dir):
        backend = FakeBackend(solved())
        monitor = PointingMonitor(FakeConnector(tmp_path, backend=backend))
        monitor.start_session()

        sample = monitor.sample_once()

        assert sample is not None and sample.frame_crop is None
        (call,) = backend.calls
        assert "frame_origin" not in call


class TestUnavailable:
    @pytest.mark.parametrize(
        ("frame_size", "limit_frame", "profile_binning"),
        [
            ((7760, 4812), None, 1),  # cropped, but PHD2 says it holds no limit frame
            ((7760, 4812), STRIP, 1),  # not the limit frame PHD2 holds
            ((ASI_294MM_WIDTH // 2, ASI_294MM_HEIGHT // 2), None, 2),  # a binned full frame
            ((STRIP.width // 2, STRIP.height // 2), STRIP, 2),  # a binned strip
        ],
    )
    def test_a_frame_that_cannot_be_placed_on_the_sensor_is_never_solved(
        self, tmp_path, journal_dir, frame_size, limit_frame, profile_binning
    ):
        backend = FakeBackend(solved())
        connector = FakeConnector(
            tmp_path, backend=backend, frame_size=frame_size, limit_frame=limit_frame, profile_binning=profile_binning
        )
        monitor = PointingMonitor(connector)
        monitor.start_session()

        sample = monitor.sample_once()

        assert sample is not None and sample.outcome is PointingSampleOutcome.Unavailable
        assert f"{frame_size[0]}x{frame_size[1]}" in (sample.reason or "")
        assert backend.calls == []
        assert not os.path.exists(connector.saved[0])

    def test_no_target_on_record_takes_no_frame(self, tmp_path, journal_dir):
        connector = FakeConnector(tmp_path, backend=FakeBackend(solved()), acquisition=False)
        monitor = PointingMonitor(connector)
        monitor.start_session()

        sample = monitor.sample_once()

        assert sample is not None and sample.outcome is PointingSampleOutcome.Unavailable
        assert connector.saved == []

    @pytest.mark.parametrize("app_state", ["Paused", "LostLock", "Stopped"])
    def test_nothing_is_sampled_unless_phd2_is_guiding(self, tmp_path, journal_dir, app_state):
        """A paused loop hands back its last frame, from before the mirror moved."""
        connector = FakeConnector(tmp_path, backend=FakeBackend(solved()))
        connector.app_state = app_state
        monitor = PointingMonitor(connector)
        monitor.start_session()

        assert monitor.sample_once() is None
        assert connector.saved == []


class TestTheRecord:
    def test_every_sample_reaches_the_journal(self, tmp_path, journal_dir):
        monitor = PointingMonitor(FakeConnector(tmp_path, backend=FakeBackend(solved())))
        monitor.start_session()
        monitor.sample_once()
        monitor.sample_once()

        lines = (journal_dir / "pointing-check.jsonl").read_text().splitlines()
        assert [json.loads(line)["outcome"] for line in lines] == ["Solved", "Solved"]
        assert monitor.status().journal == str(journal_dir / "pointing-check.jsonl")

    def test_a_new_session_starts_a_new_record(self, tmp_path, journal_dir):
        monitor = PointingMonitor(FakeConnector(tmp_path, backend=FakeBackend(solved())))
        monitor.start_session()
        monitor.sample_once()
        monitor.start_session()

        status = monitor.status()
        assert (status.solved, status.recent) == (0, [])


class TestTheCadence:
    def test_samples_arrive_while_the_session_runs(self, tmp_path, journal_dir):
        monitor = PointingMonitor(FakeConnector(tmp_path, backend=FakeBackend(solved()), interval=FAST_INTERVAL_SECONDS))
        monitor.start_session()
        try:
            deadline = time.monotonic() + WAIT_FOR_TICKS_SECONDS
            while monitor.status().solved < 2 and time.monotonic() < deadline:
                time.sleep(FAST_INTERVAL_SECONDS)
        finally:
            monitor.stop_session()
        assert monitor.status().solved >= 2

    def test_a_zero_interval_takes_nothing_until_it_is_set(self, tmp_path, journal_dir):
        """Read live: switching it on mid-session needs no restart."""
        connector = FakeConnector(tmp_path, backend=FakeBackend(solved()), interval=0)
        monitor = PointingMonitor(connector)
        monitor.start_session()
        try:
            time.sleep(FAST_INTERVAL_SECONDS * 4)
            assert connector.saved == []

            connector.parent.unit.unit_conf.phd2.validation_interval = FAST_INTERVAL_SECONDS
            deadline = time.monotonic() + WAIT_FOR_TICKS_SECONDS
            while not connector.saved and time.monotonic() < deadline:
                time.sleep(FAST_INTERVAL_SECONDS)
        finally:
            monitor.stop_session()
        assert connector.saved

    def test_stopping_ends_the_thread(self, tmp_path, journal_dir):
        monitor = PointingMonitor(FakeConnector(tmp_path, backend=FakeBackend(solved()), interval=FAST_INTERVAL_SECONDS))
        monitor.start_session()
        monitor.stop_session()
        deadline = time.monotonic() + WAIT_FOR_TICKS_SECONDS
        while any(t.name == pointing_check.THREAD_NAME for t in threading.enumerate()):
            assert time.monotonic() < deadline, "the sampling thread outlived its session"
            time.sleep(FAST_INTERVAL_SECONDS)

    def test_a_sample_finishing_after_its_session_is_not_recorded(self, tmp_path, journal_dir):
        """A solve in flight when guiding restarts must not land in the new session's record."""
        monitor = PointingMonitor(FakeConnector(tmp_path, backend=FakeBackend(solved())))
        monitor.start_session()
        stale = monitor._session
        monitor.start_session()
        monitor._sample(stale)
        assert monitor.status().solved == 0


class TestTheConnector:
    """The session follows PHD2's own guiding events, and the status carries it."""

    class Recorder:
        def __init__(self):
            self.events: list[str] = []

        def start_session(self):
            self.events.append("start")

        def stop_session(self):
            self.events.append("stop")

        def status(self):
            return PointingCheckStatus(solved=3)

    def connector(self):
        from phd2.phd2 import PHD2Accumulator, PHD2Connector
        from science.lock_validity import LockValidityMonitor

        c = object.__new__(PHD2Connector)
        c.pointing_monitor = self.Recorder()
        c.lock = threading.Lock()
        c.lock_validity = LockValidityMonitor()
        c._launch_handover = lambda: False
        c.start_activity = lambda *a, **kw: None
        c.end_activity = lambda *a, **kw: None
        c._read_failures = FailureStreaks()
        c.ra_accumulator = PHD2Accumulator()
        c.dec_accumulator = PHD2Accumulator()
        c.parent = SimpleNamespace(unit=SimpleNamespace(unit_conf=SimpleNamespace(phd2=SimpleNamespace(profile="test"))))
        return c

    def test_starting_to_guide_starts_a_session(self):
        c = self.connector()
        c._handle_event({"Event": "StartGuiding"})
        assert c.pointing_monitor.events == ["start"]

    def test_guiding_stopped_ends_it(self):
        c = self.connector()
        c._handle_event({"Event": "GuidingStopped"})
        assert c.pointing_monitor.events == ["stop"]

    def test_looping_stopped_does_not(self):
        """Looping is not a guide session, so it has none to end."""
        c = self.connector()
        c._handle_event({"Event": "LoopingExposuresStopped"})
        assert c.pointing_monitor.events == []

    def test_the_guider_status_carries_the_record(self):
        c = self.connector()
        c.sky_quality = SimpleNamespace(state=SimpleNamespace(), latest_update=None)
        c.get_limit_frame = c.get_exclude_region = c.get_lock_position = lambda: None
        c.get_status = lambda: ("Guiding", 0.0)
        c.is_settling = lambda: False
        c.app_state = "Guiding"
        c.avg_dist = 0.0
        c._connected = True

        status = c.guider_status()

        assert status.pointing_check is not None and status.pointing_check.solved == 3
