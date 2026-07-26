import logging
import math
import os
import time
from threading import Thread
from typing import TYPE_CHECKING, Annotated

from fastapi import Query

from acquirer import DEC_REGEX, RA_REGEX
from common.activities import FocuserActivities, UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.config.rois import SkyRoiConfig
from common.filer import Filer, MoveGuardian
from common.interfaces.imager import ImagerRoi, ImagerSettings
from common.mast_logging import init_log
from common.parsers import sexagesimal_degrees_to_decimal, sexagesimal_hours_to_decimal
from common.paths import PathMaker
from common.rois import UnitRoi
from common.utils import boxed_log
from focus_analysis import (
    FocusAnalysisError,
    PS3AutofocusStatus,
    PS3FocusAnalysisResult,
    analyze_focus_files,
)
from plotting import plot_autofocus_analysis
from stage import StagePresetPosition

if TYPE_CHECKING:
    from unit import Unit  # type: ignore[import-untyped]

logger = logging.getLogger("mast.unit." + __name__)
init_log(logger)
filer = Filer(logger)


class AutofocusResult:
    success: bool
    best_position: float | None
    tolerance: float | None
    time_stamp: str


# PS3FocusSample, PS3FocusAnalysisResult and PS3AutofocusStatus now live in
# focus_analysis.py (imported above) so the ps3cli step can be reused by the
# autofocus-solve validation harness without pulling in the unit/hardware deps.


