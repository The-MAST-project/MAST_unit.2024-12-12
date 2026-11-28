"""`MastGuider`: the in-house guider's `GuiderInterface` backend -- the hardware shell around the pure
`GuideCycle` (design §3).

On `start_guiding` (blocking; the facade runs it on a thread, as it does the solving backend):

1. **Reuse the acquisition's spec-mirror-in solve** (Option A -- no per-frame solve): its
   `SolvingSolution` (CRVAL + CD matrix, on the binned grid) becomes a `PixelToSky`, with the fibre as
   CRPIX. The CD matrix carries parity, so no RA sign is guessed.
2. **Capture one binning-2 full frame** for the reference and choose the fixed guide-star set
   (`MastGuiderCore.reference`), in the region around the fibre, growing until enough good stars.
3. **Loop**: capture a binning-2 full frame in memory (ZWO imager, `start_exposure` /
   `wait_for_image_ready` / `image_array` -- no file), run one `GuideCycle`, send a correction to the
   mount when the cycle says so, and record the cycle in the nightly guide log. Hold the cadence.

Guiding runs while `UnitActivities.Guiding` is set; `stop_guiding` clears it and the loop unwinds
(ends the exposure series, closes and ships the guide log). Binning is fixed at 2 and the ROI is the
whole frame, disregarding the configured guiding ROI/binning -- the mast guider's one grid, shared
with the reused solve (see `acquirer._mast_full_frame_binned2`). The decision logic is pure and
unit-tested in `guide_cycle`; this part drives hardware and is validated on a unit.
"""

from __future__ import annotations

import datetime
import os
import time
from pathlib import Path

from astropy.coordinates import Angle

from common.activities import Activities, UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.filer import Filer
from common.interfaces.guiding import GuiderInterface
from common.mast_logging import get_logger, observing_night
from common.paths import PathMaker
from common.utils import Coord, boxed_log, function_name
from solvers.source_selection import SelectionParams

from .core import MastGuiderCore, NoMeasurementError
from .correction_policy import CorrectionPolicy
from .guide_cycle import APPLIED, GuideCycle
from .guide_log import ALERT, REFERENCE_SHIFT, VALIDATION, GuideLog
from .mount_corrector import MountCorrector
from .pixel_to_sky import PixelToSky
from .validation import VALID_ALERT, VALID_SHIFT, GuideValidator

logger = get_logger(__name__)

#: The mast guider always images the whole frame at binning 2 (asking the camera, not downsampling in
#: code), on the reused solve's grid. Not from config -- it is what "mast" means.
GUIDE_BINNING = 2
#: The ASI full-well in the RAW16 container: the saturation level for the reference star selection,
#: which has no FITS header to read SATURATE from.
FULL_SCALE_ADU = 65535.0
#: How often the loop runs an inline validation solve (design §3.3, "once a minute"). Time-based, so
#: it is independent of the cadence; a validation round pauses guiding for one solve, which is benign.
VALIDATION_INTERVAL_SECONDS = 60.0


