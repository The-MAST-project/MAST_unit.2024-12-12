import datetime
import json
import os.path
from pathlib import Path
from typing import TYPE_CHECKING

import astropy.units as u
from astropy.coordinates import Angle

from acquisition import Acquisition, ApproachMode
from common.activities import UnitActivities
from common.asi import ASI_294MM_HEIGHT, ASI_294MM_WIDTH
from common.config import Config
from common.config.rois import RoisConfig, SkyRoiConfig
from common.config.unit import AcquisitionConfig, ToleranceConfig
from common.const import Const
from common.corrections import Correction, Corrections
from common.filer import Filer, MoveGuardian
from common.interfaces.solving import SolverInterface, SolvingResult, SolvingTolerance
from common.mast_logging import get_logger
from common.models.statuses import ImagerSettings
from common.safety import safety_get_sensor
from common.solving import SolverId
from common.utils import Coord, boxed_log, function_name, isoformat_zulu
from mount import SettleMode

logger = get_logger(__name__)
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
        from solvers.mastrometry import MastrometryDotNet
        from solvers.planewave_cli import PlaneWaveCli
        from solvers.planewave_shm import PlaneWaveShm

        self._backend: SolverInterface | None = None

        unit_config = unit.unit_conf or Config().get_unit()
        assert unit_config is not None, f"{function_name()}: unit_config is None"

        solving_config = unit_config.solving
        assert solving_config is not None, f"{function_name()}: solving_config is None"

        if solving_config.method not in solving_config.valid_methods:
            raise ValueError(
                f"invalid solving method '{solving_config.method}', must be one of {solving_config.valid_methods}"
            )

        match solving_config.method:
            case "AstrometryDotNet":
                self._backend = AstrometryDotNet()
            case "MastrometryDotNet":
                self._backend = MastrometryDotNet()
            case "PlaneWaveCli":
                self._backend = PlaneWaveCli()
            case "PlanewaveShm":
                self._backend = PlaneWaveShm()

        if not self._backend:
            raise Exception(f"could not figure out solving backend '{solving_config.method=}'")

        self._initialized = True

    def log_and_store_error(self, message: str):
        logger.error(message)
        self.unit.errors.append(message)

    def solve(self, imager_settings: ImagerSettings, target: Coord, phase: Const.SolvingPhase) -> SolvingResult | None:
        op = function_name()

        assert self._backend is not None, "solve: self._backend is None"

        if self.unit.is_active(UnitActivities.Solving):
            imager_settings.make_file_name()
            if self._backend.name == "mastrometry.net":
                assert imager_settings.image_path is not None, f"{op}: imager_settings.image_path is None"
                imager_settings.image_path = imager_settings.image_path.replace(
                    f"binning={imager_settings.binning}",
                    "binning=1x1",
                )

                if imager_settings.roi is not None:
                    imager_settings.image_path = imager_settings.image_path.replace(
                        f"binned_roi={imager_settings.roi.binned(imager_settings.binning)}",
                        f"binned_roi={ASI_294MM_WIDTH}x{ASI_294MM_HEIGHT}",
                    )

            #
            # Start exposure
            #
            assert self.unit.imager is not None

            #
            # Try to fetch wind-speed from safety system
            #
            wind_speed = safe = reasons_for_not_safe = None
            result = safety_get_sensor("wind-speed", timeout=0.5, max_age=datetime.timedelta(minutes=1))
            if result is not None:
                wind_speed, safe, reasons_for_not_safe = result

            msg = f"{op}: starting {imager_settings.seconds=} acquisition exposure"
            if wind_speed is not None:
                msg += f" (wind_speed={wind_speed} Km/h, {safe=}, reasons={reasons_for_not_safe})"
            logger.info(msg)

            response = self.unit.imager.start_exposure(imager_settings)
            if response and response.failed:
                self.log_and_store_error(f"{op}: could not start acquisition exposure: {response=}")

                return SolvingResult(
                    succeeded=False,
                    errors=[f"could not start exposure ({[response.errors]})"],
                )

            self.unit.imager.wait_for_image_saved()
            return self._backend.solve(unit=self.unit, settings=imager_settings, target=target, phase=phase)

    def solve_and_correct(  # noqa: C901
        self,
        target: Coord,
        approach_mode: ApproachMode,
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

        if self.unit.acquirer is not None and not self.unit.acquirer.latest_acquisition:
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
            elif self.unit.imager is not None:
                sky_roi_config = SkyRoiConfig(
                    sky_x=self.unit.imager.full_frame.x,
                    sky_y=self.unit.imager.full_frame.y,
                    width=self.unit.imager.full_frame.width,
                    height=self.unit.imager.full_frame.height,
                )

            assert self.unit is not None, f"{op}: self.unit is None"
            assert self.unit.unit_conf is not None, f"{op}: self.unit.unit_conf is None"

            conf = AcquisitionConfig(
                exposure=imager_settings.seconds,
                binning=imager_settings.binning,
                rois=RoisConfig({self.unit.fcu_version: sky_roi_config}),
                gain=imager_settings.gain or 100,
                tries=self.unit.unit_conf.acquisition.tries,
                tolerance=tolerance,
            )
            if self.unit.acquirer is None:
                raise Exception(f"{op}: unit.acquirer is None, cannot create latest_acquisition")

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

        if self.unit.acquirer is not None and self.unit.acquirer.latest_acquisition is not None:
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
                result = self.solve(
                    imager_settings=imager_settings, target=target, phase="sky" if phase == "sky" else "spec"
                )
            except TimeoutError:
                self.log_and_store_error("plate solving timed out, continuing ...")
                continue

            self.latest_result = result
            if result is None:
                self.log_and_store_error(f"{op}: plate_solve returned None")
                continue

            if imager_settings.image_path is None:
                raise Exception(f"{op}: imager_settings.image_path is None, cannot save the image")

            # save the solver result for debugging
            result_file_name = imager_settings.image_path.replace(".fits", "-solver_result.json")
            os.makedirs(os.path.dirname(result_file_name), exist_ok=True)
            with MoveGuardian().protect(result_file_name):
                with open(result_file_name, "w") as fp:
                    fp.write(json.dumps(result.to_dict(), indent=2))
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

                # A solved frame leaves the ram disk exactly like a failed one. Only the
                # failure branch used to do this, so every SUCCESSFUL solve stranded its
                # FITS -- the largest artifact of the acquisition -- and nothing else ever
                # moved or reclaimed it. Whether that showed up depended on the solver
                # backend: astrometry_dot_net and planewave_cli move the input frame
                # themselves, mastrometry does not, and on 2026-08-04 mastrometry was the
                # configured one, so the ram disk filled over the night.
                filer.move_ram_to_shared(imager_settings.image_path)

                # dec_avg_rad = math.radians(
                #     (target.dec.arcsecond + Angle(result.solution.dec_rads * u.radian).arcsecond) / 2
                # )
                dec_avg_rad = float(target.dec.radian + result.solution.dec_rads) / 2  # type: ignore  # noqa: F841 -- #191
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
                coord_tolerance = Coord(ra=solving_tolerance.ra, dec=solving_tolerance.dec)
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
                        raise Exception(f"{function_name()}: empty imager_settings.folder")
                    file_name = str(Path(imager_settings.folder) / "corrections.json")
                    with MoveGuardian().protect(file_name):
                        with open(file_name, "w") as f:
                            f.write(latest_corrections.model_dump_json(indent=2))
                        filer.move_ram_to_shared(file_name)

                    self.unit.end_activity(UnitActivities.Solving)
                    return True

                else:
                    #
                    # Outside of tolerance, need to correct
                    #
                    assert self.unit.pw is not None and self.unit.mount is not None

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

                        match approach_mode:
                            case ApproachMode.DISCRETE_STEP:
                                logger.info(
                                    f"{op}: offsetting mount with mount_offset(ra_add_arcsec={delta_ra_arcsec}, "
                                    + f"dec_add_arcsec={delta_dec_arcsec})"
                                )
                                self.unit.pw.mount_offset(
                                    ra_add_arcsec=delta_ra_arcsec,
                                    dec_add_arcsec=delta_dec_arcsec,
                                )

                                # Discrete step: wait for the servo following-
                                # distance to spike then settle (is_moving can't
                                # see a small offset).
                                self.unit.mount.wait_until_settled(SettleMode.OFFSET_STEP)

                            case ApproachMode.GRADUAL_BY_RATE:
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

                                # Gradual ramp: wait for the commanded ra/dec
                                # channels' gradual_offset_progress to reach 1.0
                                # (guards the start-of-ramp race; is_moving can't
                                # see a ramp the servo follows).
                                self.unit.mount.wait_until_settled(SettleMode.OFFSET_GRADUAL, channels=("ra", "dec"))

                            case ApproachMode.GRADUAL_BY_TIME:
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

                                # Gradual ramp: wait for the commanded ra/dec
                                # channels' gradual_offset_progress to reach 1.0
                                # (guards the start-of-ramp race; is_moving can't
                                # see a ramp the servo follows).
                                self.unit.mount.wait_until_settled(SettleMode.OFFSET_GRADUAL, channels=("ra", "dec"))

                            case ApproachMode.STEP_WITH_TRACKING_RATE:
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

                                # Discrete jump + continuous rate: wait for the
                                # discrete step to land; the ongoing set_rate is
                                # servo-followed (small dist) and persists by
                                # design, so it reads as settled.
                                self.unit.mount.wait_until_settled(SettleMode.OFFSET_STEP)

                            case _:
                                logger.error(f"{op}: unknown approach_mode {approach_mode!r}; no offset applied")

                        self.unit.end_activity(UnitActivities.Correcting)
                        # logger.info(f"{op}: corrected by {delta_ra_arcsec=:.6f}, {delta_dec_arcsec=:.6f}")

        #
        # By now the tries have been exhausted, and we're still not within tolerance
        #

        if phase != "guiding":
            logger.info(f"{function_name()}: could not reach tolerances within {max_tries=}")
        self.unit.end_activity(UnitActivities.Solving)
        return False

    @property
    def name(self) -> str:
        assert self._backend is not None, "name: self._backend is None"
        return self._backend.name