class Autofocuser:
    def __init__(self, unit: "Unit"):  # type: ignore[name]
        self.unit = unit  # type: ignore[name]
        self.latest_result: PS3FocusAnalysisResult | None = None

    @property
    def is_autofocusing(self) -> bool:
        """
        Returns the status of the ``autofocus`` routine
        """
        if not self.unit.connected:
            return False

        return self.unit.is_active(UnitActivities.Autofocusing) or (
            self.unit.is_active(UnitActivities.AutofocusingPWI4) and self.unit.pw.status().autofocus.is_running  # type: ignore[union-attr]
        )

    def start_autofocus(  # noqa: C901
        self,
        ra_j2000_hours: Annotated[
            str | float | None,
            Query(
                pattern=RA_REGEX + r"|^\d{1,2}(\.\d+)?$",
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
                pattern=DEC_REGEX + r"|^[-+]?\d{1,2}(\.\d+)?$",
                description=(
                    "### Declination (J2000) in either:\n"
                    "- decimal degrees (e.g., `-45.5`) or\n"
                    "- sexagesimal format (e.g., `-45:30:00.123`). \n"
                    "- Decimal range: `-90 <= DEC <= 90`.\n"
                    "If not supplied, taken from telescope"
                ),
            ),
        ] = None,
        exposure: float | None = 5,  # seconds
        start_position: (int | None) = None,  # when None, start from know-as-good position
        ticks_per_step: int | None = 50,  # focuser ticks per step
        number_of_images: int | None = 5,
    ):
        """

        Parameters
        ----------
        ra_j2000_hours - if supplied start by sending the mount to these coordinates
        dec_j2000_degs - if supplied start by sending the mount to these coordinates
        exposure - exposure duration in seconds
        start_position - if supplied start by sending the focuser to this position, else to the known-as-good position
        ticks_per_step - by how many ticks to increase the focuser position between exposures
        number_of_images - how many exposures to take, MUST be odd
        binning - the binning to use, defaults to 1x1

        Returns
        -------

        """
        if ra_j2000_hours:
            if isinstance(ra_j2000_hours, str):
                if ":" in ra_j2000_hours:
                    ra_j2000_hours = sexagesimal_hours_to_decimal(ra_j2000_hours)
                else:
                    ra_j2000_hours = float(ra_j2000_hours)
            elif isinstance(ra_j2000_hours, float):
                pass

        if dec_j2000_degs:
            if isinstance(dec_j2000_degs, str):
                if ":" in dec_j2000_degs:
                    dec_j2000_degs = sexagesimal_degrees_to_decimal(dec_j2000_degs)
                else:
                    dec_j2000_degs = float(dec_j2000_degs)
            elif isinstance(dec_j2000_degs, float):
                pass

        assert self.unit.unit_conf is not None
        if number_of_images is None:
            number_of_images = self.unit.unit_conf.autofocus.images
        if number_of_images and number_of_images % 2 != 1:
            return CanonicalResponse(errors=[f"bad {number_of_images=}, MUST be odd!"])

        if start_position is None:
            start_position = self.unit.focuser.position

        if ticks_per_step is None:
            ticks_per_step = self.unit.unit_conf.autofocus.spacing

        if exposure is None:
            exposure = self.unit.unit_conf.autofocus.exposure

        Thread(
            name="wis-autofocus",
            target=self.do_start_autofocus,
            args=[
                ra_j2000_hours,
                dec_j2000_degs,
                exposure,
                start_position,
                ticks_per_step,
                number_of_images,
            ],
        ).start()

    def do_start_autofocus(  # noqa: C901
        self,
        target_ra: float | None = None,  # center of ROI
        target_dec: float | None = None,  # center of ROI
        exposure: float = 5,  # seconds
        start_position: (int | None) = None,  # when None, start from the known-as-good position
        ticks_per_step: int = 50,  # focuser ticks per step
        number_of_images: int = 5,
    ):
        """
        Use PlaneWave's new method for autofocus:
        - Move the stage to 'Sky'
        - Move the mount to (target_ra, target_dec), if supplied, otherwise stay where you are
        - Move the focuser to 'start_position', if supplied, otherwise the known-as-good position
        - Set the ROI as for the acquisition ROI
        - Take the exposures while moving the focuser by 'ticks_per_step' between images
        - Send the images to PWI4, get the results
        - TODO: Learn from the results whether more runs will get a better result, if 'yes': do so

        Parameters
        ----------
        target_ra           - Ra for telescope move
        target_dec          - Dec for telescope move
        exposure            - In seconds
        start_position      - Focuser staring position
        ticks_per_step      - Focuser steps between exposures
        number_of_images    - How many images to take
        """
        op = "do_start_autofocus"
        self.unit.errors = []
        self.latest_result = None

        assert self.unit.unit_conf is not None

        self.unit.start_activity(UnitActivities.Autofocusing)

        self.unit.stage.move_to_preset(StagePresetPosition.Sky)

        pw_status = self.unit.pw.status()
        if not pw_status.mount.is_tracking:  # type: ignore[union-attr]
            logger.info(f"{op}: starting mount tracking")
            self.unit.pw.mount_tracking_on()

        if target_ra is None or target_dec is None:
            logger.info(f"{op}: no target position was supplied, not moving the mount")
        else:
            logger.info(f"{op}: moving mount to {target_ra=}, {target_dec=} ...")
            self.unit.mount.goto_ra_dec_j2000(target_ra, target_dec)

        start_position = start_position or self.unit.unit_conf.focuser.known_as_good_position
        focuser_position: int = int(start_position - ((number_of_images / 2) * ticks_per_step))
        self.unit.focuser.position = focuser_position

        logger.debug(f"{op}: Waiting for components (stage, mount, focuser) to stop moving ...")
        while (
            self.unit.stage.is_moving or self.unit.mount.is_moving or self.unit.focuser.is_active(FocuserActivities.Moving)
        ):
            time.sleep(0.5)
        logger.debug(f"{op}: Components (stage, mount, focuser) stopped moving ...")
        if not self.unit.is_active(UnitActivities.Autofocusing):
            logger.info("activity 'Autofocusing' was stopped")
            return

        acquisition_conf = self.unit.unit_conf.acquisition
        roi_conf = acquisition_conf.rois[self.unit.fcu_version]
        assert isinstance(roi_conf, SkyRoiConfig)

        unit_roi = UnitRoi(
            roi_conf.sky_x,
            roi_conf.sky_y,
            roi_conf.width,
            roi_conf.height,
        )
        _binning = acquisition_conf.binning

        max_tries: int = self.unit.unit_conf.autofocus.max_tries
        max_tolerance: float = self.unit.unit_conf.autofocus.max_tolerance
        try_number: int = 0

        autofocus_exposure_series = self.unit.imager.start_exposure_series(purpose="autofocus")

        for try_number in range(max_tries):
            autofocus_folder = PathMaker().make_autofocus_folder()
            logger.info(f"{op}: starting autofocus try #{try_number} (of {max_tries}) in '{autofocus_folder}' ...")
            #
            # Acquire images
            #
            files: list[str] = []
            for image_no in range(number_of_images):
                autofocus_settings = ImagerSettings(
                    seconds=exposure,
                    binning=_binning,
                    roi=ImagerRoi.from_other(roi=unit_roi),
                    gain=acquisition_conf.gain,
                    image_path=os.path.join(autofocus_folder, f"FOCUS{int(focuser_position):05}.fits"),
                    save=True,
                )

                logger.info(
                    f"{op}: starting exposure #{image_no} of {number_of_images} at {focuser_position=} {autofocus_settings.roi=}..."
                )
                self.unit.imager.start_exposure(autofocus_settings)
                logger.info(f"{op}: waiting for exposure #{image_no} of {number_of_images} ...")
                self.unit.imager.wait_for_image_saved()
                assert autofocus_settings.image_path
                files.append(autofocus_settings.image_path)

                if not self.unit.is_active(UnitActivities.Autofocusing):  # have we been stopped?
                    logger.info(f"{op}: activity 'Autofocusing' was stopped")
                    self.unit.imager.end_exposure_series(autofocus_exposure_series)
                    return

                focuser_position += ticks_per_step
                logger.info(f"{op}: moving focuser by {ticks_per_step} ticks (to {focuser_position}) ...")
                self.unit.focuser.position = focuser_position
                while self.unit.focuser.is_active(FocuserActivities.Moving):
                    time.sleep(0.5)
                logger.info(f"{op}: focuser stopped moving")

                if not self.unit.is_active(UnitActivities.Autofocusing):  # have we been stopped?
                    logger.info(f"{op}: activity 'Autofocusing' was stopped")
                    self.unit.imager.end_exposure_series(autofocus_exposure_series)
                    return

            # The files are now in the autofocus_folder

            self.unit.imager.end_exposure_series(autofocus_exposure_series)

            self.unit.start_activity(UnitActivities.AutofocusAnalysis)
            try:
                status = analyze_focus_files(files, timeout=60)
            except FocusAnalysisError as ex:
                self.log_and_store_error(f"{op}: {ex}")
                filer.move_ram_to_shared(autofocus_folder)
                self.unit.end_activity(UnitActivities.AutofocusAnalysis)
                if ex.phase == "start":
                    self.unit.end_activity(UnitActivities.Autofocusing)
                    return
                continue  # next try_number

            self.unit.end_activity(UnitActivities.AutofocusAnalysis)

            if not status or not status.analysis_result:
                self.log_and_store_error(f"{op}: focus analyser stopped working but empty analysis_result")
                with MoveGuardian().protect(autofocus_folder):
                    self.save_analysis(
                        autofocus_folder, status=status, errors=["focus analyser stopped working but empty analysis_result"]
                    )
                    filer.move_ram_to_shared(autofocus_folder)
                continue  # next try_number

            if not status.analysis_result.has_solution:
                self.log_and_store_error(f"{op}: focus analyser did not find a solution")
                with MoveGuardian().protect(autofocus_folder):
                    self.save_analysis(autofocus_folder, status=status, errors=["focus analyser did not find a solution"])
                    filer.move_ram_to_shared(autofocus_folder)
                continue  # next try_number

            #
            # We have an analysis solution
            #
            self.latest_result = status.analysis_result

            logger.info(
                f"{op}: analysis result: "
                + f"{self.latest_result.best_focus_position=}, {self.latest_result.best_focus_star_diameter=}, "
                + f"{self.latest_result.tolerance=}"
            )

            error = None
            if self.latest_result.tolerance is None:
                error = "tolerance is None"
            elif math.isnan(self.latest_result.tolerance):
                error = "tolerance is NaN"
            elif self.latest_result.tolerance > max_tolerance:
                error = f"tolerance {self.latest_result.tolerance} is higher than {max_tolerance=}"
            if error:
                self.log_and_store_error(f"{op}: {error=}, ignoring analysis result")

                self.save_analysis(autofocus_folder, status=status, errors=[error])
                continue  # next try_number

            if self.latest_result.best_focus_position is not None:
                self.save_analysis(autofocus_folder, status=status)

                position: int = int(self.latest_result.best_focus_position)
                logger.info(f"{op}: moving focuser to best focus position {position} ...")
                self.unit.focuser.known_as_good_position = position
                self.unit.focuser.position = self.unit.focuser.known_as_good_position

                logger.info(f"{op}: waiting for focuser to stop moving ...")
                while self.unit.focuser.is_active(FocuserActivities.Moving):
                    time.sleep(0.5)
                logger.info(f"{op}: focuser stopped moving")

                self.unit.unit_conf.focuser.known_as_good_position = position
                try:
                    Config().set_unit(unit_name=self.unit.hostname, unit_conf=self.unit.unit_conf)
                    logger.info(
                        f"saved unit '{self.unit.hostname}' configuration for "
                        + f"focuser known-as-good-position {position}"
                    )
                except Exception as e:
                    self.log_and_store_error(
                        f"could not save unit '{self.unit.hostname}' "
                        + f"configuration for focuser known-as-good-position (exception: {e})"
                    )

            pixel_scale: float = self.unit.unit_conf.imager.pixel_scale_at_bin1
            Thread(
                name="autofocus-analysis-plotter",
                target=plot_autofocus_analysis,
                args=[self.latest_result, autofocus_folder, pixel_scale],
            ).start()

            break  # the tries loop

        if try_number == max_tries - 1:
            msg = f"{op}: could not achieve {max_tolerance=} within {max_tries=}"
            self.log_and_store_error(msg)
            boxed_log(logger=logger, lines=[msg], level=logging.ERROR)

        self.unit.mount.stop_tracking()
        self.unit.end_activity(UnitActivities.Autofocusing)

    def save_analysis(self, folder: str, status: PS3AutofocusStatus | None = None, errors: list[str] | None = None):
        filename = os.path.join(folder, "status.json")
        if status is None:
            status = PS3AutofocusStatus(
                is_running=False,
                errors=errors,
            )
        else:
            if errors:
                if not status.errors:
                    status.errors = []
                status.errors.extend(errors)

        with open(filename, "w") as f:
            f.write(status.model_dump_json(indent=4))

    def start_pwi4_autofocus(self):
        """
        Starts the ``autofocus`` routine (implemented by _PlaneWave_)

        :mastapi:
        """
        # if not self.connected:
        #     logger.error('Cannot start PlaneWave autofocus - not-connected')
        #     return

        if self.unit.pw.status().autofocus.is_running:  # type: ignore[union-attr]
            logger.info("pwi4 autofocus already running")
            return

        #
        # NOTE: The PWI4 autofocus method uses the autofocus parameters set via the PWI4 GUI
        #

        self.unit.pw.request("/autofocus/start")
        while (
            not self.unit.pw.status().autofocus.is_running  # type: ignore[union-attr]
        ):  # wait for it to actually start
            logger.debug("waiting for PlaneWave autofocus to start")
            time.sleep(1)
        if self.unit.autofocus_try == 0:
            self.unit.start_activity(UnitActivities.AutofocusingPWI4)
        logger.debug("PlaneWave autofocus has started")
        return CanonicalResponse_Ok

    def endpoint_stop_autofocus(self):
        return self.stop_autofocus()

    def stop_autofocus(self):
        """
        Stops the ``autofocus`` routine

        :mastapi:
        """
        # if not self.connected:
        #     logger.error('Cannot stop PlaneWave autofocus - not-connected')
        #     return

        if self.unit.is_active(UnitActivities.AutofocusingPWI4):
            if not self.unit.pw.status().autofocus.is_running:  # type: ignore[union-attr]
                logger.info("Cannot stop PWI4 autofocus, it is not running")
                return
            self.unit.pw.request("/autofocus/stop")
            self.unit.end_activity(UnitActivities.AutofocusingPWI4)
            return CanonicalResponse_Ok

        elif self.unit.is_active(UnitActivities.Autofocusing):
            self.unit.end_activity(UnitActivities.Autofocusing)
            return CanonicalResponse_Ok

    def log_and_store_error(self, message: str):
        logger.error(message)
        self.unit.errors.append(message)
