from typing import Optional, List, Literal
from enum import IntFlag
from common.extended_basemodel import ExtendedBaseModel
from common.utils import Coord, function_name, PLATE_SOLVING_SHM_NAME
from common.mast_logging import init_log
from common.filer import Filer
from PlaneWave.ps3cli_client import PS3CLIClient
from multiprocessing.shared_memory import SharedMemory
import numpy as np
from camera import CameraSettings
import datetime
import time
import logging
from solving import SolvingResult, SolvingSolution
from astropy.coordinates import Angle

logger = logging.Logger('planewave_shm')
init_log(logger)


class PlaneWaveShmSolvingSolution(ExtendedBaseModel):
    num_matched_stars: int
    match_rms_error_arcsec: float
    match_rms_error_pixels: float
    center_ra_j2000_rads: float
    center_dec_j2000_rads: float
    matched_arcsec_per_pixel: float
    rotation_angle_degs: float


class PlaneWaveShmSolvingResult(ExtendedBaseModel):
    state: Literal['ready', 'loading', 'extracting', 'matching', 'found_match', 'no_match', 'error', 'unknown']
    error_message: Optional[str] = None
    last_log_message: Optional[str] = None
    num_extracted_stars: Optional[int] = None
    running_time_seconds: Optional[float] = None
    solution: Optional[PlaneWaveShmSolvingSolution] = None


def planewave_shm_solve(unit: 'Unit', settings: CameraSettings, target: Coord) -> SolvingResult:
    op = function_name()
    unit.camera.wait_for_image_ready()

    width = settings.roi.numX
    height = settings.roi.numY
    pixel_scale = unit.unit_conf['camera']['pixel_scale_at_bin1'] * settings.binning.x

    shm = SharedMemory(name=PLATE_SOLVING_SHM_NAME, create=True, size=width * height * 2)
    shared_image = np.ndarray((width, height), dtype=np.uint16, buffer=shm.buf)
    shared_image[:] = unit.camera.image[:]
    ps3_shm_client: PS3CLIClient = PS3CLIClient()

    ps3_shm_client.connect('127.0.0.1', 8998)
    start = datetime.datetime.now()
    timeout_seconds: float = 50
    end = start + datetime.timedelta(seconds=timeout_seconds)
    logger.info(f"{op}: calling ps3_client.begin_platesolve_shm ...")
    ps3_shm_client.begin_platesolve_shm(
        shm_key=PLATE_SOLVING_SHM_NAME,
        height_pixels=settings.roi.numY,
        width_pixels=settings.roi.numX,
        arcsec_per_pixel_guess=pixel_scale,
        enable_all_sky_match=True,
        enable_local_quad_match=True,
        enable_local_triangle_match=True,
        ra_guess_j2000_rads=target.ra.radian,
        dec_guess_j2000_rads=target.dec.radian
    )

    ps3_solver_status: PlaneWaveShmSolvingResult
    while True:
        ps3_solver_status = PlaneWaveShmSolvingResult(**ps3_shm_client.platesolve_status())

        if (ps3_solver_status.state == 'error' or
                ps3_solver_status.state == 'no_match' or
                ps3_solver_status.state == 'found_match'):
            break

        if datetime.datetime.now() >= end:
            ps3_shm_client.platesolve_cancel()
            ps3_solver_status = PlaneWaveShmSolvingResult(**{
                'state': 'error',
                'error_message': f'time out ({timeout_seconds} seconds), cancelled'
            })
            break
        else:
            time.sleep(.1)

    unit.camera.wait_for_image_saved()
    time.sleep(2)
    Filer().move_ram_to_shared(settings.image_path)

    ret: SolvingResult = SolvingResult()
    ret.result = ps3_solver_status
    if ps3_solver_status.state == 'found_match':
        ret.succeeded = True
        ret.solution = SolvingSolution()
        ret.solution.ra_rads = ps3_solver_status.solution.center_ra_j2000_rads
        ret.solution.ra_hours = Angle(ret.solution.ra_rads, unit='radian').hour
        ret.solution.dec_rads = ps3_solver_status.solution.center_dec_j2000_rads
        ret.solution.dec_degs = Angle(ret.solution.dec_rads, unit='radian').degs
        ret.solution.matched_stars = ps3_solver_status.solution.num_matched_stars
        ret.solution.rotation_angle_degs = ps3_solver_status.solution.rotation_angle_degs
    else:
        ret.succeeded = False
        ret.errors = [ps3_solver_status.error_message, ps3_solver_status.last_log_message]

    return ret
