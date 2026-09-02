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
from flux_metering.flux_meter import FluxMeter, FluxMeterError, frame_flux, saturated_pixels
from imaging.frame_shift import MIN_CONFIDENCE, margins_from_fraction, max_reliable_shift, measure_shift
from mount import SettleMode
from spiral_search import resolve_center

if TYPE_CHECKING:
    from unit import Unit

logger = get_logger(__name__)
filer = Filer(logger)

REFERENCE_IMAGE = "reference.fits"

#: How long to wait for the whole acquisition to reach tolerance before giving up. The
#: procedure is meaningless without a converged acquisition, so this is a ceiling on
#: waiting, not a tolerance of its own.
ACQUISITION_TIMEOUT_SECONDS = 900.0

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

            self.state.phase = "spiral"
            terminal = self._walk_spiral()

            self.state.phase = "correlating"
            self.state.result = self._measure(reference)
            self._finish(terminal)
        except Exception as ex:  # the thread owns the mount; it must land it safely
            logger.exception(f"{op}: flux metering failed")
            self.state.last_error = str(ex)
            self._finish("failed")

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
            captured["flux_frame"] = self._meter.expose()  # type: ignore[union-attr]
            captured["flux_ended"] = isoformat_utc()
        except Exception as ex:  # noqa: BLE001 -- reported through `captured`, not swallowed
            captured["flux_error"] = ex

    def _expose_pair(self, imager_name: str, flux_name: str) -> FluxMeteringExposure:
        """One imager frame and one ThorCam frame, exposed in parallel.

        In parallel because they must cover the same window -- see
        `FluxMeteringParams.flux_exposure_us` -- and each records its own start and end, so
        the overlap is verifiable afterwards rather than assumed. The imager path goes
        through PHD2 and does not necessarily begin the instant it is asked.
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
        self._write_fits(flux_name, frame)

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
            self._expose_pair(f"step-{index:05d}-{n:02d}.fits", f"flux-{index:05d}-{n:02d}.fits")
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
        margin_h, margin_v = margins_from_fraction(shape, self.params.usable_fraction)

        # The reference frame and an arg-max at the origin are at the SAME pointing. They
        # are still two separate exposures, so the correlation there is a real null
        # measurement -- and a useful one, being a direct read of the noise floor -- but
        # `at_origin` has to be told that a zero shift is the correct answer rather than the
        # fixed-pattern capture it normally flags.
        expect_no_motion = best.cell == (0, 0)

        final = self._read_fits(best.imager_frame or "")
        shift = measure_shift(
            reference,
            final,
            center_x=center_x,
            center_y=center_y,
            margin_horizontal=margin_h,
            margin_vertical=margin_v,
            expect_no_motion=expect_no_motion,
        )

        limit = max_reliable_shift(shape, margin_h)
        magnitude = math.hypot(shift.dx, shift.dy)
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
        conf = self.unit.unit_conf
        scale = conf.imager.pixel_scale_at_bin1 if conf is not None else 0.0
        if not scale or scale <= 0.0:
            return None
        dec = self._dec_degrees()
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

    def _write_fits(self, file_name: str, data: np.ndarray) -> None:
        if self.state.folder is None:
            raise FluxMeteringError("no folder")
        path = os.path.join(self.state.folder, file_name)
        with MoveGuardian().protect(path):
            fits.PrimaryHDU(data=np.asarray(data)).writeto(path, overwrite=True)
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
        if self.state.folder is None:
            raise FluxMeteringError("no folder")
        local = os.path.join(self.state.folder, file_name)
        if os.path.exists(local):
            return np.asarray(fits.getdata(local), dtype=float)

        shared_folder = filer.change_top_to(FilerTop.Shared, Path(self.state.folder).as_posix())
        if shared_folder is None:
            raise FluxMeteringError(
                f"'{file_name}' is neither on the ram disk nor under a known root (folder '{self.state.folder}')"
            )
        moved = os.path.join(shared_folder, file_name)
        if not os.path.exists(moved):
            raise FluxMeteringError(f"'{file_name}' is neither at '{local}' nor at '{moved}'")
        return np.asarray(fits.getdata(moved), dtype=float)

    def _finish(self, terminal: str) -> None:
        """Write the result, put the mount back, and release the unit.

        The spiral offset is reset on EVERY ending, converged included: without a backtrack
        the mount stops wherever the search stopped, which is an arbitrary cell up to a ring
        from the arg-max and of no use to anyone. Returning to the acquired position is the
        one predictable choice.
        """
        self.state.terminal_state = terminal
        self.state.ended_at = isoformat_utc()

        # The JSON is the status model plus what only the run itself knows: what was asked
        # for, and which camera answered. One document, so a reader is never left joining
        # the products against a status they no longer have.
        document = {
            **self.status().model_dump(),
            "params": asdict(self.params),
            "flux_exposure_us": self.params.flux_exposure_us,
            "flux_meter": self._meter.description if self._meter else None,
            "saturation_level": self._meter.saturation_level if self._meter else None,
            "hostname": self.unit.hostname,
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
