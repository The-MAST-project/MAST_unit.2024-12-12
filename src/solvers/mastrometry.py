import datetime
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, cast

from astropy.coordinates import Angle
from astropy.io import fits

from common.config import Config
from common.config.rois import FcuVersion, SkyRoiConfig, SpecRoiConfig
from common.const import Const
from common.filer import Filer
from common.interfaces.solving import SolverInterface, SolvingResult
from common.mast_logging import init_log
from common.utils import Coord, boxed_log, function_name
from imagers import ImagerSettings

from .astrometry_dot_net import cygwin_to_win, generate_random_string, parse_solver_output, win_to_cygwin

logger = logging.Logger("mastrometry_dot_net")
init_log(logger)

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

        missing = False
        for i in range(0, 47):
            index_5206_file = self.index_dir / f"index-5206-{i:02d}.fits" # needed
            if not index_5206_file.exists():
                logger.warning(f"{function_name()}: RAM disk is missing index file '{index_5206_file.as_posix()}'")
                missing = True

            # index_5205_file = self.index_dir / f"index-5205-{i:02d}.fits" # niced to have
            # if not index_5205_file.exists():
            #     logger.warning(f"{function_name()}: RAM disk is missing index file '{index_5205_file.as_posix()}'")
            #     missing = True

            if missing:
                raise Exception(f"{function_name()}: RAM disk is missing some index files")

        logger.info(f"{function_name()}: RAM disk contains all needed index files")


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
    ) -> SolvingResult:
        """
        MAST specific astrometry.net solver implementation.
        The caller is expected to pass an input image that is full frame, unbinned, which gets
          downsampled by a factor 2 (2x2 binning) and cropped to the ROI (if any)
        before being sent to astrometry.net for solving.
        """

        self.unit: Unit | None = unit
        filer = Filer(logger)
        tmp_dir = generate_random_string(prefix="tmp_")
        assert filer.ram is not None
        win_tmp_dir = Path(filer.ram.root, "tmp", tmp_dir)
        win_tmp_dir.mkdir(parents=True, exist_ok=True)

        os.environ["PATH"] = (
            "C:/cygwin64/bin;/usr/lib/lapack;C:/Users/mast/PycharmProjects/MAST_unit/venv/Scripts;C:/Windows/system32;"
            + "C:/Users/mast/AppData/Local/Microsoft/WindowsApps;"
            + "C:/Program Files/MongoDB/Server/7.0/bin;C:/Users/mast/AppData/Local/Programs/Python/Launcher/;"
        )

        if full_frame_input_image_path is None:  # noqa: SIM102
            if settings is not None:
                assert (
                    settings.image_path is not None
                ), f"{function_name()}: settings.image_path is None"
                full_frame_input_image_path = settings.image_path

        assert full_frame_input_image_path is not None, f"{function_name()}: full_frame_input_image_path is None"
        original_folder = Path(full_frame_input_image_path).parent

        # Read FITS file
        with fits.open(full_frame_input_image_path) as hdul:
            header = hdul[0].header # type: ignore
            data = hdul[0].data # type: ignore

            # Get image dimensions and data type from header
            height, width = data.shape
            dtype = data.dtype
            logger.info(f"{function_name()}: Original image dimensions: {width}x{height}, dtype: {dtype}")

            refpix: tuple[int, int] | None = None
            # Crop if ROI is specified (not None)
            if settings is None or settings.roi is None:
                #
                # Full frame
                # - reference pixel is according to the phase and the configuration settings
                #
                unit_conf = unit.unit_conf if unit and unit.unit_conf else Config().get_unit()
                assert unit_conf is not None

                fcu_version = self.unit.fcu_version if self.unit else FcuVersion.v2

                assert fcu_version in unit_conf.acquisition.rois
                match phase:
                    case "sky":
                        roi = cast(SkyRoiConfig, unit_conf.acquisition.rois[fcu_version])
                        refpix = (roi.sky_x // 2, roi.sky_y // 2)
                    case "spec":
                        roi = cast(SpecRoiConfig, unit_conf.guiding.rois[fcu_version])
                        refpix = (roi.fiber_x // 2, roi.fiber_y // 2)
                logger.info(f"{function_name()}: Full frame: {refpix=})")
            else:
                #
                # Crop to ROI
                # - reference pixel is center of ROI
                #
                roi = settings.roi
                x_start, y_start = roi.x // 2, roi.y // 2
                x_end = x_start + roi.width // 2
                y_end = y_start + roi.height // 2
                data = data[y_start:y_end, x_start:x_end]
                logger.info(f"{function_name()}: ROI: {roi.width // 2}x{roi.height // 2} at ({x_start}, {y_start})")

                # Update header with new dimensions
                header['NAXIS1'] = roi.width // 2
                header['NAXIS2'] = roi.height // 2
                if 'CRPIX1' in header:
                    header['CRPIX1'] -= x_start
                if 'CRPIX2' in header:
                    header['CRPIX2'] -= y_start

            # Downsample by factor 2 (2x2 binning)
            new_height = data.shape[0] // 2
            new_width = data.shape[1] // 2

            # Reshape and average 2x2 blocks
            downsampled = data[:new_height*2, :new_width*2].reshape(
                new_height, 2, new_width, 2
            ).mean(axis=(1, 3)).astype(dtype)

            logger.info(f"{function_name()}: Downsampled by factor 2 to {new_width}x{new_height}")

            # Update header for downsampled image
            header['NAXIS1'] = new_width
            header['NAXIS2'] = new_height

            # Save downsampled image to temporary directory
            assert full_frame_input_image_path is not None
            downsampled_image_path = win_tmp_dir / f"downsampled_{str(Path(full_frame_input_image_path).name)}"
            fits.writeto(downsampled_image_path, downsampled, header, overwrite=True)
            logger.info(f"{function_name()}: Saved downsampled image to {downsampled_image_path}")

            # Build astrometry.net command
            # After downsampling by 2x: 0.2616 * 2 = 0.5232 arcsec/pixel
            effective_pixelscale = self.pixelscale * 2

            args = [

                # Scale constraints (±10% tolerance)
                "--scale-units", "arcsecperpix",
                "--scale-low", f"{0.9 * effective_pixelscale}",  # ~0.47
                "--scale-high", f"{1.1 * effective_pixelscale}",  # ~0.57

                # Index file directory
                "--index-dir", win_to_cygwin(str(self.index_dir)),

                # Performance options
                "--no-plots",
                "--overwrite",
                "--cpulimit", "30",  # timeout in seconds
                "--solved", "none",
                "--match", "none",
                "--rdls", "none",
                "--corr", "none",

                # Output control
                "--dir", win_to_cygwin(str(win_tmp_dir)),
                "--temp-dir", win_to_cygwin(str(win_tmp_dir)),
            ]

            # Add RA/Dec hint if target is provided (significantly speeds up solving)
            if target is not None:
                args += [
                    "--ra", f"{target.ra.deg}",
                    "--dec", f"{target.dec.deg}",
                    "--radius", "2.0",  # search radius in degrees
                ]

            # Additional recommended options
            args += [
                "--downsample", "1",  # already downsampled, don't do it again
                "--no-tweak",  # skip SIP distortion (faster)
            ]

            if refpix is None:
                args += ["--crpix-center"]
            else:
                args += [
                    "--crpix-x", str(refpix[0]),
                    "--crpix-y", str(refpix[1]),
                ]

            new_fits_path = Path(str(downsampled_image_path).replace(
                ".fits", f",solver={self.name}.fits").replace("downsampled_", ""))
            args += [
                "--new-fits", win_to_cygwin(str(new_fits_path)),
                win_to_cygwin(str(downsampled_image_path)),
            ]

        command = " ".join([r"C:/cygwin64/usr/local/astrometry/bin/solve-field"] + args)
        logger.info(f"{function_name()}: Running astrometry.net with {command}")
        start = datetime.datetime.now()

        completed_process = subprocess.run(command, capture_output=True, shell=True)
        stdout_lines = completed_process.stdout.decode().strip().splitlines()
        stderr_lines = completed_process.stderr.decode().strip().splitlines()
        elapsed = datetime.datetime.now() - start
        logger.info(
            f"{'succeeded' if completed_process.returncode == 0 else 'failed'}"
            + f" in {elapsed.total_seconds():.2f} seconds"
        )

        result_file = cygwin_to_win(str(new_fits_path)).replace(".fits", "-result.txt")
        with open(result_file, "w") as file:
            file.write("--- command ---\n")
            file.write(f"{command}\n")
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
            if ret.succeeded and ret.solution is not None:
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
                    and self.unit.acquirer.latest_acquisition is not None
                ):
                    self.unit.acquirer.latest_acquisition.solver_data = {
                        "index_file": ret.solution.index_file
                    }

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

        Thread(target=self.cleanup, args=([Path(result_file), Path(new_fits_path)], original_folder, win_tmp_dir)).start()
        return ret

    def cleanup(self, files_to_move: list[Path], target_folder: Path, tmp_dir: Path):
        for file in files_to_move:
            shutil.move(file, target_folder / file.name)
            logger.info(f"{function_name()}: saved '{(target_folder / file.name).as_posix()}'")
        shutil.rmtree(tmp_dir)

    def solve_and_correct(self):
        pass

if __name__ == "__main__":
    def test_solver():
        solver = MastrometryDotNet()
        result = solver.solve(
            unit=None,
            phase="spec",
            full_frame_input_image_path="D:\\tmp\\mastrometry\\full-frame.fits",
        )
        print(json.dumps(result.to_dict(), indent=2))

    test_solver()
