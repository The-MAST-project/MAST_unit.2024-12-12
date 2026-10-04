"""Plate solve the guide frame while guiding continues, as an independent pointing record.

PHD2 measures the guide star against the lock position. Nothing it reports can say
whether the lock position is still on the target: a lock on the wrong star, drift
between the guide camera and the fiber, or a handover that moved the field all read
as clean guiding. This module solves the frame PHD2 is already taking -- no pause,
no separate exposure -- and records how far the target sits from the fiber.

It is a monitor, not a corrector. Nothing here moves the mount or the lock.

**The frame must be the full sensor.** While a guide session exists the only frame
there is, is the guide frame, and the solver's SPEC reference pixel is in
full-sensor coordinates. A limit-frame crop moves that pixel by the crop origin and
the solve still succeeds (#234), so a frame of any other size is reported
unavailable rather than solved. Running the check means guiding with
``phd2.limit_frame.mode: full_frame``, the fold mirror being kept out of star
selection by the exclusion region instead.

See MAST_unit#283.
"""

from __future__ import annotations

import datetime
import os
import threading
import time
from collections import deque
from pathlib import Path

from astropy.coordinates import Angle
from astropy.io import fits

from common.asi import ASI_294MM_HEIGHT, ASI_294MM_WIDTH
from common.filer import Filer, FilerTop
from common.interfaces.solving import offset_on_sky_arcsec, target_offset_arcsec
from common.mast_logging import get_logger
from common.models.statuses import PointingCheckStatus, PointingSample, PointingSampleOutcome
from common.utils import Coord, function_name, isoformat_zulu
from stage import StagePresetPosition

logger = get_logger(__name__)

THREAD_NAME = "pointing-check"
JOURNAL_NAME = "pointing-check.jsonl"
JOURNAL_SUBFOLDER = "guiding"

#: Samples kept for the status; the journal holds the whole session.
RECENT_SAMPLES = 20

#: How often a zero `validation_interval` is re-read, so switching the check on
#: mid-session needs no restart.
IDLE_POLL_SECONDS = 10.0

#: PHD2's app state while the loop is closed. A paused loop hands back its last
#: frame, which after a handover predates the mirror moving.
GUIDING_STATE = "Guiding"

#: The SPEC phase puts the solved position at the fiber, which is what the target
#: has to stay on.
SOLVING_PHASE = "spec"


