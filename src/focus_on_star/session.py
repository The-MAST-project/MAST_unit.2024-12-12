"""``PUT /unit/focus_on_star``: one isolated star on the fibre pixel, then a narrow-field V-curve.

An on-demand refinement, run once the unit is near focus -- after ``/calibrate/focuser``, or,
failing a focuser calibration product, at ``focuser.known_as_good_position``:

1. **Sky position** -- the zenith, an explicit (ra, dec), or where the telescope points.
2. **Star** -- the GAIA DR3 catsHTM catalog is searched around it for the brightest
   star no brighter than ``star_magnitude`` with nothing brighter in the measured
   box (:mod:`focus_on_star.star_selection`).
3. **Acquire** -- slew, then solve-and-correct exactly as the acquirer's ``spec``
   phase does, so the star lands on the fibre pixel ``(fiber_x, fiber_y)``.  No
   stage move to SPEC afterwards and no guiding: the field must stay clear.
4. **Probe** -- one frame at the current focuser position: is the star in the box,
   and is it saturated?  A saturated star halves the exposure and probes again.
5. **Sweep** -- ``steps`` frames ``ticks_per_step`` apart, centred on where the
   focuser was found, HFD measured only inside the box of ``fov_percent`` of the
   frame about the fibre pixel (:func:`calibration.analysis.vcurve.analyze_focus_samples`
   with ``box=``).
6. **Finish** -- solved: the focuser goes to best focus.  Not solved: it goes back
   to where the run found it.  Neither writes the config DB: no
   ``known_as_good_position``, no calibration product.

The hardware steps reuse :class:`FocuserCalibrator`'s internals (`_goto`, `_sweep`,
`_move_focuser`, ...) through a subclass whose abort flag is ``FocusingOnStar``.

Design: mast-claude-config ``plans/focus_on_star_design.md``.
"""

from __future__ import annotations

import datetime
import os
import threading
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from astropy.coordinates import Angle
from astropy.io import fits
from pydantic import BaseModel, Field

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
from common.paths import PathMaker
from common.utils import Coord
from focus_on_star.star_selection import FocusStar, find_focus_star, isolation_radius_deg, years_since
from imaging.fov import focus_center_bin1, fov_box, fov_box_size, frame_shape_bin1
from imaging.hfd import frame_hfd
from stage import StagePresetPosition

if TYPE_CHECKING:
    from unit import Unit  # type: ignore[import-untyped]

logger = get_logger(__name__)
init_calibration_log(logger)

#: Raw ADU at or above which a star is saturated -- `imaging.hfd`'s `max_value`, which
#: drops such a star from the HFD altogether.  One star is all this run measures, so a
#: saturated one leaves nothing.
SATURATION_ADU = 63000.0

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
    star_magnitude: float = 10.0
    steps: int = 5
    ticks_per_step: int = 50

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


class FocusOnStarStatus(BaseModel):
    """What ``GET /unit/focus_on_star_status`` reports: the current run, or the last one."""

    active: bool = False
    phase: str | None = None  # selecting | slewing | acquiring | probing | sweeping | finishing
    outcome: str | None = None  # solved | failed | aborted
    folder: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    params: dict[str, Any] | None = None
    sky_position: tuple[float, float] | None = Field(default=None, description="(ra hours, dec degrees), J2000")
    star: dict[str, Any] | None = None
    focus_center: tuple[float, float] | None = Field(default=None, description="(x, y), bin-1 pixels")
    box: tuple[int, int, int, int] | None = Field(default=None, description="(x0, x1, y0, y1), sweep pixels")
    exposure: float | None = None
    start_position: int | None = None
    best_position: int | None = None
    final_position: int | None = None
    #: What says the unit is near focus: "calibration" (a /calibrate/focuser product) or
    #: "known_as_good_position" (the last ps3cli autofocus, used when there is no product).
    focus_reference: str | None = None
    errors: list[str] = Field(default_factory=list)
    analysis: HFDAutofocusStatus | None = None


