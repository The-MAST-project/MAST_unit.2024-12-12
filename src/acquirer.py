import os
import time
from threading import Thread
from typing import Annotated

import astropy.units as u
from astropy.coordinates import Angle, Latitude, Longitude
from fastapi import Query

from acquisition import Acquisition, ApproachMode
from common import asi
from common.activities import UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config.rois import FcuVersion, SkyRoiConfig
from common.endpoints import Tier, endpoint
from common.filer import MoveGuardian
from common.mast_logging import get_logger
from common.models.latest import AcquisitionState, LatestAcquisition, LatestAcquisitionPhase
from common.models.plans import PlanStage
from common.models.statuses import ImagerSettings
from common.object_resolver import TOTAL_TIMEOUT_SECONDS, ObjectNameError

# RA_PATTERN/DEC_PATTERN are deliberately no longer applied to this endpoint's coordinate
# parameters. A `pattern=` is enforced by FastAPI BEFORE the handler runs, so a malformed
# coordinate would 422 out and never reach the fallback -- which makes "if either is
# missing or unusable, resolve `object_name`" unimplementable for half the ways a
# coordinate can be unusable. Validation is `target_coordinates`', where both classes of
# bad input (malformed and out-of-range) are visible and can fall through together.
from common.parsers import target_coordinates
from common.rois import SkyRoi
from common.utils import Coord, boxed_log, function_name
from mount import SETTLE_START_GRACE_SECONDS, SLEW_SETTLE_TOLERANCE_ARCSEC, SettleMode
from phd2.phd2 import PHD2Connector
from plan_products import announce_plan_products
from solving import SolverId, SolvingTolerance
from stage import StagePresetPosition

logger = get_logger(__name__)


#: The `Unit` attributes an acquisition dereferences. Each is `None` when its component
#: failed to build, which is a routine state -- a PHD2 connect failure leaves `imager` and
#: `guider` both unbuilt (MAST_unit#84), and a missing ASCOM cover driver leaves `covers`
#: unbuilt on three of four units (MAST_unit#95).
REQUIRED_COMPONENTS = ("mount", "guider", "imager", "solver", "stage")

ACQUISITION_CANCELLED = "the acquisition was cancelled"

#: The handover's failure when guiding stops before PHD2 ever locked on a star.
GUIDING_NEVER_LOCKED = "guiding stopped before locking on a star"


def _phases_of(acquisition) -> dict[str, LatestAcquisitionPhase]:
    """Each phase's corrections, summarised: steps applied, and the last offset against tolerance."""
    phases: dict[str, LatestAcquisitionPhase] = {}
    for phase, corrections in (getattr(acquisition, "corrections", None) or {}).items():
        last = corrections.last_delta or (corrections.sequence[-1] if corrections.sequence else None)
        phases[phase] = LatestAcquisitionPhase(
            steps=len(corrections.sequence),
            tolerance_ra=corrections.tolerance_ra,
            tolerance_dec=corrections.tolerance_dec,
            last_ra_delta=last.ra_delta if last is not None else None,
            last_dec_delta=last.dec_delta if last is not None else None,
        )
    return phases


def _settle(latest: LatestAcquisition | None, acquisition, failure: str | None) -> None:
    """Give the record its final state, from how `do_acquire` returned, and write it out.

    A failure string is `FAILED` (or `ABORTED`, for a cancellation); no failure after a
    lock is `ENDED`, which the handover has already reached. No failure and no lock is the
    acquisition-tuning path (no handover), which also ends here: `ENDED`, nothing wrong.
    Succeeded: it locked, or (tuning) it ended without a failure.
    """
    if latest is None:
        return
    latest.phases = _phases_of(acquisition)
    if failure is None:
        latest.state = AcquisitionState.ENDED
        latest.finish(True)
    elif failure == ACQUISITION_CANCELLED:
        latest.state = AcquisitionState.ABORTED
        latest.finish(False, [failure])
    else:
        latest.state = AcquisitionState.FAILED
        latest.finish(False, [failure])
    latest.write()  # latest.json in the acquisition's folder


