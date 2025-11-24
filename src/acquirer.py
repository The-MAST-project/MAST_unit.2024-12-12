import logging
import os
import time
from threading import Thread
from typing import Annotated

import astropy.units as u
from astropy.coordinates import Angle, Latitude, Longitude
from fastapi import Query

import common.ASI as ASI
from acquisition import Acquisition
from common.activities import UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config.rois import SkyRoiConfig
from common.mast_logging import init_log
from common.parsers import (sexagesimal_degrees_to_decimal,
                            sexagesimal_hours_to_decimal)
from common.rois import SkyRoi
from common.tasks.models import UnitAssignmentModel
from common.tasks.notifications import \
    notify_controller_about_task_acquisition_path
from common.utils import Coord, boxed_info, function_name
from phd2.phd2 import PHD2Connector
from solving import SolverId, SolvingTolerance
from stage import StagePresetPosition

logger = logging.getLogger("mast.unit." + __name__)
init_log(logger)

RA_REGEX = r"^(\d{1,2})[: ](\d{2})[: ](\d{2}(?:\.\d{1,3})?)$"
DEC_REGEX = r"^([+-]?)(\d{1,2})[: ](\d{2})[: ](\d{2}(?:\.\d{1,3})?)$"


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

    def do_acquire(self, acquisition: Acquisition):  # noqa: C901
        """
        Called from start_acquisition()

        :param acquisition:
        :return:
        """
        op = function_name()

        self.unit.errors = []
        self.unit.reference_image = None

        self.latest_acquisition = acquisition
        acquisition_conf = acquisition.conf
        sky_roi_conf = acquisition_conf.rois[self.unit.fcu_version]

        #
        # Figure out the target, it can come from:
        # - the acquisition (target_ra: float, target_dec: float)
        # - if the acquisition doesn't have them, get them from the mount's status
        # - if they were supplied as Longitude and Latitude, transform to float
        # - pass them on to solve_and_correct() as a Coord
        #
        if not hasattr(acquisition, "target_ra") or not hasattr(
            acquisition, "target_dec"
        ):
            st = self.unit.mount.status()
            if st.ra_j2000_hours is None or st.dec_j2000_degs is None:
                self.unit.errors.append(
                    "cannot get coordinates from mount (mount not connected)"
                )
                self.unit.end_activity(UnitActivities.Acquiring)
                return

            acquisition.target_ra = st.ra_j2000_hours
            acquisition.target_dec = st.dec_j2000_degs

        target_ra_j2000_hours: float = (
            acquisition.target_ra.value
            if isinstance(acquisition.target_ra, Longitude)
            else acquisition.target_ra
        )

        target_dec_j2000_degs: float = (
            acquisition.target_dec.value
            if isinstance(acquisition.target_dec, Latitude)
            else acquisition.target_dec
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
            self.unit.mount.goto_ra_dec_j2000(
                target_ra_j2000_hours, target_dec_j2000_degs
            )

        acquisition_exposure_series = self.unit.imager.start_exposure_series(
            purpose="acquisition"
        )

        if not acquisition.skip_sky:
            phase = "sky"
            boxed_info(logger, [f"starting phase '{phase.upper()}'"])

            #
            # move the stage and mount (if needed) into position
            #
            self.unit.stage.move_to_preset(StagePresetPosition.Sky)

            while self.unit.stage.is_moving or self.unit.mount.is_moving:
                time.sleep(0.2)
            logger.info("sleeping additional 3 seconds to let the mount really stop moving ...")
            time.sleep(3)

            self.unit.end_activity(UnitActivities.Positioning)

            imager_binning = acquisition_conf.binning

            assert isinstance(sky_roi_conf, SkyRoiConfig)
            sky_roi = SkyRoi(
                sky_x=sky_roi_conf.sky_x,
                sky_y=sky_roi_conf.sky_y,
                width=sky_roi_conf.width,
                height=sky_roi_conf.height)

            gain = acquisition.gain_absolute if acquisition.gain_absolute is not None \
                else ASI.gain_percent_to_absolute(acquisition.gain_percent) if acquisition.gain_percent is not None \
                    else None

            from common.interfaces.imager import ImagerRoi, ImagerSettings

            sky_settings = ImagerSettings(
                seconds=acquisition_conf.exposure,
                base_folder=os.path.join(self.latest_acquisition.folder, phase),
                gain=gain,
                binning=imager_binning,
                roi=ImagerRoi.from_other(roi=sky_roi),
                save=True,
            )

            #
            # loop trying to solve and correct the mount till within tolerances
            #

            # set up the tolerances
            default_tolerance: Angle = Angle(1 * u.arcsecond)  # type: ignore
            ra_tolerance: Angle = default_tolerance
            dec_tolerance: Angle = default_tolerance
            phase_conf = self.unit.unit_conf.acquisition
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
        boxed_info(logger, [f"starting phase '{phase.upper()}'"])

        self.unit.stage.move_to_preset(StagePresetPosition.Spec)
        while self.unit.stage.is_moving:
            time.sleep(0.2)
        logger.info("sleeping additional 5 seconds to let the stage stop moving ...")
        time.sleep(5)
        logger.info(f"stage now at {self.unit.stage.position}")

        if self.unit.is_active(UnitActivities.Positioning):
            self.unit.end_activity(UnitActivities.Positioning)

        phase_conf = self.unit.unit_conf.guiding
        ra_tolerance = Angle(phase_conf.tolerance.ra_arcsec * u.arcsecond)  # type: ignore
        dec_tolerance = Angle(phase_conf.tolerance.dec_arcsec * u.arcsecond)  # type: ignore

        # make default guiding settings
        spec_imager_settings = self.unit.guider.make_guiding_settings(
            base_folder=os.path.join(self.latest_acquisition.folder, phase)
        )
        # override with acquisition settings
        spec_imager_settings.seconds = acquisition.conf.exposure
        if acquisition_conf.binning is not None:
            spec_imager_settings.binning = acquisition_conf.binning
        if acquisition_conf.gain is not None:
            spec_imager_settings.gain = acquisition_conf.gain

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

        if self.latest_acquisition.handover_automatically_to_guider:
            lines.append("starting PHD2 guiding")
            boxed_info(logger, lines)
            self.unit.start_activity(UnitActivities.Guiding)
            self.unit.guider.start_guiding()
        else:
            lines.append("start manual PHD2 guiding")
            boxed_info(logger, lines)
            self.unit.start_activity(UnitActivities.Guiding)

        while self.unit.is_active(UnitActivities.Guiding):
            time.sleep(1)

        # Acquisition was stopped
        self.unit.end_activity(UnitActivities.Acquiring)

        self.unit.mount.stop_tracking()
        if self.unit.acquirer.latest_acquisition is not None:
            self.unit.acquirer.latest_acquisition.post_process()

    def start_acquisition_and_guiding_for_assignment(
        self, assignment: UnitAssignmentModel
    ):
        approach_mode: int = 2
        make_corrections = True
        ra_j2000_hours = assignment.target.ra
        dec_j2000_degs = assignment.target.dec

        solver_name = self.unit.unit_conf.solving.method

        logger.info(
            f"starting acquisition for assignment {assignment.task.ulid}, "
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
            conf=self.unit.unit_conf.acquisition,
        )
        Thread(name="acquisition", target=self.do_acquire, args=[acquisition]).start()

        """
        This acquisition is part of an assignment, tell the controller where
         the products are.
        """
        if assignment.task.ulid is not None:
            notify_controller_about_task_acquisition_path(
                task_id=assignment.task.ulid,
                link="acquisition",
                src=acquisition.folder,
            )

    def endpoint_start_acquisition_and_guiding(  # noqa: C901
        self,
        seconds: float | None = 5.0,
        ra_j2000_hours: Annotated[
            str | float | None,
            Query(
                regex=RA_REGEX + r"|^\d{1,2}(\.\d+)?$",
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
                regex=DEC_REGEX + r"|^[-+]?\d{1,2}(\.\d+)?$",
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
            int | None,
            Query(ge=ASI.ControlDict[ASI.Control.Gain].min_value, le=ASI.ControlDict[ASI.Control.Gain].max_value)
        ] = ASI.ASI_294MM_DEFAULT_GAIN,
        gain_percent: Annotated[
            int | None,
            Query(ge=0, le=100)
        ] = None,
        approach_mode: int = 2,
        make_corrections: bool = True,
        skip_sky: bool = False,
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

        pw_status = self.unit.mount.pw.status()

        if ra_j2000_hours:
            if isinstance(ra_j2000_hours, str):
                if ":" in ra_j2000_hours:
                    ra_j2000_hours = sexagesimal_hours_to_decimal(ra_j2000_hours)
                else:
                    ra_j2000_hours = float(ra_j2000_hours)
            elif isinstance(ra_j2000_hours, float):
                pass
        else:
            if not pw_status.mount.is_connected:  # type: ignore
                return CanonicalResponse(
                    errors=["cannot get coordinates from mount (mount not connected)"]
                )
            ra_j2000_hours = pw_status.mount.ra_j2000_hours  # type: ignore

        if dec_j2000_degs:
            if isinstance(dec_j2000_degs, str):
                if ":" in dec_j2000_degs:
                    dec_j2000_degs = sexagesimal_degrees_to_decimal(dec_j2000_degs)
                else:
                    dec_j2000_degs = float(dec_j2000_degs)
            elif isinstance(dec_j2000_degs, float):
                pass
        else:
            if not pw_status.mount.is_connected:  # type: ignore
                return CanonicalResponse(
                    errors=["cannot get coordinates from mount (mount not connected)"]
                )
            dec_j2000_degs = pw_status.mount.dec_j2000_degs  # type: ignore

        if seconds is not None:
            self.unit.unit_conf.acquisition.exposure = seconds

        assert (
            self.unit.unit_conf.solving.method
            in self.unit.unit_conf.solving.valid_methods
        ), "unit unit_conf.solving.method is not in allowed_methods"

        solver_name = self.unit.unit_conf.solving.method

        if all([gain_absolute, gain_percent]):
            return CanonicalResponse(
                errors=["supply only one of 'gain_absolute' or 'gain_percent', not both"]
            )

        if ra_j2000_hours is None or dec_j2000_degs is None:
            return CanonicalResponse(
                errors=[
                    "cannot start acquisition - no coordinates supplied and mount not connected"
                ]
            )

        acquisition = Acquisition(
            unit=self.unit,
            approach_mode=approach_mode,
            solver_id=SolverId[solver_name],
            make_corrections=make_corrections,
            target_ra=float(ra_j2000_hours),
            target_dec=float(dec_j2000_degs),
            conf=self.unit.unit_conf.acquisition,
            gain_absolute=gain_absolute or ASI.ASI_294MM_DEFAULT_GAIN,
            gain_percent=gain_percent,
            skip_sky=skip_sky,
            handover_automatically_to_guider=handover_automatically_to_guider,
        )
        Thread(
            name="acquisition", target=self.do_acquire, args=[acquisition]
        ).start()

        return CanonicalResponse_Ok
