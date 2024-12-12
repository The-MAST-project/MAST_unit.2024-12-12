import logging
from common.mast_logging import init_log
from camera import CameraSettings
from common.utils import Coord, function_name
from common.filer import Filer
import subprocess
import os
from enum import IntFlag
from typing import Optional, List
from common.extended_basemodel import ExtendedBaseModel
from solving import SolvingResult, SolvingSolution
from astropy.coordinates import Angle

logger = logging.Logger('planewave_cli')
init_log(logger)


class PlaneWaveCliSolverExitCode(IntFlag):
    Success = 0,
    InvalidArguments = 1,
    CatalogNotFound = 2,
    NoStarMatch = 3,
    NoImageLoad = 4,
    GeneralFailure = 99


class PlaneWaveCliSolverResult(ExtendedBaseModel):
    succeeded: bool = False
    ra_j2000_hours: Optional[float] = None
    dec_j2000_degrees: Optional[float] = None
    arcsec_per_pixel: Optional[float] = None
    rot_angle_degs: Optional[float] = None
    errors: Optional[List[str]] = []


def planewave_cli_solve(unit: 'Unit', settings: CameraSettings) -> SolvingResult:
    op = function_name()
    ps3_solver_status: PlaneWaveCliSolverResult
    ret = SolvingResult()

    unit.camera.wait_for_image_saved()

    pixel_scale = unit.unit_conf['camera']['pixel_scale_at_bin1'] * settings.binning.x

    cmd = 'C:\\Program Files (x86)\\PlaneWave Instruments\\ps3cli\\ps3cli'
    image_path = settings.image_path
    result_path = os.path.join(os.path.dirname(image_path), 'result.txt')
    command = [cmd, image_path, f'{pixel_scale}', result_path, 'C:/Users/mast/Documents/Kepler']
    logger.info(f'{op}: image saved, running solver ...')

    # result = None
    completed_process: subprocess.CompletedProcess | None = None
    try:
        completed_process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, shell=True)
        Filer().move_ram_to_shared(image_path)
    except subprocess.CalledProcessError as e:
        logger.error(f'{op}: solver return code: {PlaneWaveCliSolverExitCode(e.returncode).__repr__()}')
        with open(result_path, 'w') as file:
            file.write(e.stdout.decode())
            Filer().move_ram_to_shared(result_path)

        # if it's a HARD error (not just NoStarMatch), cannot continue
        if e.returncode == PlaneWaveCliSolverExitCode.InvalidArguments or \
                e.returncode == PlaneWaveCliSolverExitCode.CatalogNotFound or \
                e.returncode == PlaneWaveCliSolverExitCode.NoImageLoad or \
                e.returncode == PlaneWaveCliSolverExitCode.GeneralFailure:
            logger.error(f"{op}: solver returned {PlaneWaveCliSolverExitCode(e.returncode).__repr__()}, " +
                         f"guiding aborted.")

            ret.succeeded = False
            ret.errors=[f"solver failed with {PlaneWaveCliSolverExitCode(e.returncode).__repr__()}"]
            return ret

    # solving succeeded, parse output
    if completed_process.returncode == PlaneWaveCliSolverExitCode.Success:
        logger.info(f"{op}: solver found a solution")
        with open(result_path, 'r') as file:
            solver_output_lines = file.readlines()

    elif completed_process.returncode == PlaneWaveCliSolverExitCode.NoStarMatch:
        logger.error(f"{op}: solver did not find a match {completed_process.returncode=}")

        ret.succeeded = False
        ret.errors=[f"solver did not find a match {completed_process.returncode=}"]
        return ret

    Filer().move_ram_to_shared(result_path)

    solver_output = {}
    # parse the solver output
    for line in solver_output_lines:
        fields = line.rstrip().split('=')
        if len(fields) != 2:
            continue
        keyword, value = fields
        solver_output[keyword] = float(value)

    if 'arcsec_per_pixel' in solver_output:
        logger.info(f"{op}: {solver_output['arcsec_per_pixel']=}")

    for key in ['ra_j2000_hours', 'dec_j2000_degrees', 'rot_angle_degs']:
        if key not in solver_output:
            logger.error(f"{op}: either 'ra_j2000_hours' or 'dec_j2000_degrees' missing in {solver_output=}")
            continue

    ret = SolvingResult()
    ret.succeeded = True
    ret.result = solver_output
    solution = SolvingSolution()
    solution.ra_hours = solver_output['ra_j2000_hours']
    solution.ra_rads = Angle(solution.ra_hours, unit='hour').radian
    solution.dec_degs = solver_output['dec_j2000_degrees']
    solution.dec_rads = Angle(solution.dec_degs, unit='degree').radian
    solution.rotation_angle_degs = solver_output['rot_angle_degs']
    solution.matched_stars = solver_output
    ret.solution = solution
    return ret
