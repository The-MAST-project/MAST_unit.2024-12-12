import datetime
import json
import os
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, cast

from astropy.coordinates import Angle
from astropy.io import fits

from common.config import Config
from common.config.rois import FcuVersion, SkyRoiConfig, SpecRoiConfig
from common.const import Const
from common.filer import Filer
from common.interfaces.imager import ImagerRoi
from common.interfaces.solving import SolverInterface, SolvingResult
from common.mast_logging import get_logger
from common.rois import SkyRoi, SpecRoi
from common.utils import Coord, boxed_log, function_name
from imagers import ImagerSettings

from .astrometry_dot_net import cygwin_to_win, generate_random_string, parse_solver_output, win_to_cygwin
from .pixel_grid import roi_center_to_crpix

logger = get_logger(__name__)
if TYPE_CHECKING:
    from unit import Unit  # type: ignore[import-untyped]


class MastrometryDotNet(SolverInterface):
    """
    A PlateSolverInterface implementation that uses the astrometry.net solver
     (same as AstrometryDotNetSolver) but handles image downsampling and cropping

    In order to make things simpler and faster, some assumptions are made:
     - the cygwin environment is used (we ditched wsl for now)
     - the imager (zwo, ascom or phd2) is used at full-frame imager (no ROI), and binning 1x1.  This
        comes to circumvent the complexity of dealing with ROIs and binned images with phd2.
     - only the needed index files are kept on the ImDisk RAM disk, to save space and speed up astrometry.net solving.
     - they are copied to the RAM disk at startup time (if not already there)
    - the full frame images are downsampled and cropped before being sent to astrometry.net (to save solving
       time), an deleted afterwards.

    An instance of this class is used as a backend by solving.Solver() to do the actual solving.
    """

    pixelscale: float = 0.2616  # arcsecs per pixel for full-frame unbinned
    f_number: float = 3.0

    def __init__(self):
        """
        Prepare things for Astrometry.net solving
        - check the ImDisk RAM disk is mounted and contains the indexes

        """
        super().__init__()

        ram = Filer().ram
        assert ram is not None, f"{function_name()}: RAM disk is not mounted"
        if not Path(ram.root).exists():
            Path(ram.root).mkdir(parents=True, exist_ok=True)

        self.index_dir = Path(str(ram.drive), "mast-indexes")
        if not self.index_dir.exists():
            raise Exception(f"{function_name()}: RAM disk path '{self.index_dir.as_posix()}' does not exist")

        # missing = False
        # for i in range(0, 47):
        #     index_5206_file = self.index_dir / f"index-5206-{i:02d}.fits" # needed
        #     if not index_5206_file.exists():
        #         logger.warning(f"{function_name()}: RAM disk is missing index file '{index_5206_file.as_posix()}'")
        #         missing = True

        #     # index_5205_file = self.index_dir / f"index-5205-{i:02d}.fits" # niced to have
        #     # if not index_5205_file.exists():
        #     #     logger.warning(f"{function_name()}: RAM disk is missing index file '{index_5205_file.as_posix()}'")
        #     #     missing = True

        #     if missing:
        #         raise Exception(f"{function_name()}: RAM disk is missing some index files")

        # logger.info(f"{function_name()}: RAM disk contains all needed index files")
        logger.warning(f"{function_name()}: Skipping RAM disk index file check (TODO: remove this after testing)")

    @property
    def name(self) -> str:
        return "mastrometry.net"

    def solve(  # noqa: C901
        self,
        unit,
        phase: Const.SolvingPhase,
        settings: ImagerSettings | None = None,
        full_frame_input_image_path: str | None = None,
        index_file: str | None = None,
        target: Coord | None = None,
    ) -> SolvingResult | None:
        """
        MAST specific astrometry.net solver implementation.

        The caller is expected to pass an input image that is full frame, unbinned, which gets
          downsampled by a factor 2 (2x2 binning) and cropped to the ROI (if any)
          before being sent to astrometry.net for solving.
        """

        self.unit: Unit | None = unit
        filer = Filer(logger)

        #
        # A temporary folder is created on the RAM disk for the solving process, and deleted afterwards.
        # This is needed to speed up the solving process by using the RAM disk.
        #
        tmp_dir = generate_random_string(prefix="tmp_")
        assert filer.ram is not None
        win_tmp_dir = Path(filer.ram.root, "tmp", tmp_dir)
        win_tmp_dir.mkdir(parents=True, exist_ok=True)

        # solve-field.exe is a cygwin binary:
        #  - C:\cygwin64\bin is needed so Windows can load cygwin1.dll
        #  - /usr/lib/lapack is a cygwin POSIX path (cygwin1.dll re-parses PATH at
        #    startup); numpy's lapack_lite extension needs it when removelines runs.
        # Pass via env= so we don't mutate the parent process's PATH.
        env = os.environ.copy()
        env["PATH"] = r"C:\cygwin64\bin" + os.pathsep + "/usr/lib/lapack" + os.pathsep + env.get("PATH", "")

        if full_frame_input_image_path is None:  # noqa: SIM102
            if settings is not None:
                assert settings.image_path is not None, f"{function_name()}: settings.image_path is None"
                full_frame_input_image_path = settings.image_path

        assert full_frame_input_image_path is not None, f"{function_name()}: full_frame_input_image_path is None"

        #
        # Read the full frame FITS file we got from te imager
        # We don't really know that the input image is full frame and unbinned, but we assume it is.
        #
        with fits.open(full_frame_input_image_path) as hdul:
            header = hdul[0].header  # type: ignore
            data = hdul[0].data  # type: ignore

            # Get image dimensions and data type from header
            height, width = data.shape
            dtype = data.dtype
            logger.info(f"{function_name()}: Full frame image dimensions: {width}x{height}, dtype: {dtype}")

            #
            # Passed to astrometry.net as the reference pixel for the solving process
            # If not specified it will default to the center pixel of the image,
            #  but if we're using an ROI it will be the center of the ROI
            #
            # NOTE: the values are in downsampled pixel units
            #
            refpix: tuple[float, float] | None = None  # (--crpix-x, --crpix-y), fractional; see pixel_grid.py

            downsample_factor: int = 2  # simulated binning factor (2x2), to speed up solving by reducing the image size

            imager_roi: ImagerRoi | None = None

            unit_conf = unit.unit_conf if unit and unit.unit_conf else Config().get_unit()
            assert unit_conf is not None

            fcu_version = self.unit.fcu_version if self.unit else FcuVersion.v2

            match phase:
                case "sky":
                    assert fcu_version in unit_conf.acquisition.rois
                    cfg = cast(SkyRoiConfig, unit_conf.acquisition.rois[fcu_version])
                    sky_roi = SkyRoi(sky_x=cfg.sky_x, sky_y=cfg.sky_y, width=cfg.width, height=cfg.height)

                    imager_roi = ImagerRoi.from_other(sky_roi)

                case "spec":
                    import common.asi as asi

                    camera_x_size = asi.ASI_294MM_WIDTH
                    camera_y_size = asi.ASI_294MM_HEIGHT
                    assert fcu_version in unit_conf.guiding.rois
                    cfg = cast(SpecRoiConfig, unit_conf.guiding.rois[fcu_version])

                    half_width = min(
                        cfg.fiber_x - cfg.margin_horizontal, camera_x_size - cfg.margin_horizontal - cfg.fiber_x
                    )
                    half_height = min(cfg.fiber_y - cfg.margin_vertical, camera_y_size - cfg.margin_vertical - cfg.fiber_y)

                    spec_roi = SpecRoi(
                        fiber_x=cfg.fiber_x, fiber_y=cfg.fiber_y, width=half_width * 2, height=half_height * 2
                    )

                    imager_roi = ImagerRoi.from_other(spec_roi)

                case _:
                    # No ROI and no recognized phase: leave imager_roi=None
                    # → no crop, refpix stays None, solve-field uses --crpix-center
                    pass

            if imager_roi is not None:
                if imager_roi.x + imager_roi.width > width or imager_roi.y + imager_roi.height > height:
                    raise Exception(
                        f"{function_name()}: ROI (x={imager_roi.x}, y={imager_roi.y}, "
                        + f"width={imager_roi.width}, height={imager_roi.height}) "
                        + f"is out of bounds for image dimensions ({width}x{height})"
                    )
                assert imager_roi._center is not None

                # Crop to the ROI, then refpix is expressed in the cropped+downsampled coord system.
                #
                # FRAGILE COORDINATE SURFACE -- see solvers/pixel_grid.py and solvers/CLAUDE.md.
                # refpix becomes solve-field's --crpix-x/--crpix-y, i.e. the WCS reference pixel,
                # i.e. where the solved RA/Dec is reported -- on the spec path that is the fiber
                # pointing. The mapping from the original-frame ROI center to the cropped+binned
                # grid MUST use the pixel-center-correct convention in pixel_grid; the old integer
                # division ((center - start) // factor) silently biased it by ~0.4". Returns floats;
                # solve-field accepts fractional CRPIX, so do NOT round.
                data = data[imager_roi.y : imager_roi.y + imager_roi.height, imager_roi.x : imager_roi.x + imager_roi.width]
                refpix = (
                    roi_center_to_crpix(imager_roi._center.x, imager_roi.x, downsample_factor),
                    roi_center_to_crpix(imager_roi._center.y, imager_roi.y, downsample_factor),
                )

                logger.info(f"{function_name()}: Cropped to {imager_roi=}, {refpix=}")
            else:
                logger.info(f"{function_name()}: No ROI, using full frame, refpix=center")

            # Downsample by factor downsample_factor (binning)
            downsampled_height = data.shape[0] // downsample_factor
            downsampled_width = data.shape[1] // downsample_factor

            # Reshape and average downsample_factor x downsample_factor blocks
            downsampled = (
                data[: downsampled_height * downsample_factor, : downsampled_width * downsample_factor]
                .reshape(downsampled_height, downsample_factor, downsampled_width, downsample_factor)
                .mean(axis=(1, 3))
                .astype(dtype)
            )

            logger.info(
                f"{function_name()}: Downsampled by factor of {downsample_factor} to "
                + f"{downsampled_width}x{downsampled_height}"
            )

            # Update header for downsampled image
            header["NAXIS1"] = downsampled_width
            header["NAXIS2"] = downsampled_height

            header["XBINNING"] = downsample_factor
            header["YBINNING"] = downsample_factor
            header["DOWNSAMP"] = (downsample_factor, "Downsampling factor applied to original image")

            if imager_roi is not None:
                header["ROI_X"] = (imager_roi.x, "X coordinate of ROI in original image")
                header["ROI_Y"] = (imager_roi.y, "Y coordinate of ROI in original image")
                header["ROI_W"] = (imager_roi.width, "Width of ROI in original image")
                header["ROI_H"] = (imager_roi.height, "Height of ROI in original image")

            # Save downsampled image to temporary directory
            assert full_frame_input_image_path is not None
            downsampled_image_path = win_tmp_dir / f"downsampled_{str(Path(full_frame_input_image_path).name)}"
            fits.writeto(downsampled_image_path, downsampled, header, overwrite=True)
            logger.info(f"{function_name()}: Saved downsampled image to '{downsampled_image_path}'")

            # Build astrometry.net command
            # After downsampling by {downsample_factor}x: 0.2616 * {downsample_factor} = {effective_pixelscale} arcsec/pixel
            effective_pixelscale = self.pixelscale * downsample_factor

            args = [
                # Scale constraints (±10% tolerance)
                "--scale-units",
                "arcsecperpix",
                "--scale-low",
                f"{0.9 * effective_pixelscale}",  # ~0.47
                "--scale-high",
                f"{1.1 * effective_pixelscale}",  # ~0.57
                # Index file directory
                "--index-dir",
                win_to_cygwin(str(self.index_dir)),
                # Performance options
                "--no-plots",
                "--overwrite",
                "--cpulimit",
                "30",  # timeout in seconds
                "--solved",
                "none",
                "--match",
                "none",
                "--rdls",
                "none",
                "--corr",
                "none",
                # Output control
                "--dir",
                win_to_cygwin(str(win_tmp_dir)),
                "--temp-dir",
                win_to_cygwin(str(win_tmp_dir)),
            ]

            # Add RA/Dec hint if target is provided (significantly speeds up solving)
            if target is not None:
                args += [
                    "--ra",
                    f"{target.ra.deg}",
                    "--dec",
                    f"{target.dec.deg}",
                    "--radius",
                    "2.0",  # search radius in degrees
                ]

            # Tweak (SIP distortion fit) is intentionally LEFT ENABLED.
            #
            # The reference solver (AstrometryDotNet) leaves tweak on, and the equivalence
            # study found that --no-tweak over-constrains the WCS to a 4-parameter linear fit:
            # different matched-source subsets then settle at slightly different rotation/scale,
            # producing up to ~7" disagreement that vanishes (-> sub-arcsecond) once SIP is fit.
            # SIP also models real optical distortion toward the frame corners, which matters
            # for full-frame pixel-location consistency. We therefore do NOT pass --no-tweak.
            #
            # Re-add "--no-tweak" here ONLY if solve latency becomes binding AND the loss of
            # corner accuracy / reference agreement is acceptable for the use case.

            if refpix is None:
                args += ["--crpix-center"]
            else:
                args += [
                    "--crpix-x",
                    str(refpix[0]),
                    "--crpix-y",
                    str(refpix[1]),
                ]

            new_fits_path = Path(
                str(downsampled_image_path).replace(".fits", f",solver={self.name}.fits").replace("downsampled_", "")
            )
            args += [
                "--new-fits",
                win_to_cygwin(str(new_fits_path)),
                win_to_cygwin(str(downsampled_image_path)),
            ]

        command = " ".join([r"C:/cygwin64/usr/local/astrometry/bin/solve-field"] + args)
        logger.info(f"{function_name()}: Running astrometry.net with '{command}'")
        start = datetime.datetime.now()

        completed_process = subprocess.run(command, capture_output=True, shell=True, env=env)
        stdout_lines = completed_process.stdout.decode().strip().splitlines()
        stderr_lines = completed_process.stderr.decode().strip().splitlines()
        elapsed = datetime.datetime.now() - start
        logger.info(
            f"{function_name()}: {'succeeded' if completed_process.returncode == 0 else 'failed'}"
            + f" in {elapsed.total_seconds():.2f} seconds"
        )

        result_file = cygwin_to_win(str(new_fits_path)).replace(".fits", "-result.txt")
        with open(result_file, "w") as file:
            file.write("--- command ---\n")
            file.write(f"{command}\n")

            file.write("\n--- returncode ---\n")
            file.write(f"{completed_process.returncode}\n")

            file.write("\n--- stdout ---\n")
            for line in stdout_lines:
                file.writelines(line + "\n")

            file.write("\n--- stderr ---\n")
            for line in stderr_lines:
                file.writelines(line + "\n")

            file.write("\n--- timing ---\n")
            file.writelines(f"elapsed: {elapsed.total_seconds():.2f} seconds\n")

        if completed_process.returncode == 0:
            ret = parse_solver_output(stdout_lines)
            if ret is not None and ret.solution is not None and ret.succeeded:
                boxed_log(
                    logger=logger,
                    lines=[
                        "FUTURE: image quality check",
                        f"#sources {ret.solution.sources}",
                        f"#matched {ret.solution.matched_stars}",
                    ],
                    center=True,
                )
                if (
                    ret.solution.index_file
                    and self.unit is not None
                    and self.unit.acquirer is not None
                    and self.unit.acquirer.latest_acquisition is not None
                ):
                    self.unit.acquirer.latest_acquisition.solver_data = {"index_file": ret.solution.index_file}

                # override solved RA/Dec from FITS header
                header = fits.getheader(new_fits_path, 0)  # type: ignore
                if "CRVAL1" in header and "CRVAL2" in header:
                    ret.solution.ra_hours = header["CRVAL1"] / 15.0
                    ret.solution.dec_degs = header["CRVAL2"]

                    ret.solution.ra_rads = float(Angle(ret.solution.ra_hours * 15.0, unit="deg").radian)  # type: ignore[assignment]
                    ret.solution.dec_rads = float(Angle(ret.solution.dec_degs, unit="deg").radian)  # type: ignore[assignment]
                else:
                    logger.warning(f"no CRVAL1/CRVAL2 in solved FITS header of '{new_fits_path}'")
        else:
            ret = SolvingResult(
                succeeded=False,
                errors=[
                    f"Exit status: {completed_process.returncode}",
                    ", ".join(stderr_lines),
                ],
            )

        Thread(target=self.cleanup, args=([Path(result_file), Path(new_fits_path)], win_tmp_dir)).start()
        # logger.info(f"{function_name()}: Temporary files left in '{win_tmp_dir}' (TODO: clean up in background thread)")
        return ret

    def cleanup(self, files_to_move: list[Path], target_folder: Path, tmp_dir: Path):
        files_to_move = [f for f in files_to_move if f.exists()]
        if files_to_move:
            if not target_folder.exists():
                target_folder.mkdir(parents=True, exist_ok=True)
            for file in files_to_move:
                shutil.move(file, target_folder / file.name)
                logger.info(f"{function_name()}: saved '{(target_folder / file.name).as_posix()}'")

        with suppress(Exception):
            shutil.rmtree(tmp_dir)

    def solve_and_correct(self):
        pass


if __name__ == "__main__":

    def test_solver():
        solver = MastrometryDotNet()
        result = solver.solve(
            unit=None,
            phase="spec",
            full_frame_input_image_path="D:\\MAST\\tmp\\mastrometry\\full-frame.fits",
        )
        print(json.dumps(result.to_dict() if result else None, indent=2))

    def test_solver_with_roi():
        from common.interfaces.imager import ImagerRoi, ImagerSettings

        test_image_path = "D:\\MAST\\tmp\\mastrometry\\full-frame.fits"
        solver = MastrometryDotNet()
        result = solver.solve(
            unit=None,
            phase="spec",
            full_frame_input_image_path=test_image_path,
            settings=ImagerSettings(
                seconds=5,
                binning=1,
                roi=ImagerRoi(x=0, y=0, width=2500, height=5500),
                image_path=test_image_path,
            ),
        )
        print(json.dumps(result.to_dict() if result else None, indent=2))

    # test_solver()
    test_solver_with_roi()
