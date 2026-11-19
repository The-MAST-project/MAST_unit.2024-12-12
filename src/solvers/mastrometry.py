import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, cast

from astropy.coordinates import Angle
from astropy.io import fits

from common.config import Config
from common.config.rois import FcuVersion, SkyRoiConfig, SpecRoiConfig
from common.const import Const
from common.filer import Filer, MoveGuardian
from common.interfaces.solving import SolverInterface, SolvingResult
from common.mast_logging import get_logger
from common.models.statuses import ImagerRoi
from common.rois import SkyRoi
from common.utils import Coord, boxed_log, function_name
from imagers import ImagerSettings
from phd2.limit_frame import derived_guiding_roi

from .astrometry_dot_net import generate_random_string, parse_solver_output, win_to_cygwin
from .pixel_grid import roi_center_to_crpix
from .ram_area import index_dir, missing_indexes, scratch_dir
from .selection_stage import FIT_ORDER, SourceSelectionSettings, StageResult, prepare, resolve_settings, solved_fits_from_wcs
from .star_map import write_star_map
from .survey_solve import write_xylist

logger = get_logger(__name__)
if TYPE_CHECKING:
    from unit import Unit  # type: ignore[import-untyped]


#: Simulated binning applied to the full frame before solving, to speed astrometry.net up.
#:
#: Module level because it is not private arithmetic: a solved `pixel_scale` is per
#: DOWNSAMPLED pixel, so anyone converting it back to a detector scale must divide by
#: exactly this. A caller that hardcoded 2 would be silently wrong the day it changed --
#: and the flux-metering run now derives its plate scale from a solve.
DOWNSAMPLE_FACTOR: int = 2


