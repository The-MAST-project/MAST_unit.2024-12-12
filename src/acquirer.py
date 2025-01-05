import time
import logging

# from astropy.coordinates.jparser import DEC_REGEX, RA_REGEX

from common.utils import function_name, Coord
from common.mast_logging import init_log
from common.activities import UnitActivities
from common.utils import UnitRoi, CanonicalResponse, boxed_info
from common.solving import SolverIdNames
from common.filer import Filer, FilerTop
from common.parsers import sexagesimal_degrees_to_decimal, sexagesimal_hours_to_decimal
from stage import StagePresetPosition
from camera import CameraSettings, CameraBinning
from astropy.coordinates import Angle
import astropy.units as u
from solving import SolvingTolerance, SolverId
from threading import Thread
from acquisition import Acquisition
import os
from typing import Optional
import datetime
from fastapi import Query
from typing import Annotated

logger = logging.getLogger('mast.unit.' + __name__)
init_log(logger)


class Acquirer:

    def __init__(self, unit: 'Unit'):
        self.unit: 'Unit' = unit
        self.folder: str | None = None
        self.latest_acquisition: Acquisition | None = None

    def do_acquire(self, acquisition: Acquisition):
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
        if not hasattr(acquisition, 'target_ra') or not hasattr(acquisition, 'target_dec'):
            st = self.unit.mount.status()
            acquisition.target_ra = st['ra_j2000_hours ']
            acquisition.target_dec = st['dec_j2000_degs ']
        target_ra_j2000_hours: float = acquisition.target_ra
        target_dec_j2000_degs: float = acquisition.target_dec

        self.unit.start_activity(UnitActivities.Acquiring)

        phase = 'sky'
        boxed_info(logger, [f"starting {phase=}"])

        #
        # move the stage and mount (if needed) into position
        #
        self.unit.start_activity(UnitActivities.Positioning)
        self.unit.stage.move_to_preset(StagePresetPosition.Sky)

        self.unit.mount.start_tracking()
        if self.latest_acquisition.slew_to_target:
            self.unit.mount.goto_ra_dec_j2000(target_ra_j2000_hours, target_dec_j2000_degs)

        while self.unit.stage.is_moving or self.unit.mount.is_moving:
            time.sleep(.2)
        self.unit.end_activity(UnitActivities.Positioning)

        sky_settings = CameraSettings(
            seconds=acquisition_conf['exposure'],
            base_folder=os.path.join(self.latest_acquisition.folder, phase),
            gain=acquisition_conf['gain'],
            binning=CameraBinning(acquisition_conf['binning']['x'], acquisition_conf['binning']['y']),
            roi=UnitRoi.from_dict(acquisition_conf['roi']).to_camera_roi(),
            save=True
        )

        #
        # loop trying to solve and correct the mount till within tolerances
        #
        tries: int = acquisition_conf['tries'] if 'tries' in acquisition_conf else 3

        # set up the tolerances
        default_tolerance: Angle = Angle(1 * u.arcsecond)
        ra_tolerance: Angle = default_tolerance
        dec_tolerance: Angle = default_tolerance
        phase_conf = self.unit.unit_conf['acquisition']
        if 'tolerance' in phase_conf:
            if 'ra_arcsec' in phase_conf['tolerance']:
                ra_tolerance = Angle(phase_conf['tolerance']['ra_arcsec'] * u.arcsecond)
            if 'dec_arcsec' in phase_conf['tolerance']:
                dec_tolerance = Angle(phase_conf['tolerance']['dec_arcsec'] * u.arcsecond)

        target = Coord(ra=Angle(target_ra_j2000_hours * u.hour), dec=Angle(target_dec_j2000_degs * u.deg))

        achieved_tolerances = self.unit.solver.solve_and_correct(target=target,
                                                                 approach_mode=acquisition.approach_mode,
                                                                 solver_id=acquisition.solver_id,
                                                                 make_corrections=acquisition.make_corrections,
                                                                 camera_settings=sky_settings,
                                                                 solving_tolerance=SolvingTolerance(ra_tolerance,
                                                                                                    dec_tolerance),
                                                                 parent_activity=UnitActivities.Acquiring,
                                                                 phase='sky', max_tries=tries)
        logger.info(f"{op}: {phase=} {achieved_tolerances=}")
        self.latest_acquisition.save_corrections(phase)

        if not achieved_tolerances:
            self.unit.end_activity(UnitActivities.Acquiring)
            self.unit.mount.stop_tracking()
            return

        phase = 'spec'
        boxed_info(logger, [f"starting {phase=}"])

        self.unit.stage.move_to_preset(StagePresetPosition.Spec)
        while self.unit.stage.is_moving:
            time.sleep(.2)
        logger.info(f"sleeping additional 5 seconds to let the stage stop moving ...")
        time.sleep(5)
        logger.info(f"stage now at {self.unit.stage.position}")

        phase_conf = self.unit.unit_conf['guiding']
        if 'tolerance' in phase_conf:
            if 'ra_arcsec' in phase_conf['tolerance']:
                ra_tolerance = Angle(phase_conf['tolerance']['ra_arcsec'] * u.arcsecond)
            if 'dec_arcsec' in phase_conf['tolerance']:
                dec_tolerance = Angle(phase_conf['tolerance']['dec_arcsec'] * u.arcsecond)

        spec_settings = self.unit.guider.make_guiding_settings(
            base_folder=os.path.join(self.latest_acquisition.folder, phase))
        achieved_tolerances = self.unit.solver.solve_and_correct(target=target,
                                                                 approach_mode=acquisition.approach_mode,
                                                                 solver_id=acquisition.solver_id,
                                                                 make_corrections=acquisition.make_corrections,
                                                                 camera_settings=spec_settings,
                                                                 solving_tolerance=SolvingTolerance(ra_tolerance,
                                                                                                    dec_tolerance),
                                                                 parent_activity=UnitActivities.Acquiring,
                                                                 phase=phase, max_tries=tries)
        self.latest_acquisition.save_corrections(phase)
        logger.info(f"{op}: {phase=} {achieved_tolerances=}")
        if not achieved_tolerances:
            self.unit.end_activity(UnitActivities.Acquiring)
            self.unit.mount.stop_tracking()
            return

        self.unit.reference_image = self.unit.camera.image

        phase = 'guiding'
        boxed_info(logger, [f"starting {phase=}"])

        # the guider runs until UnitActivities.Guiding is stopped
        # self.unit.guider.do_guide_by_solving_with_shm(
        #     target=target,
        #     approach_mode=acquisition.approach_mode,
        #     folder=os.path.join(self.latest_acquisition.folder, phase)
        # )

        cadence = self.unit.unit_conf['guiding']['cadence_seconds']
        end: datetime.datetime | None = None
        folder = os.path.join(self.latest_acquisition.folder, phase)
        guiding_settings = self.unit.guider.make_guiding_settings(folder)

        self.unit.start_activity(UnitActivities.Guiding)
        while self.unit.is_active(UnitActivities.Guiding):
            start = datetime.datetime.now()
            if cadence:
                end = start + datetime.timedelta(seconds=cadence)
            self.unit.solver.solve_and_correct(target=target, approach_mode=acquisition.approach_mode,
                                               solver_id=acquisition.solver_id,
                                               make_corrections=acquisition.make_corrections,
                                               camera_settings=guiding_settings,
                                               solving_tolerance=SolvingTolerance(ra_tolerance, dec_tolerance),
                                               parent_activity=UnitActivities.Acquiring, phase='guiding')

        self.unit.acquirer.latest_acquisition.save_corrections('guiding')

        if cadence:
            now = datetime.datetime.now()
            if now < end:
                sec = (end - now).seconds
                boxed_info(logger, f"sleeping {sec:.2f} seconds till end-of-cadence ...")
                time.sleep(sec)

        self.unit.end_activity(UnitActivities.Acquiring)
        self.unit.mount.stop_tracking()
        self.unit.acquirer.latest_acquisition.post_process()

    RA_REGEX = r"^(\d{1,2}):(\d{2}):(\d{2}(?:\.\d{1,3})?)$"
    DEC_REGEX = r"^([+-]?)(\d{1,2}):(\d{2}):(\d{2}(?:\.\d{1,3})?)$"

    def start_acquisition_and_guiding(self,
                                      ra_j2000_hours: Annotated[
                                          Optional[str],
                                          Query(
                                              regex=RA_REGEX + r"|^\d{1,2}(\.\d+)?$",
                                              description=(
                                                      "### Right Ascension (J2000) in either:\n"
                                                      "- decimal hours (e.g., `12.5`) or\n"
                                                      "- sexagesimal format (e.g., `12:30:45.123`). \n"
                                                      "- Decimal range: `0 <= RA < 24`."
                                              ),
                                          ),
                                      ] = None,
                                      dec_j2000_degs: Annotated[
                                          Optional[str],
                                          Query(
                                              regex=DEC_REGEX + r"|^[-+]?\d{1,2}(\.\d+)?$",
                                              description=(
                                                      "### Declination (J2000) in either:\n"
                                                      "- decimal degrees (e.g., `-45.5`) or\n"
                                                      "- sexagesimal format (e.g., `-45:30:00.123`). \n"
                                                      "- Decimal range: `-90 <= DEC <= 90`."
                                              ),
                                          ),
                                      ] = None,
                                      approach_mode: int = 2,
                                      solver_name: SolverIdNames = 'AstrometryDotNet',
                                      make_corrections: bool = True,
                                      ):
        """
        Starts an acquisition

        :param approach_mode:
        :param solver_name:
        :param make_corrections:
        :param ra_j2000_hours: The target's RA
        :param dec_j2000_degs: The target's Dec
        :return: The folder path on the MAST-SHARE with the acquisition's products
        """

        if ':' in ra_j2000_hours:
            ra_j2000_hours = sexagesimal_hours_to_decimal(ra_j2000_hours)
        else:
            ra_j2000_hours = float(ra_j2000_hours)

        if ':' in dec_j2000_degs:
            dec_j2000_degs = sexagesimal_degrees_to_decimal(dec_j2000_degs)
        else:
            dec_j2000_degs = float(dec_j2000_degs)

        acquisition = Acquisition(unit=self.unit, approach_mode=approach_mode, solver_id=SolverId[solver_name],
                                  make_corrections=make_corrections, target_ra=ra_j2000_hours,
                                  target_dec=dec_j2000_degs, conf=self.unit.unit_conf['acquisition'])
        Thread(name='acquisition', target=self.do_acquire, args=[acquisition]).start()

        return CanonicalResponse(value=Filer(logger).change_top_to(FilerTop.Shared, acquisition.folder))
