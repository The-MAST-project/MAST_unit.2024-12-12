import os
import subprocess
from enum import IntFlag
from typing import TYPE_CHECKING

from astropy.coordinates import Angle
from astropy.io import fits

from common.extended_basemodel import ExtendedBaseModel
from common.filer import Filer, MoveGuardian
from common.interfaces.solving import SolverInterface, SolvingResult, SolvingSolution
from common.mast_logging import get_logger
from common.utils import Coord, function_name
from imagers import ImagerSettings

logger = get_logger(__name__)
filer = Filer(logger)

if TYPE_CHECKING:
    from unit import Unit


class PlaneWaveCliSolverExitCode(IntFlag):
    Success = 0
    InvalidArguments = 1
    CatalogNotFound = 2
    NoStarMatch = 3
    NoImageLoad = 4
    GeneralFailure = 99


class PlaneWaveCliSolverResult(ExtendedBaseModel):
    succeeded: bool = False
    ra_j2000_hours: float | None = None
    dec_j2000_degrees: float | None = None
    arcsec_per_pixel: float | None = None
    rot_angle_degs: float | None = None
    errors: list[str] | None = []


class PlaneWaveCli(SolverInterface):
    def solve(self, unit: "Unit", imager_settings: ImagerSettings, target: Coord) -> SolvingResult:  # type: ignore[name]  # noqa: C901

        op = function_name()
        # ps3_solver_status: PlaneWaveCliSolverResult
        ret = SolvingResult(succeeded=True)

        unit.imager.wait_for_image_saved()

        assert imager_settings.binning and imager_settings.binning.x is not None, (
            f"{op}: imager_settings.binning is not set or has unset x binning"
        )

        pixel_scale = unit.unit_conf.imager.pixel_scale_at_bin1 * imager_settings.binning.x

        cmd = "C:\\Program Files (x86)\\PlaneWave Instruments\\ps3cli\\ps3cli"
        assert imager_settings.image_path, f"{op}: settings.image_path is not set"

        image_path = imager_settings.image_path
        result_path = os.path.join(os.path.dirname(image_path), "result.txt")
        command = [
            cmd,
            image_path,
            f"{pixel_scale}",
            result_path,
            "C:/Users/mast/Documents/Kepler",
        ]
        logger.info(f"{op}: image saved, running solver ...")

        # result = None
        completed_process: subprocess.CompletedProcess | None = None
        try:
            # ps3cli writes result_path during the run; protect it so a concurrent
            # ram->shared move can't grab a half-written result.txt.
            with MoveGuardian().protect(result_path):
                completed_process = subprocess.run(
                    command,
                    capture_output=True,
                    check=True,
                    shell=True,
                )
            filer.move_ram_to_shared(image_path)
        except subprocess.CalledProcessError as e:
            logger.error(f"{op}: solver return code: {PlaneWaveCliSolverExitCode(e.returncode).__repr__()}")
            # Write + close result.txt, THEN move it (protected). The move was previously
            # inside the open() block, which risked moving a still-open file.
            with MoveGuardian().protect(result_path):
                with open(result_path, "w") as file:
                    file.write(e.stdout.decode())
                filer.move_ram_to_shared(result_path)

            # if it's a HARD error (not just NoStarMatch), cannot continue
            if (
                e.returncode == PlaneWaveCliSolverExitCode.InvalidArguments
                or e.returncode == PlaneWaveCliSolverExitCode.CatalogNotFound
                or e.returncode == PlaneWaveCliSolverExitCode.NoImageLoad
                or e.returncode == PlaneWaveCliSolverExitCode.GeneralFailure
            ):
                logger.error(
                    f"{op}: solver returned {PlaneWaveCliSolverExitCode(e.returncode).__repr__()}, " + "guiding aborted."
                )

                ret.succeeded = False
                ret.errors = [f"solver failed with {PlaneWaveCliSolverExitCode(e.returncode).__repr__()}"]
                return ret

        # solving succeeded, parse output
        assert completed_process is not None, f"{op}: completed_process is None"
        if completed_process.returncode == PlaneWaveCliSolverExitCode.Success:
            logger.info(f"{op}: solver found a solution")
            with open(result_path) as file:
                solver_output_lines = file.readlines()

        elif completed_process.returncode == PlaneWaveCliSolverExitCode.NoStarMatch:
            logger.error(f"{op}: solver did not find a match {completed_process.returncode=}")

            ret.succeeded = False
            ret.errors = [f"solver did not find a match {completed_process.returncode=}"]
            return ret

        filer.move_ram_to_shared(result_path)

        solver_output = {}
        # parse the solver output
        for line in solver_output_lines:
            fields = line.rstrip().split("=")
            if len(fields) != 2:
                continue
            keyword, value = fields
            solver_output[keyword] = float(value)

        if "arcsec_per_pixel" in solver_output:
            logger.info(f"{op}: {solver_output['arcsec_per_pixel']=}")

        for key in ["ra_j2000_hours", "dec_j2000_degrees", "rot_angle_degs"]:
            if key not in solver_output:
                logger.error(f"{op}: either 'ra_j2000_hours' or 'dec_j2000_degrees' missing in {solver_output=}")
                continue

        ret = SolvingResult(succeeded=True)
        ret.succeeded = True
        ret.native_result = solver_output
        solution = SolvingSolution()
        solution.ra_hours = solver_output["ra_j2000_hours"]
        solution.ra_rads = Angle(solution.ra_hours, unit="hour").radian  # type: ignore[assignment]
        solution.dec_degs = solver_output["dec_j2000_degrees"]
        solution.dec_rads = Angle(solution.dec_degs, unit="degree").radian  # type: ignore[assignment]
        solution.rotation_angle_degs = solver_output["rot_angle_degs"]
        solution.matched_stars = solver_output["matched_stars"]
        ret.solution = solution

        assert unit.imager.latest_settings is not None, f"{op}: unit.imager.latest_settings is None"
        assert unit.imager.latest_settings.image_path is not None and unit.imager.latest_settings.roi is not None, (
            f"{op}: unit.imager.latest_settings.image_path or roi is None"
        )

        # Update FITS headers
        with fits.open(unit.imager.latest_settings.image_path, mode="update") as hdul:  # type: ignore[misc]
            header = fits.Header()

            roi = unit.imager.latest_settings.roi
            header["CRPIX1"] = roi.x + (roi.width / 2)
            header.comments["CRPIX1"] = "RA reference pixel"
            header["CRPIX2"] = roi.y + (roi.height / 2)
            header.comments["CRPIX2"] = "DEC reference pixel"

            header["CRVAL1"] = Angle(solution.ra_hours, unit="hour").degs
            header.comments["CRVAL1"] = "solved ra of reference pixel"
            header["CRVAL2"] = solution.dec_degs
            header.comments["CRVAL2"] = "solved dec of reference pixel"

            binning = unit.imager.latest_settings.binning
            pixel_scale_at_binning1 = unit.unit_conf.imager.pixel_scale_at_bin1
            if binning:
                header["CDELT1"] = pixel_scale_at_binning1 * binning.x
                header.comments["CDELT1"] = "ra pixel scale"
                header["CDELT2"] = pixel_scale_at_binning1 * binning.y
                header.comments["CDELT2"] = "dec pixel scale"

            header["CUNIT1"] = "deg"
            header["CUNIT2"] = "deg"

            hdul.flush()

        return ret

    def solve_and_correct(self):
        pass

    @property
    def name(self) -> str:
        return "PlaneWaveCli"
