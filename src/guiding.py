import logging
from common.utils import Coord, function_name, CanonicalResponse, CanonicalResponse_Ok
from common.paths import PathMaker
from common.filer import Filer
from common.mast_logging import init_log
from common.activities import UnitActivities
from common.utils import UnitRoi
from camera import CameraSettings, CameraBinning
import astropy.units as u
from astropy.coordinates import Angle
import time
import os
import json
import datetime
import subprocess
from solving import SolvingTolerance, PlaneWaveCliSolverExitCode
import concurrent.futures
from threading import Thread

from skimage.registration import phase_cross_correlation

logger = logging.Logger('mast.unit.' + __name__)
init_log(logger)

guider_address_port = ('127.0.0.1', 8001)


class Guider:

    def __init__(self, unit: 'Unit'):
        self.unit: 'Unit' = unit

    def end_guiding(self):
        self.unit.end_activity(UnitActivities.Guiding)
        logger.info(f'guiding ended')

    def make_guiding_settings(self, base_folder: str | None = None) -> CameraSettings:
        """
        The 'guiding' camera exposure settings are used:
        -In the second acquisition phase (stage at 'spec' position)
        - While guiding

        :param base_folder:
        :return: camera settings for guiding exposures
        """

        guiding_conf = self.unit.unit_conf['guiding']

        h_margin = 300  # right and left
        v_margin = 200  # top and bottom

        unit_roi = UnitRoi.from_dict(guiding_conf['roi'])  # we use only the center and compute the sizes
        unit_roi.width = (min(unit_roi.fiber_x, self.unit.camera.cameraXSize - unit_roi.fiber_x) - h_margin) * 2
        unit_roi.height = (min(unit_roi.fiber_y, self.unit.camera.cameraYSize - unit_roi.fiber_y) - v_margin) * 2

        x_binning = guiding_conf['binning']
        binning: CameraBinning = CameraBinning(x_binning, x_binning)

        return CameraSettings(
            seconds=guiding_conf['exposure'],
            base_folder=base_folder,
            gain=guiding_conf['gain'],
            binning=binning,
            roi=unit_roi.to_camera_roi(binning=binning),
            save=True
        )

    def do_guide_by_solving_with_shm(self, approach_mode: int = 1, target: Coord | None = None, folder: str | None = None):
        """
        If target was supplied, send telescope to 'target', else guide at current mount's coordinates
        Perform guiding (at cadence, while UnitActivities.Guiding was not ended) calling self.solver.solve_and_correct()

        :param target: If supplied send telescope to 'target' before guiding, else guide 'in place'
        :param folder: Where to save the images.  If not supplied, make a new one in 'Guidings'
        """
        op: str = function_name()

        #
        # Target: either given or taken from the mount
        #
        if target is None:
            pw4_status = self.unit.pw.status()
            target = Coord(
                ra=Angle(pw4_status.mount.ra_j2000_hours * u.hour),
                dec=Angle(pw4_status.mount.dec_j2000_degs * u.deg)
            )
            logger.info(f"{op}: guiding at current coordinates {target}")

        #
        # Images folder, either given (part of an acquisition sequence) or under 'Guidings'
        #
        if folder is None:
            folder = PathMaker().make_guidings_folder()

        #
        # Camera settings
        #
        guiding_settings = self.make_guiding_settings(folder)

        #
        # Cadence and tolerances
        #
        guiding_conf = self.unit.unit_conf['guiding']
        cadence: float = guiding_conf['cadence_seconds']
        arc_seconds: float = guiding_conf['tolerance']['ra_arcsec'] if \
            ('tolerance' in guiding_conf and 'ra_arcsec' in guiding_conf['tolerance']) else .3
        tolerance = Angle(arc_seconds * u.arcsec)

        #
        # All is ready, start guiding
        #
        self.unit.start_activity(UnitActivities.Guiding)
        while self.unit.is_active(UnitActivities.Guiding):
            start = datetime.datetime.now()
            if cadence != 0.0:
                end = start + datetime.timedelta(seconds=cadence)
            self.unit.solver.solve_and_correct(target=target,
                                               approach_mode=approach_mode,
                                               camera_settings=guiding_settings,
                                               solving_tolerance=SolvingTolerance(tolerance, tolerance),
                                               phase='guiding',
                                               parent_activity=UnitActivities.Guiding)

            if cadence != 0.0:
                now = datetime.datetime.now()
                if now < end:
                    sec = (end - now).seconds
                    logger.info(f"sleeping {sec} seconds till end-of-cadence ...")
                    time.sleep(sec)

        self.unit.acquirer.latest_acquisition.save_corrections('guiding')

    # def do_guide_by_solving_without_shm(self, base_folder: str | None = None):
    #     def guiding_was_stopped() -> bool:
    #         if not self.unit.is_active(UnitActivities.Guiding):
    #             self.end_guiding()
    #             return True
    #         return False
    #
    #     def stopped_while_waiting_for_next_cycle() -> bool:
    #         """
    #         Sleeps for up to self.unit_conf['guiding']['interval'] seconds, checking every second if
    #          the guiding was stopped (via end_activity(UnitActivities.Guiding))
    #
    #         Returns
    #         -------
    #             True if the guiding was stopped
    #             False if the time expired.
    #
    #         """
    #         # avoid sleeping for a long time, for better agility at sensing that guiding was stopped
    #         start: datetime.datetime = self.unit.timings[UnitActivities.Guiding].start_time
    #         now = datetime.datetime.now()
    #         cadence = datetime.timedelta(seconds=float(self.unit.unit_conf['guiding']['cadence']))
    #         end = start + cadence
    #         elapsed_seconds = (now - start).seconds
    #         if elapsed_seconds > cadence.seconds:
    #             logger.error(f"the guiding cycle took {elapsed_seconds} seconds ({cadence.seconds=}), not sleeping")
    #             return False
    #
    #         remaining_seconds = (end - now).seconds
    #         logger.info(f"done solving cycle, sleeping {remaining_seconds} seconds ...")
    #         while datetime.datetime.now() < end:
    #             if guiding_was_stopped():
    #                 logger.info(f"guiding was stopped, stopped sleeping ")
    #                 return True
    #             time.sleep(1)
    #         return False
    #
    #     filer = Filer()
    #     cmd = 'C:\\Program Files (x86)\\PlaneWave Instruments\\ps3cli\\ps3cli'
    #
    #     op = function_name()
    #     guiding_conf = self.unit.unit_conf['guiding']
    #     guiding_roi: UnitRoi = UnitRoi(
    #         fiber_x=guiding_conf['fiber_x'],
    #         fiber_y=guiding_conf['fiber_y'],
    #         width=self.unit.camera.guiding_roi_width,
    #         height=self.unit.camera.guiding_roi_height
    #     )
    #     binning: CameraBinning = CameraBinning(guiding_conf['binning'], guiding_conf['binning'])
    #     guiding_settings: CameraSettings = CameraSettings(
    #         seconds=guiding_conf['exposure'],
    #         base_folder=base_folder,
    #         gain=guiding_conf['gain'],
    #         binning=binning,
    #         roi=guiding_roi.to_camera_roi(binning=binning),
    #         save=True
    #     )
    #
    #     # TODO: use self.solver.solve_and_correct()
    #
    #     while self.unit.is_active(UnitActivities.Guiding):
    #
    #         # root_path = path_maker.make_guiding_root_name()
    #         # image_path = f"{root_path}image.fits"
    #         # result_path = f"{root_path}result.txt"
    #         # correction_path = f"{root_path}correction.txt"
    #
    #         logger.info(f'{op}: starting {self.unit.unit_conf['guiding']['exposure']} seconds guiding exposure')
    #         response = self.unit.camera.do_start_exposure(guiding_settings)
    #         if response.failed:
    #             logger.error(f"{op}: could not start guiding exposure: {response=}")
    #             return response
    #
    #         time.sleep(1)  # wait for exposure to start
    #         while not self.unit.camera.image_saved_event.wait(1):
    #             if guiding_was_stopped():
    #                 return CanonicalResponse_Ok
    #             time.sleep(2)
    #         self.unit.camera.image_saved_event.clear()
    #
    #         if guiding_was_stopped():
    #             return CanonicalResponse_Ok
    #
    #         try:
    #             pixel_scale = self.unit.unit_conf['camera']['pixel_scale_at_bin1'] * guiding_conf['binning']
    #         except Exception as e:
    #             error = f"could not calculate pixel scale, exception={e}"
    #             logger.error(error)
    #             return CanonicalResponse(errors=[error])
    #
    #         image_path = guiding_settings.image_path
    #         result_path = os.path.join(os.path.dirname(image_path), 'result.txt')
    #         correction_path = os.path.join(os.path.dirname(image_path), 'correction.json')
    #         command = [cmd, image_path, f'{pixel_scale}', result_path, 'C:/Users/mast/Documents/Kepler']
    #         logger.info(f'{op}: image saved, running solver ...')
    #
    #         # result = None
    #         try:
    #             result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, shell=True)
    #             filer.move_ram_to_shared(image_path)
    #         except subprocess.CalledProcessError as e:
    #             # solving failed.  should we maybe move a little?
    #             logger.error(f'{op}: solver return code: {PlateSolverExitCode(e.returncode).__repr__()}')
    #             with open(result_path, 'w') as file:
    #                 file.write(e.stdout.decode())
    #                 filer.move_ram_to_shared(result_path)
    #
    #             # if it's a HARD error (not just NoStarMatch), cannot continue
    #             if e.returncode == PlateSolverExitCode.InvalidArguments or \
    #                     e.returncode == PlateSolverExitCode.CatalogNotFound or \
    #                     e.returncode == PlateSolverExitCode.NoImageLoad or \
    #                     e.returncode == PlateSolverExitCode.GeneralFailure:
    #                 logger.error(f"{op}: solver returned {PlateSolverExitCode(e.returncode).__repr__()}, " +
    #                              f"guiding aborted.")
    #                 self.end_guiding()
    #                 return CanonicalResponse(
    #                     errors=[f"solver failed with {PlateSolverExitCode(e.returncode).__repr__()}"])
    #
    #             if stopped_while_waiting_for_next_cycle():
    #                 return CanonicalResponse_Ok
    #
    #             continue  # to next guiding cycle
    #
    #         # solving succeeded, parse output
    #         if result.returncode == PlateSolverExitCode.Success:
    #             logger.info(f"{op}: solver found a solution")
    #             with open(result_path, 'r') as file:
    #                 lines = file.readlines()
    #         elif result.returncode == PlateSolverExitCode.NoStarMatch:
    #             logger.error(f"{op}: solver did not find a match {result.returncode=}")
    #             if stopped_while_waiting_for_next_cycle():
    #                 return CanonicalResponse_Ok
    #             continue
    #
    #         filer.move_ram_to_shared(result_path)
    #
    #         solver_output = {}
    #         for line in lines:
    #             fields = line.rstrip().split('=')
    #             if len(fields) != 2:
    #                 continue
    #             keyword, value = fields
    #             solver_output[keyword] = float(value)
    #         if 'arcsec_per_pixel' in solver_output:
    #             logger.info(f"{op}: {solver_output['arcsec_per_pixel']=}")
    #
    #         for key in ['ra_j2000_hours', 'dec_j2000_degrees', 'rot_angle_degs']:
    #             if key not in solver_output:
    #                 logger.error(f"{op}: either 'ra_j2000_hours' or 'dec_j2000_degrees' missing in {solver_output=}")
    #                 continue
    #
    #         # rot_angle_degs = solver_output['rot_angle_degs']
    #         # TODO: calculate mount offsets using (camera.offset.x, camera.offset.y) and rot_angle_degs
    #
    #         pw_status = self.unit.pw.status()
    #         mount_ra_hours = pw_status.mount.ra_j2000_hours
    #         mount_dec_degs = pw_status.mount.dec_j2000_degs
    #
    #         solved_ra_hours = solver_output['ra_j2000_hours']
    #         solved_dec_degs = solver_output['dec_j2000_degrees']
    #         # self.pw.mount_model_add_point(solved_ra_hours, solved_dec_degs)
    #
    #         try:
    #             delta_ra_hours = solved_ra_hours - mount_ra_hours    # mind sign and mount offset direction
    #             delta_dec_degs = solved_dec_degs - mount_dec_degs    # ditto
    #             # delta_ra_hours = mount_ra_hours - solved_ra_hours    # mind sign and mount offset direction
    #             # delta_dec_degs = mount_dec_degs - solved_dec_degs    # ditto
    #
    #             delta_ra_arcsec = Angle(delta_ra_hours * u.hour).arcsecond
    #             delta_dec_arcsec = Angle(delta_dec_degs * u.deg).arcsecond
    #         except Exception as e:
    #             return CanonicalResponse(exception=e)
    #
    #         correction = {
    #             'ra': {
    #                 'mount_ra_hours': mount_ra_hours,
    #                 'solved_ra_hours': solved_ra_hours,
    #                 'delta_ra_arcsec': delta_ra_arcsec,
    #                 'min_ra_correction_arcsec': self.unit.min_ra_correction_arcsec,
    #                 'needs_correction': False,
    #             }, 'dec': {
    #                 'mount_dec_degs': mount_dec_degs,
    #                 'solved_dec_degs': solved_dec_degs,
    #                 'delta_dec_arcsec': delta_dec_arcsec,
    #                 'min_dec_correction_arcsec': self.unit.min_dec_correction_arcsec,
    #                 'needs_correction': False,
    #             }
    #         }
    #
    #         try:
    #             if abs(delta_ra_arcsec) >= self.unit.min_ra_correction_arcsec:
    #                 correction['ra']['correction_arcsec'] = delta_ra_arcsec
    #                 correction['ra']['correction_deg'] = Angle(delta_ra_arcsec * u.arcsec).degree
    #                 correction['ra']['correction_sexa'] = (Angle(delta_ra_arcsec * u.arcsec).
    #                                                        to_string(unit='degree', sep=':', precision=3))
    #                 correction['ra']['needs_correction'] = True
    #
    #             if abs(delta_dec_arcsec) >= self.unit.min_dec_correction_arcsec:
    #                 correction['dec']['correction_arcsec'] = delta_dec_arcsec
    #                 correction['dec']['correction_deg'] = Angle(delta_dec_arcsec * u.arcsec).degree
    #                 correction['dec']['correction_sexa'] = (Angle(delta_dec_arcsec * u.arcsec).
    #                                                         to_string(unit='degree', sep=':', precision=3))
    #                 correction['dec']['needs_correction'] = True
    #         except Exception as e:
    #             return CanonicalResponse(exception=e)
    #
    #         with open(correction_path, 'w') as file:
    #             json.dump(correction, file, indent=2)
    #             logger.info(f"saved correction file to {correction_path}")
    #
    #         if correction['ra']['needs_correction'] or correction['dec']['needs_correction']:
    #             logger.info(f'{op}: telling mount to offset by ra={delta_ra_arcsec:.3f}arcsec, ' +
    #                         f'dec={delta_dec_arcsec:.3f}arcsec')
    #             # self.pw.mount_offset(ra_add_arcsec=correction['ra']['correction_arcsec'],
    #             #                      dec_add_arcsec=correction['dec']['correction_arcsec'])
    #             # stat = self.pw.status()
    #             # while stat.mount.is_slewing:
    #             #     time.sleep(.1)
    #             #     stat = self.pw.status()
    #             # time.sleep(5)
    #             # logger.info(f"mount stopped slewing")
    #         else:
    #             logger.info(f"{op}: correction abs({correction['ra']['correction_arcsec']}) " +
    #                         f"< {self.unit.min_ra_correction_arcsec=} " +
    #                         f"and abs({correction['dec']['correction_arcsec']=}) < " +
    #                         f"{self.unit.min_dec_correction_arcsec=} " +
    #                         f"too small , skipped.")
    #
    #         filer.move_ram_to_shared(correction_path)
    #
    #         if stopped_while_waiting_for_next_cycle():
    #             return CanonicalResponse_Ok
    #         # continue looping

    # def start_guiding_by_solving(self, ra_j2000_hours: float, dec_j2000_degs: float):
    #     """
    #     Starts ``guiding`` by periodically exposing, plate-solving and correcting the mount
    #
    #     :mastapi:
    #     """
    #     # if not self.connected:
    #     #     logger.warning('cannot start guiding - not-connected')
    #     #     return
    #
    #     if self.unit.is_active(UnitActivities.Guiding):
    #         return CanonicalResponse(errors=['already guiding'])
    #
    #     pw_stat = self.unit.pw.status()
    #     self.unit.was_tracking_before_guiding = pw_stat.mount.is_tracking
    #     if not self.unit.was_tracking_before_guiding:
    #         self.unit.pw.mount_tracking_on()
    #         logger.info('started mount tracking')
    #
    #     self.unit.start_activity(UnitActivities.Guiding)
    #
    #     executor = concurrent.futures.ThreadPoolExecutor()
    #     executor.thread_names_prefix = 'guiding-executor'
    #     target: Coord = Coord(ra=Angle(ra_j2000_hours * u.hour), dec=Angle(dec_j2000_degs * u.deg))
    #     future = executor.submit(self.do_guide_by_solving_with_shm, target=target, folder=None)
    #     time.sleep(2)
    #     if future.running():
    #         def stop_tracking(_):
    #             self.unit.pw.mount_tracking_off()
    #
    #         if not self.unit.was_tracking_before_guiding:
    #             future.add_done_callback(stop_tracking)
    #         return CanonicalResponse_Ok
    #     else:
    #         return future.result()  # a CanonicalResponse with errors

    def stop_acquisition_and_guiding(self):
        """
        Stops the ``autoguide`` routine

        :mastapi:
        """
        # if not self.connected:
        #     logger.warning('Cannot stop guiding - not-connected')
        #     return

        if not self.unit.is_active(UnitActivities.Acquiring) and not not self.unit.is_active(UnitActivities.Guiding):
            error = "not acquiring or guiding"
            logger.error(error)
            return CanonicalResponse(errors=[error])

        self.unit.end_activity(UnitActivities.Acquiring)
        self.unit.end_activity(UnitActivities.Guiding)

        if not self.unit.was_tracking_before_guiding:
            self.unit.mount.stop_tracking()
            logger.info('stopped tracking')

        return CanonicalResponse_Ok

    @property
    def is_guiding(self) -> bool:
        if not self.unit.connected:
            return False

        return self.unit.is_active(UnitActivities.Guiding)

    # @property
    # def guiding(self) -> bool:
    #     return self.is_guiding()

    # def endpoint_start_guiding_by_cross_correlation(self):
    #     Thread(name='shift-analysis-guider', target=self.do_start_guiding_by_cross_correlation).start()
    #
    # def do_start_guiding_by_cross_correlation(self, base_folder: str | None = None):
    #     """
    #     Uses the last acquisition image as reference and phase_cross_correlation to detect pixel level shifts and
    #      correct them.
    #     """
    #     op = function_name()
    #
    #     self.unit.start_activity(UnitActivities.Guiding)
    #     #
    #     # prepare exposure settings for guiding
    #     #
    #     guiding_conf = self.unit.unit_conf['guiding']
    #
    #     guiding_roi: UnitRoi = UnitRoi(
    #         fiber_x=guiding_conf['fiber_x'],
    #         fiber_y=guiding_conf['fiber_y'],
    #         width=guiding_conf['width'],
    #         height=guiding_conf['height']
    #     )
    #
    #     binning: CameraBinning = CameraBinning(guiding_conf['binning'], guiding_conf['binning'])
    #     guiding_settings: CameraSettings = CameraSettings(
    #         seconds=guiding_conf['exposure'],
    #         base_folder=base_folder,
    #         gain=guiding_conf['gain'],
    #         binning=binning,
    #         roi=guiding_roi.to_camera_roi(binning=binning),
    #         save=True
    #     )
    #
    #     cadence: int = guiding_conf['cadence_seconds']
    #     min_ra_correction_arcsec: float = guiding_conf['min_ra_correction_arcsec']
    #     min_dec_correction_arcsec: float = guiding_conf['min_dec_correction_arcsec']
    #
    #     min_ra_correction = Angle(min_ra_correction_arcsec * u.arcsecond)
    #     min_dec_correction = Angle(min_dec_correction_arcsec * u.arcsecond)
    #     pixel_scale_arcsec = self.unit.unit_conf['camera']['pixel_scale_at_bin1'] * guiding_conf['binning']
    #
    #     if self.unit.reference_image is not None:
    #         logger.info(f"{op}: using existing reference image")
    #         reference_image = self.unit.reference_image
    #     else:
    #         logger.info(f"{op}: taking a reference image {guiding_roi=}")
    #         self.unit.camera.do_start_exposure(guiding_settings)
    #         logger.info(f"{op}: waiting for image ...")
    #         self.unit.camera.wait_for_image_ready()
    #         logger.info(f"{op}: reference image is ready")
    #         reference_image = self.unit.camera.image
    #         logger.info(f"{op}: got reference image from camera")
    #
    #     while self.unit.is_active(UnitActivities.Guiding):   # may be deactivated by stop_guiding()
    #         start = datetime.datetime.now()
    #         end = start + datetime.timedelta(seconds=cadence)
    #
    #         response = self.unit.camera.do_start_exposure(guiding_settings)
    #         if not response.succeeded:
    #             logger.error(f"{op}: failed to start_exposure ({response.errors=}")
    #             time.sleep(cadence)
    #             continue
    #
    #         logger.info(f"{op}: waiting for the image ...")
    #         self.unit.camera.wait_for_image_ready()
    #         logger.info(f"{op}: image is ready")
    #         image = self.unit.camera.image
    #         logger.info(f"{op}: read the image from the camera")
    #
    #         rotation_angle: Angle | None = Angle(self.unit.solver.latest_result.solution.rotation_angle_degs * u.deg) \
    #             if self.unit.solver.latest_result.state == 'found_match' else None
    #         if rotation_angle is None:
    #             raise Exception(f"{op}: missing rotation angle, cannot use cross correlation results")
    #
    #         shifted_pixels, error, phase_diff = phase_cross_correlation(reference_image, image, upsample_factor=1000)
    #         #
    #         # TODO: formula for converting shifted_pixels into (ra,dec), using the rotation angle
    #         #
    #         delta_ra: Angle = Angle(shifted_pixels[1] * pixel_scale_arcsec * u.arcsecond)
    #         delta_dec: Angle = Angle(shifted_pixels[0] * pixel_scale_arcsec * u.arcsecond)
    #
    #         delta_ra = delta_ra if abs(delta_ra.arcsecond) >= min_ra_correction else 0
    #         delta_dec = delta_dec if abs(delta_dec.arcsecond) >= min_dec_correction else 0
    #
    #         if delta_ra or delta_dec:
    #             logger.info(f"{op}: correcting mount by ({delta_ra=}, {delta_dec=} ...")
    #             self.unit.start_activity(UnitActivities.Correcting)
    #             self.unit.pw.mount_offset(ra_add_arcsec=delta_ra.arcsecond, dec_add_arcsec=delta_dec.arcsecond)
    #             while self.unit.mount.is_slewing:
    #                 time.sleep(.5)
    #             self.unit.end_activity(UnitActivities.Correcting)
    #
    #         now = datetime.datetime.now()
    #         if now < end:
    #             time.sleep((end - now).seconds)
