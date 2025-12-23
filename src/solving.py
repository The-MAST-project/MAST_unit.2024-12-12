import datetime
import json
import logging
import os.path
import time
from pathlib import Path
from typing import TYPE_CHECKING

import astropy.units as u
from astropy.coordinates import Angle

from acquisition import Acquisition
from common.activities import UnitActivities
from common.config import Config
from common.config.rois import RoisConfig, SkyRoiConfig
from common.config.unit import AcquisitionConfig, ToleranceConfig
from common.const import Const
from common.corrections import Correction, Corrections
from common.filer import Filer
from common.interfaces.imager import ImagerSettings
from common.interfaces.solving import SolverInterface, SolvingResult, SolvingTolerance
from common.mast_logging import init_log
from common.safety import safety_get_sensor
from common.solving import SolverId
from common.utils import Coord, boxed_log, function_name, isoformat_zulu

logger = logging.Logger("mast.unit." + __name__)
init_log(logger)
filer = Filer(logger)


class Solver(SolverInterface):
    if TYPE_CHECKING:
        from unit import Unit

    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, unit: "Unit"):
        if self._initialized:
            return

        self.unit = unit
        self.latest_result: SolvingResult | None = None

        from solvers.astrometry_dot_net import AstrometryDotNet
        from solvers.planewave_cli import PlaneWaveCli
        from solvers.planewave_shm import PlaneWaveShm

        self._backend: SolverInterface | None = None

        solving_config = Config().get_unit().solving
        method = solving_config.method
        valid_methods = solving_config.valid_methods
        if method not in valid_methods:
            raise ValueError(
                f"invalid solving method '{method}' in configuration, must be one of {valid_methods}"
            )

        if method == "AstrometryDotNet":
            self._backend = AstrometryDotNet()
        elif method == "PlaneWaveCli":
            self._backend = PlaneWaveCli()
        elif method == "PlanewaveShm":
            self._backend = PlaneWaveShm()
        if not self._backend:
            raise Exception("could not figure out solving backend")

        self._initialized = True

    def log_and_store_error(self, message: str):
        logger.error(message)
        self.unit.errors.append(message)

    def solve(
        self, imager_settings: ImagerSettings, target: Coord, phase: Const.SolvingPhase
    ) -> SolvingResult | None:
        op = function_name()

        assert self._backend is not None, "solve: self._backend is None"

        if self.unit.is_active(UnitActivities.Solving):

            imager_settings.make_file_name()

            #
            # Start exposure
            #

            #
            # Try to fetch wind-speed from safety system
            #
            wind_speed = safe = reasons_for_not_safe = None
            result = safety_get_sensor(
                "wind-speed", timeout=0.5, max_age=datetime.timedelta(minutes=1)
            )
            if result is not None:
                wind_speed, safe, reasons_for_not_safe = result

            msg = f"{op}: starting {imager_settings.seconds=} acquisition exposure"
            if wind_speed is not None:
                msg += f" (wind_speed={wind_speed} Km/h, {safe=}, reasons={reasons_for_not_safe})"
            logger.info(msg)

            response = self.unit.imager.start_exposure(imager_settings)
            if response and response.failed:
                self.log_and_store_error(
                    f"{op}: could not start acquisition exposure: {response=}"
                )

                return SolvingResult(
                    succeeded=False,
                    errors=[f"could not start exposure ({[response.errors]})"],
                )

            self.unit.imager.wait_for_image_saved()
            return self._backend.solve(
                unit=self.unit, settings=imager_settings, target=target, phase=phase
            )

    def solve_and_correct(  # noqa: C901
        self,
        target: Coord,
        approach_mode: int,
        solver_id: SolverId,
        make_corrections: bool,
        imager_settings: ImagerSettings,
        solving_tolerance: SolvingTolerance,
        phase: str,
        parent_activity: UnitActivities | None = None,
        max_tries: int = 3,
    ) -> bool:
        """
        Tries for max_tries times to:
        - Take an exposure using imager_settings
        - Plate solve the image
        - If the solved coordinates are NOT within the solving_tolerance from the target, correct the mount


        :param target: (ra, dec) tuple
        :param approach_mode:
        :param make_corrections:
        :param solver_id:
        :param imager_settings: Imager settings for the exposure
        :param solving_tolerance: How close do we need to be to stop trying
        :param parent_activity: If the parent_activity (e.g. UnitActivities.Acquiring, UnitActivities.Guiding)
               is stopped, this function stops as well
        :param max_tries: How many times to try to get withing the solving_tolerance
        :param phase: One of ['sky', 'spec', 'guiding']

        :rtype: bool
        :return: True if succeeded to achieve tolerances within max_tries, False otherwise

        """

        op = function_name()
        if phase:
            op += f":{phase=}:{approach_mode=}"

        def was_cancelled() -> bool:
            return not self.unit.is_active(UnitActivities.Solving) or (
                parent_activity is not None and not self.unit.is_active(parent_activity)
            )

        self.unit.start_activity(UnitActivities.Solving)

        if not self.unit.acquirer.latest_acquisition:
            # when not part of an acquisition sequence
            tolerance = ToleranceConfig(
                ra_arcsec=float(solving_tolerance.ra.arcsecond),  # type: ignore
                dec_arcsec=float(solving_tolerance.dec.arcsecond),  # type: ignore
            )  # type: ignore

            if imager_settings.roi:
                sky_roi_config = SkyRoiConfig(
                    sky_x=imager_settings.roi.x,
                    sky_y=imager_settings.roi.y,
                    width=imager_settings.roi.width,
                    height=imager_settings.roi.height,
                )
            else:
                sky_roi_config = SkyRoiConfig(
                    sky_x=self.unit.imager.full_frame.x,
                    sky_y=self.unit.imager.full_frame.y,
                    width=self.unit.imager.full_frame.width,
                    height=self.unit.imager.full_frame.height,
                )

            conf = AcquisitionConfig(
                exposure=imager_settings.seconds,
                binning=imager_settings.binning,
                rois=RoisConfig({ self.unit.fcu_version: sky_roi_config}),
                gain=imager_settings.gain or 100,
                tries=self.unit.unit_conf.acquisition.tries,
                tolerance=tolerance,
            )
            if self.unit.acquirer is None:
                raise Exception(
                    f"{op}: unit.acquirer is None, cannot create latest_acquisition"
                )

            if target is None:
                mount_status = self.unit.mount.status
                target = Coord(
                    ra=Angle(
                        mount_status.ra_j2000_hours,
                        unit="hourangle",
                        dec=Angle(mount_status.dec_j2000_degs, unit="deg"),
                    )
                )

            self.unit.acquirer.latest_acquisition = Acquisition(
                unit=self.unit,
                approach_mode=approach_mode,
                solver_id=solver_id,
                make_corrections=make_corrections,
                target_ra=target.ra.arcsecond,  # type: ignore
                target_dec=target.dec.arcsecond,  # type: ignore
                conf=conf,
            )

            self.unit.acquirer.latest_acquisition.corrections = {}

        if phase not in self.unit.acquirer.latest_acquisition.corrections:
            # in case there were no corrections yet for this phase
            self.unit.acquirer.latest_acquisition.corrections[phase] = Corrections(
                phase=phase,
                target_ra=target.ra.hour,  # type: ignore
                target_dec=target.dec.deg,  # type: ignore
                tolerance_ra=solving_tolerance.ra.arcsecond,  # type: ignore
                tolerance_dec=solving_tolerance.dec.arcsecond,  # type: ignore
            )
        latest_corrections = self.unit.acquirer.latest_acquisition.corrections[phase]

        for try_number in range(max_tries):
            if was_cancelled():
                return False

            op = (
                f"{function_name()}:{phase.upper()}:[{try_number}_of_{max_tries}]"
                if phase != "guiding"
                else f"{function_name()}:{phase.upper()}"
            )
            boxed_log(lines=["Plate solving", f"{phase.upper()} [{try_number}_of_{max_tries}]"], center=True, logger=logger)

            # run the plate solver
            try:
                result = self.solve(imager_settings=imager_settings, target=target,
                                    phase="sky" if phase == "sky" else "spec")
            except TimeoutError:
                self.log_and_store_error("plate solving timed out, continuing ...")
                continue

            self.latest_result = result
            if result is None:
                self.log_and_store_error(f"{op}: plate_solve returned None")
                continue

            if imager_settings.image_path is None:
                raise Exception(
                    f"{op}: imager_settings.image_path is None, cannot save the image"
                )

            # save the solver result for debugging
            result_file_name = imager_settings.image_path.replace(
                ".fits", "-solver_result.json"
            )
            os.makedirs(os.path.dirname(result_file_name), exist_ok=True)
            with open(result_file_name, "w") as fp:
                fp.write(json.dumps(result.to_dict(), indent=2))
            time.sleep(2)
            filer.move_ram_to_shared(result_file_name)

            #
            # From "PlateSolve3 server documentation"
            #
            # state: Indicates the state of the solver as one of the following values:
            # 	ready: Solver has not yet attempted a solve since starting, but is ready to accept a request
            # 	loading: Solver is loading the image
            # 	extracting: Solver is locating stars within the image
            # 	matching: Solver is attempting to match detected stars to the star catalog
            # 	found_match: Solver successfully performed a match and is finished processing
            # 	no_match: Solver failed to find a match and is finished processing
            # 	error: An error occurred and processing was stopped
            #
            # error_message: If "state" == "error", this contains a string describing the error. Otherwise, this is null
            #
            # last_log_message: A string containing a report of the most recently-performed step in the solver
            #

            if not result.succeeded:
                msg = None
                if result.errors:
                    msg = f"errors: '{result.errors}'"
                boxed_log(logger, f"{op}: plate solver failed, {msg=}")
                self.unit.errors.append(f"{op}: plate solver failed, {msg=}")
                filer.move_ram_to_shared(imager_settings.image_path)
                continue  # next try

            else:
                boxed_log(
                    logger,
                    f"phase: {phase.upper()}, plate solver found a match, YEY, YEPEEE, HURRAY !!!",
                )

                # dec_avg_rad = math.radians((target.dec.arcsecond + Angle(result.solution.dec_rads * u.radian).arcsecond) / 2)  # type: ignore
                dec_avg_rad = float(target.dec.radian + result.solution.dec_rads) / 2  # type: ignore
                assert result.solution is not None, f"{op}: result.solution is None"
                # delta_ra_arcsec = (
                #     target.ra.arcsecond - result.solution.ra_hours * 15 * 3600
                # )  # type: ignore

                # Oren's solution to avoid RA wrap-around issues
                delta_ra_deg = (target.ra.deg - result.solution.ra_hours * 15) % 360  # type: ignore
                if delta_ra_deg > 180:
                    delta_ra_deg -= 360
                delta_ra_arcsec = delta_ra_deg * 3600

                # Eran's original delta_ra calculation with cos(dec) correction
                # - Angle(result.solution.ra_rads * u.radian).arcsecond  # type: ignore
                # ) * math.cos(
                #     dec_avg_rad
                # )  # type: ignore
                delta_dec_arcsec = target.dec.arcsecond - Angle(result.solution.dec_rads * u.radian).arcsecond  # type: ignore

                abs_delta_ra_arcsec = abs(delta_ra_arcsec)
                abs_delta_dec_arcsec = abs(delta_dec_arcsec)

                coord_solved = Coord(
                    ra=Angle(result.solution.ra_rads * u.radian),  # type: ignore
                    dec=Angle(result.solution.dec_rads * u.radian),  # type: ignore
                )
                coord_delta = Coord(
                    ra=Angle(delta_ra_arcsec * u.arcsecond),  # type: ignore
                    dec=Angle(delta_dec_arcsec * u.arcsecond),  # type: ignore
                )
                coord_tolerance = Coord(
                    ra=solving_tolerance.ra, dec=solving_tolerance.dec
                )
                logger.info(
                    f"{op}: target: {target}, solved: {coord_solved}, delta: {coord_delta}, "
                    + f"tolerance: {coord_tolerance}"
                )

                # The various solvers will update the FITS file with their findings

                if (
                    abs_delta_ra_arcsec <= solving_tolerance.ra.arcsecond  # type: ignore
                    and abs_delta_dec_arcsec <= solving_tolerance.dec.arcsecond  # type: ignore
                ):
                    #
                    # Within tolerance, no correction is needed
                    #
                    boxed_log(
                        logger,
                        [
                            f"{op}: WITHIN TOLERANCES",
                            f"deltas: ({delta_ra_arcsec:.9f}, {delta_dec_arcsec:.9f}) ",
                            f"tolerance: ({solving_tolerance.ra.arcsecond:.9f}, {solving_tolerance.dec.arcsecond:.9f})",
                        ],
                        center=True,
                    )

                    latest_corrections.last_delta = Correction(
                        time=isoformat_zulu(datetime.datetime.now(datetime.UTC)),
                        ra_delta=delta_ra_arcsec,  # type: ignore
                        dec_delta=delta_dec_arcsec,  # type: ignore
                    )

                    if not imager_settings.folder:
                        raise Exception(
                            f"{function_name()}: empty imager_settings.folder"
                        )
                    file_name = str(Path(imager_settings.folder) / "corrections.json")
                    with open(file_name, "w") as f:
                        f.write(latest_corrections.model_dump_json(indent=2))
                    time.sleep(2)
                    filer.move_ram_to_shared(file_name)

                    self.unit.end_activity(UnitActivities.Solving)
                    return True

                else:
                    #
                    # Outside of tolerance, need to correct
                    #

                    latest_corrections.sequence.append(
                        Correction(
                            time=isoformat_zulu(datetime.datetime.now(datetime.UTC)),
                            ra_delta=delta_ra_arcsec,  # type: ignore
                            dec_delta=delta_dec_arcsec,  # type: ignore
                        )
                    )

                    if phase == "guiding" and not make_corrections:
                        boxed_log(
                            logger,
                            [
                                f"{phase=} and {make_corrections=} -> NOT OFFSETTING BY",
                                f" ({delta_ra_arcsec:.9f}, {delta_dec_arcsec:.9f}) arcsec",
                            ],
                            center=True,
                        )
                    else:
                        boxed_log(
                            logger,
                            f"phase: {phase.upper()}: OFFSETTING BY ({delta_ra_arcsec:.9f}, {delta_dec_arcsec:.9f}) arcsec",
                        )

                        self.unit.start_activity(UnitActivities.Correcting)

                        if approach_mode == 1:
                            logger.info(
                                f"{op}: offsetting mount with mount_offset(ra_add_arcsec={delta_ra_arcsec}, "
                                + f"dec_add_arcsec={delta_dec_arcsec})"
                            )
                            self.unit.pw.mount_offset(
                                ra_add_arcsec=delta_ra_arcsec,
                                dec_add_arcsec=delta_dec_arcsec,
                            )

                        elif approach_mode == 2:

                            if abs_delta_ra_arcsec > 10:
                                # ra_rate_arcsec_per_sec = abs_delta_ra_arcsec * 0.1
                                ra_rate_arcsec_per_sec = abs_delta_ra_arcsec * 0.2
                            elif abs_delta_ra_arcsec > 1:
                                # ra_rate_arcsec_per_sec = 1
                                ra_rate_arcsec_per_sec = 2
                            else:
                                # ra_rate_arcsec_per_sec = 0.1
                                ra_rate_arcsec_per_sec = 0.2

                            if abs_delta_dec_arcsec > 10:
                                # dec_rate_arcsec_per_sec = abs_delta_dec_arcsec * 0.1
                                dec_rate_arcsec_per_sec = abs_delta_dec_arcsec * 0.2
                            elif abs_delta_dec_arcsec > 1:
                                # dec_rate_arcsec_per_sec = 1
                                dec_rate_arcsec_per_sec = 2
                            else:
                                # dec_rate_arcsec_per_sec = 0.1
                                dec_rate_arcsec_per_sec = 0.2

                            logger.info(
                                f"{op}: offsetting mount with mount_offset("
                                + f"ra_add_gradual_offset_arcsec={delta_ra_arcsec}, "
                                + f"ra_gradual_offset_rate={ra_rate_arcsec_per_sec}, "
                                + f"dec_add_gradual_offset_arcsec={delta_dec_arcsec}, "
                                + f"dec_gradual_offset_rate={dec_rate_arcsec_per_sec})"
                            )
                            self.unit.pw.mount_offset(
                                ra_add_gradual_offset_arcsec=delta_ra_arcsec,
                                ra_gradual_offset_rate=ra_rate_arcsec_per_sec,
                                dec_add_gradual_offset_arcsec=delta_dec_arcsec,
                                dec_gradual_offset_rate=dec_rate_arcsec_per_sec,
                            )

                        elif approach_mode == 3:

                            if abs_delta_ra_arcsec > 100:
                                ra_offsetting_seconds = 5
                            elif abs_delta_ra_arcsec > 10:
                                ra_offsetting_seconds = 3
                            else:
                                ra_offsetting_seconds = 2

                            if abs_delta_dec_arcsec > 100:
                                dec_offsetting_seconds = 5
                            elif abs_delta_dec_arcsec > 10:
                                dec_offsetting_seconds = 3
                            else:
                                dec_offsetting_seconds = 2

                            logger.info(
                                f"{op}: offsetting mount with mount_offset("
                                + f"ra_add_gradual_offset_arcsec={delta_ra_arcsec}, "
                                + f"ra_gradual_offset_seconds={ra_offsetting_seconds}, "
                                + f"dec_add_gradual_offset_arcsec={delta_dec_arcsec}, "
                                + f"dec_gradual_offset_seconds={dec_offsetting_seconds})"
                            )
                            self.unit.pw.mount_offset(
                                ra_reset=0,
                                dec_reset=0,
                                ra_add_gradual_offset_arcsec=delta_ra_arcsec,
                                ra_gradual_offset_seconds=ra_offsetting_seconds,
                                dec_add_gradual_offset_arcsec=delta_dec_arcsec,
                                dec_gradual_offset_seconds=dec_offsetting_seconds,
                            )

                        elif approach_mode == 4:

                            if abs_delta_ra_arcsec > 100:
                                ra_rate_arcsec_per_sec = abs_delta_ra_arcsec * 0.2
                            elif abs_delta_ra_arcsec > 10:
                                ra_rate_arcsec_per_sec = abs_delta_ra_arcsec * 0.5
                            else:
                                ra_rate_arcsec_per_sec = 1

                            if abs_delta_dec_arcsec > 100:
                                dec_rate_arcsec_per_sec = abs_delta_dec_arcsec * 0.2
                            elif abs_delta_dec_arcsec > 10:
                                dec_rate_arcsec_per_sec = abs_delta_dec_arcsec * 0.5
                            else:
                                dec_rate_arcsec_per_sec = 1

                            logger.info(
                                f"{op}: offsetting mount with mount_offset("
                                + f"ra_add_arcsec={delta_ra_arcsec}, "
                                + f"ra_set_rate_arcsec_per_sec={ra_rate_arcsec_per_sec}, "
                                + f"dec_add_arcsec={delta_dec_arcsec}, "
                                + f"dec_set_rate_arcsec_per_sec={dec_rate_arcsec_per_sec})"
                            )
                            self.unit.pw.mount_offset(
                                ra_add_arcsec=delta_ra_arcsec,
                                dec_add_arcsec=delta_dec_arcsec,
                                ra_set_rate_arcsec_per_sec=ra_rate_arcsec_per_sec,
                                dec_set_rate_arcsec_per_sec=dec_rate_arcsec_per_sec,
                            )

                        ra_progress = 0
                        dec_progress = 0
                        while ra_progress < 1 or dec_progress < 1:
                            st = self.unit.pw.status()
                            ra_progress = st.mount.offsets.axis0_arcsec.gradual_offset_progress  # type: ignore
                            dec_progress = st.mount.offsets.axis1_arcsec.gradual_offset_progress  # type: ignore
                            logger.info(f"{op}: {ra_progress=}, {dec_progress=}")
                            time.sleep(1)

                        while self.unit.mount.is_moving:
                            logger.info("mount still moving, sleeping 1 second ...")
                            time.sleep(1)

                        self.unit.end_activity(UnitActivities.Correcting)
                        # logger.info(f"{op}: corrected by {delta_ra_arcsec=:.6f}, {delta_dec_arcsec=:.6f}")

        #
        # By now the tries have been exhausted, and we're still not within tolerance
        #

        if phase != "guiding":
            logger.info(
                f"{function_name()}: could not reach tolerances within {max_tries=}"
            )
        self.unit.end_activity(UnitActivities.Solving)
        return False