class Acquirer:
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from unit import Unit

    def __init__(self, unit: "Unit") -> None:
        """Initialize the Acquirer with a Unit instance.

        Args:
            unit: The Unit instance that owns this Acquirer.
        """
        self.unit = unit
        self.folder: str | None = None
        self.latest_acquisition: Acquisition | None = None

    #: The latest acquisition's record, published as `FullUnitStatus.latest.acquisition`. None
    #: until the first one starts. A class attribute so an Acquirer built without `__init__`
    #: (the tests) has it too; `start` replaces it on the instance, never mutates this one.
    latest: LatestAcquisition | None = None

    def missing_components(self) -> list[str]:
        """The `REQUIRED_COMPONENTS` this unit did not build, in declaration order."""
        return [name for name in REQUIRED_COMPONENTS if getattr(self.unit, name) is None]

    def start(self, acquisition: Acquisition, plan_id: str | None = None) -> None:
        """Publish a fresh `RUNNING` record, then run `acquisition` on its own thread.

        The record is replaced *before* this returns, so before the endpoint answers: a
        caller's first status read after a successful PUT describes this acquisition, never
        the previous one. That ordering is what lets it carry no id
        (mast-claude-config plans/control_acquisition_design.md, 4.1).
        """
        latest = LatestAcquisition(
            target_ra_hours=getattr(acquisition, "target_ra", None),
            target_dec_degrees=getattr(acquisition, "target_dec", None),
            handed_over=bool(getattr(acquisition, "handover_automatically_to_guider", False)),
            solver=str(getattr(acquisition, "solver_id", None) or "") or None,
        ).begin(getattr(acquisition, "folder", None), plan_id)
        acquisition.latest = latest
        self.latest = latest
        Thread(name="acquisition", target=self.run_acquisition, args=[acquisition]).start()

    def run_acquisition(self, acquisition: Acquisition):
        """Thread entry point for an acquisition: run it, then always release its folder.

        do_acquire() returns early in several places -- mount not connected, a phase that
        never reached tolerance -- and can raise. None of those paths reach post_process(),
        so releasing the ram-disk folder there covered only the acquisitions that succeeded,
        and precisely the ones that failed (partially written, most in need of clearing)
        were the ones left behind.

        The except clause matters as much as the finally: this runs on a thread, so an
        escaping exception would otherwise go to threading's excepthook, i.e. to a stderr
        file nobody reads -- the same way a TypeError in the mastrometry solver's cleanup
        stayed invisible for months.
        """
        try:
            acquisition.failure = self.do_acquire(acquisition)
        except Exception as ex:
            logger.exception(f"{function_name()}: acquisition failed")
            acquisition.failure = f"the acquisition raised: {ex!r}"
        finally:
            # Before the folder release, which can wait on artifacts still moving: a caller
            # polling the outcome should learn how this ended as soon as it has.
            _settle(getattr(acquisition, "latest", None), acquisition, acquisition.failure)
            # Removed once every protected artifact has reached the shared area, and never
            # before -- so a frame that failed to move keeps its folder instead of being
            # deleted with it.
            MoveGuardian().release_folder(acquisition.folder, logger=logger)

    def _cancelled(self) -> bool:
        """True once `Acquiring` has been cleared -- the protocol's cancel signal.

        Clearing the flag is how everything here is stopped, and `solve_and_correct`
        already polls it through `parent_activity`, so a cancellation landing during a
        SOLVE was always honoured. What it was not honoured during is everything between
        the solves: the stage moves, the `is_moving` spins and the flat five-second
        settles. That is most of an acquisition's wall-clock time, and the likeliest
        moment for an operator to give up on one.
        """
        return not self.unit.is_active(UnitActivities.Acquiring)

    def _cancellable_sleep(self, seconds: float, poll: float = 0.2) -> bool:
        """Sleep, returning False the moment the acquisition is cancelled."""
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return not self._cancelled()
            if self._cancelled():
                return False
            time.sleep(min(poll, remaining))

    def _await_stage(self) -> bool:
        """Wait out a stage move, returning False if cancelled while it runs."""
        while self.unit.required_stage.is_moving:
            if self._cancelled():
                return False
            time.sleep(0.2)
        return True

    def _await_slew(self, slew_commanded_at: float | None) -> bool:
        """Wait out the slew commanded at `slew_commanded_at` (monotonic; None if there was
        none), returning False if cancelled while it ran.

        Only the part of the start grace not yet spent since the goto is passed on: the
        grace guards a stale `is_slewing` read just after the goto, and by the time the
        stage has been waited on it has usually run out.

        A settle timeout is logged by the mount and does not fail the acquisition: the
        correction loop recovers from a frame taken slightly off.
        """
        if slew_commanded_at is not None:
            elapsed = time.monotonic() - slew_commanded_at
            self.unit.required_mount.wait_until_settled(
                SettleMode.SLEW,
                dist_tolerance_arcsec=SLEW_SETTLE_TOLERANCE_ARCSEC,
                start_grace_seconds=max(0.0, SETTLE_START_GRACE_SECONDS - elapsed),
            )
        return not self._cancelled()

    def _abandon(self, op: str, acquisition_exposure_series=None) -> None:
        """Unwind exactly as a failed phase does.

        A cancelled acquisition must leave the unit in the same state as one that could
        not reach tolerance -- tracking stopped, the exposure series closed, and no
        activity flag left set. `Positioning` in particular: `do_acquire` raises it around
        the stage moves, which is precisely where a cancellation now lands.
        """
        logger.info(f"{op}: acquisition cancelled; unwinding")
        self.unit.end_activity(UnitActivities.Acquiring)
        if self.unit.is_active(UnitActivities.Positioning):
            self.unit.end_activity(UnitActivities.Positioning)
        try:
            if self.unit.mount is not None:
                self.unit.required_mount.stop_tracking()
            if acquisition_exposure_series is not None and self.unit.imager is not None:
                self.unit.required_imager.end_exposure_series(acquisition_exposure_series)
        except Exception:  # the unwind must finish even if part of it fails
            logger.exception(f"{op}: error while unwinding a cancelled acquisition")

    def _hand_over_to_guider(self, op: str, outcome: LatestAcquisition | None) -> str | None:
        """Start guiding and stay with it until it stops. None, or why the handover failed.

        `outcome` becomes `GUIDING` the first time the guider is locked on a star -- for
        PHD2, its state being exactly "Guiding", not merely told to guide -- and `ENDED`
        when guiding stops after that.

        Three ways to fail, each unwound as a failed phase is:

        - **the guider refuses** (PHD2 cannot connect, or its profile's binning or bpp do
          not match). It returns errors without raising `Guiding`, and this used to
          ignore them: the wait below fell straight through, the mount stopped tracking,
          and the acquisition reported success.
        - **cancelled before a lock** (`stop_acquisition_and_guiding`, `unit/abort`).
        - **guiding stops before a lock**: `GUIDING_NEVER_LOCKED`.

        A guider that is told to guide and never locks keeps this waiting, as before;
        the caller's timeout is what ends that (control aborts the unit).

        The solving backend (anything not PHD2) has no star lock, and its `start_guiding`
        blocks for the whole session, so for it `GUIDING` is set as guiding starts and the
        call is waited out.
        """
        guider = self.unit.required_guider

        if not isinstance(guider._backend, PHD2Connector):
            if outcome is not None:
                outcome.state = AcquisitionState.GUIDING
                outcome.succeeded = True  # its goal; the run goes on, guiding
            response = guider.start_guiding()  # blocks until guiding stops
            if response.failed:
                self._abandon(op)
                return f"guiding failed: {'; '.join(response.errors or [])}"
        else:
            response = guider.start_guiding()
            if response.failed:
                self._abandon(op)
                return f"handover to guiding failed: {'; '.join(response.errors or [])}"

            locked = False
            while self.unit.is_active(UnitActivities.Guiding):
                if not locked and guider.is_locked:
                    locked = True
                    if outcome is not None:
                        outcome.state = AcquisitionState.GUIDING
                        outcome.succeeded = True  # its goal; the run goes on, guiding
                    logger.info(f"{op}: guiding, locked on a star")
                time.sleep(1)

            if not locked:
                cancelled = self._cancelled()
                self._abandon(op)
                return ACQUISITION_CANCELLED if cancelled else GUIDING_NEVER_LOCKED

        # Guiding was stopped, after a lock
        self.unit.end_activity(UnitActivities.Acquiring)
        self.unit.required_mount.stop_tracking()
        return None

    def _phase_failure(self, phase: str, tries: int) -> str:
        """Why a phase ended without reaching tolerance: `solve_and_correct` returns False
        for a cancellation too, and the two call for different responses."""
        if self._cancelled():
            return f"{ACQUISITION_CANCELLED} during phase '{phase.upper()}'"
        return f"phase '{phase.upper()}' did not reach tolerance in {tries} tries"

    def spec_phase_tolerance(self) -> SolvingTolerance:
        """How close the SPEC phase must put the target: the guiding tolerance."""
        assert self.unit.unit_conf is not None
        tolerance = self.unit.unit_conf.guiding.tolerance
        return SolvingTolerance(
            Angle(tolerance.ra_arcsec * u.arcsecond),  # type: ignore
            Angle(tolerance.dec_arcsec * u.arcsecond),  # type: ignore
        )

    def spec_phase_imager_settings(self, acquisition: Acquisition, exposure: float, phase: str = "spec") -> ImagerSettings:
        """How the SPEC phase exposes.

        The guiding settings, overridden by the acquisition's own; on FCU v2 the full
        ASI frame at bin 1. A method rather than inline in `do_acquire` because
        focus_on_star solves exactly as this phase does -- the solve's reference pixel is
        the fibre, which is where that run wants its star -- and two copies of these
        overrides would drift.
        """
        assert self.unit.unit_conf is not None
        acquisition_conf = self.unit.unit_conf.acquisition

        # make default guiding settings
        spec_imager_settings = self.unit.required_guider.make_guiding_settings(
            base_folder=os.path.join(acquisition.folder, phase)
        )
        # override with acquisition settings
        spec_imager_settings.seconds = exposure
        if acquisition_conf.binning is not None:
            spec_imager_settings.binning = acquisition_conf.binning
        if acquisition_conf.gain is not None:
            spec_imager_settings.gain = acquisition_conf.gain

        spec_imager_settings.use_set_limit_frame = acquisition.use_set_limit_frame

        # override for fcu_v2 to use full frame
        if self.unit.fcu_version == FcuVersion.v2:
            from common.asi import ASI_294MM_HEIGHT, ASI_294MM_WIDTH
            from common.models.statuses import ImagerPixel, ImagerRoi

            # ROI to be used for the exposures
            spec_imager_settings.roi = ImagerRoi(
                x=0,
                y=0,
                # width=self.unit.required_imager.full_frame.width,
                # height=self.unit.required_imager.full_frame.height,
                width=ASI_294MM_WIDTH,
                height=ASI_294MM_HEIGHT,
            )
            # override the roi to be the ASI full frame, we get it from PHD2
            spec_imager_settings.roi.x = 0
            spec_imager_settings.roi.y = 0
            spec_imager_settings.roi.width = ASI_294MM_WIDTH
            spec_imager_settings.roi.height = ASI_294MM_HEIGHT
            spec_imager_settings.roi._center = ImagerPixel(
                x=spec_imager_settings.roi.width // 2, y=spec_imager_settings.roi.height // 2
            )

            # spec_imager_settings.use_set_limit_frame = True
            spec_imager_settings.use_set_limit_frame = False
            spec_imager_settings.binning = 1

        return spec_imager_settings

    def do_acquire(self, acquisition: Acquisition) -> str | None:  # noqa: C901
        """Run one acquisition. Returns None when it reached tolerance, else why it did not."""
        op = function_name()

        self.unit.errors = []
        self.unit.reference_image = None

        # The endpoint refuses before spawning this thread, so reaching here with a missing
        # component means a caller that did not check -- `start_acquisition`, or a future
        # one. Recorded on `unit.errors` because that is what `/unit/status` publishes; an
        # AssertionError here would reach only the log, and only after the caller had been
        # told the acquisition started.
        missing = self.missing_components()
        if missing:
            msg = f"{op}: cannot acquire, these components did not initialize: {', '.join(missing)}"
            logger.error(msg)
            self.unit.errors.append(msg)
            return msg

        self.latest_acquisition = acquisition

        # Bound ONCE, here, and used for the whole acquisition. The configuration is live
        # now, so re-reading it per step could shift values under a run in progress; an
        # operation uses what it started with. Within one configuration generation this is
        # the same object every time, so binding it costs nothing.
        assert self.unit.unit_conf is not None
        acquisition_conf = self.unit.unit_conf.acquisition
        # A per-call override, not a configuration change (MAST_unit#195).
        exposure = acquisition.exposure if acquisition.exposure is not None else acquisition_conf.exposure
        sky_roi_conf = acquisition_conf.rois[self.unit.fcu_version]

        #
        # Figure out the target, it can come from:
        # - the acquisition (target_ra: float, target_dec: float)
        # - if the acquisition doesn't have them, get them from the mount's status
        # - if they were supplied as Longitude and Latitude, transform to float
        # - pass them on to solve_and_correct() as a Coord
        #
        if not hasattr(acquisition, "target_ra") or not hasattr(acquisition, "target_dec"):
            st = self.unit.required_mount.status()
            if st.ra_j2000_hours is None or st.dec_j2000_degs is None:
                msg = "cannot get coordinates from mount (mount not connected)"
                self.unit.errors.append(msg)
                self.unit.end_activity(UnitActivities.Acquiring)
                return msg

            acquisition.target_ra = st.ra_j2000_hours
            acquisition.target_dec = st.dec_j2000_degs

        target_ra_j2000_hours: float = (
            acquisition.target_ra.value if isinstance(acquisition.target_ra, Longitude) else acquisition.target_ra
        )

        target_dec_j2000_degs: float = (
            acquisition.target_dec.value if isinstance(acquisition.target_dec, Latitude) else acquisition.target_dec
        )

        target = Coord(
            ra=Angle(target_ra_j2000_hours, unit="hour"),
            dec=Angle(target_dec_j2000_degs, unit="deg"),
        )

        self.unit.start_activity(UnitActivities.Acquiring)

        if not self.unit.required_imager.connected:
            self.unit.required_imager.connected = True

        tries: int = acquisition_conf.tries

        self.unit.required_mount.start_tracking()
        self.unit.start_activity(UnitActivities.Positioning)
        slew_commanded_at: float | None = None
        if self.latest_acquisition.slew_to_target:
            self.unit.required_mount.goto_ra_dec_j2000(target_ra_j2000_hours, target_dec_j2000_degs)
            slew_commanded_at = time.monotonic()

        acquisition_exposure_series = self.unit.required_imager.start_exposure_series(purpose="acquisition")

        if self.unit.fcu_version == FcuVersion.v1 and not acquisition.skip_sky:
            phase = "sky"
            boxed_log(logger, [f"starting phase '{phase.upper()}'"])

            #
            # move the stage and mount (if needed) into position
            #
            self.unit.required_stage.move_to_preset(StagePresetPosition.Sky)

            if not self._await_stage() or not self._await_slew(slew_commanded_at):
                self._abandon(op)
                return ACQUISITION_CANCELLED

            self.unit.end_activity(UnitActivities.Positioning)

            imager_binning = acquisition_conf.binning

            assert isinstance(sky_roi_conf, SkyRoiConfig)
            sky_roi = SkyRoi(
                sky_x=sky_roi_conf.sky_x, sky_y=sky_roi_conf.sky_y, width=sky_roi_conf.width, height=sky_roi_conf.height
            )

            gain = (
                acquisition.gain_absolute
                if acquisition.gain_absolute is not None
                else asi.gain_percent_to_absolute(acquisition.gain_percent)
                if acquisition.gain_percent is not None
                else None
            )

            from common.models.statuses import ImagerRoi, ImagerSettings

            sky_settings = ImagerSettings(
                seconds=exposure,
                base_folder=os.path.join(self.latest_acquisition.folder, phase),
                gain=gain,
                binning=imager_binning,
                roi=ImagerRoi.from_other(roi=sky_roi),
                save=True,
                use_set_limit_frame=acquisition.use_set_limit_frame,
            )

            #
            # loop trying to solve and correct the mount till within tolerances
            #

            # set up the tolerances
            default_tolerance: Angle = Angle(1 * u.arcsecond)  # type: ignore
            ra_tolerance: Angle = default_tolerance
            dec_tolerance: Angle = default_tolerance
            phase_conf = acquisition_conf
            ra_tolerance = Angle(phase_conf.tolerance.ra_arcsec * u.arcsecond)  # type: ignore
            dec_tolerance = Angle(phase_conf.tolerance.dec_arcsec * u.arcsecond)  # type: ignore

            target = Coord(
                ra=Angle(target_ra_j2000_hours * u.hourangle),  # type: ignore
                dec=Angle(target_dec_j2000_degs * u.deg),  # type: ignore
            )

            achieved_tolerances = self.unit.required_solver.solve_and_correct(
                target=target,
                approach_mode=acquisition.approach_mode,
                solver_id=acquisition.solver_id,
                make_corrections=acquisition.make_corrections,
                imager_settings=sky_settings,
                solving_tolerance=SolvingTolerance(ra_tolerance, dec_tolerance),
                parent_activity=UnitActivities.Acquiring,
                phase="sky",
                max_tries=tries,
            )
            logger.info(f"{op}: phase '{phase.upper()}' {achieved_tolerances=}")
            self.latest_acquisition.save_corrections(phase)

            if not achieved_tolerances:
                failure = self._phase_failure(phase, tries)
                self.unit.end_activity(UnitActivities.Acquiring)
                self.unit.required_mount.stop_tracking()
                self.unit.required_imager.end_exposure_series(acquisition_exposure_series)
                return failure

        phase = "spec"
        boxed_log(logger, [f"starting phase '{phase.upper()}'"])

        match self.unit.fcu_version:
            case FcuVersion.v1:
                self.unit.required_stage.move_to_preset(StagePresetPosition.Spec)
            case FcuVersion.v2:
                self.unit.required_stage.move_to_preset(StagePresetPosition.Sky)

        if not self._await_stage():
            self._abandon(op, acquisition_exposure_series)
            return ACQUISITION_CANCELLED
        logger.info("sleeping additional 5 seconds to let the stage stop moving ...")
        if not self._cancellable_sleep(5):
            self._abandon(op, acquisition_exposure_series)
            return ACQUISITION_CANCELLED
        logger.info(f"stage now at {self.unit.required_stage.position}")
        if not self._await_slew(slew_commanded_at):
            self._abandon(op, acquisition_exposure_series)
            return ACQUISITION_CANCELLED

        if self.unit.is_active(UnitActivities.Positioning):
            self.unit.end_activity(UnitActivities.Positioning)

        spec_imager_settings = self.spec_phase_imager_settings(acquisition, exposure, phase)

        achieved_tolerances = self.unit.required_solver.solve_and_correct(
            target=target,
            approach_mode=acquisition.approach_mode,
            solver_id=acquisition.solver_id,
            make_corrections=acquisition.make_corrections,
            imager_settings=spec_imager_settings,
            solving_tolerance=self.spec_phase_tolerance(),
            parent_activity=UnitActivities.Acquiring,
            phase=phase,
            max_tries=tries,
        )
        self.latest_acquisition.save_corrections(phase)
        logger.info(f"{op}: phase '{phase.upper()}' {achieved_tolerances=}")
        if not achieved_tolerances:
            failure = self._phase_failure(phase, tries)
            self.unit.end_activity(UnitActivities.Acquiring)
            self.unit.required_mount.stop_tracking()
            self.unit.required_imager.end_exposure_series(acquisition_exposure_series)
            return failure

        if self.unit.required_imager.can_image_to_memory:
            self.unit.reference_image = self.unit.required_imager.image_array

        self.unit.required_imager.end_exposure_series(acquisition_exposure_series)

        lines = ["acquisition completed", "telescope is tracking"]
        # Bound once: this block asks for both six times over, and the message below does
        # not fit a line otherwise.
        imager, guider = self.unit.required_imager, self.unit.required_guider
        if (
            (not isinstance(imager._backend, PHD2Connector))
            and isinstance(guider._backend, PHD2Connector)
            and imager.connected
        ):
            lines.append(f"camera disconnected imager={type(imager._backend)}, guider={type(guider._backend)}")
            imager.disconnect()

        # Move the stage to SPEC (FCU v2 was left at Sky by the solve; v1 is already there)
        # and wait for it to settle -- needed by BOTH the auto-handover and the manual
        # acquisition-tuning paths, so guiding always starts with the stage at SPEC.
        if self.unit.fcu_version == FcuVersion.v2:
            self.unit.required_stage.move_to_preset(StagePresetPosition.Spec)
            lines.append("moving stage to SPEC")
        if not self._await_stage():
            self._abandon(op)
            return ACQUISITION_CANCELLED
        logger.info("sleeping additional 5 seconds to let the stage stop moving ...")
        if not self._cancellable_sleep(5):
            self._abandon(op)
            return ACQUISITION_CANCELLED

        if self.latest_acquisition.handover_automatically_to_guider:
            lines.append("starting PHD2 guiding")
            boxed_log(logger, lines)
            failure = self._hand_over_to_guider(op, getattr(self.latest_acquisition, "latest", None))
            if failure is not None:
                return failure
        else:
            lines.append("acquisition tuning: guiding must be started manually via /start_guiding")
            boxed_log(logger, lines)
            # End the acquisition but KEEP the mount tracking: the operator fine-tunes the
            # pointing with external tools, then starts guiding via the /start_guiding endpoint.
            self.unit.end_activity(UnitActivities.Acquiring)

        if self.unit.required_acquirer.latest_acquisition is not None:
            self.unit.required_acquirer.latest_acquisition.post_process()
        return None

    def _target_coordinates(self, op, ra_in, dec_in, object_name, resolve_timeout, pw_status):
        """Where to acquire: (ra_hours, dec_degrees, None), or (None, None, why-not).

        Coordinates first, then `object_name`, by common's `target_coordinates` -- the one
        place that rule lives, shared with the plans control writes
        (plans/control_acquisition_design.md 3.8). Only the last rule is the unit's own:
        with neither, **the telescope's current position**, as before.

        A name that fails to resolve is an ERROR and never falls through to the telescope.
        Quietly acquiring wherever the mount happens to point, having been asked for a
        named object, is the misresolution failure in its purest form -- everything below
        works perfectly and the spectrum is of the wrong sky. Coordinates supplied but
        unusable, with no name, are an error too, said as such: "no coordinates" would read
        as an omission, and send the caller hunting for a missing parameter.
        """
        try:
            ra, dec, resolved = target_coordinates(ra_in, dec_in, object_name, resolve_timeout)
        except ObjectNameError as e:
            return None, None, f"{op}: could not resolve '{object_name}' -- {e}"
        except ValueError as e:
            return None, None, f"{op}: {e}"
        if resolved is not None:
            logger.info(
                f"{op}: '{object_name}' resolved to ra={resolved.ra_j2000_hours:.6f}h "
                f"dec={resolved.dec_j2000_degs:+.6f}d via {resolved.database or resolved.resolver}"
            )
        if ra is not None and dec is not None:
            return ra, dec, None

        if not pw_status.mount.is_connected:
            return None, None, "cannot get coordinates from mount (mount not connected)"

        # Connected is not the same as pointing somewhere. PWI4 reports the axes' position
        # as null before the mount has a fix -- between power-on and find_home, say -- so
        # this is a real state and not paranoia, and it needs saying differently from "not
        # connected" or whoever reads it goes looking at cables.
        ra, dec = pw_status.mount.ra_j2000_hours, pw_status.mount.dec_j2000_degs
        if ra is None or dec is None:
            return (
                None,
                None,
                (
                    f"{op}: the mount is connected but reports no position, so there is nothing "
                    "to fall back to; supply coordinates or an object_name"
                ),
            )
        return ra, dec, None

    def prepare_acquisition(
        self,
        seconds: float | None = 5.0,
        ra_j2000_hours: str | float | None = None,
        dec_j2000_degs: str | float | None = None,
        object_name: str | None = None,
        resolve_timeout: float = TOTAL_TIMEOUT_SECONDS,
        gain_absolute: int | None = asi.ASI_294MM_DEFAULT_GAIN,
        gain_percent: int | None = None,
        approach_mode: ApproachMode = ApproachMode.GRADUAL_BY_RATE,
        make_corrections: bool = True,
        skip_sky: bool = False,
        use_set_limit_frame: bool = True,
        handover_automatically_to_guider: bool = True,
    ) -> Acquisition | CanonicalResponse:
        """Check the request and build its `Acquisition`, or return the refusal.

        Split from the endpoint so a caller already on its own thread can run the
        acquisition with `run_acquisition` and know when it has ended, rather than start it
        on another thread and infer the end from `Acquiring` (MAST_unit#316).
        """
        op = function_name()

        missing = self.missing_components()
        if missing:
            return CanonicalResponse(
                errors=[f"cannot start acquisition, these components did not initialize: {', '.join(missing)}"]
            )

        assert self.unit.required_mount.pw is not None, f"{op}: unit.required_mount.pw is None"

        pw_status = self.unit.required_mount.pw.status()

        ra_j2000_hours, dec_j2000_degs, problem = self._target_coordinates(
            op, ra_j2000_hours, dec_j2000_degs, object_name, resolve_timeout, pw_status
        )
        if problem is not None:
            return CanonicalResponse(errors=[problem])

        assert self.unit.unit_conf is not None
        assert self.unit.unit_conf.solving.method in self.unit.unit_conf.solving.valid_methods, (
            "unit unit_conf.solving.method is not in allowed_methods"
        )

        solver_name = self.unit.unit_conf.solving.method

        if all([gain_absolute, gain_percent]):
            return CanonicalResponse(errors=["supply only one of 'gain_absolute' or 'gain_percent', not both"])

        # A backstop, not the diagnosis. Every way of failing to get coordinates now
        # returns its own reason from `_target_coordinates` above, so reaching here means
        # that function grew a path which returns neither coordinates nor a problem.
        # Deliberately not phrased as "no coordinates supplied and mount not connected":
        # that was accurate when this was the only check, and would now send the reader
        # after a fault that has already been ruled out.
        if ra_j2000_hours is None or dec_j2000_degs is None:
            return CanonicalResponse(errors=[f"{op}: no target coordinates were determined, and no reason was given"])

        assert self.unit.unit_conf is not None
        return Acquisition(
            unit=self.unit,
            approach_mode=approach_mode,
            solver_id=SolverId[solver_name],
            make_corrections=make_corrections,
            target_ra=float(ra_j2000_hours),
            target_dec=float(dec_j2000_degs),
            exposure=seconds,
            gain_absolute=gain_absolute or asi.ASI_294MM_DEFAULT_GAIN,
            gain_percent=gain_percent,
            skip_sky=skip_sky,
            use_set_limit_frame=use_set_limit_frame,
            handover_automatically_to_guider=handover_automatically_to_guider,
        )

    @endpoint(tier=Tier.OPERATION)
    def endpoint_start_acquisition_and_guiding(
        self,
        seconds: float | None = 5.0,
        ra_j2000_hours: Annotated[
            str | float | None,
            Query(
                description=(
                    "### Right Ascension (J2000) in either:\n"
                    "- decimal hours (e.g., `12.5`) or\n"
                    "- sexagesimal format (e.g., `12:30:45.123`). \n"
                    "- Decimal range: `0 <= RA < 24`.\n"
                    "**Wins over `object_name`** when this and `dec_j2000_degs` are both "
                    "supplied and both valid. If either is missing or unusable, "
                    "`object_name` is resolved instead; with no `object_name`, taken from "
                    "the telescope."
                ),
            ),
        ] = None,
        dec_j2000_degs: Annotated[
            str | float | None,
            Query(
                description=(
                    "### Declination (J2000) in either:\n"
                    "- decimal degrees (e.g., `-45.5`) or\n"
                    "- sexagesimal format (e.g., `-45:30:00.123`). \n"
                    "- Decimal range: `-90 <= DEC <= 90`.\n"
                    "**Wins over `object_name`** when this and `ra_j2000_hours` are both "
                    "supplied and both valid. If either is missing or unusable, "
                    "`object_name` is resolved instead; with no `object_name`, taken from "
                    "the telescope."
                ),
            ),
        ] = None,
        object_name: Annotated[
            str | None,
            Query(
                description=(
                    "### Object name, resolved to J2000 coordinates.\n"
                    "**Used only when `ra_j2000_hours` and `dec_j2000_degs` are not both "
                    "supplied and valid** -- explicit coordinates always take precedence.\n"
                    "- resolved against **TNS** (`AT`/`SN` designations) and **Sesame** "
                    "(SIMBAD / NED / VizieR)\n"
                    "- **moving targets are refused**: comets, minor planets and "
                    "solar-system bodies have no fixed J2000\n"
                    "- a name that does not resolve is an **error**; the acquisition does "
                    "not fall back to the telescope's current position"
                ),
            ),
        ] = None,
        resolve_timeout: Annotated[
            float,
            Query(gt=0, description="Seconds to spend resolving `object_name` before giving up"),
        ] = TOTAL_TIMEOUT_SECONDS,
        gain_absolute: Annotated[
            int | None, Query(ge=asi.ControlDict[asi.Control.Gain].min_value, le=asi.ControlDict[asi.Control.Gain].max_value)
        ] = asi.ASI_294MM_DEFAULT_GAIN,
        gain_percent: Annotated[int | None, Query(ge=0, le=100)] = None,
        approach_mode: ApproachMode = ApproachMode.GRADUAL_BY_RATE,
        make_corrections: bool = True,
        skip_sky: bool = False,
        use_set_limit_frame: bool = True,
        handover_automatically_to_guider: bool = True,
        # Internal: set by MAST_control's plan execution, so the unit labels the products
        # folder with a `plan_id` file and says where it is (plans/control_acquisition_design.md
        # 5.10, 6.5). Accepted, but kept out of the OpenAPI schema: not for an operator.
        plan_id: Annotated[str | None, Query(include_in_schema=False)] = None,
    ):
        """
        Starts an acquisition

        **Where to point**, in strict order of precedence:

        1. `ra_j2000_hours` **and** `dec_j2000_degs`, when both are supplied and both are
           valid -- `object_name` is then not resolved at all.
        2. otherwise `object_name`, if one was given. "Otherwise" covers a coordinate that
           is missing *and* one that will not convert.
        3. otherwise the telescope's current position.

        A name that fails to resolve is an **error**; it never falls through to rule 3.
        Being asked for a named object and quietly acquiring wherever the mount happens to
        point is the failure this ordering exists to prevent.

        :param seconds:
        :param approach_mode:
        :param solver_name:
        :param make_corrections:
        :param ra_j2000_hours: The target's RA; beats `object_name` when paired with a valid Dec
        :param dec_j2000_degs: The target's Dec; beats `object_name` when paired with a valid RA
        :param object_name: Resolved to J2000 only if the coordinates above are not both usable
        :param resolve_timeout: Seconds to spend resolving `object_name`
        :param skip_sky: Skip the 'sky' phase
        :param handover_automatically_to_guider: After acquisition, start PHD2 guiding automatically
        :return: The folder path on the MAST-SHARE with the acquisition's products
        """

        prepared = self.prepare_acquisition(
            seconds=seconds,
            ra_j2000_hours=ra_j2000_hours,
            dec_j2000_degs=dec_j2000_degs,
            object_name=object_name,
            resolve_timeout=resolve_timeout,
            gain_absolute=gain_absolute,
            gain_percent=gain_percent,
            approach_mode=approach_mode,
            make_corrections=make_corrections,
            skip_sky=skip_sky,
            use_set_limit_frame=use_set_limit_frame,
            handover_automatically_to_guider=handover_automatically_to_guider,
        )
        if isinstance(prepared, CanonicalResponse):
            return prepared
        # The folder exists from here (`Acquisition.__init__` makes it): say where, before
        # anything is written into it, so control's link is there from the start.
        self.unit.claim_plan(plan_id)  # accepted: the unit works for the plan from here
        announce_plan_products(plan_id, PlanStage.Acquisition, prepared.folder)
        self.start(prepared, plan_id)

        return CanonicalResponse_Ok
