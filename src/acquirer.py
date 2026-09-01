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
from common.filer import Filer, MoveGuardian
from common.mast_logging import get_logger
from common.models.assignments import AssignmentNotification, UnitAssignment
from common.notifications import Notifier
from common.parsers import (
    DEC_PATTERN,
    RA_PATTERN,
    sexagesimal_degrees_to_decimal,
    sexagesimal_hours_to_decimal,
)
from common.rois import SkyRoi
from common.utils import Coord, boxed_log, function_name
from mount import SettleMode
from phd2.phd2 import PHD2Connector
from solving import SolverId, SolvingTolerance
from stage import StagePresetPosition

logger = get_logger(__name__)


#: The `Unit` attributes an acquisition dereferences. Each is `None` when its component
#: failed to build, which is a routine state -- a PHD2 connect failure leaves `imager` and
#: `guider` both unbuilt (MAST_unit#84), and a missing ASCOM cover driver leaves `covers`
#: unbuilt on three of four units (MAST_unit#95).
REQUIRED_COMPONENTS = ("mount", "guider", "imager", "solver", "stage")


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

    def missing_components(self) -> list[str]:
        """The `REQUIRED_COMPONENTS` this unit did not build, in declaration order."""
        return [name for name in REQUIRED_COMPONENTS if getattr(self.unit, name) is None]

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
            self.do_acquire(acquisition)
        except Exception:
            logger.exception(f"{function_name()}: acquisition failed")
        finally:
            # Removed once every protected artifact has reached the shared area, and never
            # before -- so a frame that failed to move keeps its folder instead of being
            # deleted with it.
            MoveGuardian().release_folder(acquisition.folder, logger=logger)

    def do_acquire(self, acquisition: Acquisition):  # noqa: C901
        """
        Called from start_acquisition()

        :param acquisition:
        :return:
        """
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
            return

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
            st = self.unit.mount.status()
            if st.ra_j2000_hours is None or st.dec_j2000_degs is None:
                self.unit.errors.append("cannot get coordinates from mount (mount not connected)")
                self.unit.end_activity(UnitActivities.Acquiring)
                return

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

        if not self.unit.imager.connected:
            self.unit.imager.connected = True

        tries: int = acquisition_conf.tries

        self.unit.mount.start_tracking()
        self.unit.start_activity(UnitActivities.Positioning)
        if self.latest_acquisition.slew_to_target:
            self.unit.mount.goto_ra_dec_j2000(target_ra_j2000_hours, target_dec_j2000_degs)

        acquisition_exposure_series = self.unit.imager.start_exposure_series(purpose="acquisition")

        if self.unit.fcu_version == FcuVersion.v1 and not acquisition.skip_sky:
            phase = "sky"
            boxed_log(logger, [f"starting phase '{phase.upper()}'"])

            #
            # move the stage and mount (if needed) into position
            #
            self.unit.stage.move_to_preset(StagePresetPosition.Sky)

            while self.unit.stage.is_moving:
                time.sleep(0.2)
            self.unit.mount.wait_until_settled(SettleMode.SLEW)

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

            achieved_tolerances = self.unit.solver.solve_and_correct(
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
                self.unit.end_activity(UnitActivities.Acquiring)
                self.unit.mount.stop_tracking()
                self.unit.imager.end_exposure_series(acquisition_exposure_series)
                return

        phase = "spec"
        boxed_log(logger, [f"starting phase '{phase.upper()}'"])

        match self.unit.fcu_version:
            case FcuVersion.v1:
                self.unit.stage.move_to_preset(StagePresetPosition.Spec)
            case FcuVersion.v2:
                self.unit.stage.move_to_preset(StagePresetPosition.Sky)

        while self.unit.stage.is_moving:
            time.sleep(0.2)
        logger.info("sleeping additional 5 seconds to let the stage stop moving ...")
        time.sleep(5)
        logger.info(f"stage now at {self.unit.stage.position}")

        if self.unit.is_active(UnitActivities.Positioning):
            self.unit.end_activity(UnitActivities.Positioning)

        assert self.unit.unit_conf is not None
        phase_conf = self.unit.unit_conf.guiding
        ra_tolerance = Angle(phase_conf.tolerance.ra_arcsec * u.arcsecond)  # type: ignore
        dec_tolerance = Angle(phase_conf.tolerance.dec_arcsec * u.arcsecond)  # type: ignore

        # make default guiding settings
        spec_imager_settings = self.unit.guider.make_guiding_settings(
            base_folder=os.path.join(self.latest_acquisition.folder, phase)
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
                # width=self.unit.imager.full_frame.width,
                # height=self.unit.imager.full_frame.height,
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

        achieved_tolerances = self.unit.solver.solve_and_correct(
            target=target,
            approach_mode=acquisition.approach_mode,
            solver_id=acquisition.solver_id,
            make_corrections=acquisition.make_corrections,
            imager_settings=spec_imager_settings,
            solving_tolerance=SolvingTolerance(ra_tolerance, dec_tolerance),
            parent_activity=UnitActivities.Acquiring,
            phase=phase,
            max_tries=tries,
        )
        self.latest_acquisition.save_corrections(phase)
        logger.info(f"{op}: phase '{phase.upper()}' {achieved_tolerances=}")
        if not achieved_tolerances:
            self.unit.end_activity(UnitActivities.Acquiring)
            self.unit.mount.stop_tracking()
            self.unit.imager.end_exposure_series(acquisition_exposure_series)
            return

        if self.unit.imager.can_image_to_memory:
            self.unit.reference_image = self.unit.imager.image_array

        self.unit.imager.end_exposure_series(acquisition_exposure_series)

        lines = ["acquisition completed", "telescope is tracking"]
        if (
            (not isinstance(self.unit.imager._backend, PHD2Connector))
            and isinstance(self.unit.guider._backend, PHD2Connector)
            and self.unit.imager.connected
        ):
            lines.append(
                f"camera disconnected imager={type(self.unit.imager._backend)}, guider={type(self.unit.guider._backend)}"
            )
            self.unit.imager.disconnect()

        # Move the stage to SPEC (FCU v2 was left at Sky by the solve; v1 is already there)
        # and wait for it to settle -- needed by BOTH the auto-handover and the manual
        # acquisition-tuning paths, so guiding always starts with the stage at SPEC.
        if self.unit.fcu_version == FcuVersion.v2:
            self.unit.stage.move_to_preset(StagePresetPosition.Spec)
            lines.append("moving stage to SPEC")
        while self.unit.stage.is_moving:
            time.sleep(0.2)
        logger.info("sleeping additional 5 seconds to let the stage stop moving ...")
        time.sleep(5)

        if self.latest_acquisition.handover_automatically_to_guider:
            lines.append("starting PHD2 guiding")
            boxed_log(logger, lines)
            self.unit.guider.start_guiding()

            while self.unit.is_active(UnitActivities.Guiding):
                time.sleep(1)

            # Guiding was stopped
            self.unit.end_activity(UnitActivities.Acquiring)
            self.unit.mount.stop_tracking()
        else:
            lines.append("acquisition tuning: guiding must be started manually via /start_guiding")
            boxed_log(logger, lines)
            # End the acquisition but KEEP the mount tracking: the operator fine-tunes the
            # pointing with external tools, then starts guiding via the /start_guiding endpoint.
            self.unit.end_activity(UnitActivities.Acquiring)

        if self.unit.acquirer.latest_acquisition is not None:
            self.unit.acquirer.latest_acquisition.post_process()

    def start_acquisition_and_guiding_for_assignment(self, assignment: UnitAssignment):
        """Acquire and hand over to guiding for a controller-issued assignment.

        **Unreferenced and unexercised.** Nothing in this repository calls it, no route
        serves it, and no test covers it -- so it has never run, and restoring it does not
        change any behaviour a caller can reach today. It is kept because it is the only
        expression of the *unattended* assignment flow: `handover_automatically_to_guider`
        is `True` here and `False` in every reachable path, and the `in-progress`
        notification that tells the controller where the products are exists nowhere else.

        Treat every line as unverified. Two things in particular are asserted rather than
        observed: that `assignment.plan` carries `target.ra_hours` / `target.dec_degrees`
        in the shape read below, and that `run_acquisition` on this thread reaches the
        SPEC hand-over without the operator step the reachable path relies on. Before this
        is wired to anything, it wants a hardware pass of its own -- see MAST_unit#156 for
        the sibling problem that a thread answering `Ok` reports nothing.
        """
        approach_mode: ApproachMode = ApproachMode.GRADUAL_BY_RATE
        make_corrections = True
        ra_j2000_hours = assignment.plan.target.ra_hours
        dec_j2000_degs = assignment.plan.target.dec_degrees

        assert self.unit.unit_conf is not None
        solver_name = self.unit.unit_conf.solving.method

        logger.info(
            f"starting acquisition for assignment {assignment.plan.ulid}, "
            f"approach_mode={approach_mode}, solver_name={solver_name} (from unit config), "
            f"make_corrections={make_corrections}, "
            f"ra_j2000_hours={ra_j2000_hours}, dec_j2000_degs={dec_j2000_degs}"
        )

        acquisition = Acquisition(
            unit=self.unit,
            approach_mode=approach_mode,
            solver_id=SolverId[solver_name],
            make_corrections=make_corrections,
            target_ra=float(ra_j2000_hours),
            target_dec=float(dec_j2000_degs),
            # No caller-supplied exposure on this path: the configured one applies.
            # It previously inherited whatever the HTTP endpoint had last written into
            # unit_conf.acquisition.exposure (MAST_unit#195).
            exposure=None,
            # Unattended assignments auto-hand-over to guiding; the Acquisition default is
            # False (manual acquisition-tuning), which would stall the assignment at SPEC.
            handover_automatically_to_guider=True,
        )
        Thread(name="acquisition", target=self.run_acquisition, args=[acquisition]).start()

        """
        This acquisition is part of an assignment, tell the controller where
         the products are.
        """
        if assignment.plan.ulid is not None:
            Notifier().assignment_notification(
                AssignmentNotification(
                    assignment_id=assignment.plan.ulid,
                    state="in-progress",
                    # Relative to the shared root, not the absolute ram path: the controller
                    # symlinks this, and its shared root is spelled differently from ours --
                    # `/Storage/mast-share/MAST/<host>` against our `Z:/MAST/<host>/`.
                    # MAST_spec#39.
                    #
                    # Measured from `ram.root`, which is where the folder still IS -- not from
                    # `shared.root`, which is the tempting reading of the line above. The two
                    # hierarchies mirror each other below their roots (move_ram_to_shared only
                    # swaps the root), so the ram-relative path is already the shared-relative
                    # one. Measuring from shared.root instead raises ValueError on Windows,
                    # where the roots are on different drives: "path is on mount 'D:', start
                    # on mount 'Z:'".
                    shared_top=os.path.relpath(acquisition.folder, Filer().ram.root),
                    shared_subpath="acquisition",
                )
            )

    @endpoint(tier=Tier.OPERATION)
    def endpoint_start_acquisition_and_guiding(
        self,
        seconds: float | None = 5.0,
        ra_j2000_hours: Annotated[
            str | float | None,
            Query(
                pattern=RA_PATTERN,
                description=(
                    "### Right Ascension (J2000) in either:\n"
                    "- decimal hours (e.g., `12.5`) or\n"
                    "- sexagesimal format (e.g., `12:30:45.123`). \n"
                    "- Decimal range: `0 <= RA < 24`.\n"
                    "If not supplied, taken from telescope"
                ),
            ),
        ] = None,
        dec_j2000_degs: Annotated[
            str | float | None,
            Query(
                pattern=DEC_PATTERN,
                description=(
                    "### Declination (J2000) in either:\n"
                    "- decimal degrees (e.g., `-45.5`) or\n"
                    "- sexagesimal format (e.g., `-45:30:00.123`). \n"
                    "- Decimal range: `-90 <= DEC <= 90`.\n"
                    "If not supplied, taken from telescope"
                ),
            ),
        ] = None,
        gain_absolute: Annotated[
            int | None, Query(ge=asi.ControlDict[asi.Control.Gain].min_value, le=asi.ControlDict[asi.Control.Gain].max_value)
        ] = asi.ASI_294MM_DEFAULT_GAIN,
        gain_percent: Annotated[int | None, Query(ge=0, le=100)] = None,
        approach_mode: ApproachMode = ApproachMode.GRADUAL_BY_RATE,
        make_corrections: bool = True,
        skip_sky: bool = False,
        use_set_limit_frame: bool = True,
        handover_automatically_to_guider: bool = True,
    ):
        """
        Starts an acquisition

        :param seconds:
        :param approach_mode:
        :param solver_name:
        :param make_corrections:
        :param ra_j2000_hours: The target's RA
        :param dec_j2000_degs: The target's Dec
        :param skip_sky: Skip the 'sky' phase
        :param handover_automatically_to_guider: After acquisition, start PHD2 guiding automatically
        :return: The folder path on the MAST-SHARE with the acquisition's products
        """

        op = function_name()

        missing = self.missing_components()
        if missing:
            return CanonicalResponse(
                errors=[f"cannot start acquisition, these components did not initialize: {', '.join(missing)}"]
            )

        assert self.unit.mount.pw is not None, f"{op}: unit.mount.pw is None"

        pw_status = self.unit.mount.pw.status()

        # One call, whatever the form. The old code dispatched on `":" in value` and
        # sent everything else to float() -- so a space-separated coordinate, which
        # RA_REGEX explicitly allowed, raised an uncaught ValueError and returned 500.
        # The parsers take sexagesimal, decimal and surrounding whitespace alike.
        if ra_j2000_hours:
            try:
                ra_j2000_hours = sexagesimal_hours_to_decimal(ra_j2000_hours)
            except ValueError as e:
                return CanonicalResponse(errors=[f"{op}: bad ra_j2000_hours -- {e}"])
        else:
            if not pw_status.mount.is_connected:  # type: ignore
                return CanonicalResponse(errors=["cannot get coordinates from mount (mount not connected)"])
            ra_j2000_hours = pw_status.mount.ra_j2000_hours  # type: ignore

        if dec_j2000_degs:
            try:
                dec_j2000_degs = sexagesimal_degrees_to_decimal(dec_j2000_degs)
            except ValueError as e:
                return CanonicalResponse(errors=[f"{op}: bad dec_j2000_degs -- {e}"])
        else:
            if not pw_status.mount.is_connected:  # type: ignore
                return CanonicalResponse(errors=["cannot get coordinates from mount (mount not connected)"])
            dec_j2000_degs = pw_status.mount.dec_j2000_degs  # type: ignore

        assert self.unit.unit_conf is not None
        assert self.unit.unit_conf.solving.method in self.unit.unit_conf.solving.valid_methods, (
            "unit unit_conf.solving.method is not in allowed_methods"
        )

        solver_name = self.unit.unit_conf.solving.method

        if all([gain_absolute, gain_percent]):
            return CanonicalResponse(errors=["supply only one of 'gain_absolute' or 'gain_percent', not both"])

        if ra_j2000_hours is None or dec_j2000_degs is None:
            return CanonicalResponse(errors=["cannot start acquisition - no coordinates supplied and mount not connected"])

        assert self.unit.unit_conf is not None
        acquisition = Acquisition(
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
        Thread(name="acquisition", target=self.run_acquisition, args=[acquisition]).start()

        return CanonicalResponse_Ok
