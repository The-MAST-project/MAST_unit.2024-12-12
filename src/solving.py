import math
import os.path

from common.utils import function_name, Coord, boxed_info
from common.mast_logging import init_log
from common.filer import Filer
from acquisition import Acquisition
import logging
import time
from typing import List, Optional
from camera import CameraSettings
from common.activities import UnitActivities
from common.corrections import Corrections, Correction
from common.solving import SolverId
from astropy.coordinates import Angle
import astropy.units as u
import datetime
import json

logger = logging.Logger("mast.unit." + __name__)
init_log(logger)
filer = Filer(logger)


class SolvingSolution:
    ra_rads: Optional[float] = None
    dec_rads: Optional[float] = None
    ra_hours: float = 0.0
    dec_degs: float = 0.0
    matched_stars: int = 0
    catalog_stars: int = 0
    rotation_angle_degs: Optional[float] = None
    pixel_scale: Optional[float] = None

    def to_dict(self):
        return {
            "ra_rads": self.ra_rads,
            "dec_rads": self.dec_rads,
            "ra_hours": self.ra_hours,
            "dec_degs": self.dec_degs,
            "matched_stars": self.matched_stars,
            "catalog_stars": self.catalog_stars,
            "rotation_angle_degs": self.rotation_angle_degs,
            "pixel_scale": self.pixel_scale,
        }


class SolvingResult:

    succeeded: bool
    errors: Optional[List[str]] = None
    solution: SolvingSolution
    native_result = None

    def __init__(
        self,
        succeeded: bool,
        errors: Optional[List[str]] = None,
        solution: Optional[SolvingSolution] = None,
        native_result=None,
    ):
        self.succeeded = succeeded
        self.errors = errors
        self.solution = solution
        self.native_result = native_result

    def to_dict(self):
        return {
            "succeeded": self.succeeded,
            "errors": self.errors,
            "solution": self.solution.to_dict() if self.solution else None,
            "native_result": (
                self.native_result.to_dict() if self.native_result else None
            ),
        }


class SolvingTolerance:
    ra: Angle
    dec: Angle

    def __init__(self, ra: Angle, dec: Angle):
        self.ra = ra
        self.dec = dec


