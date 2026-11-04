"""``PUT /unit/focus_on_star``: one isolated star on the fibre pixel, then a narrow-field V-curve.

An on-demand refinement, run once the unit is near focus -- after ``/calibrate/focuser``, or,
failing a focuser calibration product, at ``focuser.known_as_good_position``:

1. **Sky position** -- the zenith, an explicit (ra, dec), or where the telescope points.
2. **Star** -- the GAIA DR3 catsHTM catalog is searched around it for the brightest
   star no brighter than ``star_magnitude`` with nothing brighter within
   ``isolation_radius_px`` (:mod:`focus_on_star.star_selection`).
3. **Acquire** -- slew, then solve-and-correct exactly as the acquirer's ``spec``
   phase does, so the star lands on the fibre pixel ``(fiber_x, fiber_y)``.  No
   stage move to SPEC afterwards and no guiding: the field must stay clear.
4. **Probe** -- one frame at the current focuser position, at the fixed ``exposure``:
   is the star at the fibre pixel, and is it saturated?  A saturated star is not
   measured with a shorter exposure: a fainter magnitude limit is estimated from it
   (:func:`fainter_limit`) and steps 2-4 run once more.  Saturated again, the run fails.
5. **Sweep** -- ``steps`` frames ``ticks_per_step`` apart, centred on where the
   focuser was found, measuring **only the chosen star**, followed from frame to frame
   (:func:`calibration.analysis.vcurve.analyze_focus_samples` with ``star_xy=``).  Its
   FWHM is logged beside the HFD on every frame; the fit uses the HFD.
6. **Finish** -- solved: the focuser goes to best focus.  Not solved: it goes back
   to where the run found it.  Neither writes the config DB: no
   ``known_as_good_position``, no calibration product.
7. **Sky quality** -- solved only: one more frame at best focus, measured by
   :mod:`focus_on_star.sky_quality` into ``latest.sky_quality.measured`` -- the
   star's FWHM and HFD, the box's zero point against Gaia, its background.  It
   never fails the run: the focus is already found.

The hardware steps reuse :class:`FocuserCalibrator`'s internals (`_goto`, `_sweep`,
`_move_focuser`, ...) through a subclass whose abort flag is ``FocusingOnStar``.

Design: mast-claude-config ``plans/focus_on_star_design.md``.
"""

from __future__ import annotations

import datetime
import os
import threading
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

import numpy as np
from astropy.coordinates import Angle
from astropy.io import fits

from acquisition import Acquisition, ApproachMode
from calibration.analysis.models import HFDAutofocusStatus
from calibration.analysis.vcurve import analyze_focus_samples
from calibration.calibrator import zenith_dec
from calibration.logging_context import init_calibration_log, phase_logging
from calibration.phases.artifacts import move_to_shared, plot_vcurve, save_status
from calibration.phases.focuser import FocuserCalibrator, FocuserMoveError
from catalogs.catshtm import CatalogError, CatsHtm
from common.activities import UnitActivities
from common.config.calibration import FocusOnStarSettings
from common.config.rois import FcuVersion
from common.filer import MoveGuardian
from common.interfaces.solving import SolverId
from common.mast_logging import get_logger
from common.models.latest import LatestFocusOnStar, LatestMeasuredSkyQuality
from common.paths import PathMaker
from common.utils import Coord
from focus_on_star import sky_quality
from focus_on_star.star_selection import FocusStar, field_catalog, find_focus_star, years_since
from imaging.fov import focus_center_bin1, fov_box, fov_box_size, frame_shape_bin1
from imaging.hfd import field_stars, measure_star, peak_fraction
from stage import StagePresetPosition

if TYPE_CHECKING:
    from unit import Unit  # type: ignore[import-untyped]

logger = get_logger(__name__)
init_calibration_log(logger)

#: Raw ADU at or above which a star is saturated -- `imaging.hfd`'s `max_value`, which
#: drops such a star from the HFD altogether.  One star is all this run measures, so a
#: saturated one leaves nothing.
SATURATION_ADU = 63000.0

