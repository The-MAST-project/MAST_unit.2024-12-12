"""The unit self-calibration orchestrator and its API surface.

One :class:`Calibrator` singleton owns the whole surface::

    POST /calibrate                 ?force=false                  # all three, in order
    POST /calibrate/focuser         ?force=false&ra=&dec=
    POST /calibrate/optical_center  ?force=false&ra=&dec=
    POST /calibrate/stage           ?force=false&ra=&dec=&move_to_spec=false
    POST /calibrate/abort
    GET  /calibrate/status
    GET  /calibrate/config          ?refresh=true

Each ``POST`` returns a ``CanonicalResponse`` **immediately**; the work runs on a
background thread under a ``UnitActivities.Calibrating*`` flag, is visible via
``/calibrate/status``, and is abortable.  ``/status`` reports the *run*
(what is active, which products exist, how the last one ended); ``/config``
reports the *stored block* -- the settings the phases read and the full
products they wrote.

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

import dataclasses
import functools
import logging
import threading
from typing import TYPE_CHECKING, Annotated

import numpy as np
from fastapi import Query
from fastapi.routing import APIRouter
from pydantic import BaseModel

from calibration.logging_context import init_calibration_log, phase_logging
from common.activities import UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.config.calibration import CalibrationConfig, CalibrationSettings
from common.const import Const
from common.paths import PathMaker

if TYPE_CHECKING:
    from unit import Unit  # type: ignore[import-untyped]

logger = logging.getLogger("mast.unit." + __name__)
init_calibration_log(logger)

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


def _jsonable(obj):
    """Project a phase result into something ``CanonicalResponse`` can serialize.

    ``/calibrate/status`` is the ONLY channel through which a background run's
    outcome is observable, so it must never be the thing that fails.  It used
    to: ``latest["stage"]`` is a ``StageGeometryResult`` -- a plain dataclass
    whose ``stage_positions`` / ``distances`` are ``np.ndarray``.  Pydantic has
    no serializer for those and *raises* rather than skipping, so once a stage
    run had happened every ``GET /calibrate/status`` returned 500.  The trap is
    that ``latest[phase]`` is assigned before the ``has_solution`` check, so a
    **failed** run broke status too -- exactly when the errors it carries are
    what you need to read.

    Hence a projection here rather than a fix at the one assignment site: it
    holds for ``optical_center`` when that phase goes live, and for any future
    result type, instead of relying on each phase to remember.  The raw objects
    stay in ``self.latest`` for in-process inspection and plotting.

    ``ndarray`` becomes a list (these are per-frame arrays of ``n_positions``
    points -- five or so, so the payload cost is nil) and NaN/Infinity survive,
    because a NaN sample is real data: it is how a frame with no usable star is
    recorded, and dropping it would silently shorten the sweep.
    """
    if isinstance(obj, np.ndarray):
        return [_jsonable(x) for x in obj.tolist()]
    if isinstance(obj, np.generic):  # np.float64 & friends -> Python scalars
        return obj.item()
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        # Field-by-field, NOT dataclasses.asdict(): asdict deep-copies every
        # value, and on an ndarray field that copy is pointless work.
        return {f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


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

        # setattr, not `wrapper.not_implemented_reason = ...`: attributes cannot
        # be declared on a function object, so direct assignment is a type-check
        # error.  Runtime-identical; `_start` reads it back with getattr.
        setattr(wrapper, "not_implemented_reason", reason)  # noqa: B010
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

    def __init__(self, unit: Unit | None = None):
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

    def _require_unit(self) -> Unit:
        """The bound unit, for the phase methods -- raise rather than crash.

        ``_start`` refuses to launch an unbound calibrator, but the ``do_*``
        methods are also directly callable (the umbrella calls them, and so can
        anything else) -- and Pylance cannot see a cross-method invariant.  One
        explicit check, bound to a local, narrows ``Unit | None`` to ``Unit``
        for the whole method and turns the unbound case into a
        ``CalibrationError`` that ``_start``'s wrapper funnels into
        ``/calibrate/status`` instead of an ``AttributeError`` traceback.
        """
        if self.unit is None:
            raise CalibrationError("calibrator is not bound to a unit")
        return self.unit

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

    def resolve_coord(self, ra: float | None, dec: float | None) -> tuple[float | None, float | None]:
        """Pointing for a phase -- **the zenith** by default.

        Explicit argument -> config -> observatory.  The two axes resolve at
        different moments, deliberately:

        * ``ra=None`` is returned as ``None``, meaning *transit*.  The caller
          substitutes the current LST at slew time, because LST advances while
          the phase does its hardware preparation (stage home, focuser move).
        * ``dec=None`` resolves **here** to the site latitude -- the zenith.
          Latitude does not change, so there is nothing to gain by deferring
          it, and resolving centrally means all three phases get a concrete
          number without each re-implementing the lookup.

        ``dec`` may still come back ``None`` if neither the mount nor the config
        can supply a latitude; every phase already treats a missing coordinate
        as "calibrate at the current pointing", which is a better outcome than
        failing a run over a default.
        """
        coord = self.settings.coord
        ra = ra if ra is not None else coord.ra
        dec = dec if dec is not None else coord.dec
        if dec is None:
            dec = self._zenith_dec()
        logger.debug(
            f"resolved pointing: ra={ra if ra is not None else 'LST (transit)'} "
            f"dec={dec if dec is not None else 'unknown (no slew)'}"
        )
        return ra, (float(dec) if dec is not None else None)

    def _zenith_dec(self) -> float | None:
        """The declination of the zenith: the observatory's latitude.

        Taken from the **mount** first.  PWI4 reports it in the very status
        object the phases already read for the LST
        (``pw.status().site.latitude_degs``), and the mount is the thing that
        actually points -- so its own site setting is authoritative for where
        zenith is.  The MAST config carries a latitude too, and the two differ
        in the 4th decimal (~2 m, harmless), but preferring the mount avoids
        pointing by one source of truth while slewing by another.

        Falls back to the configured site latitude, then to ``None``.
        """
        pw = getattr(self.unit, "pw", None)
        if pw is not None:
            try:
                latitude = pw.status().site.latitude_degs
                if latitude is not None:
                    return float(latitude)
            except Exception as ex:
                logger.warning(f"could not read the mount's latitude: {ex}")
        try:
            site = Config().local_site
            location = getattr(site, "location", None) if site is not None else None
            latitude = getattr(location, "latitude", None) if location is not None else None
            if latitude is not None:
                logger.debug("zenith dec: falling back to the configured site latitude")
                return float(latitude)
        except Exception as ex:
            logger.warning(f"could not read the configured site latitude: {ex}")
        logger.warning("no latitude available -- pointing is unresolved; the phase will calibrate at the current pointing")
        return None

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
            "umbrella": (self.unit.is_active(UnitActivities.Calibrating) if self.unit else False),
            "phase": self.active_phase,
            "products": {p: self.product(p) is not None for p in PHASE_ORDER},
            "latest": _jsonable(self.latest),
            "errors": self.errors,
        }

    def config(self, refresh: bool = True) -> dict:
        """The unit's stored ``calibration`` block -- settings *and* products.

        The counterpart to :meth:`status`: that one reports the run, this one
        reports what is persisted -- the inputs each phase reads and the full
        product records (values, provenance, quality), where ``status`` only
        answers present/absent.

        ``refresh=True`` (default) goes through ``Config().get_unit()``, which
        re-runs the common+unit merge and validation; ``refresh=False`` returns
        ``unit.unit_conf``, the copy bound at startup that the phases actually
        read.

        **Neither re-reads MongoDB.**  ``Config.config_db()`` returns a snapshot
        cached in the process (behind an ``lru_cache``), so an edit made to the
        DB after startup -- by hand, or by another machine -- is invisible to
        both until the service restarts.  This is documented rather than fixed
        because forcing a reload means reaching into ``common/config``, which is
        submoduled into four checkouts.  So: after changing a calibration
        setting in the DB, RESTART the unit before expecting a run to use it.
        ``source`` reports which of the two in-process views you got.

        ``present=False`` means the unit's DB entry carries no ``calibration``
        block at all; ``calibration`` then holds the model defaults -- the same
        ones :attr:`settings` falls back to, so the payload shows the values the
        phases would really use rather than a bare ``null``.
        """
        conf = None
        source = "config-merge"
        if refresh:
            try:
                conf = Config().get_unit()
            except Exception as ex:
                logger.error(f"config: DB read failed ({ex!r}) -- falling back to the loaded config")
        if conf is None:
            source = "memory"
            conf = getattr(self.unit, "unit_conf", None)
        if conf is None:
            raise CalibrationError("no unit configuration available (neither the DB nor the loaded config)")

        cal = getattr(conf, "calibration", None)
        logger.debug(f"config: source={source} unit={conf.name} present={cal is not None}")
        return {
            "source": source,
            "unit": conf.name,
            "present": cal is not None,
            "calibration": (cal if cal is not None else CalibrationConfig()).model_dump(mode="json"),
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
            return CanonicalResponse(errors=[f"a calibration is already running (phase={self.active_phase})"])
        # Clear BOTH, not just errors.  `latest` used to survive into the next
        # run, so while a run was in flight `/calibrate/status` served the
        # PREVIOUS run's result as if it were current -- during run 0004 it
        # reported run 0003's sweep positions and "0 consistent stars", which
        # reads exactly like a finished, failed run.  Status is the only channel
        # a background run has; it must never show a stale result as live.
        self.errors = []
        self.latest = {}
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

        self._thread = threading.Thread(name=f"mast-{target.__name__}", target=run, daemon=True)
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

    def endpoint_config(
        self,
        refresh: Annotated[
            bool,
            Query(
                description="Re-merge via Config (false: the copy bound at startup). Neither re-reads MongoDB -- restart to pick up DB edits."
            ),
        ] = True,
    ):
        """The unit's stored calibration settings + products."""
        try:
            return CanonicalResponse(value=self.config(refresh))
        except CalibrationError as ex:
            # A read, not a run: report it in this response rather than parking it
            # in self.errors, which belongs to the last calibration run.
            return CanonicalResponse(errors=[f"config: {ex}"])

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
        unit = self._require_unit()
        runners = {
            "focuser": self.do_calibrate_focuser,
            "optical_center": self.do_calibrate_optical_center,
            "stage": self.do_calibrate_stage,
        }
        unit.start_activity(UnitActivities.Calibrating)
        logger.info(f"{op}: starting, order={' -> '.join(PHASE_ORDER)}, {force=}")
        logger.debug(
            f"{op}: coord ra={ra} dec={dec}; existing products: { {p: self.product(p) is not None for p in PHASE_ORDER} }"
        )
        try:
            for phase in PHASE_ORDER:
                if not unit.is_active(UnitActivities.Calibrating):
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
            unit.end_activity(UnitActivities.Calibrating)

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
        unit = self._require_unit()
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
            folder = PathMaker().make_calibration_folder("focuser")
        except Exception as ex:
            folder = None
            logger.warning(f"{op}: could not create the run folder ({ex}); continuing without one (memory imager only)")
        logger.debug(
            f"{op}: standalone={not _umbrella}; folder={folder}; "
            f"settings images={st.images} spacing={st.spacing} exposure={st.exposure} "
            f"binning={st.binning} max_tries={st.max_tries}"
        )

        unit.start_activity(UnitActivities.CalibratingFocus)
        with phase_logging("focuser"):
            try:
                calibrator = FocuserCalibrator(unit)
                status = calibrator.calibrate(
                    settings=st,
                    ra_j2000_hours=ra,
                    dec_j2000_degs=dec,
                    folder=folder,
                )
                self.latest["focuser"] = status
                self.errors.extend(calibrator.errors)
                result = status.analysis_result if status else None
                if result is None or not result.has_solution:
                    self._fail(f"{op}: no focus solution")
                else:
                    logger.info(
                        f"{op}: best_position={result.best_focus_position:.1f} "
                        f"Dmin={result.best_focus_star_diameter:.2f}px "
                        f"tolerance={result.tolerance:.1f}"
                    )
                return status
            finally:
                unit.end_activity(UnitActivities.CalibratingFocus)

    def do_calibrate_optical_center(self, force=False, ra=None, dec=None, _umbrella=False):
        """Coma-null optical center + low-coma radius; writes
        ``calibration.products.optical_center``.

        Requires ``calibration.products.focuser`` -- coma is only clean at best
        focus (defocused, a star is a pupil donut with no usable coma) -- and
        commands the focuser there on entry: a *hardware* precondition the phase
        makes happen, versus the *product* precondition it requires.
        """
        op = "do_calibrate_optical_center"
        unit = self._require_unit()
        from calibration.phases.optical_center import OpticalCenterCalibrator

        if self._skip_if_present("optical_center", force):
            return None

        logger.debug(f"{op}: standalone={not _umbrella}; checking required products")
        focus = self.product("focuser")
        if focus is None:
            raise CalibrationError("no calibration.products.focuser -- run 'focuser' first")
        logger.debug(f"{op}: prerequisite met -- focus={focus.best_position}")

        unit.start_activity(UnitActivities.CalibratingOpticalCenter)
        with phase_logging("optical_center"):
            try:
                # Hardware the phase makes happen: coma is only clean at best
                # focus, so command it here; the phase itself waits (bounded)
                # for the move to finish before exposing.
                if unit.focuser is None:
                    raise CalibrationError("unit has no focuser -- cannot go to best focus")
                logger.debug(f"{op}: setting focuser.position = {focus.best_position} (calibrated best focus)")
                unit.focuser.position = focus.best_position

                st = self.settings.optical_center
                ra, dec = self.resolve_coord(ra, dec)
                # Same tolerance as the other phases: a memory imager never
                # needs the folder, so failing to create one must not abort.
                try:
                    folder = PathMaker().make_calibration_folder("optical_center")
                except Exception as ex:
                    folder = None
                    logger.warning(
                        f"{op}: could not create the run folder ({ex}); continuing without one (memory imager only)"
                    )
                logger.debug(
                    f"{op}: folder={folder}; settings exposure={st.exposure} "
                    f"number_of_frames={st.number_of_frames} "
                    f"coma_tolerance={st.coma_tolerance} "
                    f"min_frames_passing={st.min_frames_passing}"
                )
                calibrator = OpticalCenterCalibrator(unit)
                result = calibrator.calibrate(
                    settings=st,
                    ra_j2000_hours=ra,
                    dec_j2000_degs=dec,
                    folder=folder,
                )
                self.latest["optical_center"] = result
                self.errors.extend(calibrator.errors)
                if result is None:
                    self._fail(f"{op}: no optical-center solution")
                else:
                    logger.info(
                        f"{op}: center=({result.center_x:.1f}, {result.center_y:.1f}) "
                        f"radiality={result.radiality:.2f} "
                        f"residual_rms={result.residual_rms:.1f}px"
                    )
                return result
            finally:
                unit.end_activity(UnitActivities.CalibratingOpticalCenter)

    def do_calibrate_stage(self, force=False, ra=None, dec=None, move_to_spec=False, _umbrella=False):
        """Pick-off stage geometry -- the one phase whose drive already exists.

        Delegates to ``calibration.phases.stage.StageCalibrator``, which sweeps
        the mirror, detects the shadow at each position and solves for the stage
        coordinate placing the centerline on the optical center.
        """
        op = "do_calibrate_stage"
        unit = self._require_unit()
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
        logger.debug(
            f"{op}: prerequisites met -- optical_center=({oc.center_x:.1f}, {oc.center_y:.1f}) "
            f"epoch={oc.mechanical_epoch}, focus={focus.best_position}"
        )

        unit.start_activity(UnitActivities.CalibratingStage)
        with phase_logging("stage"):
            try:
                # Hardware the phase makes happen: the focuser goes to the calibrated
                # best focus (no inter-phase carry-over is assumed).
                if unit.focuser is None:
                    raise CalibrationError("unit has no focuser -- cannot go to best focus")
                logger.debug(f"{op}: setting focuser.position = {focus.best_position} (calibrated best focus)")
                unit.focuser.position = focus.best_position

                st = self.settings.stage
                ra, dec = self.resolve_coord(ra, dec)
                # Same tolerance as the focus phase: a memory-capable imager
                # never needs the folder, so failing to create one must not
                # abort the run -- but a file-only imager (PHD2) fails its first
                # precondition without it, which is why this phase could not run
                # at all before: the folder was simply never passed.
                try:
                    folder = PathMaker().make_calibration_folder("stage")
                except Exception as ex:
                    folder = None
                    logger.warning(
                        f"{op}: could not create the run folder ({ex}); continuing without one (memory imager only)"
                    )
                logger.debug(
                    f"{op}: folder={folder}; settings n_positions={st.n_positions} "
                    f"span_steps={st.span_steps} "
                    f"exposure={st.exposure} settle={st.settle_seconds} "
                    f"require_bracketed={st.require_bracketed}"
                )
                result = StageCalibrator(unit).calibrate(
                    folder=folder,
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
                    logger.info(
                        f"{op}: spec_position={result.spec_position:.1f} "
                        f"(bracketed={result.bracketed}, residual_rms={result.residual_rms:.2f}px)"
                    )
                return result
            finally:
                unit.end_activity(UnitActivities.CalibratingStage)

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
            base_path + "/focuser",
            methods=["POST"],
            tags=[tag],
            endpoint=self.endpoint_calibrate_focuser,
        )
        router.add_api_route(
            base_path + "/optical_center",
            methods=["POST"],
            tags=[tag],
            endpoint=self.endpoint_calibrate_optical_center,
        )
        router.add_api_route(
            base_path + "/stage",
            methods=["POST"],
            tags=[tag],
            endpoint=self.endpoint_calibrate_stage,
        )
        router.add_api_route(base_path + "/status", tags=[tag], endpoint=self.endpoint_status)
        router.add_api_route(base_path + "/config", tags=[tag], endpoint=self.endpoint_config)
        router.add_api_route(base_path + "/abort", methods=["POST"], tags=[tag], endpoint=self.endpoint_abort)
        return router
