"""The unit self-calibration orchestrator and its API surface.

One :class:`Calibrator` singleton owns all four endpoints::

    POST /calibrate                 ?force=false                  # all three, in order
    POST /calibrate/focuser         ?force=false&ra=&dec=
    POST /calibrate/optical_center  ?force=false&ra=&dec=
    POST /calibrate/stage           ?force=false&ra=&dec=&move_to_spec=false

Each returns a ``CanonicalResponse`` **immediately**; the work runs on a
background thread under a ``UnitActivities.Calibrating*`` flag, is visible via
``/calibrate/status``, and is abortable.

**Logging convention -- the debug log is the decision trace.**  Every decision
point goes to ``logger.debug``: phase order, skip-because-present, prerequisite
checks, regime choices, each mount/stage/focuser command with its reason, gate
pass/fail and DB writes.  There is deliberately no separate per-run trace file;
``common.mast_logging`` already rotates daily under ``%LOCALAPPDATA%/mast/<date>/``,
which is what preserves the trace per unit and per night.  Outcomes a human needs
*without* enabling debug -- phase start/end, the solved value, failures -- stay at
``info`` / ``error``.

**The order is physics, not preference** -- ``focuser -> optical_center ->
stage``.  Clean coma only exists at best focus (defocused, a star is a
pupil-donut with no usable coma), so focus precedes optical_center; the stage
*inserts* the folding mirror and obstructs the field, so it runs last.
``/calibrate`` is the only thing that sequences them.

**Two kinds of precondition, handled differently.**  *Hardware* state a phase
needs, it makes happen on entry (slew, ``stage.home()``, set the focuser) with
no assumption of inter-phase carry-over.  *Calibration products* a phase needs,
it requires: absent means a hard error, never an implicit run of another phase.
The rule is deliberately identical standalone and inside ``/calibrate`` -- a
missing product there is a bug, not a state to paper over.

Note this class does **not** inherit ``Component``: it drives no hardware of its
own, so ``detected`` / ``connected`` / ``powerdown`` would be meaningless stubs.
It is a routine owner, like ``Autofocuser``.  Nor does it inherit ``Activities``
-- the calibration flags are ``UnitActivities``, so they are set on the unit
(matching what ``StageCalibrator`` already does).

Design reference: mast-claude-config ``plans/calibration_orchestration.md``.
"""

from __future__ import annotations

import functools
import logging
import threading
from typing import TYPE_CHECKING, Annotated

from fastapi import Query
from fastapi.routing import APIRouter

from common.activities import UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config.calibration import CalibrationSettings
from common.const import Const
from common.mast_logging import init_log
from common.paths import PathMaker

if TYPE_CHECKING:
    from unit import Unit  # type: ignore[import-untyped]

logger = logging.getLogger("mast.unit." + __name__)
init_log(logger)

#: The phase order the physics fixes.  Do not reorder.
PHASE_ORDER = ("focuser", "optical_center", "stage")

#: Per-phase activity flag, set for that phase's duration.
PHASE_ACTIVITY = {
    "focuser": UnitActivities.CalibratingFocus,
    "optical_center": UnitActivities.CalibratingOpticalCenter,
    "stage": UnitActivities.CalibratingStage,
}

#: Every calibration flag -- the single-flight guard tests all of them.
ALL_CALIBRATION_ACTIVITIES = (UnitActivities.Calibrating, *PHASE_ACTIVITY.values())


class CalibrationError(Exception):
    """A phase could not run: a required calibration product is missing, or a
    hardware precondition could not be established."""