def _isoformat_utc() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


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
            self.state = FocusOnStarStatus(
                active=True,
                phase="selecting",
                folder=folder,
                started_at=_isoformat_utc(),
                params=asdict(params),
            )
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
        self.state.active = self.is_active
        return self.state

    def do_focus_on_star(self, params: FocusOnStarParams, folder: str | None) -> None:
        """The whole run, on its own thread. Never raises; always ends the flag."""
        try:
            with phase_logging("focus_on_star"):
                FocusOnStarRun(self.unit, self.state).run(params, self._settings(), folder)
        except Exception as ex:  # noqa: BLE001 -- the run's own thread: anything escaping reaches only stderr
            logger.exception("focus_on_star: run failed")
            self.state.errors.append(f"run failed: {ex!r}")
            self.state.outcome = self.state.outcome or "failed"
        finally:
            self.state.ended_at = _isoformat_utc()
            self.state.active = False
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
            iso = isolation_radius_deg(w, h, conf.imager.pixel_scale_at_bin1, ls.isolation_margin_px)
            state.focus_center, state.box = center, box
            logger.info(f'{op}: box {w}x{h} px about the fibre pixel {center} -> {box}; isolation {iso * 3600:.0f}"')

            # --- 3. star -------------------------------------------------------
            star = self._select_star(sky, params, ls, iso)
            if star is None:
                return False
            state.star = star.to_dict()

            # --- 4. acquire on the fibre pixel ---------------------------------
            state.phase = "slewing"
            self._position_stage()
            if not self._goto(star.ra_deg / 15.0, star.dec_deg):
                aborted = True
                return self._abort(f"{op}: stopped during the slew")
            state.phase = "acquiring"
            if not self._acquire(star, ls):
                aborted = not self._still_calibrating()
                return self._fail_run(f"{op}: the star was not acquired on the fibre pixel")

            # --- 5. probe ------------------------------------------------------
            start_position = int(unit.required_focuser.position)
            state.start_position = start_position
            st = conf.calibration.settings.focuser.model_copy(
                update={
                    "images": params.steps,
                    "spacing": params.ticks_per_step,
                    "exposure": ls.exposure,
                    "binning": ls.binning,
                }
            )
            state.phase = "probing"
            st = self._probe(st, start_position, box, ls, folder)
            if st is None:
                aborted = not self._still_calibrating()
                return False
            state.exposure = st.exposure

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
                    box=box,
                )
                state.analysis = status
                result = status.analysis_result
                logger.info(f"{op}: sweep {self.tries_used}/{ls.max_tries}: {status.message}")
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

    def _select_star(self, sky, params, ls: FocusOnStarSettings, iso: float) -> FocusStar | None:
        ra_h, dec = sky
        try:
            star = find_focus_star(
                CatsHtm(ls.catalog_root, ls.catalog_name),
                ra_deg=ra_h * 15.0,
                dec_deg=dec,
                mag_limit=params.star_magnitude,
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
                f"focus_on_star: no star with {ls.mag_column} >= {params.star_magnitude} and nothing brighter "
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

    def _probe(self, st, position: int, box, ls: FocusOnStarSettings, folder):
        """One frame at the start position: is the star in the box, and unsaturated?

        Returns the settings to sweep with (exposure possibly reduced), or None.
        """
        for attempt in range(ls.max_saturation_retries + 1):
            if not self._still_calibrating():
                return self._abort("focus_on_star: stopped while probing")
            image = self._expose(st, folder, tag=f"FOCUS{position:05d}_probe{attempt}")
            if image is None:
                return self._fail_run("focus_on_star: the probe exposure failed")
            data = np.asarray(image if isinstance(image, np.ndarray) else fits.getdata(image), dtype=float)
            x0, x1, y0, y1 = box
            peak = float(data[y0:y1, x0:x1].max())
            if peak >= SATURATION_ADU:
                if attempt == ls.max_saturation_retries:
                    return self._fail_run(
                        f"focus_on_star: the star is saturated (peak {peak:.0f} ADU) even at "
                        f"{st.exposure:g}s; choose a fainter star_magnitude"
                    )
                st = st.model_copy(update={"exposure": st.exposure / 2.0})
                logger.info(f"focus_on_star: probe peak {peak:.0f} ADU is saturated; exposure -> {st.exposure:g}s")
                continue
            hfd, n = frame_hfd(image, box=box)
            if n == 0:
                return self._fail_run(
                    "focus_on_star: no star measured in the box on the probe frame -- "
                    "the acquisition missed it, or the sky is not clear"
                )
            logger.info(f"focus_on_star: probe at {position}: {n} star(s) in the box, HFD {hfd:.2f}px, peak {peak:.0f}")
            return st
        return None

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
