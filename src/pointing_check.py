"""Plate solve the guide frame while guiding continues, as an independent pointing record.

PHD2 measures the guide star against the lock position. Nothing it reports can say
whether the lock position is still on the target: a lock on the wrong star, drift
between the guide camera and the fiber, or a handover that moved the field all read
as clean guiding. This module solves the frame PHD2 is already taking -- no pause,
no separate exposure -- and records how far the target sits from the fiber.

It is a monitor, not a corrector. Nothing here moves the mount or the lock.

**It also judges an applied lock nudge.** PHD2 cannot say whether a nudge put the target
on the fiber: with multi-star refinement on, its loop can hold the field off the moved
lock while every offset it reports reads zero (mast01, 4 November). The first
`verify_samples` solves taken after the nudge, in the mirror state it ran in, give its
sky verdict (MAST_unit#401).

**The frame is placed on the sensor before it is solved.** While a guide session
exists the only frame there is, is the guide frame, and the solver's SPEC reference
pixel is in full-sensor coordinates. A limit-frame crop moves that pixel by the crop
origin and the solve still succeeds (#234), so a cropped frame is solved with the
origin of the limit frame PHD2 holds; a frame that is neither the full sensor nor
that limit frame, unbinned, is reported unavailable rather than solved. With a crop
the fiber may lie outside the frame, and its position is extrapolated from the
frame's solution.

See MAST_unit#283.
"""

from __future__ import annotations

import datetime
import os
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from astropy.coordinates import Angle
from astropy.io import fits

from common.asi import ASI_294MM_HEIGHT, ASI_294MM_WIDTH
from common.filer import Filer, FilingArea
from common.interfaces.solving import offset_on_sky_arcsec, target_offset_arcsec
from common.mast_logging import get_logger
from common.models.latest import LockNudgeOutcome, NudgeSkyVerdict
from common.models.statuses import ImagerRoi, PointingCheckStatus, PointingSample, PointingSampleOutcome
from common.utils import Coord, function_name, isoformat_zulu
from stage import StagePresetPosition

logger = get_logger(__name__)

THREAD_NAME = "pointing-check"
JOURNAL_NAME = "pointing-check.jsonl"
JOURNAL_SUBFOLDER = "guiding"
#: Beside the journal: solve-field's records of each guide-frame solve (MastrometryDotNet's
#: meta-files and result text). The frames themselves are not kept.
SOLVES_SUBFOLDER = "solves"

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

#: Unusable samples in a row (not solved, or not placeable) a watched nudge tolerates
#: beyond the `verify_samples` it needs, before it is left Unverified.
VERDICT_GRACE_SAMPLES = 2


@dataclass
class NudgeWatch:
    """An applied nudge waiting for its sky verdict, in the session it was applied in."""

    session: int
    outcome: LockNudgeOutcome
    #: Samples begun before this cannot count: one started between the lock move and its
    #: confirmation would judge the old pointing.
    since: datetime.datetime
    mirror_in: bool | None
    verify_samples: int
    verify_arcsec: float
    offsets: list[float] = field(default_factory=list)
    unusable: int = 0


def guide_step_context(before, after) -> dict:
    """The offsets of the GuideStep a saved frame belongs to, for its sample. `before` and
    `after` are the connector's latest step either side of the save: a newer step arriving
    during it means the frame may be the newer one."""
    if before.count == 0:
        return {}
    context: dict = {"guide_offset_px": (before.dx, before.dy), "guide_step_paired": before.count == after.count}
    if before.primary_dx is not None and before.primary_dy is not None:
        context["primary_offset_px"] = (before.primary_dx, before.primary_dy)
    return context


@dataclass(frozen=True)
class GuideFramePlacement:
    """Where a guide frame PHD2 saved sits on the sensor: `crop` is the limit frame it was
    cut from, or None for the whole sensor."""

    crop: ImagerRoi | None
    #: Sensor columns to leave out of the solve: the stars beside the mirror's shadow band.
    mask: tuple[int, int] | None = None

    def solve_kwargs(self) -> dict[str, tuple[int, int]]:
        """What the solver needs to place its sensor-pixel ROI on this frame, and what to leave out."""
        kwargs = {} if self.crop is None else {"frame_origin": (self.crop.x, self.crop.y)}
        if self.mask is not None:
            kwargs["exclude_columns"] = self.mask
        return kwargs


def place_guide_frame(connector, width: int, height: int) -> GuideFramePlacement | None:
    """Where a `width` x `height` guide frame sits on the sensor, or None if it cannot be told.

    PHD2 delivers exactly the limit frame it holds, starting at its origin, so a frame
    of that size, unbinned, sits there on the sensor.
    """
    mask = connector.conf.guide_solve_mask.columns
    if (width, height) == (ASI_294MM_WIDTH, ASI_294MM_HEIGHT):
        return GuideFramePlacement(crop=None, mask=mask)
    if connector.profile_binning != 1:
        return None
    limit = connector.get_limit_frame()
    if limit is None or (limit.width, limit.height) != (width, height):
        return None
    return GuideFramePlacement(crop=limit, mask=mask)