def not_implemented(reason: str):
    """Mark a phase whose live drive does not exist yet.

    One marker, two effects: :meth:`Calibrator._start` refuses the request
    **synchronously** -- so the caller gets ``errors`` in the HTTP response
    instead of a misleading ``"ok"`` followed by a background failure visible
    only by polling ``/calibrate/status`` -- and calling the phase directly
    still raises, so the orchestrator cannot silently skip past it.

    Deleting the decorator is the single edit that makes a phase live; there is
    no separate list of "what is built" to drift out of date.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            raise NotImplementedError(f"{fn.__name__}: {reason}")

        wrapper.not_implemented_reason = reason
        return wrapper

    return decorator


class Calibrator:
    """Singleton owning the calibration phases and their endpoints."""

    _instance: Calibrator | None = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self, unit: "Unit" | None = None):
        if self._initialized:
            # Re-constructing with a unit re-binds it; otherwise keep the binding.
            if unit is not None:
                self.unit = unit
            return
        self.unit = unit
        self.errors: list[str] = []
        self.latest: dict = {}
        self._thread: threading.Thread | None = None
        self._initialized = True

    # ------------------------------------------------------------------ state
    @property
    def is_calibrating(self) -> bool:
        """True while any calibration phase (or the umbrella) is active."""
        if self.unit is None:
            return False
        return any(self.unit.is_active(a) for a in ALL_CALIBRATION_ACTIVITIES)

    @property
    def active_phase(self) -> str | None:
        if self.unit is None:
            return None
        for name, activity in PHASE_ACTIVITY.items():
            if self.unit.is_active(activity):
                return name
        return None

    def product(self, phase: str):
        """The persisted calibration product for ``phase``, or ``None``.

        ``force=false`` means "skip when the product exists" -- there is no
        staleness or temperature model in v1: *needs doing* == *product absent*.
        """
        conf = getattr(self.unit, "unit_conf", None)
        cal = getattr(conf, "calibration", None) if conf else None
        products = getattr(cal, "products", None) if cal else None
        if products is None:
            return None
        return getattr(products, phase, None)

    @property
    def settings(self):
        """The unit's ``calibration.settings`` block, or built-in defaults.

        Never ``None``: a unit whose DB entry predates the settings block still
        gets the model defaults, so a phase can always read its inputs.
        """
        conf = getattr(self.unit, "unit_conf", None)
        cal = getattr(conf, "calibration", None) if conf else None
        settings = getattr(cal, "settings", None) if cal else None
        return settings if settings is not None else CalibrationSettings()

    def resolve_coord(self, ra: float | None, dec: float | None) -> tuple[float | None, float]:
        """Pointing for a phase: explicit argument -> config -> runtime default.

        ``ra=None`` is returned as ``None`` and means *transit* -- the caller
        substitutes the current LST, which is only knowable at run time (a fixed
        RA in config would be unobservable for much of the year).
        """
        coord = self.settings.coord
        ra = ra if ra is not None else coord.ra
        dec = dec if dec is not None else coord.dec
        if dec is None:
            dec = 20.0
        logger.debug(f"resolved pointing: ra={ra if ra is not None else 'LST (transit)'} dec={dec}")
        return ra, float(dec)

    def _skip_if_present(self, phase: str, force: bool) -> bool:
        """Whether ``phase`` should be skipped because its product already exists.

        Lives on the phase, not the orchestrator, so ``force`` behaves identically
        for a standalone ``POST /calibrate/<phase>`` and for the same phase run
        inside ``/calibrate`` -- one rule, one place.
        """
        if force:
            logger.debug(f"{phase}: force=True -- running even if a product exists")
            return False
        if self.product(phase) is not None:
            logger.debug(f"{phase}: skipping -- calibration.products.{phase} already exists (force=False)")
            return True
        logger.debug(f"{phase}: no existing product -- running")
        return False

    def status(self) -> dict:
        return {
            "calibrating": self.is_calibrating,
            "umbrella": (
                self.unit.is_active(UnitActivities.Calibrating) if self.unit else False
            ),
            "phase": self.active_phase,
            "products": {p: self.product(p) is not None for p in PHASE_ORDER},
            "latest": self.latest,
            "errors": self.errors,
        }

    # -------------------------------------------------------------- launching
    def _start(self, target, *args) -> CanonicalResponse:
        """Single-flight guard + background launch, shared by all four endpoints."""
        if self.unit is None:
            return CanonicalResponse(errors=["calibrator is not bound to a unit"])
        reason = getattr(target, "not_implemented_reason", None)
        if reason is not None:
            # Fail fast: this phase can never succeed, so do not spawn a thread
            # and do not answer "ok" for work that is guaranteed to fail.
            logger.debug(f"refusing {target.__name__}: {reason}")
            return CanonicalResponse(errors=[f"{target.__name__}: {reason}"])
        if self.is_calibrating:
            logger.debug(f"rejecting {target.__name__}: single-flight -- phase={self.active_phase} is active")
            return CanonicalResponse(
                errors=[f"a calibration is already running (phase={self.active_phase})"]
            )
        self.errors = []
        logger.debug(f"launching {target.__name__}{args}")

        def run():
            """Nothing may escape a background thread.

            An exception here would otherwise print a traceback to stderr and
            die silently as far as the API is concerned -- ``/calibrate/status``
            would show no error and the caller polling it could not tell the run
            had failed.  Funnel every failure into ``self.errors`` instead, so
            status stays the single source of truth for how a run ended.
            """
            try:
                target(*args)
            except CalibrationError as ex:
                self._fail(f"{target.__name__}: {ex}")
            except NotImplementedError as ex:
                self._fail(f"{target.__name__}: not implemented -- {ex}")
            except Exception as ex:
                logger.exception(f"{target.__name__}: unexpected failure")
                self._fail(f"{target.__name__}: unexpected failure: {ex!r}")

        self._thread = threading.Thread(
            name=f"mast-{target.__name__}", target=run, daemon=True
        )
        self._thread.start()
        return CanonicalResponse_Ok

    # ------------------------------------------------------------- endpoints
    def endpoint_calibrate(
        self,
        force: Annotated[bool, Query(description="Redo phases whose product already exists")] = False,
    ):
        """Run all three phases in order, skipping any whose product exists."""
        return self._start(self.do_calibrate, force)

    def endpoint_calibrate_focuser(
        self,
        force: bool = False,
        ra: float | None = None,
        dec: float | None = None,
    ):
        """HFD autofocus; writes ``calibration.products.focuser``."""
        return self._start(self.do_calibrate_focuser, force, ra, dec)

    def endpoint_calibrate_optical_center(
        self,
        force: bool = False,
        ra: float | None = None,
        dec: float | None = None,
    ):
        """Coma-null optical center + low-coma radius; writes ``calibration.products.optical_center``."""
        return self._start(self.do_calibrate_optical_center, force, ra, dec)

    def endpoint_calibrate_stage(
        self,
        force: bool = False,
        ra: float | None = None,
        dec: float | None = None,
        move_to_spec: bool = False,
    ):
        """Pick-off "spec" stage position; writes ``calibration.products.stage``."""
        return self._start(self.do_calibrate_stage, force, ra, dec, move_to_spec)

    def endpoint_status(self):
        return CanonicalResponse(value=self.status())

    def endpoint_abort(self):
        """Clear every calibration flag; running phases poll these and bail."""
        if self.unit is None:
            return CanonicalResponse(errors=["calibrator is not bound to a unit"])
        for activity in ALL_CALIBRATION_ACTIVITIES:
            if self.unit.is_active(activity):
                logger.debug(f"abort: clearing {activity.name}")
                self.unit.end_activity(activity)
        return CanonicalResponse_Ok

    # ------------------------------------------------------------- the phases
    def do_calibrate(self, force: bool = False, ra=None, dec=None):
        """Orchestrate ``focuser -> optical_center -> stage`` under the umbrella."""
        op = "do_calibrate"
        runners = {
            "focuser": self.do_calibrate_focuser,
            "optical_center": self.do_calibrate_optical_center,
            "stage": self.do_calibrate_stage,
        }
        self.unit.start_activity(UnitActivities.Calibrating)
        logger.info(f"{op}: starting, order={' -> '.join(PHASE_ORDER)}, {force=}")
        logger.debug(f"{op}: coord ra={ra} dec={dec}; existing products: "
                     f"{ {p: self.product(p) is not None for p in PHASE_ORDER} }")
        try:
            for phase in PHASE_ORDER:
                if not self.unit.is_active(UnitActivities.Calibrating):
                    logger.info(f"{op}: aborted before '{phase}'")
                    return
                # The skip-when-present decision belongs to the phase itself, so
                # it is not repeated (and cannot diverge) here.
                logger.debug(f"{op}: entering phase '{phase}' (force={force})")
                try:
                    runners[phase](force=force, ra=ra, dec=dec, _umbrella=True)
                    logger.debug(f"{op}: phase '{phase}' returned")
                except CalibrationError as ex:
                    # A missing prerequisite inside the orchestrator is a bug in the
                    # order, not something to work around -- stop the whole run.
                    self._fail(f"{op}: phase '{phase}' failed: {ex}")
                    return
                except Exception as ex:
                    # Name the phase: without this the outer thread wrapper reports
                    # only "do_calibrate failed", losing which phase actually broke.
                    logger.exception(f"{op}: phase '{phase}' raised")
                    self._fail(f"{op}: phase '{phase}' failed: {ex!r}")
                    return
            logger.info(f"{op}: finished")
        finally:
            self.unit.end_activity(UnitActivities.Calibrating)

    def do_calibrate_focuser(self, force=False, ra=None, dec=None, _umbrella=False):
        """HFD autofocus; writes ``calibration.products.focuser``.

        Always runnable -- it requires no other calibration product.  Without an
        optical centre it simply falls back to a geometric low-coma disk, so a
        cold unit can calibrate focus first (which is exactly the order
        ``/calibrate`` needs, since coma is only clean at best focus).

        Does **not** touch ``focuser.known_as_good_position``: the ps3cli path
        owns that, and promoting a calibrated focus into it is a later decision.
        """
        op = "do_calibrate_focuser"
        from calibration.phases.focuser import FocuserCalibrator

        if self._skip_if_present("focuser", force):
            return None

        st = self.settings.focuser
        ra, dec = self.resolve_coord(ra, dec)
        # A memory-capable imager (the ZWO) never needs the folder, so a failure
        # to create it -- missing RAM disk, permissions -- must not abort the
        # run.  Only a file-only imager genuinely requires it, and the phase
        # itself fails clearly in that case.
        try:
            folder = PathMaker().make_autofocus_folder()
        except Exception as ex:
            folder = None
            logger.warning(f"{op}: could not create the run folder ({ex}); "
                           f"continuing without one (memory imager only)")
        logger.debug(f"{op}: standalone={not _umbrella}; folder={folder}; "
                     f"settings images={st.images} spacing={st.spacing} exposure={st.exposure} "
                     f"binning={st.binning} max_tries={st.max_tries}")

        self.unit.start_activity(UnitActivities.CalibratingFocus)
        try:
            calibrator = FocuserCalibrator(self.unit)
            status = calibrator.calibrate(
                settings=st, ra_j2000_hours=ra, dec_j2000_degs=dec, folder=folder,
            )
            self.latest["focuser"] = status
            self.errors.extend(calibrator.errors)
            result = status.analysis_result if status else None
            if result is None or not result.has_solution:
                self._fail(f"{op}: no focus solution")
            else:
                logger.info(f"{op}: best_position={result.best_focus_position:.1f} "
                            f"Dmin={result.best_focus_star_diameter:.2f}px "
                            f"tolerance={result.tolerance:.1f}")
            return status
        finally:
            self.unit.end_activity(UnitActivities.CalibratingFocus)

    @not_implemented("the coma-slope fit and optical-center drive are not built yet")
    def do_calibrate_optical_center(self, force=False, ra=None, dec=None, _umbrella=False):
        """NOT IMPLEMENTED -- the optical-center drive.

        ``calibration.analysis.optical_center.find_optical_center`` is built and
        validated.  Missing: the **coma-slope fit** ``k`` (ellipticity vs. field
        radius, forced through the origin) that yields ``low_coma_radius =
        coma_tolerance / k``; pooling sources across N frames into one weighted
        fit (per-frame centers scatter ~10^2 px, so one frame is untrustworthy);
        and the ``calibration.optical_center`` write.

        Requires ``calibration.focuser.best_position`` -- coma is only clean at
        best focus.
        """

    def do_calibrate_stage(self, force=False, ra=None, dec=None, move_to_spec=False, _umbrella=False):
        """Pick-off stage geometry -- the one phase whose drive already exists.

        Delegates to ``calibration.phases.stage.StageCalibrator``, which sweeps
        the mirror, detects the shadow at each position and solves for the stage
        coordinate placing the centerline on the optical center.
        """
        op = "do_calibrate_stage"
        from calibration.phases.stage import StageCalibrator

        if self._skip_if_present("stage", force):
            return None

        logger.debug(f"{op}: standalone={not _umbrella}; checking required products")
        oc = self.product("optical_center")
        focus = self.product("focuser")
        if oc is None:
            raise CalibrationError("no calibration.products.optical_center -- run 'optical_center' first")
        if focus is None:
            raise CalibrationError("no calibration.products.focuser -- run 'focuser' first")
        logger.debug(f"{op}: prerequisites met -- optical_center=({oc.center_x:.1f}, {oc.center_y:.1f}) "
                     f"epoch={oc.mechanical_epoch}, focus={focus.best_position}")

        self.unit.start_activity(UnitActivities.CalibratingStage)
        try:
            # Hardware the phase makes happen: the focuser goes to the calibrated
            # best focus (no inter-phase carry-over is assumed).
            logger.debug(f"{op}: setting focuser.position = {focus.best_position} (calibrated best focus)")
            self.unit.focuser.position = focus.best_position

            st = self.settings.stage
            ra, dec = self.resolve_coord(ra, dec)
            logger.debug(f"{op}: settings n_positions={st.n_positions} span_steps={st.span_steps} "
                         f"exposure={st.exposure} settle={st.settle_seconds} "
                         f"require_bracketed={st.require_bracketed}")
            result = StageCalibrator(self.unit).calibrate(
                target_ra_j2000_hours=ra,
                target_dec_j2000_degs=dec,
                n_positions=st.n_positions,
                span_steps=st.span_steps,
                exposure=st.exposure,
                settle_seconds=st.settle_seconds,
                require_bracketed=st.require_bracketed,
                # explicit argument wins; otherwise the configured default
                move_to_spec=move_to_spec or st.move_to_spec,
            )
            self.latest["stage"] = result
            if result is None or not result.has_solution:
                self._fail(f"{op}: no solution")
            else:
                logger.info(f"{op}: spec_position={result.spec_position:.1f} "
                            f"(bracketed={result.bracketed}, residual_rms={result.residual_rms:.2f}px)")
            return result
        finally:
            self.unit.end_activity(UnitActivities.CalibratingStage)

    # ---------------------------------------------------------------- helpers
    def _fail(self, msg: str):
        logger.error(msg)
        self.errors.append(msg)

    # ----------------------------------------------------------------- router
    @property
    def api_router(self) -> APIRouter:
        base_path = Const.BASE_UNIT_PATH + "/calibrate"
        tag = "Calibration"

        router = APIRouter()
        router.add_api_route(base_path, methods=["POST"], tags=[tag], endpoint=self.endpoint_calibrate)
        router.add_api_route(
            base_path + "/focuser", methods=["POST"], tags=[tag],
            endpoint=self.endpoint_calibrate_focuser,
        )
        router.add_api_route(
            base_path + "/optical_center", methods=["POST"], tags=[tag],
            endpoint=self.endpoint_calibrate_optical_center,
        )
        router.add_api_route(
            base_path + "/stage", methods=["POST"], tags=[tag],
            endpoint=self.endpoint_calibrate_stage,
        )
        router.add_api_route(base_path + "/status", tags=[tag], endpoint=self.endpoint_status)
        router.add_api_route(
            base_path + "/abort", methods=["POST"], tags=[tag], endpoint=self.endpoint_abort
        )
        return router