#: The probe peak a replacement star is chosen for: 40% of saturation, because the star
#: peaks higher at best focus than on the probe, and higher still on a good night.
TARGET_PEAK_ADU = 0.4 * SATURATION_ADU
#: A replacement is at least this much fainter: the saturated star's flux is clipped, so
#: the estimate from it understates how much fainter it needs to be.
MIN_FAINTER_MAG = 1.0
#: How much fainter when nothing unsaturated in the box gives the PSF's peak fraction.
FALLBACK_FAINTER_MAG = 1.5


def fainter_limit(mag: float, flux: float, fraction: float | None) -> float:
    """The magnitude limit for a star that would peak near ``TARGET_PEAK_ADU``.

    A saturated star's true peak is about ``fraction`` (the peak / flux of the frame's
    unsaturated stars, which share its PSF) times its flux; the limit is fainter by the
    magnitudes that takes down to the target -- at least ``MIN_FAINTER_MAG``. With no
    ``fraction``, ``FALLBACK_FAINTER_MAG``.
    """
    if fraction is None or flux <= 0:
        dm = FALLBACK_FAINTER_MAG
    else:
        dm = max(MIN_FAINTER_MAG, 2.5 * float(np.log10(fraction * flux / TARGET_PEAK_ADU)))
    return round(mag + dm, 1)


@dataclass
class _Probe:
    """What the probe found: the star's pixel, or -- saturated -- a fainter limit to pick at."""

    star_xy: tuple[float, float] | None = None
    fainter_limit: float | None = None


#: Work that holds the mount, the stage, the focuser or the imager -- none of it may
#: overlap a focus_on_star run.
BUSY = (
    UnitActivities.Calibrating
    | UnitActivities.CalibratingFocus
    | UnitActivities.CalibratingOpticalCenter
    | UnitActivities.CalibratingStage
    | UnitActivities.Autofocusing
    | UnitActivities.AutofocusingPWI4
    | UnitActivities.Acquiring
    | UnitActivities.Guiding
    | UnitActivities.FluxMetering
    | UnitActivities.Exposing
    | UnitActivities.StartingUp
    | UnitActivities.ShuttingDown
)


@dataclass
class FocusOnStarParams:
    zenith: bool = True
    ra_j2000_hours: float | None = None
    dec_j2000_degs: float | None = None
    fov_percent: float = 10.0
    star_magnitude: float = 11.0
    steps: int = 7
    ticks_per_step: int = 75

    def problem(self) -> str | None:
        """Why these parameters cannot make a run, or None."""
        given = [v is not None for v in (self.ra_j2000_hours, self.dec_j2000_degs)]
        if self.zenith and any(given):
            return "zenith=true and ra/dec were both given; pass zenith=false to use ra/dec"
        if any(given) and not all(given):
            return "give both ra_j2000_hours and dec_j2000_degs, or neither"
        if self.steps < 3 or self.steps % 2 != 1:
            return f"steps={self.steps} must be odd and >= 3, so a point sits on the start position"
        if self.ticks_per_step <= 0:
            return f"ticks_per_step={self.ticks_per_step} must be positive"
        if not 0 < self.fov_percent <= 100:
            return f"fov_percent={self.fov_percent} must be in (0, 100]"
        return None


#: What ``GET /unit/focus_on_star_status`` reports, and ``FullUnitStatus.latest.focus_on_star``:
#: the current run, or the last one (common.models.latest). The V-curve's `HFDAutofocusStatus`
#: goes in its `analysis`.
FocusOnStarStatus = LatestFocusOnStar


def focus_reference(conf) -> str | None:
    """What puts the unit near focus, so a narrow sweep can find the minimum; None if nothing.

    A ``/calibrate/focuser`` product first. Failing that, ``focuser.known_as_good_position``,
    which the focuser goes to at startup (``focuser.py``) -- so the sweep, centred on the
    current position, is centred on it. Its default of 0 means it was never set.
    """
    cal = getattr(conf, "calibration", None)
    if cal is not None and cal.products.focuser is not None:
        return "calibration"
    focuser = getattr(conf, "focuser", None)
    if focuser is not None and focuser.known_as_good_position > 0:
        return "known_as_good_position"
    return None