class Solver:

    def __init__(self, unit: "Unit"):  # type: ignore[name]
        self.unit: "Unit" = unit  # type: ignore[name]
        self.latest_result: SolvingResult | None = None

    def plate_solve(
        self, settings: CameraSettings, target: Coord, solver_id: SolverId
    ) -> SolvingResult:
        op = function_name()

        while self.unit.is_active(UnitActivities.Solving):

            ret: SolvingResult | None = None

            settings.make_file_name()

            #
            # Start exposure
            #
            logger.info(f"{op}: starting {settings.seconds=} acquisition exposure")
            response = self.unit.camera.do_start_exposure(settings)
            if response.failed:
                self.log_and_store_error(
                    f"{op}: could not start acquisition exposure: {response=}"
                )

                ret = SolvingResult(succeeded=False)
                ret.errors = [f"could not start exposure ({[response.errors]})"]
                return ret

            if settings.binning.x != settings.binning.y:
                raise Exception(
                    f"cannot deal with non-equal horizontal and vertical binning "
                    + f"({settings.binning.x=}, {settings.binning.y=}"
                )

            from solvers.planewave_shm import planewave_shm_solve
            from solvers.planewave_cli import planewave_cli_solve
            from solvers.astrometry_dot_net import astrometry_dot_net_solve

            solvers_dispatch = {
                SolverId.PlaneWaveCli: planewave_cli_solve,
                SolverId.PlaneWaveShm: planewave_shm_solve,
                SolverId.AstrometryDotNet: astrometry_dot_net_solve,
            }

            if solver_id not in solvers_dispatch:
                logger.error(f"No dispatcher for {solver_id=}")
            else:
                return solvers_dispatch[solver_id](self.unit, settings, target)

    def solve_and_correct(
        self,
        target: Coord,
        approach_mode: int,
        solver_id: SolverId,
        make_corrections: bool,
        camera_settings: CameraSettings,
        solving_tolerance: SolvingTolerance,
        parent_activity: Optional[UnitActivities] = None,
        phase: Optional[str] = None,
        max_tries: int = 3,
    ) -> bool:
        """
        Tries for max_tries times to:
        - Take an exposure using camera_settings
        - Plate solve the image
        - If the solved coordinates are NOT within the solving_tolerance from the target, correct the mount


        :param target: (ra, dec) tuple
        :param approach_mode:
        :param make_corrections:
        :param solver_id:
        :param camera_settings: Camera settings for the exposure
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
                parent_activity and not self.unit.is_active(parent_activity)
            )

        self.unit.start_activity(UnitActivities.Solving)

        if not self.unit.acquirer.latest_acquisition:
            # when not part of an acquisition sequence
            self.unit.acquirer.latest_acquisition = Acquisition(
                self.unit,
                approach_mode=approach_mode,
                solver_id=solver_id,
                make_corrections=make_corrections,
                target_ra=target.ra.arcsecond,
                target_dec=target.dec.arcsecond,
                conf={
                    "tolerance": {
                        "ra_arcsec": solving_tolerance.ra.arcsecond,
                        "dec_arcsec": solving_tolerance.dec.arcsecond,
                    }
                },
            )

            self.unit.acquirer.latest_acquisition.corrections = {}

        if phase not in self.unit.acquirer.latest_acquisition.corrections:
            # in case there were no corrections yet for this phase
            self.unit.acquirer.latest_acquisition.corrections[phase] = Corrections(
                phase=phase,
                target_ra=target.ra.hour,
                target_dec=target.dec.deg,
                tolerance_ra=solving_tolerance.ra.arcsecond,
                tolerance_dec=solving_tolerance.dec.arcsecond,
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
            # logger.info(f"{op}: calling plate_solve ...")

            # run the plate solver
            try:
                result = self.plate_solve(
                    settings=camera_settings, target=target, solver_id=solver_id
                )
            except TimeoutError:
                self.log_and_store_error(f"plate solving timed out, continuing ...")
                continue

            self.latest_result = result
            if result is None:
                self.log_and_store_error(f"{op}: plate_solve returned None")
                continue

            # save the solver result for debugging
            result_file_name = camera_settings.image_path.replace(
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
                boxed_info(logger, f"{op}: plate solver failed, {msg=}")
                self.unit.errors.append(f"{op}: plate solver failed, {msg=}")
                filer.move_ram_to_shared(camera_settings.image_path)
                continue  # next try

            else:
                boxed_info(
                    logger,
                    f"phase: {phase.upper()}, plate solver found a match, YEY, YEPEEE, HURRAY !!!",
                )
                solved_ra_arcsec: float = Angle(
                    result.solution.ra_rads * u.radian
                ).arcsecond
                solved_dec_arcsec: float = Angle(
                    result.solution.dec_rads * u.radian
                ).arcsecond

                delta_dec_arcsec: float = target.dec.arcsecond - solved_dec_arcsec
                ang_rad: float = Angle(
                    ((target.dec.arcsecond + solved_dec_arcsec) / 2) * u.arcsecond
                ).radian
                delta_ra_arcsec: float = (
                    target.ra.arcsecond - solved_ra_arcsec
                ) * math.cos(ang_rad)

                abs_delta_ra_arcsec = abs(delta_ra_arcsec)
                abs_delta_dec_arcsec = abs(delta_dec_arcsec)

                coord_solved = Coord(
                    ra=Angle(result.solution.ra_rads * u.radian),
                    dec=Angle(result.solution.dec_rads * u.radian),
                )
                coord_delta = Coord(
                    ra=Angle(delta_ra_arcsec * u.arcsecond),
                    dec=Angle(delta_dec_arcsec * u.arcsecond),
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
                    abs_delta_ra_arcsec <= solving_tolerance.ra.arcsecond
                    and abs_delta_dec_arcsec <= solving_tolerance.dec.arcsecond
                ):
                    #
                    # Within tolerance, no correction is needed
                    #
                    boxed_info(
                        logger,
                        [
                            f"{op}: WITHIN TOLERANCES",
                            f"deltas: ({delta_ra_arcsec:.9f}, {delta_dec_arcsec:.9f}) ",
                            f"tolerance: ({solving_tolerance.ra.arcsecond:.9f}, {solving_tolerance.dec.arcsecond:.9f})",
                        ],
                        center=True,
                    )

                    latest_corrections.last_delta = Correction(
                        time=datetime.datetime.now(datetime.UTC),
                        ra_arcsec=delta_ra_arcsec,
                        dec_arcsec=delta_dec_arcsec,
                    )

                    file_name = os.path.join(camera_settings.folder, "corrections.json")
                    with open(file_name, "w") as f:
                        json.dump(latest_corrections.to_dict(), f, indent=2)
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
                            time=datetime.datetime.now(datetime.UTC),
                            ra_arcsec=delta_ra_arcsec,
                            dec_arcsec=delta_dec_arcsec,
                        )
                    )

                    if phase == "guiding" and not make_corrections:
                        boxed_info(
                            logger,
                            [
                                f"{phase=} and {make_corrections=} -> NOT OFFSETTING BY",
                                f" ({delta_ra_arcsec:.9f}, {delta_dec_arcsec:.9f}) arcsec",
                            ],
                            center=True,
                        )
                    else:
                        boxed_info(
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
                            ra_progress = (
                                st.mount.offsets.axis0_arcsec.gradual_offset_progress
                            )
                            dec_progress = (
                                st.mount.offsets.axis1_arcsec.gradual_offset_progress
                            )
                            logger.info(f"{op}: {ra_progress=}, {dec_progress=}")
                            time.sleep(1)

                        while self.unit.mount.is_moving:
                            logger.info(f"mount still moving, sleeping 1 second ...")
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

    def log_and_store_error(self, message: str):
        logger.error(message)
        self.unit.errors.append(message)