def guiding_folder(acquisition_folder: str) -> Path | None:
    """Where an acquisition's guiding records go: its folder on the share, `guiding/`.

    Not the folder itself: that is on the RAM disk and is released when the
    acquisition thread ends, which is before guiding has even settled.
    """
    try:
        shared = Filer().to_top(FilingArea.SHARE, acquisition_folder)
    except ValueError:
        return None
    return Path(shared) / JOURNAL_SUBFOLDER


def unplaced_frame_reason(width: int, height: int) -> str:
    return (
        f"guide frame is {width}x{height}: neither the {ASI_294MM_WIDTH}x{ASI_294MM_HEIGHT} "
        f"sensor nor the limit frame PHD2 holds, unbinned"
    )


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
        self._watch: NudgeWatch | None = None

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

        A nudge still waiting for its verdict ends Unverified: no later solve belongs to it.
        """
        self._stop.set()
        with self._lock:
            self._end_watch(NudgeSkyVerdict.Unverified, "the guide session ended")

    def watch_nudge(self, outcome: LockNudgeOutcome, *, verify_samples: int, verify_arcsec: float) -> None:
        """Give `outcome`, an applied nudge just confirmed in this session, its sky verdict from
        the next `verify_samples` solves: Confirmed when their median offset is within
        `verify_arcsec`. `outcome` is the record the status publishes, updated in place."""
        since = datetime.datetime.now(datetime.UTC)
        mirror_in = self._mirror_in()
        with self._lock:
            self._end_watch(NudgeSkyVerdict.Unverified, "a newer nudge was applied")
            if self._connector.conf.validation_interval <= 0:
                outcome.sky_verdict = NudgeSkyVerdict.Unverified
                logger.warning(f"{function_name()}: the pointing check is off; the nudge stays Unverified")
                return
            outcome.sky_verdict = NudgeSkyVerdict.Pending
            self._watch = NudgeWatch(
                session=self._session,
                outcome=outcome,
                since=since,
                mirror_in=mirror_in,
                verify_samples=verify_samples,
                verify_arcsec=verify_arcsec,
            )

    def _end_watch(self, verdict: NudgeSkyVerdict, why: str) -> None:
        """Under `_lock`."""
        watch, self._watch = self._watch, None
        if watch is None:
            return
        watch.outcome.sky_verdict = verdict
        if verdict is not NudgeSkyVerdict.Unverified:
            watch.outcome.sky_offset_arcsec = statistics.median(watch.offsets)
        logger.info(
            f"{function_name()}: lock nudge sky verdict {verdict} ({why}; solves {watch.offsets}, "
            f'median {watch.outcome.sky_offset_arcsec}", within {watch.verify_arcsec}")'
        )

    def _advance_watch(self, session: int, sample: PointingSample) -> None:
        """Under `_lock`: let one recorded sample count toward the watched nudge's verdict."""
        watch = self._watch
        if watch is None or watch.session != session:
            return
        if datetime.datetime.fromisoformat(sample.time) <= watch.since or sample.mirror_in != watch.mirror_in:
            return
        if sample.outcome is PointingSampleOutcome.Solved and sample.offset_arcsec is not None:
            watch.offsets.append(sample.offset_arcsec)
            watch.unusable = 0
            if len(watch.offsets) >= watch.verify_samples:
                within = statistics.median(watch.offsets) <= watch.verify_arcsec
                self._end_watch(NudgeSkyVerdict.Confirmed if within else NudgeSkyVerdict.Unconfirmed, "solved")
            return
        watch.unusable += 1
        if watch.unusable >= watch.verify_samples + VERDICT_GRACE_SAMPLES:
            self._end_watch(NudgeSkyVerdict.Unverified, f"{watch.unusable} unusable samples in a row")

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
            "lock_validity": str(connector.lock_validity.state.validity),
        }

        # save_image() hands the caller a file it owns: 94 MB at full size, so it is
        # never kept.
        step = connector.latest_guide_step
        image_path = connector.save_image()
        context |= guide_step_context(step, connector.latest_guide_step)
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
        placement = place_guide_frame(self._connector, width, height)
        if placement is None:
            return PointingSample(
                outcome=PointingSampleOutcome.Unavailable, reason=unplaced_frame_reason(width, height), **context
            )
        if (crop := placement.crop) is not None:
            context = {**context, "frame_crop": (crop.x, crop.y, crop.width, crop.height)}

        started = time.monotonic()
        try:
            result = self._connector.parent.unit.solver._backend.solve(
                unit=self._connector.parent.unit,
                phase=SOLVING_PHASE,
                full_frame_input_image_path=image_path,
                keep_artifacts=False,
                metafiles_folder=None if self._journal is None else self._journal.parent / SOLVES_SUBFOLDER,
                target=target,
                **placement.solve_kwargs(),
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
            self._advance_watch(session, sample)
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
        return guiding_folder(acquisition_folder)
