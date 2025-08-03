import datetime
import logging
import os
import re
import shutil
import subprocess
import sys

# from typing import TYPE_CHECKING
from astropy.coordinates import Angle

from common.filer import Filer
from common.interfaces.solving import SolverInterface, SolvingResult, SolvingSolution
from common.mast_logging import init_log
from common.utils import Coord, boxed_info, function_name, generate_random_string
from imagers import ImagerSettings

logger = logging.Logger("astrometry_dot_net")
init_log(logger)

# if TYPE_CHECKING:
#     from unit import Unit  # type: ignore[import-untyped]

class AstrometryDotNetSolverResult:
    index_file: str = ""

    def to_dict(self):
        return {
            "index_file": self.index_file,
        }


def win_to_cygwin(path: str) -> str:
    if path.startswith("C:") or path.startswith("c:"):
        path = path.replace("C:", r"/cygdrive/c").replace("c:", r"/cygdrive/c")
    elif path.startswith("D:") or path.startswith("d:"):
        path = path.replace("D:", r"/cygdrive/d").replace("d:", r"/cygdrive/d")
    return path.replace("\\", "/")


def cygwin_to_win(path: str) -> str:
    # return path.replace('/cygdrive/d', 'D:').replace('/', '\\')
    return path.replace("/cygdrive/d", "D:")


def win_to_wsl(path: str) -> str:
    if path.startswith("C:") or path.startswith("c:"):
        path = path.replace("C:", r"/mnt/c").replace("c:", r"/mnt/c")
    elif path.startswith("C:") or path.startswith("c:"):
        path = path.replace("D:", r"/mnt/d").replace("d:", r"/mnt/d")
    return path.replace("\\", "/")


def _parse_solver_output(lines: list[str]) -> SolvingResult:  # noqa: C901
    op = function_name()

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

    ret = SolvingResult(succeeded=False)
    ret.solution = SolvingSolution()
    ret.native_result = AstrometryDotNetSolverResult()
    pattern_float = r"[-+]?\d+(\.\d+)?"
    pattern_int = r"\b\d+\b"

    try:
        for line in lines:
            if line.startswith("Field rotation angle"):  # (1)
                match = re.match(r"^.* is (" + pattern_float + r") degrees", line)
                if match:
                    ret.solution.rotation_angle_degs = float(match.group(1))
                    # logger.info(f"{ret.solution.rotation_angle_degs=}")
                else:
                    logger.error("bad match for ret.solution.rotation_angle_degs")

            elif line.startswith("Field center: (RA,Dec) ="):  # (2)
                match = re.match(
                    r".*[(](" + pattern_float + r"), (" + pattern_float + r")[)].*",
                    line,
                )
                if match:
                    ra_degs = float(match.group(1))
                    dec_degs = float(match.group(3))
                    # logger.info(f"{ra_degs=}, {dec_degs=}")
                    ret.solution.ra_rads = float(Angle(ra_degs, unit="deg").radian) # type: ignore[assignment]
                    ret.solution.dec_rads = float(Angle(dec_degs, unit="deg").radian) # type: ignore[assignment]
                    ret.solution.ra_hours = Angle(ra_degs, unit="deg").hour # type: ignore[assignment]
                    ret.solution.dec_degs = dec_degs
                else:
                    logger.error("bad match for ra_degs, dec_degs")

            elif line.startswith("Field 1: solved with index"):  # (3)
                ret.succeeded = True
                match = re.match(r"^.*solved with index (.*)\.$", line)
                if match:
                    ret.native_result.index_file = match.group(1)
                    # logger.info(f"{ret.native_result.index_file=}")
                else:
                    logger.error("bad match for ret.native_result.index_file")

            elif line.startswith("  log-odds ratio"):  # (4)
                match = re.match(r"^.*[)], (\d+) match,", line)
                if match:
                    ret.solution.matched_stars = int(match.group(1))
                    # logger.info(f"{ret.solution.matched_stars=}")
                else:
                    logger.error("bad match for ret.solution.matched_stars")

            elif line.startswith("  RA,Dec = "):  # (5)
                match = re.match(
                    r"^.*pixel scale (" + pattern_float + r") arcsec", line
                )
                if match:
                    ret.solution.pixel_scale = float(match.group(1))
                    # logger.info(f"{ret.solution.pixel_scale=}")
                else:
                    logger.error("bad match for ret.solution.pixel_scale")

            elif line.startswith("simplexy:"):
                # simplexy: found 3 sources.
                match = re.match(
                    r"^.*found" + pattern_int + r"sources", line
                )
                if match:
                    ret.solution.matched_stars = int(match.group(1))

    except Exception as e:
        logger.error(f"{op}: exception: {e}")

    return ret

