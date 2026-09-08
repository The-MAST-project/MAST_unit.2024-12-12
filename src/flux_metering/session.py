"""One run of `acquire_and_find_max_flux`.

Acquire a star, walk a spiral, find the pointing where most light reaches the fibre, and
correlate the imager frame taken there against the reference to get `dx, dy` -- the offset
between where acquisition PUT the star and where the fibre actually is.

Design: `mast-claude-config/plans/flux_metering_design.md`. The decisions that are easiest
to undo by accident, and why they are what they are:

* **The ring is the unit of the stopping rule** (section 4). A square spiral circles the
  origin, so flux rises and falls on every ring; "it went up then down" stops at the first
  near-pass. A complete ring with no improvement is the smallest statement about the
  neighbourhood that means anything.
* **Ring membership is READ, not assumed.** PWI4 owns the traversal order, so the ring is
  derived from the offsets it reports -- `max(|x|, |y|)` -- and a ring is finished when the
  reported ring first exceeds it.
* **No backtrack** (section 3.1). The correlation reads the imager frame already taken at the
  arg-max index, which is why every frame is kept at full sampling: which one turns out to be
  the arg-max is unknown until the search ends.
* **Saturation is recorded, never acted on** (section 5.4). `argmax_saturated` is what says
  whether a run's answer is usable.
* Nothing here writes configuration.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import shutil
import statistics
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from astropy.io import fits

from common.activities import UnitActivities
from common.filer import Filer, FilerTop, MoveGuardian
from common.mast_logging import get_logger
from common.models.statuses import (
    FluxMeteringExposure,
    FluxMeteringResult,
    FluxMeteringStatus,
    FluxMeteringStep,
    ImagerSettings,
)
from common.parsers import sexagesimal_degrees_to_decimal, sexagesimal_hours_to_decimal
from common.paths import PathMaker
from common.utils import function_name
from flux_metering.correlate import measure_pair
from flux_metering.flux_meter import FluxMeter, FluxMeterError, frame_flux, saturated_pixels
from imaging.frame_shift import MIN_CONFIDENCE
from mount import SettleMode
from spiral_search import resolve_center
from stage import StagePresetPosition

if TYPE_CHECKING:
    from unit import Unit

logger = get_logger(__name__)
filer = Filer(logger)

REFERENCE_IMAGE = "reference.fits"

#: How long to wait for the whole acquisition to reach tolerance before giving up. The
#: procedure is meaningless without a converged acquisition, so this is a ceiling on
#: waiting, not a tolerance of its own.
ACQUISITION_TIMEOUT_SECONDS = 900.0

#: How long to wait for the solver's background cleanup to put the solved frame beside the
#: original. It is normally there within a second; this only avoids assuming it.
SOLVED_FRAME_WAIT_SECONDS = 30.0

#: How long to wait, at the end of a run, for a reference solve that is somehow still
#: going. It has had the whole spiral already; this only stops a wedged solver from holding
#: the run open forever.
SOLVE_JOIN_TIMEOUT_SECONDS = 60.0

#: How long to wait for the folding mirror to reach SPEC. A Sky->Spec traverse was measured
#: at 21-22 seconds on mast02 (2026-09-02), so this is a generous ceiling on a move that
#: normally takes half a minute -- not a tolerance.
STAGE_TIMEOUT_SECONDS = 120.0

#: A pixel count, not a boolean. The ThorCam's field is black, so one hot pixel or a cosmic
#: ray would otherwise mark every frame of a 30-minute run as saturated.
SATURATED_PIXELS_ALLOWED = 5

#: Below this much free space on the ram disk, a step waits for the mover to catch up before
#: exposing again.
#:
#: Every frame is handed to `move_ram_to_shared` the moment it is written, and that is
#: asynchronous, so in normal running the disk holds only the in-flight backlog -- roughly
#: 10 MB/s of demand against a share that can take far more. This matters when the share
#: STALLS: the backlog then grows instead of draining, and with ~94 MB per imager frame a run
#: would fill the disk mid-exposure and fail somewhere confusing, part-written.
RAM_DISK_MIN_FREE_BYTES = 3 * 1024**3

#: How long to let the mover drain before giving up on a run. Long enough to ride out a brief
#: share hiccup, short enough not to sit through an outage with the mount parked on a cell.
RAM_DISK_DRAIN_TIMEOUT_SECONDS = 120.0


class FluxMeteringError(Exception):
    """A flux-metering run could not be set up, or could not do something it needs to do."""


@dataclass
class FluxMeteringParams:
    """What the operator chose. Carried whole so the result can echo it back."""

    seconds: float = 5.0
    ra_j2000_hours: float | None = None
    dec_j2000_degs: float | None = None
    gain: int | None = None
    x_step_arcsec: float = 0.5
    y_step_arcsec: float = 0.5
    max_rings: int = 6
    patience_rings: int = 1
    max_radius_arcsec: float = 10.0
    flux_gain: int = 0
    flux_black_level: int = 3
    number_of_frames: int = 3
    skip_acquisition: bool = False
    usable_fraction: float = 0.66

    @property
    def flux_exposure_us(self) -> int:
        """The ThorCam exposure follows the imager's.

        Not a convenience: a millisecond exposure samples one instant of a twinkling star,
        so the flux curve would carry scintillation on top of the coupling signal it exists
        to resolve -- and the arg-max is decided exactly where that curve is flattest.
        Seconds of integration smooth it.
        """
        return round(self.seconds * 1_000_000)


class FluxMeteringSession:
    """One run at a time, owned by the Unit."""

    def __init__(self, unit: Unit, flux_meter: FluxMeter | None = None):
        self.unit = unit
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Injected for tests and for any machine without a Zelux attached; `None` means
        #: open a real ThorCam when the run starts.
        self._injected_meter = flux_meter
        self._meter: FluxMeter | None = None
        self._solve_thread: threading.Thread | None = None
        self._reference_solution: dict[str, Any] | None = None
        self._solver_name: str | None = None
        self.state = FluxMeteringStatus()
        self.params = FluxMeteringParams()
        self.steps: list[FluxMeteringStep] = []

    # ------------------------------------------------------------------- control --

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def has_run(self) -> bool:
        """Whether there is anything to report.

        `FullUnitStatus.flux_metering` is None until this is true, so the field costs a unit
        that never meters flux nothing at all -- and once a run has happened it stays
        populated, because the last run's dx/dy is worth having in the unit's own status
        rather than only in the products on the share.
        """
        return self._thread is not None

    def require_can_start(self) -> str | None:
        """Why a run cannot start, or None.

        A guard rather than an `assert`: asserts are stripped under `python -O`, and the
        response envelope renders `AssertionError` as an anonymous error carrying nothing
        the caller can act on.

        Everything here is checked BEFORE the thread is dispatched -- a route that
        dispatches and answers Ok has already spent the request.
        """
        if self.is_active:
            return "a flux-metering run is already in progress"
        if self.unit.mount is None or self.unit.imager is None:
            return "the mount and the imager must both be present"
        busy = (
            UnitActivities.Acquiring
            | UnitActivities.Guiding
            | UnitActivities.Autofocusing
            | UnitActivities.StartingUp
            | UnitActivities.ShuttingDown
        )
        if self.unit.activities & busy:
            return f"the unit is busy ({self.unit.activities_verbal})"
        if self.unit.acquirer is None:
            return "no acquirer, so the star cannot be put on the fibre"
        # The fibre sees light only with the folding mirror in, so a run without a stage
        # would meter darkness and report it as a flux curve. Here with its siblings
        # rather than as an `assert` at the call site: an assert is stripped under
        # `python -O`, and the envelope renders AssertionError as an anonymous error.
        if self.unit.stage is None:
            return "no stage, so the folding mirror cannot be put in"

        # Checked here rather than discovered mid-run: a run that starts with the ram disk
        # already near full has nowhere to put its first frames, and the mover cannot help
        # if what filled it was somebody else's stranded products.
        free = self._ram_disk_free_bytes()
        if free is not None and free < RAM_DISK_MIN_FREE_BYTES:
            return (
                f"only {free / 1024**3:.1f} GB free where the frames are written; "
                f"at least {RAM_DISK_MIN_FREE_BYTES / 1024**3:.0f} GB is wanted before starting"
            )
        return None

    def start(self, params: FluxMeteringParams):
        """Validate, claim the unit, and dispatch. Returns a refusal or the initial state."""
        with self._lock:
            refusal = self.require_can_start()
            if refusal is not None:
                return refusal

            self.params = params
            self.steps = []
            self._stop.clear()
            folder = PathMaker().make_flux_metering_folder()
            self.state = FluxMeteringStatus(
                active=True,
                phase="acquiring",
                folder=folder,
                started_at=isoformat_utc(),
            )
            # Raised here rather than in the thread: the endpoint declares this flag as its
            # completion signal, so a caller answered Ok must find it already set.
            self.unit.start_activity(UnitActivities.FluxMetering)
            self._thread = threading.Thread(
                name="acquire_and_find_max_flux",
                target=self.do_acquire_and_find_max_flux,
                daemon=True,
            )
            self._thread.start()
            logger.info(f"flux metering started, products under '{folder}'")
            return self.status()

    def abort(self) -> None:
        """Ask the run to stop. It finishes the exposure it is inside, then unwinds."""
        if self.is_active:
            logger.info("flux metering: abort requested")
            self._stop.set()

    def status(self) -> FluxMeteringStatus:
        """The typed model, not a dict.

        `FullUnitStatus.flux_metering` is typed as this, and the endpoint contract's one
        load-bearing exception is that a status returns its bare model -- an envelope nested
        inside the payload would break every consumer silently.
        """
        self.state.active = self.is_active
        self.state.steps = list(self.steps)
        return self.state

    # -------------------------------------------------------------------- the run --

    def do_acquire_and_find_max_flux(self) -> None:
        """The whole run, on its own thread. Never raises: the finally clause owns the
        unwind, and an escaping exception would leave the activity flag set and hang every
        caller watching it."""
        op = function_name()
        try:
            if not self._position_folding_mirror():
                self._finish("failed")
                return
            self._open_meter()
            if self.params.skip_acquisition:
                # Engineering only. The spiral, the products and the correlation can then be
                # exercised with the mount wherever it happens to point -- in daylight, in a
                # closed enclosure, with no star and no solve. Without this the whole path is
                # untestable until a clear night, which is the worst possible first exposure
                # for code that drives a mount.
                #
                # A run started this way is NOT a calibration: the star is not on the assumed
                # fibre, so `dx, dy` measures nothing. `skip_acquisition` is echoed into
                # result.json with the rest of the parameters, so such a run can never be
                # mistaken for a real one after the fact.
                logger.warning(
                    "flux metering: acquisition SKIPPED by request -- this run is a shakedown, "
                    "and its dx/dy is not a fibre-position measurement"
                )
            elif not self._acquire():
                self._finish("acquisition_failed")
                return

            self.state.phase = "reference"
            reference = self._expose_reference()
            # Started here, joined in `_finish`: it overlaps the whole spiral, so its ~13 s
            # costs the run nothing.
            self.start_solving_the_reference()

            self.state.phase = "spiral"
            terminal = self._walk_spiral()

            self.state.phase = "correlating"
            # Joined HERE, not in `_finish`: the correlation below converts arcsec to
            # pixels with the SOLVED plate scale, so the answer has to be in before
            # `_measure` runs, not merely before the document is written. It has had the
            # whole spiral to finish and normally returned long ago.
            self._await_reference_solve()
            self.state.result = self._measure(reference)
            if self.state.result is not None:
                self.do_record_sky_offset(self.state.result)
            self._finish(terminal)
        except Exception as ex:  # the thread owns the mount; it must land it safely
            logger.exception(f"{op}: flux metering failed")
            self.state.last_error = str(ex)
            self._finish("failed")

    def _position_folding_mirror(self) -> bool:
        """Put the folding mirror in, and WAIT for it to arrive. True when it is there.

        On the worker thread rather than in the endpoint, because it takes 21-22 seconds
        on this hardware and the route is documented to answer at once. `state.phase` says
        what is happening, so the wait is visible in `find_max_flux_status` rather than
        merely long.

        Waits on POSITION, not on `Stage.is_moving`. `is_moving` is a plain attribute
        refreshed by the stage's 2-second `ontimer` poll, and `move_to_preset` does not set
        it -- so for up to one poll period after a move is commanded it still reads False,
        and a `while stage.is_moving` loop falls straight through. On the night of
        2026-09-02 that raced every time: `_await_stage` returned 6 ms into a 133,000-count
        move, and the acquisition went on to expose with the mirror a quarter of the way
        across. `at_preset` is true when the mirror is actually there, whenever the poll
        last ran.
        """
        stage = self.unit.stage
        if stage is None:  # `require_can_start` refuses this; here so the wait cannot lie
            self.state.last_error = "no stage, so the folding mirror cannot be put in"
            return False

        self.state.phase = "positioning"
        if stage.at_preset(StagePresetPosition.Spec):
            return True

        logger.info("flux metering: moving the folding mirror to SPEC")
        stage.move_to_preset(StagePresetPosition.Spec)

        deadline = time.monotonic() + STAGE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._stop.is_set():
                self.state.last_error = "aborted while positioning the folding mirror"
                return False
            if stage.at_preset(StagePresetPosition.Spec):
                logger.info(f"flux metering: folding mirror at SPEC (position={stage.position})")
                return True
            time.sleep(0.2)

        self.state.last_error = (
            f"the folding mirror did not reach SPEC within {STAGE_TIMEOUT_SECONDS:g}s (position={stage.position})"
        )
        return False

    def start_solving_the_reference(self) -> None:
        """Kick off the reference solve; it runs while the spiral walks.

        A background thread because the solve takes ~13 s against a spiral that takes
        tens of minutes, and nothing in the walk depends on the answer -- it is joined in
        `_finish`, by which time it has long finished. Blocking here would add 13 s to
        every run for a number nobody needs until the end.
        """
        if self.state.reference_frame is None:
            return
        self._solve_thread = threading.Thread(
            name="solve-reference",
            target=self.do_solve_reference,
            args=(self.state.reference_frame,),
            daemon=True,
        )
        self._solve_thread.start()

    def do_solve_reference(self, file_name: str) -> None:
        """Plate-solve the reference frame, recording the outcome in `_reference_solution`.

        Never raises and never fails the run. A WCS is worth having and is not worth losing
        a spiral for: it is what turns `dx, dy` in detector pixels into a statement about
        the sky, and its `pixel_scale` is a MEASURED plate scale to set beside the
        configured one that `commanded_offset_px` depends on (MAST_unit#138 records that
        value having been 0.0 in the database).

        Solving does not touch the frame. The backend opens it read-only and works in
        `<ram>/tmp/`, and its artifacts are named `<frame>,solver=<name>.fits` and
        `...-result.txt`, so they land beside the original rather than over it.
        """
        started = time.monotonic()
        try:
            from solvers.mastrometry import MastrometryDotNet

            # The backend directly, not `Solver`: `Solver.solve()` takes an exposure of its
            # own, and the whole point here is to solve a frame that already exists.
            # mastrometry is what the design fixes for this procedure in any case.
            solver = MastrometryDotNet()
            self._solver_name = solver.name
            result = solver.solve(
                unit=self.unit,
                phase="spec",
                full_frame_input_image_path=self._frame_path(file_name),
            )
        except Exception as ex:  # noqa: BLE001 -- a lost WCS must not cost the run
            logger.exception("flux metering: solving the reference frame failed")
            self._reference_solution = {"frame": file_name, "succeeded": False, "errors": [str(ex)]}
            return

        elapsed = time.monotonic() - started
        if result is None or not result.succeeded:
            errors = list(result.errors) if result is not None and result.errors else ["no result"]
            logger.warning(f"flux metering: the reference frame did not solve ({errors})")
            self._reference_solution = {"frame": file_name, "succeeded": False, "errors": errors}
            return

        solution = result.solution.model_dump() if result.solution is not None else {}
        logger.info(
            f"flux metering: reference solved in {elapsed:.1f}s -- "
            f"ra={solution.get('ra_hours')}h dec={solution.get('dec_degs')}d "
            f"scale={solution.get('pixel_scale')} rotation={solution.get('rotation_angle_degs')}"
        )
        self._reference_solution = {
            "frame": file_name,
            "succeeded": True,
            "elapsed_seconds": round(elapsed, 1),
            # NOTE `pixel_scale` is of the DOWNSAMPLED frame the solver builds, not of the
            # detector: the backend bins 2x2 before solving. Recorded raw, and not halved
            # here, because the factor is the backend's business and a derived number that
            # silently assumes it would be wrong the day it changes.
            "downsample_factor_note": "pixel_scale and the CD matrix are per downsampled pixel",
            **solution,
        }

    def _await_reference_solve(self) -> None:
        """Wait out the reference solve, at most once. Bounded, so a wedged solver cannot
        hold a run open -- it has already had the whole spiral."""
        thread = self._solve_thread
        if thread is None or not thread.is_alive():
            return
        logger.info("flux metering: waiting for the reference solve to finish")
        thread.join(timeout=SOLVE_JOIN_TIMEOUT_SECONDS)
        if thread.is_alive():
            logger.warning("flux metering: the reference solve did not finish in time")
            self._reference_solution = {"succeeded": False, "errors": ["the solve did not finish in time"]}

    def _solved_pixel_scale_at_bin1(self) -> float | None:
        """The plate scale the reference frame actually had, in arcsec per bin-1 pixel.

        The solver reports its scale per DOWNSAMPLED pixel -- it bins the full frame before
        handing it to astrometry.net -- so the factor is divided out here, using the
        backend's own constant rather than a hardcoded 2 that would go quietly wrong the
        day the backend changed it.
        """
        solution = self._reference_solution
        if not solution or not solution.get("succeeded"):
            return None
        scale = solution.get("pixel_scale")
        if not scale or scale <= 0:
            return None
        from solvers.mastrometry import DOWNSAMPLE_FACTOR

        return scale / DOWNSAMPLE_FACTOR

    def _solved_frame_name(self) -> str | None:
        """The solver's own copy of the reference frame, which carries the WCS.

        Named by the backend as `<frame>,solver=<name>.fits`, so it sits beside the original
        rather than over it. Built from the solver's reported name rather than a literal, so
        a different backend does not silently produce a path that never resolves.
        """
        if self._solver_name is None or self.state.reference_frame is None:
            return None
        return self.state.reference_frame.replace(".fits", f",solver={self._solver_name}.fits")

    def do_record_sky_offset(self, result: FluxMeteringResult) -> None:
        """Write the measurement into the solved frame's FITS header.

        Into the SOLVED frame, never the original: that one is an input, and the whole
        correlation depends on it being byte-identical to what the run exposed. The solved
        copy is an artifact the backend produced, so annotating it costs nothing and puts
        the answer where the WCS that produced it already lives -- open the file and both
        the astrometry and what it was used for are in one header.

        The backend hands that file to a background cleanup thread, so it may not be in
        place the instant the solve returns; this waits briefly rather than assuming. Every
        failure is swallowed: a header that could not be annotated is a lost convenience,
        not a lost run, and the same numbers are in `result.json` regardless.
        """
        name = self._solved_frame_name()
        if name is None or result.dx is None:
            return

        deadline = time.monotonic() + SOLVED_FRAME_WAIT_SECONDS
        path = None
        while time.monotonic() < deadline:
            try:
                path = self._frame_path(name)
                break
            except FluxMeteringError:
                time.sleep(0.5)
        if path is None:
            logger.warning(f"flux metering: '{name}' never appeared; the sky offset is in result.json only")
            return

        try:
            with MoveGuardian().protect(path), fits.open(path, mode="update") as hdul:
                header = hdul[0].header  # type: ignore[union-attr]
                header["FMDXPX"] = (round(result.dx, 4), "flux metering dx, detector pixels")
                header["FMDYPX"] = (round(result.dy, 4), "flux metering dy, detector pixels")
                if result.sky_dx_arcsec is not None and result.sky_dy_arcsec is not None:
                    header["FMDXSKY"] = (round(result.sky_dx_arcsec, 4), "dRA*cos(dec), arcsec")
                    header["FMDYSKY"] = (round(result.sky_dy_arcsec, 4), "dDec, arcsec")
                if result.confidence is not None:
                    header["FMCONF"] = (round(result.confidence, 4), "correlation confidence")
                header["FMLOWCNF"] = (bool(result.low_confidence), "below MIN_CONFIDENCE: dx/dy not usable")
                header["HISTORY"] = "MAST flux metering: FMDX/FMDY are the fibre offset measured against this frame"
            logger.info(f"flux metering: recorded the sky offset in '{name}'")
        except Exception as ex:  # noqa: BLE001 -- an un-annotated header must not cost a run
            logger.warning(f"flux metering: could not annotate '{name}': {ex}")

    def _sky_offset(self, dx: float, dy: float) -> tuple[float | None, float | None, str]:
        """(dRA*cos(dec), dDec) in arcsec for a detector offset, and how it was obtained.

        This is what makes `dx, dy` mean something outside this detector. The whole point of
        the procedure is a fibre position, and a fibre position expressed in pixels is only
        interpretable by someone holding the same camera at the same rotation.

        Uses the WCS **CD matrix** rather than `rotation_angle_degs`. The rotation angle says
        how the field is turned; it does not say whether it is MIRRORED, and assuming the
        wrong parity flips the sign of dRA while leaving its magnitude correct -- an error
        that looks entirely plausible and is exactly the class section 9.1 of the design
        calls "the likeliest bug in the whole procedure". The CD matrix carries rotation,
        scale and parity in one object and cannot be half-right.

        The matrix is per DOWNSAMPLED pixel, because the backend bins before solving, so the
        offset is scaled into that grid first. Near the reference pixel the standard
        coordinates it produces are dRA*cos(dec) and dDec to well under a milliarcsecond at
        these separations.
        """
        solution = self._reference_solution
        if not solution or not solution.get("succeeded"):
            return None, None, "the reference frame did not solve"
        cd = [solution.get(k) for k in ("cd1_1", "cd1_2", "cd2_1", "cd2_2")]
        if any(term is None for term in cd):
            return None, None, "the solved frame carried no CD matrix"

        from solvers.mastrometry import DOWNSAMPLE_FACTOR

        cd1_1, cd1_2, cd2_1, cd2_2 = (float(term) for term in cd)  # type: ignore[arg-type]
        dxd, dyd = dx / DOWNSAMPLE_FACTOR, dy / DOWNSAMPLE_FACTOR
        return (
            3600.0 * (cd1_1 * dxd + cd1_2 * dyd),
            3600.0 * (cd2_1 * dxd + cd2_2 * dyd),
            "the solved frame's WCS CD matrix (rotation and parity together)",
        )

    def _effective_dec(self) -> tuple[float | None, str]:
        """(declination, where it came from) for the cos(dec) factor.

        The SOLVED declination wins, for the same reason the solved plate scale does: it is
        measured from this run's own reference frame. `_dec_degrees()` reads
        `mount.status().dec_j2000_degs`, which is correct only while the run is happening --
        it is wherever the telescope is pointing when asked, so a re-correlation an hour
        later gets an unrelated part of the sky. The solve pins it to the frame.

        The mount is the fallback, and which was used is recorded beside the answer.
        """
        solution = self._reference_solution
        if solution and solution.get("succeeded") and solution.get("dec_degs") is not None:
            return float(solution["dec_degs"]), "solved from the reference frame"
        mount_dec = self._dec_degrees()
        if mount_dec is not None:
            return mount_dec, "the mount's pointing (the reference did not solve)"
        return None, "neither a solve nor a mount reading"

    def _effective_pixel_scale(self) -> tuple[float | None, str]:
        """(scale, where it came from) for the arcsec->pixel conversion.

        The SOLVED scale wins. It is measured from this run's own reference frame, where
        the configured one is a database value that has already been wrong once: MAST_unit#138
        records `pixel_scale_at_bin1` sitting at 0.0, which made `commanded_offset_px` -- the
        only check on whether a correlation means anything -- silently absent.

        Configuration is the fallback, not the default, and which was used is recorded
        beside the answer. The two have been measured to agree to about 0.15% (solved
        0.2620 against configured 0.2616 on 2026-09-02), so this changes no conclusion
        drawn so far; it removes the dependence on a value nothing verifies.
        """
        solved = self._solved_pixel_scale_at_bin1()
        if solved is not None:
            return solved, "solved from the reference frame"
        configured = self._pixel_scale()
        if configured is not None:
            return configured, "the configured pixel_scale_at_bin1 (the reference did not solve)"
        return None, "neither a solve nor a configured plate scale"

    def _acquire(self) -> bool:
        """Put the star on the ASSUMED fibre position, and wait for it to get there.

        Everything about the acquisition is fixed rather than operator-facing: mastrometry,
        gradual-by-rate, corrections on, sky phase skipped (the fibre only sees light with
        the folding mirror in, so the spec phase is the one that matters), and no handover
        to the guider.
        """
        acquirer = self.unit.acquirer
        if acquirer is None:
            self.state.last_error = "no acquirer"
            return False

        response = acquirer.endpoint_start_acquisition_and_guiding(
            seconds=self.params.seconds,
            ra_j2000_hours=self.params.ra_j2000_hours,
            dec_j2000_degs=self.params.dec_j2000_degs,
            gain_absolute=self.params.gain,
            skip_sky=True,
            use_set_limit_frame=True,
            handover_automatically_to_guider=False,
        )
        if response is not None and getattr(response, "failed", False):
            self.state.last_error = f"acquisition refused: {response.errors}"
            return False

        deadline = time.monotonic() + ACQUISITION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._stop.is_set():
                self.state.last_error = "aborted during acquisition"
                return False
            if not self.unit.is_active(UnitActivities.Acquiring):
                break
            time.sleep(1.0)
        else:
            self.state.last_error = f"acquisition did not finish within {ACQUISITION_TIMEOUT_SECONDS:g}s"
            return False

        return True

    def _ram_disk_free_bytes(self) -> int | None:
        """Free space where the frames are being written, or None if it cannot be read."""
        folder = self.state.folder or (filer.ram.root if filer.ram else None)
        if folder is None:
            return None
        try:
            return shutil.disk_usage(folder).free
        except OSError as ex:
            logger.error(f"could not read free space on '{folder}': {ex}")
            return None

    def _await_disk_space(self) -> bool:
        """True when there is room for the next step; False when the run should stop.

        Unreadable free space returns True: a run must not be stopped by the inability to
        ask a question, only by a real answer.
        """
        free = self._ram_disk_free_bytes()
        if free is None or free >= RAM_DISK_MIN_FREE_BYTES:
            return True

        logger.warning(
            f"ram disk down to {free / 1024**3:.1f} GB; waiting up to "
            f"{RAM_DISK_DRAIN_TIMEOUT_SECONDS:g}s for the mover to drain"
        )
        filer.flush(timeout=RAM_DISK_DRAIN_TIMEOUT_SECONDS)

        free = self._ram_disk_free_bytes()
        if free is not None and free < RAM_DISK_MIN_FREE_BYTES:
            self.state.last_error = (
                f"ram disk has {free / 1024**3:.1f} GB free and the mover is not draining it; "
                f"the shared area is probably unreachable. Stopping before a part-written frame."
            )
            logger.error(self.state.last_error)
            return False
        return True

    def _expose_reference(self) -> np.ndarray:
        """The frame the shift is measured FROM, chosen the same way a step's is.

        The reference is one of the two correlation inputs, so it gets the same treatment as
        the other: a burst at one pointing, reduced by the median, and the imager frame
        paired with the nearest ThorCam sample. Treating the two sides differently would put
        a systematic between them that no later analysis could separate from the answer.
        """
        exposures = [
            self._expose_pair(f"reference-{n:02d}.fits", f"reference-flux-{n:02d}.fits")
            for n in range(self.params.number_of_frames)
        ]
        _flux, representative = self.representative_of([e.flux for e in exposures])
        chosen = exposures[representative].imager_frame or ""
        self.state.reference_frame = chosen
        self.state.frames += len(exposures)
        logger.info(f"reference: '{chosen}' (nearest the median of {len(exposures)})")
        return self._read_fits(chosen)

    def _walk_spiral(self) -> str:
        """Walk until a ring adds nothing, a cap is hit, or the operator aborts.

        Returns the terminal state, which the result reports: `converged`, `max_rings`,
        `max_radius`, or `aborted`. They mean different things -- only `converged` says the
        arg-max is a peak rather than the best of a truncated search.
        """
        mount = self.unit.mount
        if mount is None or mount.pw is None:
            raise FluxMeteringError("no mount")

        mount.pw.mount_spiral_offset_new(self.params.x_step_arcsec, self.params.y_step_arcsec)

        best_flux: float | None = None
        best_ring = 0
        current_ring = 0
        rings_without_improvement = 0
        index = 0

        while True:
            if self._stop.is_set():
                return "aborted"
            if not self._await_disk_space():
                return "disk_full"

            cell, ring, offset = self._read_spiral_offset()
            step = self._measure_step(index, cell, ring, offset)
            self.steps.append(step)

            if best_flux is None or step.flux > best_flux:
                best_flux, best_ring = step.flux, ring if ring is not None else 0
                self.state.best_flux = step.flux
                self.state.best_index = index
                self.state.best_cell = cell

            if ring is not None and ring > current_ring:
                # A ring just finished. It is the completed ring, not the step, that the
                # stopping rule can say anything about.
                rings_without_improvement = 0 if best_ring >= current_ring else rings_without_improvement + 1
                current_ring = ring
                if rings_without_improvement >= self.params.patience_rings:
                    return "converged"
                if ring > self.params.max_rings:
                    return "max_rings"

            if offset is not None and self._radius_arcsec(offset) > self.params.max_radius_arcsec:
                return "max_radius"

            mount.pw.mount_spiral_offset_next()
            mount.wait_until_settled(SettleMode.OFFSET_STEP)
            index += 1

    def _read_spiral_offset(self):
        """(cell, ring, offset_arcsec) as PWI4 reports them, or Nones.

        PWI4 owns the traversal, so the ring is derived from what it reports rather than
        assumed from a cell ordering of our own. `spiral_offset` is absent on PWI4 older
        than 4.0.11b8, and a step with no cell is recorded rather than guessed at.
        """
        try:
            spiral = self.unit.pw.status().mount.spiral_offset  # type: ignore[union-attr]
        except Exception as ex:  # noqa: BLE001 -- a telemetry hiccup must not end the run
            logger.error(f"could not read the spiral offset: {ex}")
            return None, None, None
        if spiral is None or spiral.x is None or spiral.y is None:
            return None, None, None
        cell = (int(spiral.x), int(spiral.y))
        offset = (cell[0] * self.params.x_step_arcsec, cell[1] * self.params.y_step_arcsec)
        return cell, max(abs(cell[0]), abs(cell[1])), offset

    def _radius_arcsec(self, offset: tuple[float, float]) -> float:
        """Sky angle from the origin, with cos(dec) on the RA axis.

        `x_step_arcsec` is RA COORDINATE arcsec, so the angle on the sky along that axis is
        the offset times cos(dec). Without it the radius reads 25% high at dec +41 -- the
        same factor `_pixels_from_reference` carries, MAST_unit#136 -- and the cap would
        mean a different thing at every declination.
        """
        dec = self._dec_degrees()
        scale = math.cos(math.radians(dec)) if dec is not None else 1.0
        return math.hypot(offset[0] * scale, offset[1])

    def _pixel_scale(self) -> float | None:
        """The configured plate scale, or None when it is unset.

        Its own method because `_finish` records it beside the run's products: a
        re-correlation months later must convert arcseconds with the scale THIS run used,
        not with whatever the configuration says by then.
        """
        conf = self.unit.unit_conf
        scale = conf.imager.pixel_scale_at_bin1 if conf is not None else 0.0
        return scale if scale and scale > 0.0 else None

    def _dec_degrees(self) -> float | None:
        try:
            return float(self.unit.mount.status().dec_j2000_degs)  # type: ignore[union-attr,arg-type]
        except Exception:  # noqa: BLE001
            return None

    # ---------------------------------------------------------------- measurement --

    def do_flux_exposure(self, captured: dict[str, Any]) -> None:
        """Take one ThorCam frame on this thread, recording the outcome in `captured`.

        A named target rather than a closure so the dispatch site says what is running
        without the reader opening it -- invariant 9, and the reason `run_acquisition`
        was worth a check.

        The failure is recorded, never raised: a bare thread that raises prints to stderr
        and vanishes, and the parent would then find no frame and record a flux of zero --
        which reads as "no light reached the fibre", a measurement rather than a failure.
        """
        try:
            captured["flux_started"] = isoformat_utc()
            # Sampled here, beside the timestamps, rather than read when the frame is
            # written. The write follows by milliseconds, so reading it there would
            # usually be right -- but right by luck about timing, and wrong the moment a
            # write is retried or deferred. The same trap as `_commanded_offset_px`, whose
            # declination is only correct while the run is still happening.
            captured["pointing"] = self._pointing()
            captured["flux_frame"] = self._meter.expose()  # type: ignore[union-attr]
            captured["flux_ended"] = isoformat_utc()
        except Exception as ex:  # noqa: BLE001 -- reported through `captured`, not swallowed
            captured["flux_error"] = ex

    def _pointing(self) -> dict[str, float | None]:
        """Where the mount is, in one read.

        One `mount.status()` for all four values: separate reads would sample four
        different instants, and during a spiral step the mount is being offset between
        them. Alt/az are deliberately absent -- they are not on `MountStatus` and would
        cost a PWI4 round trip, and both they and the airmass are derivable afterwards
        from RA/Dec, DATE-OBS and the site coordinates.
        """
        try:
            status = self.unit.mount.status()  # type: ignore[union-attr]
            return {
                "ra_j2000_hours": status.ra_j2000_hours,
                "dec_j2000_degs": status.dec_j2000_degs,
                "ha_hours": status.ha_hours,
                "lmst_hours": status.lmst_hours,
            }
        except Exception:  # noqa: BLE001 -- metadata must never fail an exposure
            logger.warning("could not read the mount's pointing for the frame header")
            return {}

    def _expose_pair(
        self, imager_name: str, flux_name: str, step: tuple[int, Any, int, Any] | None = None
    ) -> FluxMeteringExposure:
        """One imager frame and one ThorCam frame, exposed in parallel.

        In parallel because they must cover the same window -- see
        `FluxMeteringParams.flux_exposure_us` -- and each records its own start and end, so
        the overlap is verifiable afterwards rather than assumed. The imager path goes
        through PHD2 and does not necessarily begin the instant it is asked.

        `step` is `(index, cell, ring, offset_arcsec)` and is passed DOWN from
        `_measure_step` rather than read off `self.state`, which does not yet describe this
        step: the state is advanced after the exposures complete, so it still holds the
        previous one here. The reference exposure passes None and its frame omits the step
        cards entirely, rather than carrying zeros that would read as the origin cell.
        """
        captured: dict[str, Any] = {}
        flux_thread = threading.Thread(name="flux-exposure", target=self.do_flux_exposure, args=(captured,))
        flux_thread.start()

        imager_started = isoformat_utc()
        try:
            self._expose_imager(imager_name)
        finally:
            # Joined in `finally` so a failed imager exposure cannot leave the ThorCam
            # thread writing into `captured` while the next step is already using it.
            flux_thread.join()
        imager_ended = isoformat_utc()

        if "flux_error" in captured:
            raise FluxMeteringError(f"the flux exposure failed: {captured['flux_error']}")

        frame = captured["flux_frame"]
        self._write_fits(flux_name, frame, cards=self._flux_cards(captured, imager_name, step))

        n_saturated = saturated_pixels(frame, self._meter.saturation_level)  # type: ignore[union-attr]
        return FluxMeteringExposure(
            flux=frame_flux(frame, self.params.flux_black_level),
            saturated_pixels=n_saturated,
            saturated=n_saturated > SATURATED_PIXELS_ALLOWED,
            imager_frame=imager_name,
            flux_frame=flux_name,
            imager_started_utc=imager_started,
            imager_ended_utc=imager_ended,
            flux_started_utc=captured["flux_started"],
            flux_ended_utc=captured["flux_ended"],
        )

    @staticmethod
    def representative_of(fluxes: list[float]) -> tuple[float, int]:
        """The median flux, and which exposure is nearest it.

        Median rather than mean because the arg-max is decided where the coupling curve is
        flattest, and that is exactly where a single outlier -- a cosmic ray, a gust, a
        tracking glitch -- has most leverage over which cell wins.

        The nearest exposure is the one whose imager frame the correlation would use, so the
        shift is measured from the same instant as the flux that chose the step. With an ODD
        count the median is itself a sample and this returns that exposure exactly; with an
        even count the median is interpolated and this picks the nearer of the two middle
        ones, which is why an odd count is the better choice.
        """
        median = float(statistics.median(fluxes))
        nearest = min(range(len(fluxes)), key=lambda i: abs(fluxes[i] - median))
        return median, nearest

    def _measure_step(self, index: int, cell, ring, offset) -> FluxMeteringStep:
        """One step: `number_of_frames` exposure pairs at one pointing, reduced to a median.

        The mount does not move between them, so the several imager frames differ only by
        seeing, noise and whatever the tracking drifted -- which is why choosing among them
        by flux is defensible: it picks a typical moment rather than an excursion.
        """
        exposures = [
            self._expose_pair(
                f"step-{index:05d}-{n:02d}.fits",
                f"flux-{index:05d}-{n:02d}.fits",
                # From the arguments, not self.state: the state still describes the
                # PREVIOUS step here, being advanced only once these exposures are done.
                step=(index, cell, ring, offset),
            )
            for n in range(self.params.number_of_frames)
        ]
        flux, representative = self.representative_of([e.flux for e in exposures])
        chosen = exposures[representative]

        self.state.index = index
        self.state.cell = cell
        self.state.ring = ring
        self.state.frames += len(exposures)
        saturated_count = sum(1 for e in exposures if e.saturated)
        if chosen.saturated:
            self.state.saturated_frames += 1

        logger.info(
            f"step {index}: cell={cell} ring={ring} flux={flux:.0f} (median of {len(exposures)}) "
            f"representative={representative} saturated={saturated_count}/{len(exposures)}"
        )
        return FluxMeteringStep(
            index=index,
            cell=cell,
            ring=ring,
            offset_arcsec=offset,
            flux=flux,
            exposures=exposures,
            representative=representative,
            saturated_exposures=saturated_count,
            saturated_pixels=chosen.saturated_pixels,
            saturated=chosen.saturated,
            imager_frame=chosen.imager_frame,
            flux_frame=chosen.flux_frame,
            imager_started_utc=chosen.imager_started_utc,
            imager_ended_utc=chosen.imager_ended_utc,
            flux_started_utc=chosen.flux_started_utc,
            flux_ended_utc=chosen.flux_ended_utc,
        )

    def _measure(self, reference: np.ndarray) -> FluxMeteringResult | None:
        """Correlate the reference against the arg-max frame, and say what it means."""
        if not self.steps:
            return None

        best = max(self.steps, key=lambda s: s.flux)
        shape = reference.shape
        center_x, center_y, center_source = resolve_center(None, None, shape)

        # The reference frame and an arg-max at the origin are at the SAME pointing. They
        # are still two separate exposures, so the correlation there is a real null
        # measurement -- and a useful one, being a direct read of the noise floor -- but
        # `at_origin` has to be told that a zero shift is the correct answer rather than the
        # fixed-pattern capture it normally flags.
        expect_no_motion = best.cell == (0, 0)

        final = self._read_fits(best.imager_frame or "")
        # Through `correlate.measure_pair`, which `spiral_correlate_steps` also calls. Two
        # implementations of one measurement would eventually disagree, and the
        # disagreement would surface as a fibre position rather than as an error.
        shift, magnitude, limit = measure_pair(
            reference,
            final,
            center_x=center_x,
            center_y=center_y,
            usable_fraction=self.params.usable_fraction,
            expect_no_motion=expect_no_motion,
        )
        sky_dx, sky_dy, sky_source = self._sky_offset(shift.dx, shift.dy)
        return FluxMeteringResult(
            dx=shift.dx,
            dy=shift.dy,
            confidence=shift.confidence,
            at_origin=shift.at_origin,
            low_confidence=shift.confidence < MIN_CONFIDENCE,
            magnitude_px=magnitude,
            max_reliable_shift_px=limit,
            beyond_limit=magnitude > limit,
            fiber_x=center_x,
            fiber_y=center_y,
            fiber_source=center_source,
            # Stated rather than left as arithmetic for the reader: a sign error is then
            # visible by eye on the first run instead of after five.
            proposed_fiber_x=center_x + shift.dx,
            proposed_fiber_y=center_y + shift.dy,
            argmax_index=best.index,
            argmax_cell=best.cell,
            argmax_ring=best.ring,
            argmax_frame=best.imager_frame,
            argmax_offset_arcsec=best.offset_arcsec,
            argmax_saturated=best.saturated,
            saturated_frame_count=self.state.saturated_frames,
            commanded_offset_px=self._commanded_offset_px(best.offset_arcsec),
            commanded_offset_source=self._effective_pixel_scale()[1],
            sky_dx_arcsec=sky_dx,
            sky_dy_arcsec=sky_dy,
            sky_offset_source=sky_source,
        )

    def _commanded_offset_px(self, offset_arcsec) -> tuple[float, float] | None:
        """The arg-max cell's commanded offset, in detector pixels.

        This is the run's own check on its answer: it should equal (dx, dy) in magnitude and
        sign. Disagreeing signs mean the convention is inverted; disagreeing magnitudes mean
        the plate scale is wrong, and this measures it.

        None -- rather than a wrong number -- when the plate scale is unset. It is 0.0 in the
        configuration database today (MAST_unit#138), so that is the live path, and no check
        at all beats a check that always reads zero.
        """
        if offset_arcsec is None:
            return None
        scale, _source = self._effective_pixel_scale()
        if not scale or scale <= 0.0:
            return None
        dec, _dec_source = self._effective_dec()
        # cos(dec) on the RA axis for the same reason the radius cap carries it: the step is
        # RA COORDINATE arcsec, and the sky moves by that times cos(dec).
        ra_scale = math.cos(math.radians(dec)) if dec is not None else 1.0
        return (offset_arcsec[0] * ra_scale / scale, offset_arcsec[1] / scale)

    # ------------------------------------------------------------------- plumbing --

    def _open_meter(self) -> None:
        if self._injected_meter is not None:
            self._meter = self._injected_meter
        else:
            from flux_metering.thorcam import ThorCam

            cam = ThorCam()
            cam.open()
            self._meter = cam
        try:
            self._meter.configure(
                exposure_us=self.params.flux_exposure_us,
                gain=self.params.flux_gain,
                black_level=self.params.flux_black_level,
            )
        except FluxMeterError:
            self._meter.close()
            self._meter = None
            raise

    def _expose_imager(self, file_name: str, read_back: bool = False) -> np.ndarray | None:
        """Expose full-frame at bin 1, save, read back, hand to the mover.

        Full frame and bin 1 because the correlation wants full detector sampling, and
        because any of these frames may turn out to be the arg-max -- which one is not known
        until the search ends.

        The read is inside `protect()` so a mover cannot take the file while astropy has it
        open; the mover runs on its own thread, which is what keeps that claim from
        self-deadlocking against this one.
        """
        imager, conf = self.unit.imager, self.unit.unit_conf
        if imager is None or conf is None or self.state.folder is None:
            raise FluxMeteringError("no imager, configuration or folder")

        path = os.path.join(self.state.folder, file_name)
        with MoveGuardian().protect(path):
            imager.latest_settings = ImagerSettings(
                seconds=self.params.seconds,
                save=True,
                image_path=path,
                binning=1,
                roi=imager.full_frame,
                gain=self.params.gain or conf.acquisition.gain,
            )
            response = imager.start_exposure(imager.latest_settings)
            if response is not None and response.failed:
                raise FluxMeteringError(f"exposure of '{file_name}' failed: {response.errors}")
            imager.wait_for_image_saved()
            # Only when the caller actually wants the pixels. A step does not: it keeps the
            # file name and reads the one frame that turns out to matter at the end. Reading
            # every frame back would cost a 94 MB disk read and a 374 MB float64 allocation
            # per exposure -- three per step -- for a result nothing looks at.
            data = np.asarray(fits.getdata(path), dtype=float) if read_back else None
        filer.move_ram_to_shared(path)
        return data

    def _flux_cards(
        self, captured: dict[str, Any], imager_name: str, step: tuple[int, Any, int, Any] | None
    ) -> list[tuple[str, Any, str]]:
        """The header for one ThorCam frame: (keyword, value, comment).

        These frames used to carry nothing but the six mandatory structural keywords, so a
        flux frame separated from its run folder was anonymous -- and `flux-00007-00.fits`
        is a name that repeats in every run on the share. Everything here is already known
        at the moment of the exposure; it was simply never written down.

        `BLKLEVEL` is the one that matters most. `frame_flux` subtracts it, so without it
        the frame cannot be re-reduced: the number needed to recompute the flux was absent
        from the data you would recompute it from.

        Deliberately NO WCS. The ThorCam sees only the light emerging from the fibre and
        has no field, so `CRVAL`/`CRPIX`/`CTYPE` would assert that these pixels map to sky.
        A missing card is an absence; a wrong WCS invites a solver to solve it and DS9 to
        overlay catalogues on it.
        """
        meter = self._meter
        pointing = captured.get("pointing") or {}
        date, seq = self._run_labels()

        cards: list[tuple[str, Any, str]] = [
            ("DATE-OBS", captured.get("flux_started"), "UTC at the start of this exposure"),
            ("DATE-END", captured.get("flux_ended"), "UTC at the end of this exposure"),
            # Seconds, matching imagers/saving.py. Not EXPOSURE, which the PHD2-written
            # imager frames use: writing both would create a third convention rather than
            # settle the two that exist.
            ("EXPTIME", self.params.flux_exposure_us / 1e6, "exposure time in seconds"),
            # The CAMERA, per the FITS standard, and matching what PHD2 writes on the
            # imager frames. This deliberately disagrees with imagers/saving.py, which
            # puts the hostname here; the hostname belongs in TELESCOP, below. Do not
            # "fix" this into agreement with the wrong one.
            ("INSTRUME", meter.model if meter else None, "the flux meter"),
            ("CAMSN", meter.serial_number if meter else None, "flux meter serial number"),
            ("GAIN", self.params.flux_gain, "flux meter gain"),
            ("BLKLEVEL", self.params.flux_black_level, "black level subtracted by frame_flux"),
            ("SATURATE", meter.saturation_level if meter else None, "full scale ADU"),
            ("TELESCOP", self.unit.hostname, "the unit"),
            ("CREATOR", "MAST flux_metering", "what wrote this frame"),
            # The observing night is NOT derivable from DATE-OBS by a reader who does not
            # know it turns at 12:00 UTC, so it is stated.
            ("RUNDATE", date, "observing night (turns at 12:00 UTC)"),
            ("RUNSEQ", seq, "flux metering run sequence"),
            # The single card that ties this frame to the imager frame sharing its exposure
            # window. That pairing otherwise exists only inside result.json.
            ("IMGFRAME", imager_name, "imager frame of the same exposure pair"),
        ]

        if step is not None:
            index, cell, ring, offset = step
            cards += [
                ("STEPIDX", index, "spiral step index"),
                # Split because a tuple is not a legal FITS card value.
                ("CELLX", cell[0] if cell else None, "spiral cell x"),
                ("CELLY", cell[1] if cell else None, "spiral cell y"),
                ("RING", ring, "spiral ring"),
                ("OFFRA", offset[0] if offset else None, "commanded RA offset, arcsec"),
                ("OFFDEC", offset[1] if offset else None, "commanded Dec offset, arcsec"),
            ]

        ra_hours = pointing.get("ra_j2000_hours")
        cards += [
            # Degrees, so no reader has to guess whether RA is hours or degrees.
            ("RA", ra_hours * 15.0 if ra_hours is not None else None, "mount J2000 RA, degrees"),
            ("DEC", pointing.get("dec_j2000_degs"), "mount J2000 Dec, degrees"),
            ("EQUINOX", 2000.0, "equinox of RA/DEC"),
            ("RADESYS", "ICRS", "reference frame of RA/DEC"),
            ("HA", pointing.get("ha_hours"), "hour angle, hours"),
            ("LMST", pointing.get("lmst_hours"), "local mean sidereal time, hours"),
        ]
        if self.params.ra_j2000_hours is not None and self.params.dec_j2000_degs is not None:
            # Only when one was actually requested. Left absent on a skip_acquisition run
            # so a reader can tell "no target was asked for" from "the target was here".
            cards.append(
                (
                    "OBJECT",
                    f"{self.params.ra_j2000_hours:.6f}h {self.params.dec_j2000_degs:+.6f}d",
                    "requested target (J2000)",
                )
            )
        return cards

    def _run_labels(self) -> tuple[str | None, str | None]:
        """(observing night, sequence) from the run folder, whose shape is
        `<...>/<date>/FluxMetering/<seq>`."""
        if self.state.folder is None:
            return None, None
        parts = Path(self.state.folder).parts
        if len(parts) >= 3 and parts[-2] == "FluxMetering":
            return parts[-3], parts[-1]
        return None, None

    def _write_fits(self, file_name: str, data: np.ndarray, cards: list[tuple[str, Any, str]] | None = None) -> None:
        if self.state.folder is None:
            raise FluxMeteringError("no folder")
        path = os.path.join(self.state.folder, file_name)
        header = fits.Header()
        for keyword, value, comment in cards or []:
            # A card whose value is unknown is omitted rather than written empty: a header
            # that says nothing about a thing is honest, one that says "" is not.
            if value is not None:
                header[keyword] = (value, comment)
        with MoveGuardian().protect(path):
            fits.PrimaryHDU(data=np.asarray(data), header=header).writeto(path, overwrite=True)
        filer.move_ram_to_shared(path)

    def _read_fits(self, file_name: str) -> np.ndarray:
        """Read a frame back, from the share if the mover has already taken it.

        The fallback is the normal path, not an edge case: frames are handed to
        `move_ram_to_shared` as they are written, so by the time the arg-max frame is
        wanted -- at the end of the run -- it has usually gone. The reference frame,
        read moments after being written, usually has not. A first real run failed here
        for exactly that reason while the reference had read fine.

        `change_top_to` compares against roots stored POSIX-style ("D:/MAST/"), while
        this folder comes from pathlib and is spelled with backslashes, so the prefix
        test silently fails unless the path is converted first. `move_ram_to_shared`
        documents the same two-spellings problem and converts for the same reason.
        """
        return np.asarray(fits.getdata(self._frame_path(file_name)), dtype=float)

    def _frame_path(self, file_name: str) -> str:
        """Where a frame actually is: the ram disk, or the share if the mover took it.

        Split out of `_read_fits` so the solver can be pointed at the same file without
        reading it into memory first -- a full frame is 94 MB on disk and 374 MB as float64.
        """
        if self.state.folder is None:
            raise FluxMeteringError("no folder")
        local = os.path.join(self.state.folder, file_name)
        if os.path.exists(local):
            return local

        shared_folder = filer.change_top_to(FilerTop.Shared, Path(self.state.folder).as_posix())
        if shared_folder is None:
            raise FluxMeteringError(
                f"'{file_name}' is neither on the ram disk nor under a known root (folder '{self.state.folder}')"
            )
        moved = os.path.join(shared_folder, file_name)
        if not os.path.exists(moved):
            raise FluxMeteringError(f"'{file_name}' is neither at '{local}' nor at '{moved}'")
        return moved

    def _finish(self, terminal: str) -> None:
        """Write the result, put the mount back, and release the unit.

        The spiral offset is reset on EVERY ending, converged included: without a backtrack
        the mount stops wherever the search stopped, which is an arbitrary cell up to a ring
        from the arg-max and of no use to anyone. Returning to the acquired position is the
        one predictable choice.
        """
        self.state.terminal_state = terminal
        self.state.ended_at = isoformat_utc()

        # Normally already joined, before the correlation. Repeated here because `_finish`
        # also runs on the failure paths, which never reach that point.
        self._await_reference_solve()

        # The JSON is the status model plus what only the run itself knows: what was asked
        # for, and which camera answered. One document, so a reader is never left joining
        # the products against a status they no longer have.
        document = {
            **self.status().model_dump(),
            # `status()` derives `active` from the worker thread being alive, and _finish
            # runs INSIDE that thread -- so the live status is necessarily active=True and
            # mid-phase right here, and assigning to self.state earlier would simply be
            # overwritten. This document describes a run that is over, and says so;
            # `terminal_state` carries how it ended. Without these two the result.json on
            # the share claims forever that the run is still going, which is what every
            # file written before 2026-09-02 does.
            "active": False,
            "phase": "idle",
            "params": asdict(self.params),
            "flux_exposure_us": self.params.flux_exposure_us,
            "flux_meter": self._meter.description if self._meter else None,
            "saturation_level": self._meter.saturation_level if self._meter else None,
            "hostname": self.unit.hostname,
            # The two live reads the arcsec->pixel conversion depends on, frozen at the
            # values this run actually used. Both are correct only while the run is
            # happening: the plate scale can be reconfigured, and the declination is read
            # from wherever the mount is pointing at the time. `spiral_correlate_steps`
            # recomputes commanded offsets for arbitrary pairs from these, and reports no
            # commanded offset at all for runs that predate them -- rather than a number
            # derived from an unrelated pointing.
            "pixel_scale_at_bin1": self._pixel_scale(),
            # The scale actually used for the arcsec->pixel conversion, and where it came
            # from. `spiral_correlate_steps` reads this so a re-correlation converts the way
            # the run did, rather than picking its own source years later.
            "pixel_scale_at_bin1_used": self._effective_pixel_scale()[0],
            "pixel_scale_source": self._effective_pixel_scale()[1],
            "pixel_scale_at_bin1_solved": self._solved_pixel_scale_at_bin1(),
            # The declination the conversion used, and its source. NOT `_pointing()`, which
            # is per-frame telemetry: this is the one number the arcsec->pixel maths needs.
            "dec_degrees": self._effective_dec()[0],
            "dec_source": self._effective_dec()[1],
            "dec_degrees_mount": self._dec_degrees(),
            # The reference frame's own WCS: where the field actually was, at what scale and
            # rotation. `pixel_scale` here is MEASURED, so it can be set against the
            # configured `pixel_scale_at_bin1` above that `commanded_offset_px` relies on.
            "reference_solution": self._reference_solution,
        }

        try:
            self._write_result(document)
        except Exception:  # a lost result must not also strand the mount
            logger.exception("could not write result.json")

        try:
            if self.unit.mount is not None and self.unit.mount.pw is not None:
                self.unit.mount.pw.mount_spiral_offset_new(self.params.x_step_arcsec, self.params.y_step_arcsec)
        except Exception as ex:  # noqa: BLE001
            logger.error(f"could not reset the spiral offset: {ex}")

        if self._meter is not None:
            self._meter.close()
            self._meter = None

        self.state.active = False
        self.state.phase = "idle"
        self.unit.end_activity(UnitActivities.FluxMetering)

        # `flush` before saying the run is done: `move_ram_to_shared` is asynchronous, and a
        # run writes gigabytes, so "complete" would otherwise be reported while the products
        # were still queued on a volatile RAM disk.
        if not filer.flush(timeout=300.0):
            logger.error("products were still in flight when the run ended")
        logger.info(f"flux metering ended: {terminal}, {len(self.steps)} steps")

    def _write_result(self, result: dict[str, Any]) -> None:
        if self.state.folder is None:
            return
        path = os.path.join(self.state.folder, "result.json")
        with MoveGuardian().protect(path), open(path, "w") as fp:
            json.dump(result, fp, indent=2, default=str)
        filer.move_ram_to_shared(path)


def parse_target(
    ra_j2000_hours: str | float | None, dec_j2000_degs: str | float | None
) -> tuple[float | None, float | None]:
    """Target coordinates as decimal hours and degrees, in whatever form they arrive.

    One call, whatever the form. `float()` is NOT enough and never was: `RA_PATTERN`
    deliberately accepts space-separated sexagesimal, so `"03 08 10.142"` passes the
    query validation and then fails conversion -- the same defect `acquirer.py` records
    having already fixed once, in the comment above its own parsing block. The parsers
    take sexagesimal, decimal and surrounding whitespace alike.

    None is passed through rather than defaulted: it means "take it from the mount", and
    only the acquirer can do that. Note the emptiness test is explicit rather than
    truthiness, so an RA of exactly 0 hours is a coordinate and not a missing value.
    """
    ra = None if ra_j2000_hours is None or ra_j2000_hours == "" else sexagesimal_hours_to_decimal(ra_j2000_hours)
    dec = None if dec_j2000_degs is None or dec_j2000_degs == "" else sexagesimal_degrees_to_decimal(dec_j2000_degs)
    return ra, dec


def isoformat_utc() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()