class FocusOnStarSession:
    """One run at a time, owned by the Unit."""

    def __init__(self, unit: Unit):
        self.unit = unit
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self.state = FocusOnStarStatus()
        #: The latest sky-quality frame, for `FullUnitStatus.latest.sky_quality.measured`.
        self.sky_quality: LatestMeasuredSkyQuality | None = None

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def require_can_start(self, params: FocusOnStarParams) -> str | None:
        """Why a run cannot start, or None -- checked before anything moves."""
        problem = params.problem()
        if problem is not None:
            return problem
        if self.is_active or self.unit.is_active(UnitActivities.FocusingOnStar):
            return "a focus_on_star run is already in progress"
        unit = self.unit
        missing = [
            name
            for name in ("mount", "pw", "imager", "focuser", "stage", "solver", "acquirer", "guider")
            if getattr(unit, name, None) is None
        ]
        if missing:
            return f"these components are not available: {', '.join(missing)}"
        # Operational, not merely present: before /unit/startup the mount is disconnected and
        # the stage unhomed, and the run's first move is the stage.
        if not unit.operational:
            return f"the unit is not operational: {'; '.join(unit.why_not_operational) or 'no reason recorded'}"
        if unit.activities & BUSY:
            return f"the unit is busy ({unit.activities_verbal})"
        conf = unit.unit_conf
        if conf is None:
            return "the unit has no configuration"
        if focus_center_bin1(conf, unit.fcu_version) is None:
            return f"no guiding ROI (fiber_x, fiber_y) is configured for FCU {unit.fcu_version}"
        if conf.calibration is None:
            return "the unit has no calibration configuration"
        if focus_reference(conf) is None:
            return "the unit has no focus calibration and no focuser.known_as_good_position; run /calibrate/focuser first"
        return None

    def start(self, params: FocusOnStarParams) -> str | FocusOnStarStatus:
        """Validate, claim the unit, and dispatch. Returns a refusal or the initial state."""
        with self._lock:
            refusal = self.require_can_start(params)
            if refusal is not None:
                return refusal
            try:
                folder: str | None = PathMaker().make_calibration_folder("focus_on_star")
            except Exception as ex:  # noqa: BLE001 -- a memory imager needs no folder; a file imager fails in the run
                logger.warning(f"focus_on_star: could not create the run folder ({ex})")
                folder = None
            self.state = FocusOnStarStatus(phase="selecting", params=asdict(params)).begin(folder)
            # Raised here rather than in the thread: the endpoint declares this flag as its
            # completion signal, so a caller answered Ok must find it already set.
            self.unit.start_activity(UnitActivities.FocusingOnStar)
            self._thread = threading.Thread(
                name="focus_on_star", target=self.do_focus_on_star, args=(params, folder), daemon=True
            )
            self._thread.start()
            logger.info(f"focus_on_star started, products under '{folder}'")
            return self.status()

    def abort(self) -> None:
        """Clear the flag; the run notices at its next wait and unwinds."""
        if self.unit.is_active(UnitActivities.FocusingOnStar):
            logger.info("focus_on_star: abort requested")
            self.unit.end_activity(UnitActivities.FocusingOnStar)

    def status(self) -> FocusOnStarStatus:
        return self.state

    @property
    def latest(self) -> FocusOnStarStatus | None:
        """The latest run's record, for `FullUnitStatus.latest`; None before the first."""
        return self.state if self.state.started is not None else None

    def do_focus_on_star(self, params: FocusOnStarParams, folder: str | None) -> None:
        """The whole run, on its own thread. Never raises; always ends the flag."""
        run = FocusOnStarRun(self.unit, self.state)
        try:
            with phase_logging("focus_on_star"):
                run.run(params, self._settings(), folder)
        except Exception as ex:  # noqa: BLE001 -- the run's own thread: anything escaping reaches only stderr
            logger.exception("focus_on_star: run failed")
            self.state.errors.append(f"run failed: {ex!r}")
            self.state.outcome = self.state.outcome or "failed"
        finally:
            if run.sky_quality is not None:
                self.sky_quality = run.sky_quality
            self.state.finish(self.state.outcome == "solved")
            self.state.write()  # latest.json beside the sweep's frames
            if self.unit.is_active(UnitActivities.FocusingOnStar):
                self.unit.end_activity(UnitActivities.FocusingOnStar)

    def _settings(self) -> FocusOnStarSettings:
        cal = getattr(self.unit.unit_conf, "calibration", None)
        return cal.settings.focus_on_star if cal is not None else FocusOnStarSettings()


