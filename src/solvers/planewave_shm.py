import datetime
import time
from multiprocessing.shared_memory import SharedMemory
from typing import TYPE_CHECKING, Literal

import numpy as np
from astropy.coordinates import Angle
from astropy.io import fits

from common.const import Const
from common.extended_basemodel import ExtendedBaseModel
from common.interfaces.solving import SolverInterface, SolvingResult, SolvingSolution
from common.mast_logging import get_logger
from common.utils import Coord, function_name
from imagers import ImagerSettings
from PlaneWave.ps3cli_client import PS3CLIClient

if TYPE_CHECKING:
    from unit import Unit  # type: ignore[import-untyped]

logger = get_logger(__name__)
class PlaneWaveShmSolvingSolution(ExtendedBaseModel):
    num_matched_stars: int
    match_rms_error_arcsec: float
    match_rms_error_pixels: float
    center_ra_j2000_rads: float
    center_dec_j2000_rads: float
    matched_arcsec_per_pixel: float
    rotation_angle_degs: float


class PlaneWaveShmSolvingResult(ExtendedBaseModel):
    state: Literal[
        "ready",
        "loading",
        "extracting",
        "matching",
        "found_match",
        "no_match",
        "error",
        "unknown",
    ]
    error_message: str | None = None
    last_log_message: str | None = None
    num_extracted_stars: int | None = None
    running_time_seconds: float | None = None
    solution: PlaneWaveShmSolvingSolution | None = None


class PlaneWaveShm(SolverInterface):
    def solve(self, unit: "Unit", imager_settings: ImagerSettings, target: Coord) -> SolvingResult:
        op = function_name()

        assert unit.imager.can_image_to_memory, f"{op}: unit.imager cannot image to memory"
        assert imager_settings.roi and imager_settings.roi.width is not None and imager_settings.roi.height is not None, (
            f"{op}: imager_settings.roi is not set or has unset width or height"
        )
        assert imager_settings.binning and imager_settings.binning.x is not None, (
            f"{op}: imager_settings.binning is not set or has unset x binning"
        )

        unit.imager.wait_for_image_ready()

        width = imager_settings.roi.width
        height = imager_settings.roi.height
        pixel_scale = unit.unit_conf.imager.pixel_scale_at_bin1 * imager_settings.binning.x

        shm = SharedMemory(name=Const.PLATE_SOLVING_SHM_NAME, create=True, size=width * height * 2)
        shared_image = np.ndarray((width, height), dtype=np.uint16, buffer=shm.buf)

        assert unit.imager.image_array is not None, "{op}: unit.imager.image_array is None"

        shared_image[:] = unit.imager.image_array[:]
        ps3_shm_client: PS3CLIClient = PS3CLIClient()

        ps3_shm_client.connect("127.0.0.1", 8998)
        start = datetime.datetime.now()
        timeout_seconds: float = 50
        end = start + datetime.timedelta(seconds=timeout_seconds)
        logger.info(f"{op}: calling ps3_client.begin_platesolve_shm ...")
        ps3_shm_client.begin_platesolve_shm(
            shm_key=Const.PLATE_SOLVING_SHM_NAME,
            height_pixels=imager_settings.roi.width,
            width_pixels=imager_settings.roi.height,
            arcsec_per_pixel_guess=pixel_scale,
            enable_all_sky_match=True,
            enable_local_quad_match=True,
            enable_local_triangle_match=True,
            ra_guess_j2000_rads=target.ra.radian,
            dec_guess_j2000_rads=target.dec.radian,
        )

        ps3_solver_status: PlaneWaveShmSolvingResult
        while True:
            status_result = ps3_shm_client.platesolve_status()
            status_dict = {str(k): v for k, v in (status_result.items() if status_result else {})}
            ps3_solver_status = PlaneWaveShmSolvingResult(state="unknown", **status_dict)

            if (
                ps3_solver_status.state == "error"
                or ps3_solver_status.state == "no_match"
                or ps3_solver_status.state == "found_match"
            ):
                break

            if datetime.datetime.now() >= end:
                ps3_shm_client.platesolve_cancel()
                ps3_solver_status = PlaneWaveShmSolvingResult(
                    state="error",
                    error_message=f"time out ({timeout_seconds} seconds), cancelled",
                    num_extracted_stars=None,
                    running_time_seconds=None,
                    solution=None,
                    last_log_message=None,
                )
                break
            else:
                time.sleep(0.1)

        unit.imager.wait_for_image_saved()
        time.sleep(2)

        assert unit.imager.latest_settings is not None, f"{op}: unit.imager.latest_settings is None"
        assert unit.imager.latest_settings.image_path is not None, f"{op}: unit.imager.latest_settings.image_path is None"
        assert ps3_solver_status and ps3_solver_status.solution is not None, (
            f"{op}: ps3_solver_status or ps3_solver_status.solution is None"
        )

        # Update FITS headers
        with fits.open(unit.imager.latest_settings.image_path, mode="update") as hdul:  # type: ignore[misc]
            header = fits.Header()

            roi = unit.imager.latest_settings.roi
            assert roi is not None, f"{op}: roi is None"

            header["CRPIX1"] = roi.x + (roi.width / 2)
            header.comments["CRPIX1"] = "RA reference pixel"
            header["CRPIX2"] = roi.y + (roi.height / 2)
            header.comments["CRPIX2"] = "DEC reference pixel"

            header["CRVAL1"] = Angle(ps3_solver_status.solution.center_ra_j2000_rads, unit="radian").hour
            header.comments["CRVAL1"] = "solved ra of reference pixel"
            header["CRVAL2"] = Angle(ps3_solver_status.solution.center_dec_j2000_rads, unit="radian").degs
            header.comments["CRVAL2"] = "solved dec of reference pixel"

            binning = unit.imager.latest_settings.binning
            assert binning is not None, f"{op}: binning is None"

            pixel_scale_at_binning1 = unit.unit_conf.imager.pixel_scale_at_bin1
            header["CDELT1"] = pixel_scale_at_binning1 * binning.x
            header.comments["CDELT1"] = "ra pixel scale"
            header["CDELT2"] = pixel_scale_at_binning1 * binning.y
            header.comments["CDELT2"] = "dec pixel scale"

            header["CUNIT1"] = "deg"
            header["CUNIT2"] = "deg"

            hdul.flush()

        ret: SolvingResult = SolvingResult(succeeded=True)
        ret.native_result = ps3_solver_status
        if ps3_solver_status.state == "found_match":
            ret.succeeded = True
            ret.solution = SolvingSolution()
            ret.solution.ra_rads = ps3_solver_status.solution.center_ra_j2000_rads
            ret.solution.ra_hours = float(Angle(ret.solution.ra_rads, unit="radian").hour)  # type: ignore[assignment]
            ret.solution.dec_rads = ps3_solver_status.solution.center_dec_j2000_rads
            ret.solution.dec_degs = float(Angle(ret.solution.dec_rads, unit="radian").degs)  # type: ignore[assignment]
            ret.solution.matched_stars = ps3_solver_status.solution.num_matched_stars
            ret.solution.rotation_angle_degs = ps3_solver_status.solution.rotation_angle_degs
        else:
            ret.succeeded = False
            ret.errors = [
                ps3_solver_status.error_message if ps3_solver_status.error_message else "no error message",
                ps3_solver_status.last_log_message if ps3_solver_status.last_log_message else "no last log message",
            ]

        return ret

    def solve_and_correct(self):
        pass

    @property
    def name(self) -> str:
        return "PlanewaveShm"