class PointingMonitor:
    """One guide session's pointing record, sampled on its own thread."""

    def __init__(self, connector):
        self._connector = connector
        self._lock = threading.Lock()
        self._session = 0
        self._stop = threading.Event()
        self._status = PointingCheckStatus()
        self._recent: deque[PointingSample] = deque(maxlen=RECENT_SAMPLES)
        self._journal: Path | None = None

    def start_session(self) -> None:
        """Begin a new record and its sampling thread, ending any previous one."""
        self.stop_session()

        acquisition = self._acquisition()
        folder = None if acquisition is None or not acquisition.folder else self._journal_folder(acquisition.folder)
        journal = None if folder is None else folder / JOURNAL_NAME

        stop = threading.Event()
        with self._lock:
            self._session += 1
            session = self._session
            self._stop = stop
            self._recent.clear()
            self._journal = journal
            self._status = PointingCheckStatus(
                session_started=isoformat_zulu(datetime.datetime.now(datetime.UTC)),
                journal=None if journal is None else str(journal),
            )

        threading.Thread(name=THREAD_NAME, target=self.do_run, args=(stop, session), daemon=True).start()

    def stop_session(self) -> None:
        """Signal the sampling thread to end.

        Not joined: this is called from PHD2's event reader, and a solve in flight
        can take tens of seconds. A sample that finishes after its session ended is
        dropped by `_record`, so it cannot leak into the next session's record.
        """
        self._stop.set()

    def status(self) -> PointingCheckStatus:
        with self._lock:
            return self._status.model_copy(update={"recent": list(self._recent)})

    def sample_once(self) -> PointingSample | None:
        """Take and judge one frame now, for the current session."""
        with self._lock:
            session = self._session
        return self._sample(session)

    def do_run(self, stop: threading.Event, session: int) -> None:
        while not stop.is_set():
            interval = self._connector.conf.validation_interval
            with self._lock:
                if session == self._session:
                    self._status.interval_seconds = interval
            if interval <= 0:
                stop.wait(IDLE_POLL_SECONDS)
                continue

            started = time.monotonic()
            try:
                self._sample(session)
            except Exception:
                # The thread outlives a bad tick: one unreadable frame or a dropped PHD2
                # call should cost one sample, not the rest of the session's record.
                logger.exception(f"{function_name()}: pointing sample failed")
            stop.wait(max(0.0, interval - (time.monotonic() - started)))

    def _sample(self, session: int) -> PointingSample | None:
        connector = self._connector
        if connector.app_state != GUIDING_STATE:
            return None

        now = isoformat_zulu(datetime.datetime.now(datetime.UTC))
        acquisition = self._acquisition()
        if acquisition is None:
            return self._record(
                session,
                PointingSample(
                    time=now, outcome=PointingSampleOutcome.Unavailable, reason="no acquisition target on record"
                ),
            )

        target = Coord(ra=Angle(acquisition.target_ra, unit="hour"), dec=Angle(acquisition.target_dec, unit="deg"))
        context = {
            "time": now,
            "mirror_in": self._mirror_in(),
            "lock_position": connector.get_lock_position(),
            "avg_dist": connector.avg_dist,
            "lock_validity": str(connector.lock_supervisor.state.validity),
        }

        # save_image() hands the caller a file it owns: 94 MB at full size, so it is
        # never kept.
        image_path = connector.save_image()
        try:
            sample = self._judge(image_path, target, context)
        finally:
            try:
                os.remove(image_path)
            except OSError as ex:
                logger.error(f"{function_name()}: could not remove {image_path}: {ex!r}")
        return self._record(session, sample)

    def _judge(self, image_path: str, target: Coord, context: dict) -> PointingSample:
        header = fits.getheader(image_path)
        width, height = header["NAXIS1"], header["NAXIS2"]
        if (width, height) != (ASI_294MM_WIDTH, ASI_294MM_HEIGHT):
            return PointingSample(
                outcome=PointingSampleOutcome.Unavailable,
                reason=(
                    f"guide frame is {width}x{height}, not the {ASI_294MM_WIDTH}x{ASI_294MM_HEIGHT} sensor: "
                    f"a limit frame or binning is in force"
                ),
                **context,
            )

        started = time.monotonic()
        try:
            result = self._connector.parent.unit.solver._backend.solve(
                unit=self._connector.parent.unit,
                phase=SOLVING_PHASE,
                full_frame_input_image_path=image_path,
                target=target,
            )
        except Exception as ex:
            logger.error(f"{function_name()}: could not solve the guide frame: {ex!r}")
            return PointingSample(
                outcome=PointingSampleOutcome.NotSolved,
                reason=f"solve raised: {ex!r}",
                solve_seconds=time.monotonic() - started,
                **context,
            )
        solve_seconds = time.monotonic() - started

        if not result or not result.succeeded or result.solution is None:
            return PointingSample(
                outcome=PointingSampleOutcome.NotSolved,
                reason="; ".join(result.errors) if result and result.errors else "no match",
                solve_seconds=solve_seconds,
                **context,
            )

        solution = result.solution
        d_ra_arcsec, d_dec_arcsec = target_offset_arcsec(target, solution)
        return PointingSample(
            outcome=PointingSampleOutcome.Solved,
            ra_hours=solution.ra_hours,
            dec_degs=solution.dec_degs,
            d_ra_arcsec=d_ra_arcsec,
            d_dec_arcsec=d_dec_arcsec,
            offset_arcsec=offset_on_sky_arcsec(d_ra_arcsec, d_dec_arcsec, solution.dec_degs),
            matched_stars=solution.matched_stars,
            solve_seconds=solve_seconds,
            **context,
        )

    def _record(self, session: int, sample: PointingSample) -> PointingSample:
        with self._lock:
            if session != self._session:
                return sample
            self._recent.append(sample)
            match sample.outcome:
                case PointingSampleOutcome.Solved:
                    self._status.solved += 1
                case PointingSampleOutcome.NotSolved:
                    self._status.not_solved += 1
                case PointingSampleOutcome.Unavailable:
                    self._status.unavailable += 1
            journal = self._journal

        logger.info(
            f"{function_name()}: {sample.outcome} offset={sample.offset_arcsec} arcsec "
            f"mirror_in={sample.mirror_in} reason={sample.reason}"
        )
        if journal is not None:
            try:
                journal.parent.mkdir(parents=True, exist_ok=True)
                with journal.open("a") as fp:
                    fp.write(sample.model_dump_json() + "\n")
            except OSError as ex:
                logger.error(f"{function_name()}: could not append to {journal}: {ex!r}")
        return sample

    def _acquisition(self):
        acquirer = self._connector.parent.unit.acquirer
        return None if acquirer is None else acquirer.latest_acquisition

    def _mirror_in(self) -> bool | None:
        stage = self._connector.parent.unit.stage
        return None if stage is None else stage.at_preset(StagePresetPosition.Spec)

    def _journal_folder(self, acquisition_folder: str) -> Path | None:
        """The acquisition's folder on the share.

        Not the folder itself: that is on the RAM disk and is released when the
        acquisition thread ends, which is before guiding has even settled.
        `change_top_to` compares against POSIX-spelled roots, hence `as_posix`.
        """
        shared = Filer().change_top_to(FilerTop.Shared, Path(acquisition_folder).as_posix())
        return None if shared is None else Path(shared) / JOURNAL_SUBFOLDER