class FocusOnStarRun(FocuserCalibrator):
    """One run's hardware sequence, built on the calibration focuser's internals."""

    RUN_ACTIVITIES = (UnitActivities.FocusingOnStar,)

    def __init__(self, unit: Unit, state: FocusOnStarStatus):
        super().__init__(unit)
        self.state = state
        self.sky_quality: LatestMeasuredSkyQuality | None = None

    # ------------------------------------------------------------------ entry
    def run(self, params: FocusOnStarParams, ls: FocusOnStarSettings, folder: str | None) -> bool:  # noqa: C901
        """Returns True when the focuser was left at a measured best focus."""
        op = "focus_on_star"
        unit, state = self.unit, self.state
        conf, imager = unit.unit_conf, unit.required_imager
        assert conf is not None and conf.calibration is not None

        # Bound before the try, so the finally can always read them.
        status: HFDAutofocusStatus | None = None
        solved = False
        aborted = False
        start_position: int | None = None
        series = imager.start_exposure_series(purpose="focus-on-star")
        try:
            state.focus_reference = focus_reference(conf)
            logger.info(f"{op}: near focus by {state.focus_reference}")

            # --- 1. sky position -----------------------------------------------
            sky = self._sky_position(params)
            if sky is None:
                return self._fail_run(f"{op}: could not determine the sky position")
            state.sky_position = sky

            # --- 2. geometry ---------------------------------------------------
            shape = frame_shape_bin1(imager)
            center = focus_center_bin1(conf, unit.fcu_version)
            assert center is not None  # require_can_start checked it
            box = fov_box(shape, center, params.fov_percent, ls.binning)
            w, h = fov_box_size(shape, params.fov_percent)
            iso = ls.isolation_radius_px * conf.imager.pixel_scale_at_bin1 / 3600.0
            state.focus_center, state.box = center, box
            logger.info(f'{op}: box {w}x{h} px about the fibre pixel {center} -> {box}; isolation {iso * 3600:.0f}"')
            st = conf.calibration.settings.focuser.model_copy(
                update={
                    "images": params.steps,
                    "spacing": params.ticks_per_step,
                    "exposure": ls.exposure,
                    "binning": ls.binning,
                }
            )
            state.exposure = st.exposure

            # --- 3-5. star, acquisition, probe: a saturated star is replaced once -----
            mag_limit = params.star_magnitude
            star_xy: tuple[float, float] | None = None
            for pick in (1, 2):
                star = self._select_star(sky, mag_limit, ls, iso)
                if star is None:
                    return False
                state.star = star.to_dict()

                state.phase = "slewing"
                self._position_stage()
                if not self._goto(star.ra_deg / 15.0, star.dec_deg):
                    aborted = True
                    return self._abort(f"{op}: stopped during the slew")
                state.phase = "acquiring"
                if not self._acquire(star, ls):
                    aborted = not self._still_calibrating()
                    return self._fail_run(f"{op}: the star was not acquired on the fibre pixel")

                start_position = int(unit.required_focuser.position)
                state.start_position = start_position
                state.phase = "probing"
                probe = self._probe(st, start_position, center, box, star, ls, folder, pick)
                if probe is None:
                    aborted = not self._still_calibrating()
                    return False
                if probe.star_xy is not None:
                    star_xy = probe.star_xy
                    break
                if pick == 2:
                    return self._fail_run(
                        f"{op}: the replacement star G={star.mag:.2f} is saturated too at {st.exposure:g}s"
                    )
                mag_limit = probe.fainter_limit
            assert star_xy is not None

            # --- 6. sweep ------------------------------------------------------
            state.phase = "sweeping"
            seed = start_position
            for attempt in range(ls.max_tries):
                self.tries_used = attempt + 1
                samples = self._sweep(seed, st, folder)
                if samples is None:
                    aborted = True
                    return self._abort(f"{op}: stopped during sweep {self.tries_used}")
                status = analyze_focus_samples(
                    samples,
                    tolerance_frac=ls.tolerance_frac,
                    min_fit_r2=ls.min_fit_r2,
                    min_edge_rise=ls.min_edge_rise,
                    star_xy=star_xy,
                    search_radius=ls.star_search_px / ls.binning,
                )
                state.analysis = status
                result = status.analysis_result
                logger.info(f"{op}: sweep {self.tries_used}/{ls.max_tries}: {status.message}")
                self._log_sweep_frames(status)
                if result is not None and result.has_solution:
                    dmin = result.best_focus_star_diameter
                    if dmin is not None and dmin > ls.max_best_hfd_px:
                        self._log_error(f"{op}: rejecting best focus with HFD {dmin:.1f}px > {ls.max_best_hfd_px}")
                    else:
                        solved = True
                        break
                if attempt == ls.max_tries - 1:
                    break
                new_seed = self._recentre(status, seed, st)
                if new_seed is None:
                    break
                seed = new_seed

            # --- 7. finish -----------------------------------------------------
            state.phase = "finishing"
            result = status.analysis_result if (status is not None and solved) else None
            if result is not None and result.best_focus_position is not None:
                best = self._clamp(int(round(result.best_focus_position)), st)
                logger.info(f"{op}: best focus {best} (start {start_position}, HFD {result.best_focus_star_diameter:.2f}px)")
                self._move_focuser(best, st)
                state.best_position = best
                self._sky_quality(st, star, star_xy, box, ls, folder)
                return True
            self._log_error(f"{op}: no focus solution after {self.tries_used} sweep(s); returning to {start_position}")
            self._move_focuser(start_position, st)
            return False
        except FocuserMoveError as ex:
            aborted = not self._still_calibrating()
            self._log_error(f"{op}: {ex}")
            return False
        finally:
            imager.end_exposure_series(series)
            state.outcome = "solved" if solved and not aborted else ("aborted" if aborted else "failed")
            state.errors.extend(e for e in self.errors if e not in state.errors)
            try:
                state.final_position = int(unit.required_focuser.position)
            except Exception:  # noqa: BLE001 -- a status field, never worth failing the unwind over
                state.final_position = None
            if not solved and unit.mount is not None:
                # As a failed acquisition leaves it; a solved run keeps tracking the star.
                try:
                    unit.required_mount.stop_tracking()
                except Exception as ex:  # noqa: BLE001
                    logger.warning(f"{op}: could not stop tracking: {ex}")
            final = status or HFDAutofocusStatus(message="run produced no analysis", errors=list(self.errors))
            final.tries_used = self.tries_used
            final.solved = solved
            plot_vcurve(folder, final.analysis_result)
            save_status(folder, final)
            self._save_run_record(folder)
            move_to_shared(folder)

    # ---------------------------------------------------------------- steps
    def _sky_position(self, params: FocusOnStarParams) -> tuple[float, float] | None:
        """(ra hours, dec degrees): the zenith, the given position, or the mount's."""
        pw = self.unit.required_pw
        if params.zenith:
            ra = pw.status().site.lmst_hours  # of-date LST as J2000 RA: ~0.15 deg, well inside the search
            dec = zenith_dec(self.unit)
        elif params.ra_j2000_hours is not None and params.dec_j2000_degs is not None:
            ra, dec = params.ra_j2000_hours, params.dec_j2000_degs
        else:
            mount = pw.status().mount
            ra, dec = mount.ra_j2000_hours, mount.dec_j2000_degs
        if ra is None or dec is None:
            return None
        return float(ra), float(dec)

    def _select_star(self, sky, mag_limit: float, ls: FocusOnStarSettings, iso: float) -> FocusStar | None:
        ra_h, dec = sky
        try:
            star = find_focus_star(
                CatsHtm(ls.catalog_root, ls.catalog_name),
                ra_deg=ra_h * 15.0,
                dec_deg=dec,
                mag_limit=mag_limit,
                isolation_deg=iso,
                mag_column=ls.mag_column,
                search_radius_deg=ls.search_radius_deg,
                search_growth=ls.search_growth,
                max_search_radius_deg=ls.max_search_radius_deg,
                years_of_proper_motion=years_since(ls.catalog_epoch) if ls.apply_proper_motion else None,
            )
        except CatalogError as ex:
            return self._fail_run(f"focus_on_star: catalog unavailable: {ex}")
        if star is None:
            return self._fail_run(
                f"focus_on_star: no star with {ls.mag_column} >= {mag_limit} and nothing brighter "
                f'within {iso * 3600:.0f}" found within {ls.max_search_radius_deg} deg of '
                f"({ra_h:.4f}h, {dec:+.4f})"
            )
        logger.info(
            f"focus_on_star: star G={star.mag:.2f} at ({star.ra_deg:.5f}, {star.dec_deg:+.5f}), "
            f"{star.separation_deg:.3f} deg from the sky position ({star.n_rejected} brighter-neighbour rejections)"
        )
        return star

    def _position_stage(self) -> None:
        """Where the acquirer's spec phase puts the stage before it solves (acquirer.py)."""
        preset = StagePresetPosition.Spec if self.unit.fcu_version == FcuVersion.v1 else StagePresetPosition.Sky
        self.unit.required_stage.move_to_preset(preset)
        self._wait_stage()

    def _acquire(self, star: FocusStar, ls: FocusOnStarSettings) -> bool:
        """Solve and correct as the spec phase does: the solve's reference pixel is the fibre."""
        unit = self.unit
        conf = unit.unit_conf
        acquirer = unit.acquirer
        assert conf is not None and acquirer is not None
        acquisition = Acquisition(
            unit=unit,
            approach_mode=ApproachMode.GRADUAL_BY_RATE,
            solver_id=SolverId[conf.solving.method],
            make_corrections=True,
            target_ra=star.ra_deg / 15.0,
            target_dec=star.dec_deg,
            exposure=ls.exposure,
            skip_sky=True,
            use_set_limit_frame=False,
        )
        # Fresh, so solve_and_correct records this run's corrections rather than appending
        # to whichever acquisition came last.
        acquirer.latest_acquisition = acquisition
        target = Coord(ra=Angle(star.ra_deg / 15.0, unit="hour"), dec=Angle(star.dec_deg, unit="deg"))
        try:
            achieved = unit.required_solver.solve_and_correct(
                target=target,
                approach_mode=acquisition.approach_mode,
                solver_id=acquisition.solver_id,
                make_corrections=True,
                imager_settings=acquirer.spec_phase_imager_settings(acquisition, ls.exposure),
                solving_tolerance=acquirer.spec_phase_tolerance(),
                phase="spec",
                parent_activity=UnitActivities.FocusingOnStar,
                max_tries=ls.acquisition_tries,
            )
            acquisition.save_corrections("spec")
            return bool(achieved)
        finally:
            MoveGuardian().release_folder(acquisition.folder, logger=logger)

    def _probe(self, st, position: int, center, box, star: FocusStar, ls: FocusOnStarSettings, folder, pick: int):
        """One frame at the start position: is the star at the fibre pixel, and unsaturated?

        Returns its pixel; or, saturated, the fainter limit to pick a replacement at; or None
        when the run cannot go on.
        """
        if not self._still_calibrating():
            return self._abort("focus_on_star: stopped while probing")
        image = self._expose(st, folder, tag=f"FOCUS{position:05d}_probe{pick}")
        if image is None:
            return self._fail_run("focus_on_star: the probe exposure failed")
        data = np.asarray(image if isinstance(image, np.ndarray) else fits.getdata(image), dtype=float)
        near = (center[0] / ls.binning, center[1] / ls.binning)
        m = measure_star(data, near, search_radius=ls.star_search_px / ls.binning, max_value=SATURATION_ADU)
        if m is None:
            return self._fail_run(
                f"focus_on_star: no star within {ls.star_search_px} px of the fibre pixel on the probe frame -- "
                "the acquisition missed it, or the sky is not clear"
            )
        if m.saturated:
            fraction = peak_fraction(data, box, max_value=SATURATION_ADU)
            limit = fainter_limit(star.mag, m.flux, fraction)
            logger.info(
                f"focus_on_star: the star G={star.mag:.2f} is saturated at {st.exposure:g}s (peak {m.peak:.0f} ADU, "
                f"flux >= {m.flux:.0f}, peak fraction {fraction if fraction is None else round(fraction, 4)}); "
                f"picking again at G >= {limit}"
            )
            return _Probe(fainter_limit=limit)
        logger.info(
            f"focus_on_star: probe at {position}: star at ({m.x:.1f}, {m.y:.1f}), HFD {m.hfd:.2f}px, "
            f"FWHM {m.fwhm:.2f}px, peak {m.peak:.0f} ADU"
        )
        return _Probe(star_xy=(m.x, m.y))

    def _sky_quality(self, st, star: FocusStar, near_xy, box, ls: FocusOnStarSettings, folder) -> None:
        """Step 7: one frame at best focus, into `self.sky_quality`. Never raises.

        The star is looked for over three times the sweep's search radius: it has drifted
        through the sweep, and nothing brighter lies within the isolation radius.
        """
        op = "focus_on_star: sky quality"
        unit = self.unit
        record = LatestMeasuredSkyQuality(exposure_seconds=st.exposure, binning=ls.binning, star_mag=star.mag)
        record.begin(folder, plan_id=self.state.plan_id)
        self.sky_quality = record
        errors: list[str] = []
        try:
            conf = unit.unit_conf
            assert conf is not None
            scale = conf.imager.pixel_scale_at_bin1 * ls.binning
            record.plate_scale_arcsec = scale
            try:
                record.altitude_degs = unit.required_pw.status().mount.altitude_degs
            except Exception as ex:  # noqa: BLE001 -- only the airmass is lost
                errors.append(f"{op}: no mount altitude ({ex})")
            record.airmass = sky_quality.airmass(record.altitude_degs)

            if not self._still_calibrating():
                record.finish(False, [f"{op}: stopped"])
                return
            self.state.phase = "sky_quality"
            # Mid-exposure, from just before the exposure is asked for: close enough for a
            # header stamp, and it does not depend on what the imager reports.
            asked_at = datetime.datetime.now(datetime.UTC)
            image = self._expose(st, folder, tag="SKYQUALITY")
            record.observed_at = (asked_at + datetime.timedelta(seconds=st.exposure / 2)).isoformat()
            if image is None:
                record.finish(False, [f"{op}: the exposure failed"])
                return
            data = np.asarray(image if isinstance(image, np.ndarray) else fits.getdata(image), dtype=float)
            m = measure_star(data, near_xy, search_radius=3 * ls.star_search_px / ls.binning, max_value=SATURATION_ADU)
            if m is None:
                record.finish(False, [f"{op}: the star was not found near ({near_xy[0]:.0f}, {near_xy[1]:.0f})"])
                return
            if m.saturated:
                errors.append(f"{op}: the star is saturated at best focus (peak {m.peak:.0f} ADU); no FWHM or HFD")

            x0, x1, y0, y1 = box
            record.background_adu = float(np.median(data[y0:y1, x0:x1]))
            stars, _ = field_stars(data, box, max_value=SATURATION_ADU)
            radius_deg = 0.5 * float(np.hypot(x1 - x0, y1 - y0)) * scale / 3600.0
            try:
                cat_ra, cat_dec, cat_mag = field_catalog(
                    CatsHtm(ls.catalog_root, ls.catalog_name),
                    ra_deg=star.ra_deg,
                    dec_deg=star.dec_deg,
                    radius_deg=radius_deg,
                    mag_column=ls.mag_column,
                    faintest_mag=sky_quality.FAINTEST_G,
                    years_of_proper_motion=years_since(ls.catalog_epoch) if ls.apply_proper_motion else None,
                )
            except CatalogError as ex:
                errors.append(f"{op}: catalog unavailable ({ex}); no zero point")
                cat_ra = cat_dec = cat_mag = np.empty(0)

            found = sky_quality.measure(
                m,
                star.ra_deg,
                star.dec_deg,
                stars,
                cat_ra,
                cat_dec,
                cat_mag,
                scale_arcsec=scale,
                exposure_seconds=st.exposure,
            )
            for name, value in vars(found).items():
                setattr(record, name, value)
            if found.fwhm_px is not None:
                record.fwhm_arcsec = found.fwhm_px * scale
            if found.hfd_px is not None:
                record.hfd_arcsec = found.hfd_px * scale
            if found.fwhm_px and found.hfd_px:
                record.hfd_over_fwhm = found.hfd_px / found.fwhm_px
            if found.zero_point_mag is None and len(cat_mag):
                errors.append(
                    f"{op}: no zero point ({found.detected_stars} detected, {found.catalog_stars} in the catalog, "
                    f"{found.matched_stars} matched)"
                )
            record.finish(found.fwhm_px is not None, errors)
            logger.info(
                f'{op}: FWHM {_fmt(record.fwhm_arcsec)}", HFD {_fmt(record.hfd_arcsec)}", '
                f"zero point {_fmt(record.zero_point_mag)} mag ({record.zero_point_stars} stars), "
                f"airmass {_fmt(record.airmass)}, background {_fmt(record.background_adu, 0)} ADU"
            )
        except Exception as ex:  # noqa: BLE001 -- the focus is already found; this must not undo it
            logger.exception(f"{op}: failed")
            record.finish(False, [*errors, f"{op}: {ex!r}"])
        finally:
            for e in record.errors:
                logger.warning(e)
            record.write("sky_quality.json")

    def _log_sweep_frames(self, status: HFDAutofocusStatus) -> None:
        """The star's HFD and FWHM on each sweep frame -- the FWHM is logged only."""
        result = status.analysis_result
        for sample in (result.focus_samples if result is not None else None) or []:
            position = "?" if sample.focus_position is None else f"{sample.focus_position:.0f}"
            hfd = "-" if sample.hfd_pixels is None else f"{sample.hfd_pixels:.2f}px"
            fwhm = "-" if sample.fwhm_pixels is None else f"{sample.fwhm_pixels:.2f}px"
            logger.info(f"focus_on_star: sweep frame at {position}: HFD {hfd}, FWHM {fwhm}")

    # -------------------------------------------------------------- helpers
    def _fail_run(self, message: str) -> None:
        self._log_error(message)
        return None

    def _save_run_record(self, folder: str | None) -> None:
        """``focus_on_star.json``: the run's parameters, star, geometry and outcome."""
        if not folder:
            return
        path = os.path.join(folder, "focus_on_star.json")
        try:
            with MoveGuardian().protect(path), open(path, "w", encoding="utf-8") as f:
                f.write(self.state.model_dump_json(indent=4, exclude={"analysis"}))
        except Exception as ex:  # noqa: BLE001 -- an artifact, never worth failing the unwind over
            logger.warning(f"could not write {path}: {ex}")


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"