class MastGuider(GuiderInterface):
    """Drive the guide loop on binning-2 full frames, tracking a fixed star set against the reused
    spec-mirror-in solve. Implements the `GuiderInterface`; built by the `Guider` facade for the
    ``"mast"`` method."""

    def __init__(self, unit):
        Activities.__init__(self)
        if unit is None:
            raise ValueError(f"{function_name()}: unit is None")
        self.unit = unit
        self._exposure_series = None
        self._guide_log: GuideLog | None = None
        self._cycle: GuideCycle | None = None
        self._core: MastGuiderCore | None = None
        self._validator: GuideValidator | None = None
        self._target: Coord | None = None
        self._validation_folder: str | None = None
        self._last_validation: float = 0.0

    # --- GuiderInterface -------------------------------------------------------------------------

    def start_guiding(self) -> CanonicalResponse:
        """Set up from the acquisition solve + a reference frame, then loop until `stop_guiding`.
        Blocks; returns a failure response if the setup cannot be made (the loop never started)."""
        phase = "mast-guiding"
        boxed_log(logger, [f"starting '{phase.upper()}'"])
        try:
            pixel_to_sky, fibre, core = self._build_measurement()
            reference = self._acquire_reference(core, fibre)
            self._setup_validation(pixel_to_sky)
        except Exception as ex:  # noqa: BLE001 -- any setup failure is reported, not raised onto the thread
            logger.exception(f"{phase}: could not set up guiding")
            return CanonicalResponse(errors=[f"{phase}: setup failed: {ex}"])

        conf = self.unit.unit_conf.guiding
        policy = CorrectionPolicy(
            min_move_ra_arcsec=conf.min_ra_correction_arcsec,
            min_move_dec_arcsec=conf.min_dec_correction_arcsec,
        )
        self._core = core
        self._cycle = GuideCycle(core, pixel_to_sky, policy)
        corrector = MountCorrector(self.unit.pw, self.unit.mount)
        self._guide_log = self._open_guide_log(conf, reference)

        self._exposure_series = self.unit.imager.start_exposure_series(purpose=phase)
        self.unit.start_activity(UnitActivities.Guiding)
        self._last_validation = time.monotonic()  # first validation one interval in, not at once
        try:
            while self.unit.is_active(UnitActivities.Guiding):
                start = time.monotonic()
                self._guide_step(corrector)
                self._maybe_validate()
                self._hold_cadence(start, conf.cadence_seconds)
        finally:
            self._teardown(phase)
        return CanonicalResponse_Ok

    def stop_guiding(self):
        """Clear the Guiding flag; the loop sees it and unwinds (teardown ships the guide log)."""
        self.unit.end_activity(UnitActivities.Guiding)

    def status(self):
        return None

    def guider_status(self):
        """The facade's `status()` asks the backend for this; the mast guider has no extra payload."""
        return None

    @property
    def is_guiding(self) -> bool:
        return self.unit.is_active(UnitActivities.Guiding)

    # --- setup -----------------------------------------------------------------------------------

    def _build_measurement(self) -> tuple[PixelToSky, tuple[float, float], MastGuiderCore]:
        """The pixel->sky map from the reused spec-mirror-in solve, the fibre pixel (binned), and a
        fresh measurement core. Raises if there is no usable solve to reuse."""
        result = getattr(self.unit.required_solver, "latest_result", None)
        solution = getattr(result, "solution", None) if result is not None else None
        if solution is None:
            raise RuntimeError("no spec-mirror-in solve to reuse (solver.latest_result has no solution)")
        fibre = self._fibre_pixel_binned()
        crpix = (fibre[0] + 1.0, fibre[1] + 1.0)  # FITS 1-based; the absolute value is not critical
        pixel_to_sky = PixelToSky.from_solution(solution, crpix=crpix)
        core = MastGuiderCore(params=SelectionParams(full_scale=FULL_SCALE_ADU))
        return pixel_to_sky, fibre, core

    def _fibre_pixel_binned(self) -> tuple[float, float]:
        """The fibre pixel on the binning-2 grid, from the configured full-frame ``fiber_x/fiber_y``
        (guiding ROI for this unit's FCU version). This is the search centre for the star selection
        and, +1, the WCS CRPIX."""
        conf = self.unit.unit_conf.guiding
        cfg = conf.rois[self.unit.fcu_version]
        fx, fy = getattr(cfg, "fiber_x", None), getattr(cfg, "fiber_y", None)
        if fx is None or fy is None:
            raise RuntimeError(f"no configured fibre pixel for {self.unit.fcu_version} (fiber_x/fiber_y)")
        return (float(fx) / GUIDE_BINNING, float(fy) / GUIDE_BINNING)

    def _acquire_reference(self, core: MastGuiderCore, fibre: tuple[float, float]):
        """Capture one binning-2 full frame and choose the fixed guide-star set around the fibre."""
        frame = self._capture()
        return core.reference(frame, optical_center=fibre, low_coma_radius=None)

    def _setup_validation(self, pixel_to_sky: PixelToSky) -> None:
        """Build the inline validator (design §3.3) and stash what a validation solve needs: the
        acquisition target (the solve's hint) and a folder for its frames. The tolerance is the
        guiding box's radius (quadrature of the per-axis tolerances)."""
        conf = self.unit.unit_conf.guiding
        tolerance_arcsec = float((conf.tolerance.ra_arcsec**2 + conf.tolerance.dec_arcsec**2) ** 0.5)
        self._validator = GuideValidator(pixel_to_sky, tolerance_arcsec)
        acquisition = self.unit.acquirer.latest_acquisition
        self._target = Coord(
            ra=Angle(acquisition.target_ra, unit="hourangle"),
            dec=Angle(acquisition.target_dec, unit="deg"),
        )
        self._validation_folder = os.path.join(acquisition.folder, "mast-validation")

    # --- the loop --------------------------------------------------------------------------------

    def _guide_step(self, corrector: MountCorrector) -> None:
        """One capture -> decide -> (correct) -> log. A failure in a single cycle is logged and
        skipped -- it must not end the session."""
        try:
            frame = self._capture()
            result = self._cycle.cycle(frame)
            if result.decision == APPLIED and result.correction_arcsec is not None:
                corrector.apply(result.correction_arcsec[0], result.correction_arcsec[1])
            self._log_cycle(result)
        except NoMeasurementError as ex:  # defensive: cycle() already maps loss to a decision
            logger.warning(f"guide cycle: no measurement: {ex}")
            if self._guide_log is not None:
                self._guide_log.event(ALERT, reason="no_measurement", detail=str(ex))
        except Exception as ex:  # noqa: BLE001 -- one bad frame/correction must not kill the loop
            logger.exception("guide cycle failed")
            if self._guide_log is not None:
                self._guide_log.event(ALERT, reason="cycle_error", detail=str(ex))

    # --- inline validation (design §3.3) ---------------------------------------------------------

    def _maybe_validate(self) -> None:
        """Every `VALIDATION_INTERVAL_SECONDS`, solve the current pointing and act on the verdict. The
        solve is its own exposure (through the unit solver), so guiding pauses for it -- benign once a
        minute. Never raises: a validation failure must not end the session."""
        if self._validator is None:
            return
        now = time.monotonic()
        if now - self._last_validation < VALIDATION_INTERVAL_SECONDS:
            return
        self._last_validation = now
        try:
            solution = self._solve_now()
        except Exception:  # noqa: BLE001 -- a solve failure is a failed validation, not a crash
            logger.exception("validation solve failed")
            solution = None
        self._act_on_validation(self._validator.validate(solution))

    def _act_on_validation(self, result) -> None:
        """A correctable error shifts the reference (the loop drives it out, clamped and ramped); an
        out-of-position / repeated-failure alert is flagged, not chased; otherwise just record it."""
        if result.decision == VALID_SHIFT and result.shift_px is not None:
            dx, dy = result.shift_px
            self._core.shift_reference(dx, dy)
            self._cycle.note_reference_shift(dx, dy)
            logger.info(f"validation: reference shifted by ({dx:.2f}, {dy:.2f}) px (error {result.error_arcsec:.2f}\")")
            if self._guide_log is not None:
                self._guide_log.event(REFERENCE_SHIFT, shift_px=[dx, dy], error_arcsec=result.error_arcsec)
        elif result.decision == VALID_ALERT:
            logger.error(f"validation ALERT: {result.reason} (error={result.error_arcsec})")
            if self._guide_log is not None:
                self._guide_log.event(ALERT, reason=result.reason, error_arcsec=result.error_arcsec)
        else:  # VALID_OK or VALID_SOLVE_FAILED
            if self._guide_log is not None:
                self._guide_log.event(
                    VALIDATION, result=result.decision, error_arcsec=result.error_arcsec, reason=result.reason
                )

    def _solve_now(self):
        """A fresh full-frame binning-2 solve of the current pointing, through the unit solver (which
        takes its own exposure and sets CRPIX at the fibre). Returns the `SolvingSolution` or None."""
        settings = self._capture_settings(save=True, base_folder=self._validation_folder)
        self.unit.start_activity(UnitActivities.Solving)
        try:
            result = self.unit.required_solver.solve(settings, self._target, phase="spec")
        finally:
            self.unit.end_activity(UnitActivities.Solving)
        if result is None or not getattr(result, "succeeded", False) or getattr(result, "solution", None) is None:
            return None
        return result.solution

    def _capture(self):
        """A binning-2 full frame, in memory (no file). A *fresh* `ImagerSettings` each call: the ZWO
        backend halves ``roi`` in place during `start_exposure` (a shallow copy shares the ROI), so a
        reused settings object would shrink the frame every exposure."""
        settings = self._capture_settings()
        self.unit.imager.start_exposure(settings)
        self.unit.imager.wait_for_image_ready()
        frame = self.unit.imager.image_array
        if frame is None:
            raise RuntimeError("imager returned no image")
        return frame

    def _capture_settings(self, *, save: bool = False, base_folder: str | None = None):
        """A full-frame, binning-2 exposure at the guiding exposure/gain (fresh each call). ``save``
        writes it to disk under ``base_folder`` (for the validation solve, which reads from a file);
        the guide loop leaves it in memory."""
        from common.asi import ASI_294MM_HEIGHT, ASI_294MM_WIDTH  # noqa: PLC0415
        from common.models.statuses import ImagerPixel, ImagerRoi, ImagerSettings  # noqa: PLC0415

        conf = self.unit.unit_conf.guiding
        roi = ImagerRoi(x=0, y=0, width=ASI_294MM_WIDTH, height=ASI_294MM_HEIGHT)
        roi._center = ImagerPixel(x=ASI_294MM_WIDTH // 2, y=ASI_294MM_HEIGHT // 2)
        return ImagerSettings(
            seconds=conf.exposure, gain=conf.gain, binning=GUIDE_BINNING, roi=roi, save=save, base_folder=base_folder
        )

    def _hold_cadence(self, start: float, cadence_seconds: int) -> None:
        if not cadence_seconds:
            return
        remaining = (start + cadence_seconds) - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    # --- guide log ------------------------------------------------------------------------------

    def _open_guide_log(self, conf, reference) -> GuideLog | None:
        """Open the nightly guide log on the local RAM disk, tagged with a fresh session id. Never
        fails the guide start -- a log that cannot be opened is logged and left as None."""
        try:
            now = datetime.datetime.now(datetime.UTC)
            night = observing_night(now)
            session_id = f"{night}T{now:%H%M%S}Z"
            filer = Filer()
            ram = filer.ram or filer.local
            local_dir = PathMaker.make_observing_night_folder(ram.root)
            path = Path(local_dir) / f"guide-{night}.jsonl"
            config = {
                "exposure": conf.exposure,
                "binning": GUIDE_BINNING,
                "gain": conf.gain,
                "cadence_seconds": conf.cadence_seconds,
                "min_ra_correction_arcsec": conf.min_ra_correction_arcsec,
                "min_dec_correction_arcsec": conf.min_dec_correction_arcsec,
            }
            guide_stars = {"n": reference.n, "sigma_window": reference.sigma_window}
            log = GuideLog(path, session_id, config=config, guide_stars=guide_stars)
            logger.info(f"guide log '{path}' session '{session_id}'")
            return log
        except Exception:  # noqa: BLE001 -- the guide log must never fail the guide start
            logger.exception("could not open the guide log; guiding without one")
            return None

    def _log_cycle(self, result) -> None:
        if self._guide_log is None:
            return
        m = result.measurement
        self._guide_log.cycle(
            decision=result.decision,
            dx=(m.dx if m is not None else None),
            dy=(m.dy if m is not None else None),
            error_arcsec=result.error_arcsec,
            correction_arcsec=result.correction_arcsec,
            scatter_px=result.scatter_px,
            n_used=(m.n_used if m is not None else None),
        )

    def _teardown(self, reason: str) -> None:
        """End the exposure series and close + ship the guide log. Best-effort: each step is guarded
        so a failure in one still runs the rest."""
        if self._exposure_series is not None:
            try:
                self.unit.imager.end_exposure_series(self._exposure_series)
            except Exception:  # noqa: BLE001
                logger.exception("could not end the exposure series")
            self._exposure_series = None
        if self._guide_log is not None:
            try:
                self._guide_log.end(reason=reason)
                self._guide_log.ship_to(PathMaker.make_observing_night_folder(Filer().shared.root))
            except Exception:  # noqa: BLE001
                logger.exception("could not close/ship the guide log")
            self._guide_log = None
        boxed_log(logger, ["guiding stopped"])
