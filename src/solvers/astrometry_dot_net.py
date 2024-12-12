from camera import CameraSettings
from common.utils import Coord, generate_random_string
from common.filer import Filer
from common.mast_logging import init_log
import logging
from solving import SolvingSolution, SolvingResult
from typing import List
from astropy.coordinates import Angle
import tempfile
import subprocess
import re
import shutil

logger = logging.Logger('astrometry_dot_net')
init_log(logger)


class AstrometryDotNetSolverResult:
    index_file: str


def win_to_cygwin(path: str) -> str:
    if path.startswith('C:') or path.startswith('c:'):
        path = path.replace('C:', r'/cygwin/c').replace('c:', r'/cygwin/c')
    elif path.startswith('C:') or path.startswith('c:'):
        path = path.replace('D:', r'/cygwin/d').replace('d:', r'/cygwin/d')
    return path.replace('\\', '/')


def win_to_wsl(path: str) -> str:
    if path.startswith('C:') or path.startswith('c:'):
        path = path.replace('C:', r'/mnt/c').replace('c:', r'/mnt/c')
    elif path.startswith('C:') or path.startswith('c:'):
        path = path.replace('D:', r'/mnt/d').replace('d:', r'/mnt/d')
    return path.replace('\\', '/')


def _parse_solver_output(lines: List[str]) -> SolvingResult:

    #
    # Sample astrometry.net output:
    #   log-odds ratio 119.57 (8.48588e+51), 17 match, 0 conflict, 0 distractors, 103 index.    # (4)
    #   RA,Dec = (28.7753,20.9374), pixel scale 0.262399 arcsec/pix.
    #   Hit/miss:   Hit/miss: +++++++++++++++++(best)++++
    # Field 1: solved with index index-5202-00.fits.                                            # (3)
    # Field: /cygdrive/d/MAST/2024-12-05/Acquisitions/seq=0026,time=18-40-10_655,target=1.91073447044923,
    #   20.8072722891371/sky/seq=0001,time=18-40-12_083,seconds=5,binning=1x1,gain=170,
    #   roi=x=5200,y=1900,w=3000,h=3000.fits
    # Field center: (RA,Dec) = (28.775317, 20.937344) deg.                                      # (5)
    # Field center: (RA H:M:S, Dec D:M:S) = (01:55:06.076, +20:56:14.439).                      # (2)
    # Field size: 13.1336 x 13.1196 arcminutes
    # Field rotation angle: up is 116.172 degrees E of N                                        # (1)

    ret = SolvingResult(succeeded=True)
    ret.solution = SolvingSolution()
    ret.native_result = AstrometryDotNetSolverResult()

    for line in lines:
        if line.startswith('Field rotation angle'):                             # (1)
            match = re.match(r'^.* is (\d+) degrees,', line)
            if match:
                ret.solution.rotation_angle_degs = float(match.group(1))

        elif line.startswith('Field center: (RA,Dec) ='):                       # (2)
            match = re.match(r' = [(](\d+), (\d+)[)]', line)
            if match:
                ra_degs = float(match.group(1))
                dec_degs = float(match.group(2))
                ret.solution.ra_rads = Angle(ra_degs, unit='degs').radian
                ret.solution.dec_rads = Angle(dec_degs, unit='degs').radian
                ret.solution.ra_hours = Angle(ra_degs, unit='degs').hour
                ret.solution.dec_degs = dec_degs

        elif line.startswith('Field 1: solved with index'):                     # (3)
            match = re.match(r'^.*[)], (\d+) match,', line)
            if match:
                ret.native_result.index_file = float(match.group(1))

        elif line.startswith('  log-odds ratio'):                               # (4)
            match = re.match(r'^.*[)], (\d+) match,', line)
            if match:
                ret.solution.matched_stars = float(match.group(1))

        elif line.startswith('  RA,Dec = '):                                    # (5)
            match = re.match(r'^.*pixel scale (\d+) arcsec', line)
            if match:
                ret.solution.pixel_scale = float(match.group(1))

        return ret


def astrometry_dot_net_solve(unit: 'Unit', settings: CameraSettings, target: Coord) -> SolvingResult:
    unix_emulator = 'cygwin'
    tmp = tempfile.mkdtemp(prefix=generate_random_string('tmp_', 10), dir='D:\tmp')
    index_dir = r'd:\Astrometry.net\indexes'
    solver_name = 'astrometry'

    cmd = ''
    args = []
    args += ['--scale-units', 'arcsecperpix']
    args += ['--scale-low', '0.25']
    args += ['--scale-high', '0.27']
    args += ['--ra', f"{Angle(target.ra, unit='hour').degs}"]
    args += ['--dec', target.dec.value]
    args += ['--radius', 1]
    args += ['--no-plots', '--overwrite', '--solved', 'none']
    args += ['--match', 'none', '--rdls', 'none', '--corr', 'none']
    fits = settings.image_path
    new_fits = fits.replace('.fits', f",solver={solver_name}.fits")

    if unix_emulator == 'cygwin':
        cmd = r'C:\cygwin64\usr\local\astrometry\bin\solve-field'
        args += ['--dir', win_to_cygwin(tmp)]
        args += ['--temp-dir', win_to_cygwin(tmp)]
        args += ['--index-dir', win_to_cygwin(index_dir)]
        args += ['--new-fits', win_to_cygwin(new_fits)]
        args += [win_to_cygwin(fits)]

    elif unix_emulator == 'wsl':
        cmd = r'\\wsl$\usr\local\astrometry\bin\solve-field'
        args += ['--dir', win_to_wsl(tmp)]
        args += ['--temp-dir', win_to_wsl(tmp)]
        args += ['--index-dir', win_to_wsl(index_dir)]
        args += ['--new-fits', win_to_wsl(new_fits)]
        args += [win_to_wsl(fits)]

    logger.info(f"cmd: {cmd}, args: {args}")

    completed_process = subprocess.run([cmd] + args, capture_output=True)
    stdout_lines = completed_process.stdout.decode().strip().splitlines()
    stderr_lines = completed_process.stderr.decode().strip().splitlines()

    result_file = new_fits.replace('.fits', '-result.txt')
    with open(result_file, 'w') as file:
        file.write('--- command ---')
        file.write(cmd)
        file.write('--- stdout ---')
        file.writelines(stdout_lines)
        file.write('--- stderr ---')
        file.writelines(stderr_lines)

    Filer().move_ram_to_shared([result_file, new_fits])

    if completed_process.returncode == 0:
        ret = _parse_solver_output(stdout_lines)
    else:
        ret = SolvingResult(succeeded=False, errors=[f"Exit status: {completed_process.returncode}", stderr_lines])

    shutil.rmtree(tmp)

    return ret
