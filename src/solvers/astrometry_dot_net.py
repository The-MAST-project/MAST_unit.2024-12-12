import datetime
import os.path
import sys

from camera import CameraSettings
from common.utils import Coord, generate_random_string
from common.filer import Filer
from common.mast_logging import init_log
import logging
from solving import SolvingSolution, SolvingResult
from typing import List
from astropy.coordinates import Angle
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
    elif path.startswith('D:') or path.startswith('d:'):
        path = path.replace('D:', r'/cygwin/d').replace('d:', r'/cygwin/d')
    return path.replace('\\', '/')


def cygwin_to_win(path: str) -> str:
    return path.replace('/cygdrive/d', 'D:').replace('/', '\\')


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
    tmp_dir = generate_random_string(prefix='tmp_')
    win_tmp_dir = 'D:\\MAST\\tmp\\' + tmp_dir
    os.mkdir(win_tmp_dir)
    index_dir = r'd:\Astrometry.net\indexes'
    solver_name = 'astrometry'

    pixel_scale = 0.262
    index_file = None
    # Series 5 index file ranges (in arcseconds per pixel)
    index_files = {
        "index-5004": (0.5, 0.7),
        "index-5005": (0.7, 1.0),
        "index-5006": (1.0, 1.4),
        "index-5007": (1.4, 2.0),
        "index-5008": (2.0, 2.8),
        "index-5009": (2.8, 4.0),
        "index-5010": (4.0, 5.7),
        "index-5011": (5.7, 8.0),
        "index-5012": (8.0, 11.3),
        "index-5013": (11.3, 16.0),
        "index-5014": (16.0, 22.6),
        "index-5015": (22.6, 32.0),
    }
    # Find the appropriate index file
    for index, (min_arcsec, max_arcsec) in index_files.items():
        if min_arcsec <= pixel_scale < max_arcsec:
            index_file = f"{index}.fits"
            break
    # index_file = '/cygdrive/d/Astrometry.net/indexes/index-5202-01.fits'

    os.environ['PATH'] = 'C:\\cygwin64\\bin;/usr/lib/lapack;C:\\Users\\mast\\PycharmProjects\\MAST_unit\\venv\\Scripts;C:\\Windows\\system32;C:\\Windows;C:\\Windows\\System32\\Wbem;C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\;C:\\Windows\\System32\\OpenSSH\\;C:\\Users\\mast\\Documents\\PlaneWave\\ps3cli;C:\\Program Files\\Git\\cmd;C:\\Users\\mast\\Downloads\\nssm\\nssm-2.24\\win64;C:\\Program Files\\MongoDB\\Server\\7.0\\bin;C:\\Users\\mast\\AppData\\Local\\Programs\\Python\\Launcher\\;C:\\Users\\mast\\AppData\\Local\\Microsoft\\WindowsApps;C:\\Program Files\\JetBrains\\PyCharm Community Edition 2024.1\\bin;;C:\\Users\\mast\\PycharmProjects\\MAST_unit\\src\\Standa\\ximc-2.13.6\\ximc\\win64;'

    cmd = ''
    args = []
    args += ['--scale-units', 'arcsecperpix']
    args += ['--scale-low', '0.25']
    args += ['--scale-high', '0.27']
    args += ['--ra', f"{target.ra.deg}"]
    args += ['--dec', f"{target.dec.value}"]
    args += ['--radius', f"{1}"]
    args += ['--no-plots', '--overwrite', '--solved', 'none']
    args += ['--match', 'none', '--rdls', 'none', '--corr', 'none']
    if index_file:
        args += ['--index-file', index_file]
    fits = settings.image_path
    new_fits = fits.replace('.fits', f",solver={solver_name}.fits")

    if unix_emulator == 'cygwin':
        cmd = r'C:\cygwin64\usr\local\astrometry\bin\solve-field'
        args += ['--dir', '/cygdrive/d/' + tmp_dir]
        args += ['--temp-dir', '/cygdrive/d/' + tmp_dir]
        # args += ['--index-dir', '/cygdrive/d/Astrometry.net/indexes']
        args += ['--new-fits', win_to_cygwin(new_fits)]
        args += [win_to_cygwin(fits)]

    elif unix_emulator == 'wsl':
        cmd = r'\\wsl$\usr\local\astrometry\bin\solve-field'
        args += ['--dir', win_to_wsl(tmp_dir)]
        args += ['--temp-dir', win_to_wsl(tmp_dir)]
        args += ['--index-dir', win_to_wsl(index_dir)]
        args += ['--new-fits', win_to_wsl(new_fits)]
        args += [win_to_wsl(fits)]

    # logger.info(f"cmd: {cmd}, args: {args}")

    start = datetime.datetime.now()
    completed_process = subprocess.run(' '.join([cmd] + args), capture_output=True,shell=True)
    stdout_lines = completed_process.stdout.decode().strip().splitlines()
    stderr_lines = completed_process.stderr.decode().strip().splitlines()
    elapsed = datetime.datetime.now() - start
    logger.info(f"{'succeeded' if completed_process.returncode == 0 else 'failed'} in {elapsed.seconds:.3f} seconds")

    result_file = cygwin_to_win(new_fits).replace('.fits', '-result.txt')
    with open(result_file, 'w') as file:
        file.write('--- command ---\n')
        file.write(' '.join([cmd] + args) + '\n')
        file.write('--- stdout ---\n')
        for line in stdout_lines:
            file.writelines(line + '\n')
        file.write('--- stderr ---\n')
        for line in stderr_lines:
            file.writelines(line + '\n')

    Filer().move_ram_to_shared([result_file, cygwin_to_win(new_fits)])

    if completed_process.returncode == 0:
        ret = _parse_solver_output(stdout_lines)
    else:
        ret = SolvingResult(succeeded=False, errors=[f"Exit status: {completed_process.returncode}", stderr_lines])

    shutil.rmtree(win_tmp_dir)

    return ret


if __name__ == '__main__':
    camera_settings: CameraSettings = CameraSettings(
        seconds=5,
        image_path='/cygdrive/d/MAST/tmp/2024-12-05/Acquisitions/seq=0025,time=18-13-03_987,' +
                   r'target=1.42677311977099,23.5115091209584/guiding/seq=0001,time=18-22-25_715,seconds=5.0,' +
                   r'binning=1x1,gain=170.0,roi=x=300,y=1476,w=7402,h=3968.fits'
    )
    target = Coord(ra=Angle(1.42677311977099, unit='hour'), dec=Angle(23.5115091209584, unit='deg'))
    astrometry_dot_net_solve(None, camera_settings, target=target)
    print('Done.')
    sys.exit(0)