def downsampled_frame_path(scratch: Path, input_image_path: str) -> Path:
    """Where the frame `solve-field` is given is written: in the scratch folder, `downsampled_<input name>`.

    One place, because the star map has to find that exact file. Not by a glob: `solve-field`
    leaves `downsampled_<input stem>.axy` beside it, which a pattern would match first.
    """
    return scratch / f"downsampled_{Path(input_image_path).name!s}"


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

        # `ram_area.index_dir()`, not `Path(ram.drive, ...)`: Location has had one canonical
        # root, and no `drive`, since MAST_common#183 -- the old line raised AttributeError.
        self.index_dir = index_dir()
        if (why := missing_indexes(self.index_dir)) is not None:
            raise Exception(f"{function_name()}: {why}")

    @property
    def name(self) -> str:
        return "mastrometry.net"

    def solve(
        self,
        unit,
        phase: Const.SolvingPhase,
        settings: ImagerSettings | None = None,
        full_frame_input_image_path: str | None = None,
        index_file: str | None = None,
        target: Coord | None = None,
        keep_artifacts: bool = True,
        source_selection: SourceSelectionSettings | None = None,
        artifacts_dir: Path | None = None,
        fibre_xy: tuple[int, int] | None = None,
    ) -> SolvingResult | None:
        """
        MAST specific astrometry.net solver implementation.

        The caller is expected to pass an input image that is full frame, unbinned, which gets
          downsampled by a factor 2 (2x2 binning) and cropped to the ROI (if any)
          before being sent to astrometry.net for solving.

        The solved FITS and the result text are named `<input stem>,solver=<name>.fits` and
        `<input stem>,solver=<name>-result.txt`. With `keep_artifacts` they are saved beside
        the input and follow it to the shared area; without it they are discarded with the
        scratch folder, for callers that solve every few minutes and record the result
        themselves.

        `source_selection` overrides the unit's `solving.source_selection` configuration (the survey
        passes it; the unit leaves it out). Off, the solve is what it always was.

        `artifacts_dir` (the survey passes it): the solver's artifacts -- the result text, the solved
        FITS, the correspondences, the xylist and WCS of an xylist solve, the star map -- are put
        there, synchronously, and are not handed to the mover or put beside the input frame, which
        is only read. Without it, the artifacts follow the frame to the shared area as always.

        `fibre_xy` (the survey passes it): the fibre's pixel, 0-based, in the full unbinned frame, as the unit's
        `guiding.rois[...]` holds it. For a frame solved whole (no ROI) it becomes CRPIX, so the solved RA/Dec is
        the sky at the fibre -- as it is in production, where the guiding ROI is centred on the fibre. Without it,
        a whole frame is solved with CRPIX at its centre.
        """

        self.unit: Unit | None = unit

        if full_frame_input_image_path is None:  # noqa: SIM102
            if settings is not None:
                assert settings.image_path is not None, f"{function_name()}: settings.image_path is None"
                full_frame_input_image_path = settings.image_path

        assert full_frame_input_image_path is not None, f"{function_name()}: full_frame_input_image_path is None"

        #
        # A temporary folder is created on the RAM disk for the solving process, and deleted afterwards.
        # This is needed to speed up the solving process by using the RAM disk.
        #
        filer = Filer(logger)
        assert filer.ram is not None
        # Checked per solve as well as at start-up: the RAM disk, and the indexes with it, can
        # go while the unit runs. Said plainly rather than as the solver's generic failure.
        if (why := missing_indexes(self.index_dir)) is not None:
            raise Exception(f"{function_name()}: {why}")
        # On the RAM area's current root -- its fallback while the disk is gone -- not on the
        # root the Filer was built with.
        win_tmp_dir = scratch_dir(generate_random_string(prefix="tmp_"))

        # From the stem, not by editing the name: an input that is not *.fits (PHD2's
        # sav*.tmp guide frames) would otherwise give both artifacts the same path.
        artifact_stem = f"{Path(full_frame_input_image_path).stem},solver={self.name}"
        new_fits_path = win_tmp_dir / f"{artifact_stem}.fits"
        result_file = win_tmp_dir / f"{artifact_stem}-result.txt"

        try:
            return self._solve_in(
                win_tmp_dir,
                new_fits_path,
                result_file,
                phase,
                full_frame_input_image_path,
                target,
                source_selection,
                fibre_xy,
            )
        finally:
            # The solved frame (it carries the WCS -- MAST_unit#27) and the solver's own
            # output are saved beside the frame they came from, then follow it to the shared
            # area. In `finally`, so a solve or read-back that raises still removes the scratch.
            downsampled = downsampled_frame_path(win_tmp_dir, full_frame_input_image_path)
            if artifacts_dir is not None:
                stem = new_fits_path.stem
                self._run_logged(
                    self.cleanup,
                    [result_file, new_fits_path, *(win_tmp_dir / f"{stem}{ext}" for ext in (".corr", ".xyls", ".wcs"))],
                    artifacts_dir,
                    win_tmp_dir,
                    new_fits_path,
                    downsampled,
                    False,
                )
            else:
                Thread(
                    target=self._run_logged,
                    name="mastrometry-cleanup",
                    args=(
                        self.cleanup,
                        [result_file, new_fits_path] if keep_artifacts else [],
                        Path(full_frame_input_image_path).parent,
                        win_tmp_dir,
                        new_fits_path if keep_artifacts else None,
                        downsampled,
                    ),
                ).start()

    def _solve_in(  # noqa: C901
        self,
        win_tmp_dir: Path,
        new_fits_path: Path,
        result_file: Path,
        phase: Const.SolvingPhase,
        full_frame_input_image_path: str,
        target: Coord | None,
        source_selection: SourceSelectionSettings | None = None,
        fibre_xy: tuple[int, int] | None = None,
    ) -> SolvingResult | None:
        unit = self.unit

        # solve-field.exe is a cygwin binary:
        #  - C:\cygwin64\bin is needed so Windows can load cygwin1.dll
        #  - /usr/lib/lapack is a cygwin POSIX path (cygwin1.dll re-parses PATH at
        #    startup); numpy's lapack_lite extension needs it when removelines runs.
        # Pass via env= so we don't mutate the parent process's PATH.
        env = os.environ.copy()
        env["PATH"] = r"C:\cygwin64\bin" + os.pathsep + "/usr/lib/lapack" + os.pathsep + env.get("PATH", "")

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

            downsample_factor: int = DOWNSAMPLE_FACTOR

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
                    from common import asi

                    camera_x_size = asi.ASI_294MM_WIDTH
                    camera_y_size = asi.ASI_294MM_HEIGHT
                    assert fcu_version in unit_conf.guiding.rois
                    cfg = cast(SpecRoiConfig, unit_conf.guiding.rois[fcu_version])

                    imager_roi = derived_guiding_roi(cfg, camera_x_size, camera_y_size, unit_conf.guiding.binning)

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
            elif fibre_xy is not None:
                # The whole frame, with CRPIX at the fibre: the crop starts at 0, so the same conversion as an ROI's.
                refpix = (
                    roi_center_to_crpix(fibre_xy[0], 0, downsample_factor),
                    roi_center_to_crpix(fibre_xy[1], 0, downsample_factor),
                )
                logger.info(f"{function_name()}: No ROI, using full frame, refpix at the fibre {fibre_xy} -> {refpix}")
            else:
                logger.info(f"{function_name()}: No ROI, using full frame, refpix=center")

            #
            # The source selection (solvers/selection_stage.py), on the cropped, unbinned frame.
            # Off by default: with it off nothing between here and the solve changes.
            #
            selection_settings = resolve_settings(unit_conf, source_selection)
            stage: StageResult | None = None
            if selection_settings.mode != "off":
                stage = prepare(
                    data,
                    header,
                    phase,
                    selection_settings,
                    origin=(imager_roi.x, imager_roi.y) if imager_roi is not None else (0, 0),
                    frame_shape=(height, width),
                )
                if (why := stage.failed) is not None:
                    logger.error(f"{function_name()}: source selection: {why}, the frame is not solved")
                    failure = SolvingResult(succeeded=False, errors=[f"source selection: {why}"])
                    failure.selection = stage.record()
                    return failure
                data = stage.data  # darkened, for shadow_only; the input otherwise

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
            downsampled_image_path = downsampled_frame_path(win_tmp_dir, full_frame_input_image_path)
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
                # The correspondences -- the field stars matched to the index -- are the stars
                # the solution used. Kept in the scratch folder for the star map (see
                # solvers/star_map.py); the artifact name comes from the solved FITS's stem.
                "--corr",
                win_to_cygwin(str(new_fits_path.parent / f"{new_fits_path.stem}.corr")),
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

            image_args = [
                *args,
                "--new-fits",
                win_to_cygwin(str(new_fits_path)),
                win_to_cygwin(str(downsampled_image_path)),
            ]

            # The selected sources as an xylist, in the binned, cropped grid the image solve uses
            # (FRAGILE COORDINATE SURFACE: write_xylist converts through pixel_grid). There is no
            # image for --new-fits to annotate, so solve-field writes the WCS and the solved FITS
            # is made from it afterwards. The image solve above stays as the fallback.
            xylist_args: list[str] | None = None
            wcs_path = new_fits_path.parent / f"{new_fits_path.stem}.wcs"
            if stage is not None and stage.selection is not None:
                xylist_path = new_fits_path.parent / f"{new_fits_path.stem}.xyls"
                write_xylist(xylist_path, stage.selection.xylist(), downsample_factor, selection_settings.xylist_origin)
                xylist_args = [
                    *args,
                    "--wcs",
                    win_to_cygwin(str(wcs_path)),
                    "--new-fits",
                    "none",
                    "--width",
                    str(downsampled_width),
                    "--height",
                    str(downsampled_height),
                    "--x-column",
                    "X",
                    "--y-column",
                    "Y",
                    "--sort-column",
                    "FLUX",
                    "--uniformize",
                    "0",  # the selection has already spread the stars
                    "--tweak-order",
                    str(FIT_ORDER),  # tweak stays ON; only the position at CRPIX matters
                    win_to_cygwin(str(xylist_path)),
                ]

        attempts: list[tuple[str, list[str]]] = [("image", image_args)]
        if xylist_args is not None:
            attempts.insert(0, ("xylist", xylist_args))
        earlier: list[str] = []  # what failed attempts said, for the result file

        # solve-field writes new_fits_path (the solved frame, carrying the WCS). Protect it
        # for the duration: it keeps the mover off a half-written file, and -- since
        # protect() is also what marks a path as a product -- it is what stops
        # MoveGuardian.release_folder from discarding it as scratch. Every other writer in
        # the fleet does this; this solver was the one that did not.
        with MoveGuardian().protect(str(new_fits_path)):
            for kind, attempt_args in attempts:
                command = " ".join([r"C:/cygwin64/usr/local/astrometry/bin/solve-field"] + attempt_args)
                logger.info(f"{function_name()}: Running astrometry.net ({kind}) with '{command}'")
                start = time.monotonic()
                completed_process = subprocess.run(
                    command, capture_output=True, shell=True, env=env, check=False
                )  # returncode is inspected below
                elapsed = time.monotonic() - start
                stdout_lines = completed_process.stdout.decode().strip().splitlines()
                stderr_lines = completed_process.stderr.decode().strip().splitlines()
                if kind == "xylist":
                    solved = completed_process.returncode == 0 and wcs_path.exists()
                    if solved:
                        try:
                            solved_fits_from_wcs(downsampled_image_path, wcs_path, new_fits_path)
                        except Exception as e:  # noqa: BLE001 -- any failure here sends us to the image solve
                            logger.error(f"{function_name()}: could not make the solved FITS from the WCS: {e}")
                            solved = False
                    if not solved:
                        logger.warning(f"{function_name()}: the xylist solve failed, solving the image instead")
                        earlier.append(
                            f"--- attempt: xylist (failed) ---\n{command}\nreturncode: {completed_process.returncode}\n"
                            + "\n".join([*stdout_lines, *stderr_lines])
                            + f"\nelapsed: {elapsed:.2f} seconds\n\n"
                        )
                        continue
                    if stage is not None:
                        stage.solved_from = "xylist"
                else:
                    if stage is not None and stage.selection is not None:
                        stage.solved_from = "fallback"
                logger.info(
                    f"{function_name()}: {'succeeded' if completed_process.returncode == 0 else 'failed'}"
                    + f" in {elapsed:.2f} seconds"
                )
                break

        # Protected for the same two reasons as new_fits_path above: no half-written file
        # for the mover, and it counts as a product rather than as scratch.
        with MoveGuardian().protect(str(result_file)), open(result_file, "w") as file:
            file.writelines(earlier)
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
            file.writelines(f"elapsed: {elapsed:.2f} seconds\n")

        if completed_process.returncode == 0:
            ret = parse_solver_output(stdout_lines)
            if ret is not None and ret.solution is not None and ret.succeeded:
                boxed_log(
                    logger=logger,
                    lines=[
                        "FUTURE: image quality check",
                        f"#sources {ret.solution.sources}",
                        f"#matched {ret.solution.matched_stars}",
                        *([f"selection {stage.record()}"] if stage is not None else []),
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

                # The CD matrix, from the same header. Carried so a caller can convert a
                # pixel offset into a sky offset without assuming a parity: rotation alone
                # does not say whether the field is mirrored, and guessing wrong flips RA.
                if all(k in header for k in ("CD1_1", "CD1_2", "CD2_1", "CD2_2")):
                    ret.solution.cd1_1 = float(header["CD1_1"])
                    ret.solution.cd1_2 = float(header["CD1_2"])
                    ret.solution.cd2_1 = float(header["CD2_1"])
                    ret.solution.cd2_2 = float(header["CD2_2"])
                else:
                    logger.warning(f"no CD matrix in solved FITS header of '{new_fits_path}'")
        else:
            ret = SolvingResult(
                succeeded=False,
                errors=[
                    f"Exit status: {completed_process.returncode}",
                    ", ".join(stderr_lines),
                ],
            )

        if ret is not None and stage is not None:
            ret.selection = stage.record()
        return ret

    @staticmethod
    def _run_logged(target, *args):
        """Run `target` and log anything it raises.

        A bare Thread(target=...) sends an exception to threading's excepthook, i.e. to
        stderr -- which for the unit service is a file nobody reads. That is how a plain
        TypeError in cleanup() stayed invisible for months.
        """
        try:
            target(*args)
        except Exception:
            # logger.exception attaches the traceback itself; naming the exception here too
            # would only duplicate it.
            logger.exception(f"{function_name()}: {getattr(target, '__name__', target)} failed")

    def _star_map_files(self, tmp_dir: Path, solved_fits: Path, frame: Path) -> list[Path]:
        """The annotated JPEG and the per-star JSON of this solve, written in the scratch folder.

        `[]` for a solve that left no correspondences (it failed), and for anything that goes
        wrong here: a picture must never cost the artifacts that were kept before it existed.
        """
        stem = solved_fits.stem
        jpeg_path = tmp_dir / f"{stem}-stars.jpg"
        json_path = tmp_dir / f"{stem}-stars.json"
        try:
            if not frame.exists():
                return []
            # Protected like the other artifacts: no half-written file for the mover, and a
            # product, not scratch, when the folder is discarded.
            with MoveGuardian().protect(str(jpeg_path), str(json_path)):
                summary = write_star_map(
                    image_path=frame,
                    corr_path=tmp_dir / f"{stem}.corr",
                    solved_fits=solved_fits,
                    scratch=tmp_dir,
                    jpeg_path=jpeg_path,
                    json_path=json_path,
                )
            if summary is None:
                return []
            logger.info(f"{function_name()}: star map of '{stem}': {summary}")
            return [jpeg_path, json_path]
        except Exception:
            logger.exception(f"{function_name()}: could not make the star map of '{stem}'")
            return []

    def cleanup(
        self,
        files_to_move: list[Path],
        target_folder: Path,
        tmp_dir: Path,
        solved_fits: Path | None = None,
        solved_frame: Path | None = None,
        to_share: bool = True,
    ):
        """Keep the solver's own artifacts, discard its scratch.

        `target_folder` is the acquisition folder holding the input frame, so the solved
        FITS and the result text land beside it and are then handed to the mover -- the
        frame itself is moved by Solver.solve, which owns it. With `solved_fits` and
        `solved_frame` -- given when the artifacts are kept -- the star map of the solve goes
        with them.
        """
        filer = Filer(logger)
        if solved_fits is not None and solved_frame is not None:
            files_to_move = [*files_to_move, *self._star_map_files(tmp_dir, solved_fits, solved_frame)]
        files_to_move = [f for f in files_to_move if f.exists()]
        if files_to_move:
            target_folder.mkdir(parents=True, exist_ok=True)
            for file in files_to_move:
                destination = target_folder / file.name
                shutil.move(file, destination)
                logger.info(f"{function_name()}: saved '{destination.as_posix()}'")
                # Via the mover rather than a bare copy, so a dead share defers instead of
                # losing the file, exactly as for every other artifact.
                if to_share:
                    filer.move_ram_to_shared(str(destination))

        # Only now: the artifacts worth keeping are out, the rest is astrometry.net
        # intermediates (.axy/.xyls/.wcs and the downsampled frame). Discarded, not just
        # removed: the result was written under protect(), which claimed this folder and
        # recorded it as a product, and a bare rmtree left the claim's handle, its
        # `.tmp_<id>.lock` beside the folder and the records behind -- one of each per solve.
        MoveGuardian().discard_folder(tmp_dir)

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
        from common.models.statuses import ImagerRoi, ImagerSettings

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