class AstrometryDotNet(SolverInterface):

    def __init__(self):
        self.unit = None

    def solve(self, unit, settings: ImagerSettings, target: Coord) -> SolvingResult:  # type: ignore[name]

        self.unit = unit
        filer = Filer(logger)
        unix_emulator = "cygwin"
        tmp_dir = generate_random_string(prefix="tmp_")
        win_tmp_dir = r"D:/MAST/tmp/" + tmp_dir
        os.makedirs(win_tmp_dir, exist_ok=True)
        index_dir = r"D:/Astrometry.net/indexes"
        solver_name = "AstrometryDotNet"

        index_file = None
        os.environ["PATH"] = (
            "C:/cygwin64/bin;/usr/lib/lapack;C:/Users/mast/PycharmProjects/MAST_unit/venv/Scripts;C:/Windows/system32;"
            + "C:/Windows;C:/Windows/System32/Wbem;C:/Windows/System32/WindowsPowerShell/v1.0/;C:/Windows/System32/OpenSSH/;"
            + "C:/Users/mast/Documents/PlaneWave/ps3cli;C:/Program Files/Git/cmd;"
            + "C:/Users/mast/Downloads/nssm/nssm-2.24/win64;C:/Program Files/JetBrains/PyCharm Community Edition 2024.1/bin;"
            + "C:/Users/mast/AppData/Local/Microsoft/WindowsApps;"
            + "C:/Program Files/MongoDB/Server/7.0/bin;C:/Users/mast/AppData/Local/Programs/Python/Launcher/;"
            + "C:/Users/mast/PycharmProjects/MAST_unit/src/Standa/ximc-2.13.6/ximc/win64;"
        )

        assert(settings.roi is not None), f"{function_name()}: settings.roi is None"
        assert(settings.image_path is not None), (f"{function_name()}: settings.image_path is None")


        cmd = ""
        args = []
        args += ["--scale-units", "arcsecperpix"]
        args += ["--scale-low", "0.25"]
        args += ["--scale-high", "0.27"]
        args += ["--ra", f"{target.ra.deg}"]
        args += ["--dec", f"{target.dec.value}"]
        args += ["--radius", f"{1}"]
        args += ["--no-plots", "--overwrite", "--solved", "none"]
        args += ["--match", "none", "--rdls", "none", "--corr", "none"]
        args += ["--crpix-x", str(int(settings.roi.width / 2))]
        args += ["--crpix-y", str(int(settings.roi.height / 2))]

        if index_file:
            args += ["--index-file", index_file]
        fits_path = settings.image_path
        new_fits_path = fits_path.replace(".fits", f",solver={solver_name}.fits")

        if unix_emulator == "cygwin":
            tmp_path = r"/cygdrive/d/MAST/tmp/" + tmp_dir
            os.makedirs(tmp_path, exist_ok=True)

            cmd = r"C:/cygwin64/usr/local/astrometry/bin/solve-field"
            args += ["--dir", tmp_path]
            args += ["--temp-dir", tmp_path]
            # args += ['--index-dir', '/cygdrive/d/Astrometry.net/indexes']
            args += ["--index-dir", "/usr/local/astrometry/indexes-full"]
            args += ["--new-fits", win_to_cygwin(new_fits_path)]
            args += [win_to_cygwin(fits_path)]

        elif unix_emulator == "wsl":
            cmd = r"//wsl$/usr/local/astrometry/bin/solve-field"
            args += ["--dir", win_to_wsl(tmp_dir)]
            args += ["--temp-dir", win_to_wsl(tmp_dir)]
            args += ["--index-dir", win_to_wsl(index_dir)]
            args += ["--new-fits", win_to_wsl(new_fits_path)]
            args += [win_to_wsl(fits_path)]


        start = datetime.datetime.now()
        command = " ".join([cmd] + args)
        # logger.info(f"AstrometryDotNet.solve: {command=}")

        completed_process = subprocess.run(command, capture_output=True, shell=True)
        stdout_lines = completed_process.stdout.decode().strip().splitlines()
        stderr_lines = completed_process.stderr.decode().strip().splitlines()
        elapsed = datetime.datetime.now() - start
        logger.info(
            f"{'succeeded' if completed_process.returncode == 0 else 'failed'}"
            + f" in {elapsed.total_seconds():.2f} seconds"
        )

        result_file = cygwin_to_win(new_fits_path).replace(".fits", "-result.txt")
        with open(result_file, "w") as file:
            file.write("--- command ---\n")
            file.write(" ".join([cmd] + args) + "\n")
            file.write("\n--- stdout ---\n")
            for line in stdout_lines:
                file.writelines(line + "\n")
            file.write("\n--- stderr ---\n")
            for line in stderr_lines:
                file.writelines(line + "\n")

        filer.move_ram_to_shared([result_file, fits_path, cygwin_to_win(new_fits_path)])

        if completed_process.returncode == 0:
            ret = _parse_solver_output(stdout_lines)
            if ret.solution is not None:
                boxed_info(logger=logger, lines=["future image quality check",
                                                 f"solver found {ret.solution.matched_stars} stars"], center=True)
        else:
            ret = SolvingResult(
                succeeded=False,
                errors=[f"Exit status: {completed_process.returncode}", ', '.join(stderr_lines)],
            )

        shutil.rmtree(win_tmp_dir, ignore_errors=True)

        return ret

    def solve_and_correct(self):
        pass

if __name__ == "__main__":
    imager_settings: ImagerSettings = ImagerSettings(
        seconds=5,
        image_path="/cygdrive/d/MAST/tmp/2024-12-05/Acquisitions/seq=0025,time=18-13-03_987,"
        + "target=1.42677311977099,23.5115091209584/guiding/seq=0001,time=18-22-25_715,seconds=5.0,"
        + "binning=1x1,gain=170.0,roi=x=300,y=1476,w=7402,h=3968.fits",
    )
    target = Coord(
        ra=Angle(1.42677311977099, unit="hour"), dec=Angle(23.5115091209584, unit="deg")
    )
    solver = AstrometryDotNet()
    result = solver.solve(unit=None, settings=imager_settings, target=target)
    # print(json.dumps(result.to_dict(), indent=2))
    sys.exit(0)
